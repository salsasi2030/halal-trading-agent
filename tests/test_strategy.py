import pytest

from agent import config, state
from agent.indicators import Bar, atr, is_bullish_engulfing, is_hammer, rsi, sma
from agent.news import red_flags
from agent.strategy import (check_exit, evaluate, market_is_healthy, new_meta, pick_entries,
                            position_size, Signal)
from agent.universe import load_snapshot


def uptrend(n=230, start=100.0, step=0.3, wiggle=1.0):
    bars, p = [], start
    for i in range(n):
        p += step
        o = p - 0.2
        bars.append(Bar(o, p + wiggle, o - wiggle, p, 1_000_000))
    return bars


# ───────── المؤشرات ─────────

def test_sma_and_rsi_basics():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert rsi([1, 2, 3, 4, 5, 6], 2) == 100.0
    assert rsi([6, 5, 4, 3, 2, 1], 2) == pytest.approx(0.0)


def test_atr_positive():
    assert atr(uptrend(30), 14) > 0


def test_candles():
    assert is_hammer(Bar(o=100, h=101, l=94, c=100.5, v=1))
    assert not is_hammer(Bar(o=100, h=106, l=99.5, c=100.5, v=1))
    assert is_bullish_engulfing(Bar(102, 102.5, 99.5, 100, 1), Bar(99.8, 103, 99.5, 102.5, 1))


# ───────── الإشارات ─────────

def test_pullback_signal_in_uptrend():
    bars = uptrend()
    p = bars[-1].c
    # يومان هبوط حاد ثم مطرقة
    bars += [Bar(p, p + 0.2, p - 3, p - 2.8, 1e6), Bar(p - 2.8, p - 2.6, p - 6, p - 5.8, 1e6)]
    q = bars[-1].c
    bars.append(Bar(q - 0.2, q + 0.4, q - 4, q + 0.3, 1e6))
    sig = evaluate("TEST", bars)
    assert sig is not None and sig.setup == "ارتداد"


def test_breakout_signal_needs_volume():
    bars = uptrend(step=0.0, wiggle=1.0)  # تذبذب أفقي…
    bars = [Bar(b.o + i * 0.25, b.h + i * 0.25, b.l + i * 0.25, b.c + i * 0.25, b.v)
            for i, b in enumerate(bars)]  # …بميل صاعد خفيف
    p = bars[-1].c
    weak = bars + [Bar(p, p + 4.1, p - 0.1, p + 4, 1e6)]
    strong = bars + [Bar(p, p + 4.1, p - 0.1, p + 4, 3e6)]
    assert evaluate("T", weak) is None or evaluate("T", weak).setup != "اختراق"
    sig = evaluate("T", strong)
    assert sig is not None and sig.setup == "اختراق"


def test_no_signal_in_downtrend():
    bars = uptrend(step=-0.3, start=200)
    assert evaluate("DOWN", bars) is None


def test_market_filter():
    ok, _ = market_is_healthy(uptrend())
    bad, _ = market_is_healthy(uptrend(step=-0.3, start=200))
    assert ok and not bad


# ───────── الحجم والخروج ─────────

def test_position_size_caps():
    # 20% من 1000 = 200، والمخاطرة 2% = 20$ مع وقف 2×ATR
    assert position_size(1000, 1000, price=100, a=1) == pytest.approx(200)
    assert position_size(1000, 1000, price=100, a=5) == pytest.approx(200)
    assert position_size(1000, 1000, price=100, a=10) == pytest.approx(100)  # المخاطرة تحدّ الحجم
    assert position_size(1000, 50, price=100, a=1) == pytest.approx(50)      # النقد يحدّ الحجم


def test_pick_entries_respects_slots_cash_and_held():
    sigs = [Signal(f"S{i}", "ارتداد", 10 - i, 100, 1, "") for i in range(8)] + \
           [Signal("BTC/USD", "اختراق", 99, 90000, 2000, "")]
    picks = pick_entries(sigs, slots=3, equity=1000, cash=1000, held={"BTCUSD"})
    assert [s.symbol for s, _ in picks] == ["S0", "S1", "S2"]
    assert sum(sz for _, sz in picks) <= 1000 * (1 - config.CASH_BUFFER)


def test_exits():
    m = new_meta(100, 2, "ارتداد", "2026-11-16")
    assert m["stop"] == 96 and m["target"] == 100 + config.TARGET_ATR * 2
    assert "وقف خسارة" in check_exit(m, 95.9, 1)[0]
    assert "الهدف" in check_exit(m, m["target"] + 0.1, 1)[0]
    assert "المدة" in check_exit(m, 101, config.MAX_HOLD_DAYS)[0]
    reason, m2 = check_exit(m, 103.5, 1)
    assert reason is None and m2["stop"] == 100        # الوقف ارتفع لسعر الدخول
    assert "سعر الدخول" in check_exit(m2, 99.9, 2)[0]


def test_trading_days():
    assert state.trading_days_between("2026-11-20", "2026-11-23") == 1   # جمعة ← إثنين
    assert state.trading_days_between("2026-11-16", "2026-11-23") == 5


# ───────── الأخبار والقائمة ─────────

def test_news_red_flags():
    assert red_flags(["Company files for Chapter 11 protection"])
    assert not red_flags(["Company beats earnings estimates"])


def test_snapshot_universe_is_shariah_list():
    syms = load_snapshot()
    assert len(syms) > 200
    assert "NVDA" in syms and "TSLA" in syms
    for banned in ("JPM", "BAC", "MO", "LVS", "BUD"):   # بنوك، تبغ، كازينو، خمور
        assert banned not in syms
