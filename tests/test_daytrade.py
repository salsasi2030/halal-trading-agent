import datetime as dt
import json

import pytest

from agent import config, state as st
from agent import daytrade as dtm
from agent.daystrategy import (daily_context, dt_check_exit, dt_meta, evaluate_intraday,
                               market_ok, vwap)
from agent.indicators import Bar

CTX = {"atr": 2.0, "sma20": 95.0, "prev_close": 100.0}   # ATR 2% ، اتجاه صاعد


def opening(n=3, base=100.0):
    """نطاق افتتاح بين 100 و 101."""
    return [Bar(base, base + 1, base - 0.2, base + 0.5, 1000) for _ in range(n)]


# ───────── الإشارات ─────────

def test_vwap():
    assert vwap([Bar(1, 2, 0, 1, 10), Bar(1, 2, 0, 1, 10)]) == pytest.approx(1.0)


def test_orb_breakout_signal():
    bars = opening() + [Bar(100.5, 101, 100.3, 100.8, 1000)]
    bars.append(Bar(100.8, 102.1, 100.7, 102.0, 3000))   # اختراق بحجم 3× وإغلاق قوي
    sig = evaluate_intraday("AAA", bars, CTX, "10:05")
    assert sig and sig.setup == "اختراق الافتتاح"


def test_orb_needs_volume_and_time():
    weak = opening() + [Bar(100.5, 101, 100.3, 100.8, 1000), Bar(100.8, 102.1, 100.7, 102.0, 900)]
    assert evaluate_intraday("AAA", weak, CTX, "10:05") is None or \
        evaluate_intraday("AAA", weak, CTX, "10:05").setup != "اختراق الافتتاح"
    strong = opening() + [Bar(100.5, 101, 100.3, 100.8, 1000), Bar(100.8, 102.1, 100.7, 102.0, 3000)]
    assert evaluate_intraday("AAA", strong, CTX, "12:00") is None or \
        evaluate_intraday("AAA", strong, CTX, "12:00").setup != "اختراق الافتتاح"


def test_no_entry_outside_window_or_downtrend():
    bars = opening() + [Bar(100.5, 101, 100.3, 100.8, 1000), Bar(100.8, 102.1, 100.7, 102.0, 3000)]
    assert evaluate_intraday("AAA", bars, CTX, "09:40") is None
    assert evaluate_intraday("AAA", bars, CTX, "15:45") is None
    assert evaluate_intraday("AAA", bars, {**CTX, "sma20": 105}, "10:05") is None


def test_vwap_bounce_signal():
    # افتتاح بحجم كبير قرب 100.5 ثم صعود هادئ فوق VWAP طوال اليوم
    bars = [Bar(100.3, 100.8, 100.2, 100.5, 8000)] + \
           [Bar(102 + i * .1, 102.5 + i * .1, 101.9 + i * .1, 102.3 + i * .1, 500) for i in range(9)]
    vw = vwap(bars)
    bars.append(Bar(vw + 0.3, vw + 0.8, vw - 0.1, vw + 0.7, 1200))  # لمس VWAP وإغلاق أخضر
    sig = evaluate_intraday("BBB", bars, CTX, "11:40")
    assert sig and sig.setup == "ارتداد VWAP"


def test_market_ok():
    up = [Bar(100 + i, 101 + i, 99 + i, 100.5 + i, 1000) for i in range(6)]
    down = [Bar(100 - i, 101 - i, 99 - i, 99.5 - i, 1000) for i in range(6)]
    assert market_ok(up)[0] and not market_ok(down)[0]


def test_daily_context():
    daily = [Bar(100 + i, 102 + i, 99 + i, 101 + i, 1e6) for i in range(30)]
    ctx = daily_context(daily)
    assert ctx["prev_close"] == 130 and ctx["atr"] > 0


# ───────── الخروج ─────────

def test_dt_exits_and_trailing():
    m = dt_meta(100, 2, "اختراق الافتتاح", "2026-11-23")
    assert m["stop"] == 99 and m["target"] == 101.5
    assert "وقف خسارة" in dt_check_exit(m, 98.9, 0)[0]
    assert "الهدف" in dt_check_exit(m, 101.6, 0)[0]
    assert "المدة" in dt_check_exit(m, 100.2, config.DT_MAX_HOLD_DAYS)[0]
    r, m2 = dt_check_exit(m, 100.85, 0)          # +0.425 ATR → الوقف لسعر الدخول
    assert r is None and m2["stop"] == 100
    r, m3 = dt_check_exit(m2, 101.3, 0)          # +0.65 ATR → وقف متحرك
    assert r is None and m3["stop"] == pytest.approx(101.3 - 0.7)
    assert "ربح محمي" in dt_check_exit(m3, 100.5, 0)[0]


