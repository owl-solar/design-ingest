"""Command line:

    design-ingest formats
    design-ingest parse FILE [--md | --json OUT.json] [--no-cache]
    design-ingest dir FOLDER --out OUT_DIR [--md]
    design-ingest extract FILE --schema module|inverter|battery|tender [--vision] [--out OUT.json]
    design-ingest eval FOLDER [--expected expected.json] [--snapshot OUT.json]

``parse`` and ``dir`` take ``--profile FILE`` (naming conventions of drawings, see
design_ingest.profiles). ``eval`` checks results against values you know.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import ingest, ingest_dir, supported_extensions
from .schema import ParseError


def _utf8_stdout() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")  # Windows console: °, ², Cyrillic
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="design-ingest", description="Read design inputs into structured JSON/Markdown.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("formats", help="list supported file extensions")

    p = sub.add_parser("parse", help="parse one file")
    p.add_argument("file")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--md", action="store_true", help="print Markdown")
    g.add_argument("--json", metavar="OUT", help="write full JSON to OUT")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--profile", help="JSON profile with layer / title-block naming conventions")

    d = sub.add_parser("dir", help="parse all supported files in a folder")
    d.add_argument("folder")
    d.add_argument("--out", required=True, help="output folder for JSON (and .md with --md)")
    d.add_argument("--md", action="store_true")
    d.add_argument("--no-recursive", action="store_true")
    d.add_argument("--profile", help="JSON profile with layer / title-block naming conventions")

    v = sub.add_parser("eval", help="compare results with expected values (regression check on real files)")
    v.add_argument("folder")
    v.add_argument("--expected", help="default FOLDER/expected.json")
    v.add_argument("--no-cache", action="store_true")
    v.add_argument("--profile")
    v.add_argument("--snapshot", metavar="OUT", help="write the current values as a starting point")

    e = sub.add_parser("extract", help="LLM extraction into a ready schema (needs [llm] extra + ANTHROPIC_API_KEY)")
    e.add_argument("file")
    e.add_argument("--schema", required=True, choices=["module", "inverter", "battery", "tender"])
    e.add_argument("--vision", action="store_true", help="send the PDF itself (sees drawings/charts)")
    e.add_argument("--model")
    e.add_argument("--out")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "formats":
            print(" ".join(supported_extensions()))
        elif a.cmd == "parse":
            doc = ingest(a.file, use_cache=not a.no_cache, **({"profile": a.profile} if a.profile else {}))
            if a.md:
                print(doc.to_markdown())
            elif a.json:
                print(f"wrote {doc.save_json(a.json)}")
            else:
                print(doc.summary())
                info = doc.data.get("doc_info")
                if info:
                    print(f"type: {info['doc_type']} ({info['confidence']})")
                if doc.data.get("summary"):
                    print(json.dumps(doc.data["summary"], ensure_ascii=False, indent=2))
                for w in doc.warnings:
                    print(f"  ! {w}")
        elif a.cmd == "dir":
            out = Path(a.out)
            docs = ingest_dir(a.folder, recursive=not a.no_recursive, **({"profile": a.profile} if a.profile else {}))
            for doc in docs:
                stem = out / Path(doc.filename).name
                doc.save_json(stem.with_suffix(stem.suffix + ".json"))
                if a.md:
                    stem.with_suffix(stem.suffix + ".md").write_text(doc.to_markdown(), encoding="utf-8")
                print(doc.summary())
            errs = getattr(ingest_dir, "last_errors", [])
            print(f"\n{len(docs)} parsed, {len(errs)} failed -> {out}")
        elif a.cmd == "eval":
            from .eval import run

            return run(Path(a.folder), Path(a.expected) if a.expected else None, use_cache=not a.no_cache,
                       profile=a.profile, snapshot_to=Path(a.snapshot) if a.snapshot else None)
        elif a.cmd == "extract":
            from .extract import extract
            from .schemas import SCHEMAS

            res = extract(a.file, SCHEMAS[a.schema], vision=a.vision, model=a.model)
            js = res.model_dump_json(indent=2)
            if a.out:
                Path(a.out).write_text(js, encoding="utf-8")
                print(f"wrote {a.out}")
            else:
                print(js)
    except (ParseError, FileNotFoundError, ImportError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
