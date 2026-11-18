import pytest

from agent import config
from agent.strategy import Position, build_plan, validate_weights

SYMS = list(config.TARGET_WEIGHTS)
PRICES = {"SPUS": 45.0, "HLAL": 55.0, "UMMA": 25.0, "SPSK": 18.0, "SPRE": 22.0}
FRAC = {s: True for s in SYMS}


def by_symbol(plan):
    return {o.symbol: o for o in plan.orders}


def test_first_day_buys_all_targets_within_cash():
    plan = build_plan(100_000, 100_000, {}, PRICES, FRAC, set())
    orders = by_symbol(plan)
    assert set(orders) == set(SYMS)
    assert all(o.side == "buy" for o in orders.values())
    total = sum(o.notional for o in orders.values())
    assert total <= 100_000 * (1 - config.CASH_BUFFER) + 0.01
    assert orders["SPUS"].notional == pytest.approx(100_000 * 0.98 * 0.30, rel=1e-3)


def test_never_buys_outside_whitelist():
    plan = build_plan(100_000, 100_000, {}, {**PRICES, "TSLA": 200}, {**FRAC, "TSLA": True}, set())
    assert all(o.symbol in config.TARGET_WEIGHTS for o in plan.orders)


def test_stop_loss_sells_everything_and_does_not_rebuy():
    pos = {"SPUS": Position("SPUS", 100, 2700, -0.10)}
    plan = build_plan(100_000, 97_300, pos, PRICES, FRAC, set())
    spus = [o for o in plan.orders if o.symbol == "SPUS"]
    assert len(spus) == 1
    assert spus[0].side == "sell" and spus[0].qty == 100
    assert spus[0].tag == config.STOP_LOSS_TAG


def test_cooldown_blocks_buying():
    plan = build_plan(100_000, 100_000, {}, PRICES, FRAC, {"HLAL"})
    assert "HLAL" not in by_symbol(plan)
    assert any("تبريد" in n for n in plan.notes)


def test_no_trade_when_within_drift():
    eq = 10_000
    pos = {s: Position(s, 1, eq * 0.98 * w, 0.01) for s, w in config.TARGET_WEIGHTS.items()}
    plan = build_plan(eq, eq * 0.02, pos, PRICES, FRAC, set())
    assert plan.orders == []


def test_overweight_position_is_trimmed():
    eq = 10_000
    pos = {s: Position(s, 10, eq * 0.98 * w, 0.0) for s, w in config.TARGET_WEIGHTS.items()}
    pos["SPUS"] = Position("SPUS", 100, 4_500, 0.05)  # 45% بدل 30%
    plan = build_plan(eq, 200, pos, PRICES, FRAC, set())
    spus = by_symbol(plan)["SPUS"]
    assert spus.side == "sell"


def test_whole_shares_when_not_fractionable():
    frac = {s: False for s in SYMS}
    plan = build_plan(1_000, 1_000, {}, PRICES, frac, set())
    for o in plan.orders:
        assert o.qty == int(o.qty) and o.qty >= 1
        assert o.notional is None


def test_buys_scaled_down_when_cash_short():
    plan = build_plan(1_000, 300, {}, PRICES, FRAC, set())
    total = sum(o.notional for o in plan.orders)
    assert total <= 300 - 1_000 * config.CASH_BUFFER + 0.05


def test_foreign_position_untouched_but_reported():
    pos = {"AAPL": Position("AAPL", 5, 1000, -0.5)}
    plan = build_plan(10_000, 9_000, pos, PRICES, FRAC, set())
    assert "AAPL" not in by_symbol(plan)
    assert any("AAPL" in n for n in plan.notes)


def test_weights_guard():
    with pytest.raises(ValueError):
        validate_weights({"SPUS": 0.5})
    with pytest.raises(ValueError):
        validate_weights({"SPUS": 0.3, "HLAL": 0.3, "UMMA": 0.3, "SPSK": 0.3})
    validate_weights(config.TARGET_WEIGHTS)
