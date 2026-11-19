"""حالة الوكيل بين التشغيلات (تُحفظ في state.json داخل المستودع)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

STATE_FILE = Path(__file__).resolve().parent.parent / "state.json"
NY = ZoneInfo("America/New_York")


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


def today_ny(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(NY).strftime("%Y-%m-%d")


def trading_days_between(start: str, end: str) -> int:
    """عدد أيام التداول (الإثنين–الجمعة) بعد يوم الدخول وحتى اليوم."""
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end)
    days, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days