# ───────── الحلقة كاملة مع وسيط وهمي ─────────

class FakeBroker:
    def __init__(self):
        self.pos, self.orders, self.open = {}, [], True

    def clock(self): return {"is_open": self.open, "next_open": "2030-01-01T14:30:00Z"}
    def account(self): return {"equity": "100000", "cash": "99000"}
    def positions(self): return dict(self.pos)
    def tradable(self, s): return {x: True for x in s}

    def daily_bars(self, symbols, days=60):
        series = [(f"2026-10-{i + 1:02d}", Bar(90 + i * .4, 92 + i * .4, 89 + i * .4, 91 + i * .4, 1e6))
                  for i in range(25)]
        return {s: series for s in symbols}

    def intraday_bars(self, symbols, start):
        t0 = dt.datetime(2026, 11, 23, 14, 30, tzinfo=dt.timezone.utc)
        o = [Bar(100, 101, 99.8, 100.5, 1000)] * 3 + [Bar(100.5, 101, 100.3, 100.8, 1000),
                                                     Bar(100.8, 102.1, 100.7, 102.0, 3000)]
        ser = [((t0 + dt.timedelta(minutes=5 * i)).isoformat(), b) for i, b in enumerate(o)]
        spy = [((t0 + dt.timedelta(minutes=5 * i)).isoformat(), Bar(600 + i, 601 + i, 599 + i, 600.8 + i, 1e5))
               for i in range(5)]
        return {**{s: ser for s in symbols if s == "AAA"}, "SPY": spy}

    def news(self, s, d): return {}

    def buy(self, s, n):
        self.orders.append(("buy", s, n))
        return {"id": f"o{len(self.orders)}", "status": "accepted"}

    def close(self, s):
        self.orders.append(("sell", s))
        return {"id": "c", "status": "accepted"}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(dtm, "TRADES_CSV", tmp_path / "t.csv")
    monkeypatch.setattr(dtm, "REPORT_MD", tmp_path / "r.md")
    monkeypatch.setattr(dtm, "REVIEW_JSON", tmp_path / "rev.json")
    monkeypatch.setattr(dtm, "load_stocks", lambda: (["AAA", "BBB"], "test"))
    return tmp_path


NOW = dt.datetime(2026, 11, 23, 15, 1, tzinfo=dt.timezone.utc)   # 10:01 نيويورك


def test_loop_pass_buys_then_exits_on_target(sandbox):
    fb = FakeBroker()
    t = dtm.Trader(fb)
    t.run_once(NOW)
    assert ("buy", "AAA", config.DT_POSITION_USD) in fb.orders
    s = json.loads(st.STATE_FILE.read_text())
    assert s["dt_new_today"] == 1 and s["positions"]["AAA"]["mode"] == "dt"
    target = s["positions"]["AAA"]["target"]
    fb.pos = {"AAA": {"symbol": "AAA", "qty": "1", "market_value": "103", "current_price": str(target + .1),
                      "avg_entry_price": "102", "unrealized_plpc": "0.01"}}
    t.run_once(NOW + dt.timedelta(minutes=1))     # أول مرة بعد التنفيذ: يعيد الحساب على سعر التنفيذ
    t.run_once(NOW + dt.timedelta(minutes=2))
    assert any(o[0] == "sell" for o in fb.orders)
    t.write_report(NOW)
    assert "صفقات اليوم" in (sandbox / "r.md").read_text()


def test_blocked_by_news_review(sandbox):
    (sandbox / "rev.json").write_text(json.dumps({"date": "2026-11-23", "blocked": {"AAA": "أرباح غداً"}},
                                                 ensure_ascii=False))
    fb = FakeBroker()
    dtm.Trader(fb).run_once(NOW)
    assert not any(o[0] == "buy" for o in fb.orders)


def test_daily_new_trade_cap(sandbox):
    st.save({"anchor_equity": 100000, "dt_day": "2026-11-23", "dt_new_today": config.DT_MAX_NEW_PER_DAY,
             "positions": {}})
    fb = FakeBroker()
    dtm.Trader(fb).run_once(NOW)
    assert not any(o[0] == "buy" for o in fb.orders)


def test_closed_market_does_nothing(sandbox):
    fb = FakeBroker()
    fb.open = False
    assert dtm.Trader(fb).run_once(NOW)["closed"] is True


def test_before_session_no_data_request(sandbox):
    fb = FakeBroker()
    called = []
    fb.intraday_bars = lambda *a: called.append(a) or {}
    t = dtm.Trader(fb, dry=True)
    t.run_once(dt.datetime(2026, 11, 23, 7, 0, tzinfo=dt.timezone.utc))   # 2:00 فجراً نيويورك
    assert called == []
