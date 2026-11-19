"""المؤشرات الفنية وقراءة الشموع — دوال صافية قابلة للاختبار."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bar:
    o: float
    h: float
    l: float
    c: float
    v: float


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], n: int) -> float | None:
    """RSI بطريقة Wilder."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def atr(bars: list[Bar], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c)))
    val = sum(trs[:n]) / n
    for tr in trs[n:]:
        val = (val * (n - 1) + tr) / n
    return val


# ───────── الشموع ─────────

def is_bullish(b: Bar) -> bool:
    """شمعة خضراء: الإغلاق أعلى من الافتتاح."""
    return b.c > b.o


def is_hammer(b: Bar) -> bool:
    """مطرقة: ذيل سفلي طويل (ضعف الجسم على الأقل) والإغلاق في النصف العلوي — رفض للهبوط."""
    rng = b.h - b.l
    if rng <= 0:
        return False
    body = abs(b.c - b.o)
    lower = min(b.o, b.c) - b.l
    return lower >= 2 * max(body, rng * 0.05) and (b.c - b.l) / rng >= 0.5


def is_bullish_engulfing(prev: Bar, cur: Bar) -> bool:
    """ابتلاع شرائي: شمعة خضراء تغطي جسم الشمعة الحمراء قبلها."""
    return prev.c < prev.o and cur.c > cur.o and cur.c >= prev.o and cur.o <= prev.c


def closes_strong(b: Bar, pct: float = 0.75) -> bool:
    """إغلاق قوي: الإغلاق في أعلى 25% من مدى الشمعة."""
    rng = b.h - b.l
    return rng > 0 and (b.c - b.l) / rng >= pct


def is_bearish_reversal(b: Bar) -> bool:
    """شهاب: ذيل علوي طويل والإغلاق في النصف السفلي — إشارة ضعف."""
    rng = b.h - b.l
    if rng <= 0:
        return False
    body = abs(b.c - b.o)
    upper = b.h - max(b.o, b.c)
    return upper >= 2 * max(body, rng * 0.05) and (b.c - b.l) / rng <= 0.5


def candle_reading(bars: list[Bar]) -> str:
    """وصف مختصر لآخر شمعة."""
    if len(bars) < 2:
        return "—"
    prev, cur = bars[-2], bars[-1]
    if is_bullish_engulfing(prev, cur):
        return "ابتلاع شرائي"
    if is_hammer(cur):
        return "مطرقة"
    if is_bearish_reversal(cur):
        return "شهاب (ضعف)"
    if closes_strong(cur):
        return "إغلاق قوي"
    return "خضراء" if is_bullish(cur) else "حمراء"
