"""منطق التداول السريع على شموع 5 دقائق — دوال صافية قابلة للاختبار."""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .indicators import Bar, atr, closes_strong, is_bullish, is_bullish_engulfing, is_hammer, sma


@dataclass
class DaySignal:
    symbol: str
    setup: str           # "اختراق الافتتاح" أو "ارتداد VWAP"
    score: float
    price: float
    atr_d: float         # ATR اليومي
    reason: str


def daily_context(daily: list[Bar]) -> dict | None:
    """من الشموع اليومية المكتملة: ATR اليومي، متوسط 20 يوم، إغلاق الأمس."""
    if len(daily) < 21:
        return None
    a = atr(daily, 14)
    s20 = sma([b.c for b in daily], 20)
    if not a or not s20:
        return None
    return {"atr": a, "sma20": s20, "prev_close": daily[-1].c}


def vwap(bars: list[Bar]) -> float | None:
    vol = sum(b.v for b in bars)
    if vol <= 0:
        return None
    return sum((b.h + b.l + b.c) / 3 * b.v for b in bars) / vol


def market_ok(spy5: list[Bar]) -> tuple[bool, str]:
    """السوق داعم إذا كان SPY فوق متوسطه المرجّح بالحجم (VWAP) اليوم."""
    if len(spy5) < 2:
        return True, "بيانات SPY غير كافية بعد"
    vw = vwap(spy5)
    if vw is None:
        return True, "لا حجم لـ SPY"
    ok = spy5[-1].c > vw
    return ok, f"SPY {'فوق' if ok else 'تحت'} VWAP ({spy5[-1].c:.2f} مقابل {vw:.2f})"


def evaluate_intraday(symbol: str, bars: list[Bar], ctx: dict, hhmm: str,
                      spy_change: float = 0.0) -> DaySignal | None:
    """bars = شموع 5 دقائق المكتملة اليوم. hhmm = الوقت الحالي بتوقيت نيويورك."""
    if not ctx or len(bars) < config.DT_ORB_BARS + 1:
        return None
    if not (config.DT_ENTRY_START <= hhmm <= config.DT_ENTRY_END):
        return None
    last, prev = bars[-1], bars[-2]
    price = last.c
    a = ctx["atr"]
    if price < config.DT_MIN_PRICE:
        return None
    if not (config.DT_MIN_ATR_PCT <= a / price <= config.DT_MAX_ATR_PCT):
        return None
    if ctx["prev_close"] <= ctx["sma20"]:           # الاتجاه اليومي صاعد فقط
        return None
    vw = vwap(bars)
    if vw is None or price <= vw:                     # فوق VWAP فقط
        return None

    day_change = price / ctx["prev_close"] - 1
    rs = (day_change - spy_change) * 100              # القوة النسبية مقابل السوق (نقاط مئوية)
    avg_vol = sum(b.v for b in bars[:-1]) / max(len(bars) - 1, 1)
    vol_ratio = last.v / avg_vol if avg_vol else 0

    # 1) اختراق نطاق الافتتاح (أول 15 دقيقة)
    n = config.DT_ORB_BARS
    or_high = max(b.h for b in bars[:n])
    if (hhmm <= config.DT_ORB_END and len(bars) > n and price > or_high and prev.c <= or_high
            and closes_strong(last, 0.7) and vol_ratio >= config.DT_VOLUME_RATIO):
        return DaySignal(symbol, "اختراق الافتتاح", 10 + rs + vol_ratio, price, a,
                         f"اخترق قمة أول 15 دقيقة ({or_high:.2f}) بحجم {vol_ratio:.1f}× وإغلاق قوي، "
                         f"اليوم {day_change:+.1%}")

    # 2) ارتداد من VWAP في يوم صاعد
    above_share = sum(1 for b in bars if b.c > vw) / len(bars)
    touched = last.l <= vw * 1.003
    reversal = is_hammer(last) or is_bullish_engulfing(prev, last) or is_bullish(last)
    if hhmm >= "10:00" and day_change > 0 and above_share >= 0.6 and touched and reversal:
        return DaySignal(symbol, "ارتداد VWAP", 5 + rs + vol_ratio, price, a,
                         f"يوم صاعد ({day_change:+.1%})، لمس VWAP ({vw:.2f}) وارتد بشمعة "
                         f"{'مطرقة' if is_hammer(last) else 'خضراء'}")
    return None


def dt_meta(entry: float, atr_d: float, setup: str, date: str) -> dict:
    return {
        "mode": "dt",
        "entry": round(entry, 4),
        "atr": round(atr_d, 4),
        "stop": round(entry - config.DT_STOP_ATR * atr_d, 4),
        "target": round(entry + config.DT_TARGET_ATR * atr_d, 4),
        "setup": setup,
        "entry_date": date,
        "max_days": config.DT_MAX_HOLD_DAYS,
    }


def dt_check_exit(meta: dict, price: float, days_held: int) -> tuple[str | None, dict]:
    """يُستدعى كل دقيقة: وقف، هدف، مدة، ثم رفع الوقف لحماية الربح."""
    meta = dict(meta)
    entry, a = meta["entry"], meta["atr"]
    if price <= meta["stop"]:
        if meta["stop"] > entry:
            label = "وقف متحرك (ربح محمي)"
        elif meta["stop"] == entry:
            label = "وقف عند سعر الدخول"
        else:
            label = "وقف خسارة"
        return f"{label}: {price:.2f} ≤ {meta['stop']:.2f}", meta
    if price >= meta["target"]:
        return f"الهدف تحقق: {price:.2f} ≥ {meta['target']:.2f}", meta
    if days_held >= meta.get("max_days", config.DT_MAX_HOLD_DAYS):
        return f"انتهت المدة ({days_held} أيام)", meta
    if price >= entry + config.DT_BREAKEVEN_ATR * a:
        meta["stop"] = max(meta["stop"], round(entry, 4))
    if price >= entry + config.DT_TRAIL_START_ATR * a:
        meta["stop"] = max(meta["stop"], round(price - config.DT_TRAIL_ATR * a, 4))
    return None, meta
