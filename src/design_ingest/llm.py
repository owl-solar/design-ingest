"""Guard for every call that sends document content to an external model API.

Rules, so that confidential project files do not leave the computer by accident:

* **Off by default.** A call is allowed only with ``allow_external=True`` (CLI:
  ``--allow-external``) or the environment variable ``DESIGN_INGEST_ALLOW_EXTERNAL=1``.
  Otherwise ``ExternalCallBlocked`` is raised before anything is sent. Results that are
  already cached cost nothing and are not blocked.
* **Every call is logged** to ``~/.cache/design_ingest/llm_log.jsonl`` (or the file in
  ``DESIGN_INGEST_LLM_LOG``): time, purpose, model, file name, a hash prefix, what kind of
  content (text / PDF bytes), how much, and the tokens used. The content itself is never
  written to the log.
* **Daily token budget.** ``DESIGN_INGEST_TOKEN_BUDGET`` (input + output tokens per UTC day,
  summed from the log) stops further calls with ``BudgetExceeded``. Not set = no cap; the
  spend limit in the API console is the hard stop. Dollar prices change, so the cap is in
  tokens, not money.

``design-ingest llm-log`` prints the totals.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from .core import default_cache_dir


class ExternalCallBlocked(PermissionError):
    """Sending content to an external API was not allowed."""


class BudgetExceeded(RuntimeError):
    """The daily token budget is used up."""


_TRUE = ("1", "true", "yes", "on")


def log_path() -> Path:
    env = os.environ.get("DESIGN_INGEST_LLM_LOG")
    return Path(env) if env else default_cache_dir() / "llm_log.jsonl"


def is_allowed(allow_external: bool | None = None) -> bool:
    if allow_external is not None:
        return bool(allow_external)
    return os.environ.get("DESIGN_INGEST_ALLOW_EXTERNAL", "").strip().lower() in _TRUE


def require_allowed(purpose: str, allow_external: bool | None = None) -> None:
    if not is_allowed(allow_external):
        raise ExternalCallBlocked(
            f"'{purpose}' would send document content to the Anthropic API and is blocked by default. "
            "Use allow_external=True (CLI: --allow-external) or set DESIGN_INGEST_ALLOW_EXTERNAL=1, "
            "only for documents you may share with an external service."
        )


def read_log(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or log_path()
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def tokens_today(path: Path | None = None) -> int:
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    return sum(r.get("input_tokens", 0) + r.get("output_tokens", 0) for r in read_log(path) if r.get("time", "")[:10] == today)


def check_budget(estimated_tokens: int) -> None:
    raw = os.environ.get("DESIGN_INGEST_TOKEN_BUDGET", "").strip()
    if not raw:
        return
    budget = int(raw)
    used = tokens_today()
    if used + estimated_tokens > budget:
        raise BudgetExceeded(
            f"daily token budget {budget} would be exceeded ({used} used today, about {estimated_tokens} needed). "
            "Raise DESIGN_INGEST_TOKEN_BUDGET or wait until tomorrow (UTC)."
        )


def _append(record: dict[str, Any]) -> None:
    p = log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # a full disk must not lose the result of a paid call


def guarded_create(
    client: Any,
    *,
    purpose: str,
    file: str,
    sha256: str,
    kind: str,
    amount: int,
    allow_external: bool | None = None,
    **request: Any,
):
    """``client.messages.create(**request)`` behind the guard.

    ``kind`` / ``amount`` describe what is sent for the log ("text", characters or
    "pdf", bytes). Returns the API response.
    """
    require_allowed(purpose, allow_external)
    estimated = amount // 3 if kind == "text" else amount // 4  # rough: enough for a cap, not for billing
    check_budget(estimated + int(request.get("max_tokens", 0)))
    resp = client.messages.create(**request)
    usage = getattr(resp, "usage", None)
    _append({
        "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "purpose": purpose,
        "model": request.get("model"),
        "file": file,
        "sha256": sha256[:12],
        "sent": {kind: amount},
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "stop_reason": getattr(resp, "stop_reason", None),
    })
    return resp


def summarize(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Calls and tokens per day / purpose / model, plus the last calls."""
    recs = read_log() if records is None else records
    days: dict[str, dict[str, int]] = {}
    for r in recs:
        d = days.setdefault(r.get("time", "")[:10], {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        d["calls"] += 1
        d["input_tokens"] += r.get("input_tokens", 0)
        d["output_tokens"] += r.get("output_tokens", 0)
    return {
        "calls": len(recs),
        "input_tokens": sum(r.get("input_tokens", 0) for r in recs),
        "output_tokens": sum(r.get("output_tokens", 0) for r in recs),
        "per_day": days,
        "files": sorted({r.get("file", "") for r in recs}),
        "last": recs[-5:],
    }
