"""منطق القرار للمضاربة القصيرة (يوم إلى أسبوع) — دوال صافية بلا اتصال بالإنترنت."""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .indicators import (Bar, atr, candle_reading, closes_strong, is_bearish_reversal,
                         is_bullish, is_bullish_engulfing, is_hammer, rsi, sma)


@dataclass
class Signal:
    symbol: str
    setup: str          # "ارتداد" أو "اختراق"
    score: float
    price: float
    atr: float
    reason: str


def market_is_healthy(index_bars: list[Bar]) -> tuple[bool, str]:
    """السوق صحي إذا كان المؤشر فوق متوسط 200 يوم. غير ذلك: لا أسهم جديدة."""
    closes = [b.c for b in index_bars]
    s200 = sma(closes, 200)
    if s200 is None:
        return True, "بيانات المؤشر غير كافية — يُعتبر السوق صحياً"
    ok = closes[-1] > s200
    return ok, f"{config.MARKET_INDEX} {'فوق' if ok else 'تحت'} متوسط 200 يوم ({closes[-1]:.2f} مقابل {s200:.2f})"


def evaluate(symbol: str, bars: list[Bar]) -> Signal | None:
    """يفحص آخر الشموع المكتملة ويرجع إشارة دخول أو لا شيء."""
    if len(bars) < 201:
        return None
    closes = [b.c for b in bars]
    last, prev = bars[-1], bars[-2]
    price = last.c
    s50, s200 = sma(closes, 50), sma(closes, 200)
    a = atr(bars, 14)
    if not a or not s50 or not s200:
        return None

    atr_pct = a / price
    if not (config.MIN_ATR_PCT <= atr_pct <= config.MAX_ATR_PCT):
        return None
    if price <= s200 or s50 <= s200:          # لا نشتري إلا في اتجاه صاعد
        return None
    if is_bearish_reversal(last):              # شمعة ضعف — نتجنب
        return None

    candle = candle_reading(bars)

    # 1) ارتداد: تشبّع بيعي أمس (RSI(2) منخفض) + شمعة انعكاس اليوم
    rsi_prev = rsi(closes[:-1], 2)
    reversal = is_hammer(last) or is_bullish_engulfing(prev, last) or is_bullish(last)
    if rsi_prev is not None and rsi_prev < config.RSI2_PULLBACK and reversal:
        bonus = 5 if (is_hammer(last) or is_bullish_engulfing(prev, last)) else 0
        return Signal(symbol, "ارتداد", (config.RSI2_PULLBACK - rsi_prev) + bonus, price, a,
                      f"اتجاه صاعد، RSI(2) قبل آخر شمعة {rsi_prev:.0f}، آخر شمعة: {candle}")

    # 2) اختراق: إغلاق فوق أعلى سعر 20 يوماً بحجم مرتفع وإغلاق قوي
    n = config.BREAKOUT_DAYS
    prior_high = max(b.h for b in bars[-n - 1:-1])
    vols = [b.v for b in bars[-n - 1:-1]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    vol_ratio = last.v / avg_vol if avg_vol else 0
    if price > prior_high and vol_ratio >= config.BREAKOUT_VOLUME and closes_strong(last):
        return Signal(symbol, "اختراق", vol_ratio * 5, price, a,
                      f"اختراق قمة {n} يوماً بحجم {vol_ratio:.1f}× المتوسط، آخر شمعة: {candle}")
    return None


def position_size(equity: float, available_cash: float, price: float, a: float) -> float:
    """حجم الصفقة بالدولار: الأقل من (20% من رأس المال، حجم يجعل الخسارة عند الوقف ≤ 2%، النقد المتاح)."""
    stop_dist = config.STOP_ATR * a
    by_risk = equity * config.RISK_PCT / stop_dist * price if stop_dist > 0 else 0
    cap = equity * config.POSITION_PCT
    return max(0.0, min(cap, by_risk, available_cash))


def pick_entries(signals: list[Signal], slots: int, equity: float, cash: float,
                 held: set[str]) -> list[tuple[Signal, float]]:
    """يختار أفضل الإشارات حسب الدرجة في حدود الأماكن الفارغة والنقد."""
    out = []
    available = cash - equity * config.CASH_BUFFER
    for s in sorted(signals, key=lambda s: s.score, reverse=True):
        if len(out) >= slots:
            break
        if s.symbol.replace("/", "") in held:
            continue
        size = position_size(equity, available, s.price, s.atr)
        if size < max(config.MIN_ORDER_USD, 10.0):
            continue
        out.append((s, round(size, 2)))
        available -= size
    return out


def check_exit(meta: dict, price: float, days_held: int) -> tuple[str | None, dict]:
    """يرجع (سبب الخروج أو None، البيانات بعد التحديث). يُستدعى كل ساعة."""
    meta = dict(meta)
    entry, a = meta["entry"], meta["atr"]
    if price <= meta["stop"]:
        label = "وقف عند سعر الدخول" if meta["stop"] >= entry else "وقف خسارة"
        return f"{label}: {price:.2f} ≤ {meta['stop']:.2f}", meta
    if price >= meta["target"]:
        return f"الهدف تحقق: {price:.2f} ≥ {meta['target']:.2f}", meta
    if days_held >= config.MAX_HOLD_DAYS:
        return f"انتهت المدة ({days_held} أيام تداول)", meta
    if price >= entry + config.BREAKEVEN_ATR * a and meta["stop"] < entry:
        meta["stop"] = round(entry, 4)       # حماية الربح: الوقف يرتفع لسعر الدخول
    return None, meta


def new_meta(entry: float, a: float, setup: str, date: str) -> dict:
    return {
        "entry": round(entry, 4),
        "atr": round(a, 4),
        "stop": round(entry - config.STOP_ATR * a, 4),
        "target": round(entry + config.TARGET_ATR * a, 4),
        "setup": setup,
        "entry_date": date,
    }
