"""Check parser results against values you know (regression test on real files).

Put the files in a folder and next to them an ``expected.json``:

    {
      "datasheets/inverter.pdf": {
        "doc_info.doc_type": "datasheet",
        "summary.dc_voltage_max_v": 1500,
        "summary.efficiency_max_pct": {"value": 99.02, "tol": 0.01},
        "summary.mppt_voltage_range_v": [500, 1500],
        "n_tables": {"min": 1}
      },
      "layout.pdf": {"summary.total_modules": 60432, "warnings": []},
      "protocol.pdf": {"registers.len": {"min": 150}}
    }

Keys are paths into the result: ``data`` entries by dotted path (``summary.x``, lists by
index ``placemarks.0.area_ha``, ``.len`` for the length), plus ``n_chunks``, ``n_tables``,
``warnings``, ``file_type``, ``filename``. Expected values:

* a number: equal (relative tolerance 1e-6), or ``{"value": v, "tol": abs}`` / ``{"value": v, "rel": r}``
* ``{"min": a}`` / ``{"max": b}`` bounds, ``{"contains": text}`` for strings and lists
* a string, list or dict: exact equality

``python -m design_ingest.eval FOLDER`` (or ``design-ingest eval FOLDER``) prints PASS / FAIL
per file and returns exit code 1 on any failure, so it can run before every commit.
``--snapshot OUT.json`` writes the current values as a starting point: check them against
the documents by eye before you rely on them. The expected file belongs next to the real
files (git-ignored), never in the repository.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from .core import ingest, supported_extensions
from .schema import Document

_MISSING = object()


def resolve(doc: Document, key: str) -> Any:
    """Value of ``key`` in a parsed document, or ``_MISSING``."""
    top = {
        "n_chunks": lambda: len(doc.chunks),
        "n_tables": lambda: len(doc.tables),
        "warnings": lambda: doc.warnings,
        "file_type": lambda: doc.file_type,
        "filename": lambda: doc.filename,
    }
    if key in top:
        return top[key]()
    cur: Any = doc.data
    for part in key.split("."):
        if part == "len" and isinstance(cur, (list, dict, str)):
            return len(cur)
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def matches(actual: Any, expected: Any) -> tuple[bool, str]:
    """(ok, reason) for one expected value."""
    if actual is _MISSING:
        return False, "missing"
    if isinstance(expected, dict) and set(expected) & {"value", "min", "max", "contains", "tol", "rel"}:
        if "value" in expected:
            if not _num(actual):
                return False, f"got {actual!r}, expected a number"
            v = expected["value"]
            tol = expected.get("tol", abs(v) * expected.get("rel", 1e-6))
            ok = abs(actual - v) <= tol
            return ok, "" if ok else f"got {actual}, expected {v} +- {tol:g}"
        if "contains" in expected:
            ok = isinstance(actual, (str, list)) and expected["contains"] in actual
            return ok, "" if ok else f"{expected['contains']!r} not found in {str(actual)[:60]!r}"
        problems = []
        if "min" in expected and not (_num(actual) and actual >= expected["min"]):
            problems.append(f"got {actual}, expected >= {expected['min']}")
        if "max" in expected and not (_num(actual) and actual <= expected["max"]):
            problems.append(f"got {actual}, expected <= {expected['max']}")
        return not problems, "; ".join(problems)
    if _num(expected) and _num(actual):
        ok = math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-9)
        return ok, "" if ok else f"got {actual}, expected {expected}"
    ok = actual == expected
    return ok, "" if ok else f"got {str(actual)[:80]!r}, expected {str(expected)[:80]!r}"


def check_document(doc: Document, expected: dict[str, Any]) -> list[tuple[str, bool, str]]:
    out = []
    for key, exp in expected.items():
        ok, why = matches(resolve(doc, key), exp)
        out.append((key, ok, why))
    return out


def snapshot(doc: Document) -> dict[str, Any]:
    """Current top-level scalar values: a starting point for an expected file."""
    snap: dict[str, Any] = {"file_type": doc.file_type, "n_tables": len(doc.tables)}
    info = doc.data.get("doc_info")
    if info:
        snap["doc_info.doc_type"] = info["doc_type"]
    for k, v in (doc.data.get("summary") or {}).items():
        if isinstance(v, (int, float, str)) and not isinstance(v, bool):
            snap[f"summary.{k}"] = v
    for k in ("registers", "specs"):
        if doc.data.get(k):
            snap[f"{k}.len"] = len(doc.data[k])
    return snap


def run(folder: Path, expected_file: Path | None = None, use_cache: bool = True,
        recursive: bool = True, profile: str | None = None, snapshot_to: Path | None = None) -> int:
    folder = Path(folder)
    expected_file = expected_file or folder / "expected.json"
    expected = json.loads(expected_file.read_text(encoding="utf-8")) if expected_file.is_file() else {}
    exts = set(supported_extensions())
    pattern = "**/*" if recursive else "*"
    files = [p for p in sorted(folder.glob(pattern)) if p.is_file() and p.suffix.lower() in exts]
    opts = {"profile": profile} if profile else {}
    failed = passed = skipped = 0
    snap: dict[str, Any] = {}
    for rel in expected:
        if not (folder / rel).is_file():
            print(f"FAIL  {rel}: listed in the expected file but not found")
            failed += 1
    for p in files:
        rel = p.relative_to(folder).as_posix()
        try:
            doc = ingest(p, use_cache=use_cache, **opts)
        except Exception as e:
            if rel in expected:
                print(f"FAIL  {rel}: could not be parsed ({e})")
                failed += 1
            continue
        snap[rel] = snapshot(doc)
        if rel not in expected:
            skipped += 1
            continue
        results = check_document(doc, expected[rel])
        bad = [r for r in results if not r[1]]
        if bad:
            failed += 1
            print(f"FAIL  {rel}")
            for key, _, why in bad:
                print(f"        {key}: {why}")
        else:
            passed += 1
            print(f"PASS  {rel}  ({len(results)} checks)")
    print(f"\n{passed} passed, {failed} failed, {skipped} files without expectations")
    if snapshot_to:
        snapshot_to.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"snapshot written to {snapshot_to} (check the values before using them)")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="design-ingest eval", description=__doc__.split("\n\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--expected", help="expected values file (default FOLDER/expected.json)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-recursive", action="store_true")
    ap.add_argument("--profile")
    ap.add_argument("--snapshot", metavar="OUT", help="write the current values as a starting point")
    a = ap.parse_args(argv)
    return run(Path(a.folder), Path(a.expected) if a.expected else None, use_cache=not a.no_cache,
               recursive=not a.no_recursive, profile=a.profile,
               snapshot_to=Path(a.snapshot) if a.snapshot else None)


if __name__ == "__main__":
    sys.exit(main())
