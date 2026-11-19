"""نقطة تشغيل المتداول — يعمل كل ساعة والسوق الأمريكي مفتوح.

كل ساعة:   يفحص الصفقات المفتوحة (وقف الخسارة، الهدف، انتهاء المدة).
مرة يومياً: يبحث عن صفقات جديدة (ارتداد أو اختراق) في الأسهم المتوافقة والذهب والفضة والكريبتو.

التشغيل:  python -m agent.main            (تنفيذ على الحساب التجريبي)
          python -m agent.main --dry-run  (يحسب ويعرض فقط، بلا أوامر)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path

from . import config, state as st
from .broker import Broker
from .news import ai_review, red_flags
from .strategy import check_exit, evaluate, market_is_healthy, new_meta, pick_entries
from .universe import is_crypto, key, load_stocks

ROOT = Path(__file__).resolve().parent.parent
TRADES_CSV = ROOT / "trades.csv"
REPORT_MD = ROOT / "latest_report.md"
FIELDS = ["date_utc", "symbol", "side", "notional_usd", "qty", "reason", "order_id", "status"]


def log_trades(rows: list[dict]) -> None:
    new = not TRADES_CSV.exists()
    with TRADES_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def write_report(lines: list[str], to_file: bool) -> None:
    text = "\n".join(lines) + "\n"
    if to_file:
        REPORT_MD.write_text(text, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text)
    print(text)


def crypto_name(k: str, fallback: str) -> str:
    return next((c for c in config.CRYPTO if key(c) == k), fallback)


def main() -> None:  # noqa: C901 — تسلسل واضح خطوة بخطوة
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    dry = ap.parse_args().dry_run

    now = dt.datetime.now(dt.timezone.utc)
    today = st.today_ny(now)
    b = Broker()

    if not b.clock().get("is_open") and not dry:
        print("السوق الأمريكي مغلق الآن — لا شيء.")
        return

    acct = b.account()
    if acct.get("trading_blocked") or acct.get("account_blocked"):
        write_report([f"# تقرير المتداول — {today}", "⛔ الحساب موقوف من الوسيط."], True)
        return

    state = st.load()
    before = json.dumps(state, sort_keys=True)
    equity_acct = float(acct["equity"])
    if "anchor_equity" not in state:
        state["anchor_equity"] = equity_acct     # نقطة البداية: هنا رأس المال = BUDGET_USD
        state["anchor_date"] = today
    state.setdefault("positions", {})

    positions = b.positions()
    invested = sum(float(p["market_value"]) for p in positions.values())
    equity = config.BUDGET_USD + (equity_acct - state["anchor_equity"])
    cash = max(0.0, min(equity - invested, float(acct["cash"])))

    actions: list[str] = []
    rows: list[dict] = []

    def record(symbol, side, reason, res, notional=None, qty=None):
        status, oid = ("dry-run", "") if dry else (res.get("status", ""), res.get("id", ""))
        rows.append({"date_utc": now.isoformat(timespec="seconds"), "symbol": symbol, "side": side,
                     "notional_usd": notional if notional is not None else "",
                     "qty": qty if qty is not None else "", "reason": reason,
                     "order_id": oid, "status": status})
        side_ar = "🟢 شراء" if side == "buy" else "🔴 بيع"
        amount = f"${notional:,.2f}" if notional is not None else f"{qty} وحدة"
        actions.append(f"- {side_ar} **{symbol}** {amount} — {reason} → `{status}`")

    # ───────── 1) الخروج (كل ساعة) ─────────
    freed = 0.0
    for k_, p in list(positions.items()):
        sym = crypto_name(k_, p["symbol"])
        price = float(p["current_price"])
        mv = float(p["market_value"])
        if k_ in config.LEGACY:
            res = {} if dry else b.close(sym)
            record(sym, "sell", "تحويل للاستراتيجية الجديدة (بيع الصناديق القديمة)", res, qty=p["qty"])
            freed += mv
            del positions[k_]
            continue
        avg = float(p["avg_entry_price"])
        meta = state["positions"].get(k_)
        if not meta:
            meta = new_meta(avg, max(price * 0.03, 0.01), "غير معروف", today)  # مركز بلا سجل: حركة 3%
            meta["filled"] = True
        elif not meta.get("filled"):
            # أول مرة بعد التنفيذ: نعيد حساب الوقف والهدف على سعر التنفيذ الفعلي
            meta = {**new_meta(avg, meta["atr"], meta["setup"], meta["entry_date"]), "filled": True}
        reason, meta = check_exit(meta, price, st.trading_days_between(meta["entry_date"], today))
        if reason:
            pnl = (price - meta["entry"]) * float(p["qty"])
            res = {} if dry else b.close(sym)
            record(sym, "sell", f"{reason} (ربح/خسارة ≈ ${pnl:+,.2f})", res, qty=p["qty"])
            freed += mv
            state["positions"].pop(k_, None)
            del positions[k_]
        else:
            state["positions"][k_] = meta

    # تنظيف: صفقات في السجل لم تعد في الحساب (بيعت أو لم تُنفّذ) — إلا ما اشتُري للتو
    for k_ in list(state["positions"]):
        m = state["positions"][k_]
        if k_ not in positions and (m.get("filled") or m["entry_date"] < today):
            state["positions"].pop(k_)

    # ───────── 2) الدخول (مرة يومياً) ─────────
    notes: list[str] = []
    considered: list[str] = []
    entry_run = dry or state.get("last_entry_day") != today
    if entry_run:
        if today in config.FOMC_DECISION_DATES:
            notes.append("اليوم قرار الفيدرالي للفائدة — لا صفقات جديدة.")
        else:
            stocks, source = load_stocks()
            ok = b.tradable(stocks + config.METALS)
            stocks = [s for s in stocks if ok.get(s)]
            notes.append(f"قائمة الأسهم: {len(stocks)} سهماً متوافقاً من {source}.")
            universe = stocks + [m for m in config.METALS if ok.get(m)] + config.CRYPTO
            bars = b.daily_bars(universe + [config.MARKET_INDEX])

            def completed(sym):  # نستبعد شمعة اليوم غير المكتملة
                return [bar for d, bar in bars.get(sym, []) if d < today]

            healthy, regime = market_is_healthy(completed(config.MARKET_INDEX))
            notes.append(f"حالة السوق: {regime}.")
            if not healthy:
                notes.append("السوق ضعيف — لا أسهم جديدة اليوم (الذهب والفضة والكريبتو مسموحة).")

            signals = [sig for s in universe
                       if (healthy or s not in stocks) and (sig := evaluate(s, completed(s)))]
            signals.sort(key=lambda s: s.score, reverse=True)
            top = signals[:15]

            headlines = b.news([s.symbol for s in top], config.NEWS_LOOKBACK_DAYS) if top else {}
            clean = []
            for s in top:
                hs = headlines.get(key(s.symbol), [])
                flagged = red_flags(hs)
                if flagged:
                    considered.append(f"- ⛔ {s.symbol} ({s.setup}): خبر سلبي — «{flagged[0][:90]}»")
                else:
                    clean.append((s, hs))

            verdicts, view = ai_review(
                [{"symbol": s.symbol, "setup": s.setup, "why": s.reason, "headlines": hs[:5]}
                 for s, hs in clean[:config.AI_MAX_CANDIDATES]], regime)
            if view:
                notes.append(f"تحليل الأخبار والاقتصاد (ذكاء اصطناعي): {view}")
            approved = []
            for s, _ in clean:
                ok_ai, why = verdicts.get(s.symbol, (True, ""))
                if ok_ai:
                    approved.append(s)
                    considered.append(f"- ✅ {s.symbol} ({s.setup}): {s.reason}" + (f" — {why}" if why else ""))
                else:
                    considered.append(f"- ⛔ {s.symbol} ({s.setup}): رفضه تحليل الأخبار — {why}")

            prices = b.latest_prices([s.symbol for s in approved]) if approved else {}
            for s in approved:
                s.price = prices.get(s.symbol, s.price)
            slots = config.MAX_POSITIONS - len(positions)
            for s, size in pick_entries(approved, slots, equity, cash + freed,
                                        {k for k in positions}):
                res = {} if dry else b.buy(s.symbol, size)
                record(s.symbol, "buy", f"{s.setup}: {s.reason}", res, notional=size)
                if not dry and res.get("id"):
                    state["positions"][key(s.symbol)] = new_meta(s.price, s.atr, s.setup, today)
            if not signals:
                notes.append("لا توجد إشارات دخول اليوم — الانتظار أفضل من صفقة ضعيفة.")
        if not dry:
            state["last_entry_day"] = today

    # ───────── 3) الحفظ والتقرير ─────────
    if not dry and json.dumps(state, sort_keys=True) != before:
        st.save(state)
    if rows and not dry:
        log_trades(rows)
    if not actions and not entry_run:
        print("فحص الساعة: لا خروج مطلوب.")
        return

    pnl = equity - config.BUDGET_USD
    lines = [f"# تقرير المتداول — {today} ({now:%H:%M} UTC)", ""]
    if dry:
        lines.append("**وضع التجربة: لم تُرسل أي أوامر.**\n")
    lines += [
        "## المحفظة",
        f"- رأس المال: ${config.BUDGET_USD:,.2f} ← القيمة الآن **${equity:,.2f}** "
        f"({pnl:+,.2f}$ / {pnl / config.BUDGET_USD:+.2%})",
        f"- النقد: ${cash + freed:,.2f}",
        "",
        "| الرمز | النوع | الدخول | الآن | الوقف | الهدف | أيام | ربح/خسارة |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    shown = 0
    for k_, p in positions.items():
        m = state["positions"].get(k_)
        if m:
            shown += 1
            lines.append(
                f"| {p['symbol']} | {m['setup']} | {m['entry']:.2f} | {float(p['current_price']):.2f} | "
                f"{m['stop']:.2f} | {m['target']:.2f} | {st.trading_days_between(m['entry_date'], today)} | "
                f"{float(p.get('unrealized_plpc') or 0):+.1%} |")
    if not shown:
        lines.append("| — | لا صفقات مفتوحة | | | | | | |")
    lines += ["", "## الأوامر"] + (actions or ["- لا أوامر."])
    if considered:
        lines += ["", "## المرشحون اليوم"] + considered[:10]
    if notes:
        lines += ["", "## ملاحظات"] + [f"- {n}" for n in notes]
    lines += ["", "_حساب تجريبي. المضاربة عالية المخاطر ولا يوجد ضمان ربح. الأسهم من مكوّنات مؤشر "
                  "S&P 500 الشرعي؛ أي توزيعات أرباح تُستلم تحتاج تطهيراً حسب نسبة الشركة._"]
    write_report(lines, to_file=True)


if __name__ == "__main__":
    main()
