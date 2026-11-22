"""Journal statistics and retention reporting (spec 4.8).

The hub journal is append-only with no rotation today. The admin page
surfaces its size/line count plus a retention report against the
configured policy (keep 90 days online, rotate at 50 MB, archive the
rest compressed instead of deleting). This module only MEASURES and
reports; the rotation itself runs hub-side.
"""

from __future__ import annotations

import json
import os
from typing import Optional


def journal_stats(path: str) -> dict:
    if not os.path.exists(path):
        return {"exists": False, "bytes": 0, "lines": 0, "first_ts": None, "last_ts": None}
    size = os.path.getsize(path)
    lines = 0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            ts = record.get("ts")
            if isinstance(ts, (int, float)):
                if first_ts is None:
                    first_ts = float(ts)
                last_ts = float(ts)
    return {"exists": True, "bytes": size, "lines": lines, "first_ts": first_ts, "last_ts": last_ts}


def retention_report(path: str, online_days: int, rotate_mb: int) -> dict:
    stats = journal_stats(path)
    report = dict(stats)
    report["policy"] = {"online_days": online_days, "rotate_mb": rotate_mb}
    report["over_rotate_threshold"] = stats["bytes"] >= rotate_mb * 1024 * 1024
    span_days = None
    if stats["first_ts"] and stats["last_ts"]:
        span_days = round((stats["last_ts"] - stats["first_ts"]) / 86400, 2)
    report["span_days"] = span_days
    report["over_online_window"] = bool(span_days is not None and span_days > online_days)
    report["action_needed"] = report["over_rotate_threshold"] or report["over_online_window"]
    return report
