"""نقطة التشغيل اليومية للوكيل.

التشغيل:  python -m agent.main            (تنفيذ فعلي على الحساب التجريبي)
          python -m agent.main --dry-run  (يحسب ويعرض فقط، بلا أوامر)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
from pathlib import Path

from . import config
from .broker import Broker
from .strategy import Position, build_plan

ROOT = Path(__file__).resolve().parent.parent
TRADES_CSV = ROOT / "trades.csv"
REPORT_MD = ROOT / "latest_report.md"


def log_trades(rows: list[dict]) -> None:
    new = not TRADES_CSV.exists()
    with TRADES_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date_utc", "symbol", "side", "notional_usd",
                                          "qty", "reason", "order_id", "status"])
        if new:
            w.writeheader()
        w.writerows(rows)


def write_report(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    REPORT_MD.write_text(text, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    print(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    b = Broker()
    lines = [f"# تقرير الوكيل — {now:%Y-%m-%d %H:%M} UTC", ""]
    if args.dry_run:
        lines.append("**وضع التجربة (Dry run): لم تُرسل أي أوامر.**\n")

    acct = b.account()
    if acct.get("trading_blocked") or acct.get("account_blocked"):
        write_report(lines + ["⛔ الحساب موقوف عن التداول من جهة الوسيط."])
        return

    equity = float(acct["equity"])
    cash = float(acct["cash"])
    symbols = list(config.TARGET_WEIGHTS)

    raw_positions = b.positions()
    positions = {
        p["symbol"]: Position(
            symbol=p["symbol"],
            qty=float(p["qty"]),
            market_value=float(p["market_value"]),
            unrealized_plpc=float(p.get("unrealized_plpc") or 0),
        )
        for p in raw_positions
    }

    clock = b.clock()
    market_open = bool(clock.get("is_open"))

    # الجدول يعمل أكثر من مرة يومياً احتياطاً لتأخر GitHub — ننفّذ مرة واحدة فقط
    if market_open and not args.dry_run and b.orders_today():
        print("الوكيل نفّذ أوامر اليوم سابقاً — لا شيء إضافي.")
        return

    prices = b.latest_prices(symbols)
    fractionable = {}
    for s in symbols:
        a = b.asset(s)
        if not a.get("tradable"):
            lines.append(f"⚠️ {s} غير قابل للتداول حالياً لدى الوسيط.")
        fractionable[s] = bool(a.get("fractionable")) and bool(a.get("tradable"))

    cooldown = b.recent_stop_loss_symbols(config.COOLDOWN_DAYS)
    plan = build_plan(equity, cash, positions, prices, fractionable, cooldown)

    # ملخص المحفظة
    lines += [
        "## المحفظة",
        f"- القيمة الإجمالية: **${equity:,.2f}**",
        f"- النقد: ${cash:,.2f}",
        "",
        "| الرمز | القيمة $ | الوزن الحالي | الهدف | ربح/خسارة |",
        "|---|---:|---:|---:|---:|",
    ]
    for s in symbols:
        p = positions.get(s)
        mv = p.market_value if p else 0.0
        pl = f"{p.unrealized_plpc:+.1%}" if p else "—"
        lines.append(f"| {s} | {mv:,.2f} | {mv / equity:.1%} | {config.TARGET_WEIGHTS[s]:.0%} | {pl} |")

    lines += ["", "## أوامر اليوم"]
    rows = []
    if not market_open and not args.dry_run:
        lines.append("السوق الأمريكي مغلق الآن — لم تُرسل أوامر.")
    else:
        # البيع أولاً لتوفير النقد
        for o in sorted(plan.orders, key=lambda o: o.side != "sell"):
            amount = f"${o.notional:,.2f}" if o.notional is not None else f"{o.qty:g} وحدة"
            if args.dry_run:
                status, oid = "dry-run", ""
            else:
                res = b.submit(o.symbol, o.side, qty=o.qty, notional=o.notional, tag=o.tag)
                status, oid = res.get("status", ""), res.get("id", "")
            side_ar = "شراء" if o.side == "buy" else "بيع"
            lines.append(f"- {side_ar} {o.symbol}: {amount} — {o.reason} → `{status}`")
            rows.append({
                "date_utc": now.isoformat(timespec="seconds"),
                "symbol": o.symbol, "side": o.side,
                "notional_usd": o.notional if o.notional is not None else "",
                "qty": o.qty if o.qty is not None else "",
                "reason": o.reason, "order_id": oid, "status": status,
            })
        if not plan.orders:
            lines.append("- لا توجد أوامر.")

    if plan.notes:
        lines += ["", "## ملاحظات"] + [f"- {n}" for n in plan.notes]

    lines += ["", "_حساب تجريبي بأموال وهمية. لا يوجد ضمان ربح. "
                  "توزيعات الصناديق قد تحتاج تطهيراً — راجع نسبة التطهير المعلنة من جهة الإصدار._"]

    if rows and not args.dry_run:
        log_trades(rows)
    write_report(lines)


if __name__ == "__main__":
    main()
