"""قائمة الأدوات المسموح تداولها."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import requests

from . import config

SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "spus_snapshot.txt"
_TICKER = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def _clean(tickers) -> list[str]:
    out = []
    for t in tickers:
        t = (t or "").strip().upper()
        if _TICKER.match(t) and t not in config.BLACKLIST and t not in out:
            out.append(t)
    return out


def load_snapshot() -> list[str]:
    words = []
    for line in SNAPSHOT.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            words += line.split()
    return _clean(words)


def load_stocks() -> tuple[list[str], str]:
    """يرجع (القائمة، المصدر). يحاول التحميل المباشر من موقع SPUS ثم النسخة المحفوظة."""
    try:
        r = requests.get(config.SPUS_HOLDINGS_CSV, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0 halal-trading-agent"})
        r.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(r.text)))
        col = next((c for c in (rows[0].keys() if rows else [])
                    if c and c.strip().lower() in ("stockticker", "ticker", "symbol")), None)
        if col:
            tickers = _clean(row[col] for row in rows)
            if len(tickers) >= 100:
                return tickers, "SPUS (تحميل مباشر)"
    except Exception:  # noqa: BLE001 — أي فشل ننتقل للنسخة المحفوظة
        pass
    return load_snapshot(), "SPUS (نسخة محفوظة)"


def key(symbol: str) -> str:
    """توحيد الرمز (Alpaca يكتب BTC/USD في الأوامر و BTCUSD في المراكز)."""
    return symbol.replace("/", "")


def is_crypto(symbol: str) -> bool:
    return "/" in symbol or key(symbol) in {key(c) for c in config.CRYPTO}
