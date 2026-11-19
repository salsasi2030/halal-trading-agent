import datetime as dt

from agent import config
from agent.backtest import max_drawdown, report, run, stats, Trade
from agent.indicators import Bar


def dated(bars):
    d0 = dt.date(2022, 1, 3)
    return [((d0 + dt.timedelta(days=i)).isoformat(), b) for i, b in enumerate(bars)]


def trend(n, start=100.0, step=0.3):
    out, p = [], start
    for _ in range(n):
        p += step
        out.append(Bar(p - 0.2, p + 1, p - 1.2, p, 1e6))
    return out


def with_pullback_then(after):
    """اتجاه صاعد، هبوط يومين، مطرقة، ثم أيام 'after'."""
    bars = trend(230)
    p = bars[-1].c
    bars += [Bar(p, p + .2, p - 3, p - 2.8, 1e6), Bar(p - 2.8, p - 2.6, p - 6, p - 5.8, 1e6)]
    q = bars[-1].c
    bars.append(Bar(q - .2, q + .4, q - 4, q + .3, 1e6))
    return bars + after(bars[-1].c)


def test_winning_trade_hits_target():
    up = lambda c: [Bar(c + i, c + i + 6, c + i - .5, c + i + 5, 1e6) for i in range(0, 30, 5)]
    data = {"SPY": dated(trend(240)), "AAA": dated(with_pullback_then(up))}
    res = run(data, "SPY", {"AAA"}, data["SPY"][0][0])
    assert res.trades and res.trades[0].reason in ("هدف", "فجوة فوق الهدف")
    assert res.trades[0].pnl > 0
    assert res.curve[-1][1] > config.BUDGET_USD


def test_losing_trade_hits_stop_and_loss_is_limited():
    down = lambda c: [Bar(c - i, c - i + .5, c - i - 6, c - i - 5, 1e6) for i in range(0, 30, 5)]
    data = {"SPY": dated(trend(240)), "AAA": dated(with_pullback_then(down))}
    res = run(data, "SPY", {"AAA"}, data["SPY"][0][0])
    t = res.trades[0]
    assert t.pnl < 0 and "وقف" in t.reason
    assert -t.pnl <= config.BUDGET_USD * config.RISK_PCT * 3   # حتى مع الفجوة تبقى الخسارة محدودة


def test_no_stock_trades_when_market_weak():
    up = lambda c: [Bar(c + i, c + i + 6, c + i - .5, c + i + 5, 1e6) for i in range(0, 30, 5)]
    data = {"SPY": dated(trend(240, start=300, step=-0.3)), "AAA": dated(with_pullback_then(up))}
    res = run(data, "SPY", {"AAA"}, data["SPY"][0][0])
    assert res.trades == []


def test_stats_and_drawdown():
    t1 = Trade("A", "ارتداد", "d", 100, "d", 110, qty=1)
    t2 = Trade("B", "اختراق", "d", 100, "d", 95, qty=1)
    s = stats([t1, t2])
    assert s["n"] == 2 and s["win_rate"] == 0.5 and s["profit_factor"] == 2
    assert max_drawdown([100, 120, 90, 130]) == -0.25


def test_report_renders():
    up = lambda c: [Bar(c + i, c + i + 6, c + i - .5, c + i + 5, 1e6) for i in range(0, 30, 5)]
    data = {"SPY": dated(trend(240)), "AAA": dated(with_pullback_then(up))}
    res = run(data, "SPY", {"AAA"}, data["SPY"][0][0])
    lines = report(res, [("a", 100.0), ("b", 110.0)], 1000, 1.0, {"AAA"})
    text = "\n".join(lines)
    assert "SPUS" in text and "ارتداد" in text
