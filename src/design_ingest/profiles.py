"""Profiles: the naming conventions of a company or project, outside the code.

Layer names, block names and title-block labels differ between design offices. The
defaults below fit the drawings used to develop the package; for another office
write a profile file instead of editing code:

    {
      "cad": {
        "layer_groups": {
          "mv_cables": "mv.*(cable|kabel)",
          "trenches": "trench|graben",
          "fence": null
        },
        "table_block_regex": "^(\\\\d+)P(\\\\d+)\\\\b.*TABLE"
      },
      "pdf": {
        "title_keys": ["Project", "Location", "Title", "Scale", "Drawing No", "Rev"],
        "module_tables_regex": "(?:Table|String)s? of (\\\\d+) modules? \\\\((\\\\d+) pcs?\\\\.?\\\\)"
      }
    }

Use it with ``ingest(path, profile="office.json")`` or set ``DESIGN_INGEST_PROFILE``
to the file. Only the keys you give change; a layer group set to ``null`` is removed;
new groups are tried before the default ones (the first matching group wins).
JSON always works, TOML too on Python 3.11+. Keep profiles of real offices out of
the repository (``data/`` and ``samples/private/`` are git-ignored).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_PROFILE: dict[str, Any] = {
    "cad": {
        # group -> regex on the layer name (case-insensitive); first match wins
        "layer_groups": {
            "trenches": r"trench",
            "contours": r"contour",  # before earthing: "...Earth Contours" is terrain
            "earthing": r"earth|ground",
            "hedges": r"hedge",
            "fence": r"fence",
            "roads": r"road",
            "communication": r"communication|fib(er|re)|cctv|sensor",
            "string_cables_dc": r"string cable|\bdc\b.*cable|dc (neg|pos)",
            "mv_cables": r"\bmv\b.*cable|grid connection cable|substa",
            "lv_ac_cables": r"\blv\b.*cable|\bac\b.*cable|inverter.*transformer",
        },
        # block name of a module table: group 1 = rows, group 2 = modules per row
        "table_block_regex": r"^(\d+)P(\d+)\b.*PVBlock",
        # layer whose closed polylines give the fenced area
        "fence_layer_regex": r"fence",
        "fence_exclude_regex": r"hedge",
    },
    "pdf": {
        "title_keys": ["Project", "Location", "Title", "Drawn", "Checked", "Scale", "Date", "Drawing No", "Rev"],
        # "Table of 72 modules (653 pcs.)"
        "module_tables_regex": r"(?:Table|String)s? of (\d+) modules? \((\d+) pcs?\.?\)",
        # "Transformer station (4x6000 kVA)"
        "station_regex": r"(Transformer station|Inverter station|Substation)[^\n(]*\((\d+)\s*[x×]\s*(\d+)\s*kVA\)",
    },
}


def _read(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".toml":
        try:
            import tomllib
        except ImportError as e:  # Python 3.10
            raise ValueError(f"{path.name}: TOML profiles need Python 3.11+, use JSON") from e
        return tomllib.loads(text)
    return json.loads(text)


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))  # deep copy
    for section, values in (extra or {}).items():
        if not isinstance(values, dict):
            continue
        dst = out.setdefault(section, {})
        for key, val in values.items():
            if key == "layer_groups" and isinstance(val, dict):
                merged = {k: v for k, v in val.items() if v is not None}  # new groups first
                merged.update({k: v for k, v in dst.get("layer_groups", {}).items() if k not in val})
                dst["layer_groups"] = merged
            else:
                dst[key] = val
    return out


def load_profile(source: Any = None) -> dict[str, Any]:
    """Defaults merged with a profile given as dict, file path, or ``DESIGN_INGEST_PROFILE``."""
    if source is None:
        env = os.environ.get("DESIGN_INGEST_PROFILE")
        source = env or None
    if source is None:
        return _merge(DEFAULT_PROFILE, {})
    if isinstance(source, dict):
        return _merge(DEFAULT_PROFILE, source)
    path = Path(source).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"profile not found: {path}")
    return _merge(DEFAULT_PROFILE, _read(path))


def compiled_layer_groups(profile: dict[str, Any] | None) -> list[tuple[str, re.Pattern]]:
    groups = (profile or DEFAULT_PROFILE)["cad"]["layer_groups"]
    return [(g, re.compile(rx, re.I)) for g, rx in groups.items()]
