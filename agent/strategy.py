"""منطق القرار — دوال صافية بلا اتصال بالإنترنت، حتى يمكن اختبارها."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import config


@dataclass
class Position:
    symbol: str
    qty: float
    market_value: float
    unrealized_plpc: float  # نسبة الربح/الخسارة غير المحققة (مثال: -0.08 = خسارة 8%)


@dataclass
class Order:
    symbol: str
    side: str               # "buy" أو "sell"
    reason: str
    notional: float | None = None   # مبلغ بالدولار (للكسور)
    qty: float | None = None        # عدد الوحدات
    tag: str = ""                   # بادئة رقم الأمر


@dataclass
class Plan:
    orders: list[Order] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def validate_weights(weights: dict[str, float]) -> None:
    total = sum(weights.values())
    if total > 1.0 + 1e-9:
        raise ValueError(f"مجموع الأوزان {total:.2f} أكبر من 100%")
    for sym, w in weights.items():
        if w < 0 or w > config.MAX_WEIGHT + 1e-9:
            raise ValueError(f"وزن {sym} = {w:.0%} خارج الحد المسموح ({config.MAX_WEIGHT:.0%})")


def build_plan(
    equity: float,
    cash: float,
    positions: dict[str, Position],
    prices: dict[str, float],
    fractionable: dict[str, bool],
    cooldown: set[str],
    weights: dict[str, float] | None = None,
) -> Plan:
    """يحسب أوامر اليوم: وقف الخسارة أولاً، ثم إعادة التوازن نحو الأوزان المستهدفة."""
    weights = weights or config.TARGET_WEIGHTS
    validate_weights(weights)
    plan = Plan()

    if equity <= 0:
        plan.notes.append("قيمة المحفظة صفر — لا يوجد ما يُستثمر.")
        return plan

    max_order = equity * config.MAX_ORDER_PCT
    blocked = set(cooldown)
    sell_proceeds = 0.0

    # تنبيه عن أي مركز خارج القائمة البيضاء (لا نلمسه، فقط نبلّغ)
    for sym in positions:
        if sym not in weights:
            plan.notes.append(f"تنبيه: {sym} موجود في الحساب وهو خارج القائمة البيضاء — لم يُلمس.")

    # 1) وقف الخسارة
    for sym, pos in positions.items():
        if sym not in weights or pos.qty <= 0:
            continue
        if pos.unrealized_plpc <= config.STOP_LOSS_PCT:
            plan.orders.append(Order(
                symbol=sym, side="sell", qty=pos.qty, tag=config.STOP_LOSS_TAG,
                reason=f"وقف خسارة: {pos.unrealized_plpc:.1%} ≤ {config.STOP_LOSS_PCT:.0%}",
            ))
            sell_proceeds += pos.market_value
            blocked.add(sym)

    for sym in sorted(cooldown):
        if sym in weights:
            plan.notes.append(f"{sym} في فترة تبريد بعد وقف خسارة — لا شراء ({config.COOLDOWN_DAYS} يوماً).")

    # 2) إعادة التوازن
    investable = equity * (1 - config.CASH_BUFFER)
    buys: list[tuple[str, float]] = []

    for sym, w in weights.items():
        if sym in blocked:
            continue
        pos = positions.get(sym)
        current = pos.market_value if pos else 0.0
        target = investable * w
        diff = target - current
        drift = abs(current / equity - w)
        held = pos is not None and pos.qty > 0

        # أول شراء لأداة غير موجودة يتم دائماً؛ غير ذلك ننتظر حتى يتجاوز الانحراف الحد
        if held and drift < config.DRIFT_THRESHOLD:
            continue

        if diff < -config.MIN_ORDER_USD and held:
            amount = min(-diff, current, max_order)
            price = prices.get(sym) or (current / pos.qty)
            qty = amount / price
            if not fractionable.get(sym, False):
                qty = math.floor(qty)
            qty = min(qty, pos.qty)
            if qty > 0:
                plan.orders.append(Order(
                    symbol=sym, side="sell", qty=round(qty, 6),
                    reason=f"إعادة توازن: الوزن {current / equity:.0%} والهدف {w:.0%}",
                ))
                sell_proceeds += qty * price
        elif diff > config.MIN_ORDER_USD:
            buys.append((sym, min(diff, max_order)))

    # الشراء في حدود النقد المتاح (مع إبقاء الاحتياطي)
    available = cash + sell_proceeds - equity * config.CASH_BUFFER
    wanted = sum(a for _, a in buys)
    scale = 1.0 if wanted <= available else max(available, 0) / wanted if wanted else 0
    if scale < 1.0 and wanted > 0:
        plan.notes.append(f"النقد لا يكفي كل المشتريات — نُفّذ {scale:.0%} من كل أمر شراء.")

    for sym, amount in buys:
        amount = amount * scale
        if amount < config.MIN_ORDER_USD:
            continue
        if fractionable.get(sym, False):
            plan.orders.append(Order(
                symbol=sym, side="buy", notional=round(amount, 2),
                reason=f"شراء نحو الوزن المستهدف {weights[sym]:.0%}",
            ))
        else:
            price = prices.get(sym)
            if not price:
                plan.notes.append(f"{sym}: لا يوجد سعر — تخطّي الشراء.")
                continue
            qty = math.floor(amount / price)
            if qty >= 1:
                plan.orders.append(Order(
                    symbol=sym, side="buy", qty=qty,
                    reason=f"شراء نحو الوزن المستهدف {weights[sym]:.0%} (وحدات كاملة)",
                ))
            else:
                plan.notes.append(f"{sym}: المبلغ ${amount:.2f} أقل من سعر وحدة واحدة ${price:.2f}.")

    if not plan.orders:
        plan.notes.append("لا توجد أوامر اليوم — المحفظة ضمن الحدود.")
    return plan
