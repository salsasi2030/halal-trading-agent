"""المتداول السريع — يراقب السوق كل دقيقة طوال الجلسة.

كل دقيقة:      يفحص الصفقات المفتوحة (وقف، هدف، وقف متحرك، مدة).
كل 5 دقائق:    يبحث عن صفقات جديدة على شموع 5 دقائق (اختراق الافتتاح، ارتداد VWAP).

التشغيل:
  python -m agent.daytrade --loop          حلقة طوال الجلسة (GitHub Actions)
  python -m agent.daytrade                 تمريرة واحدة
  python -m agent.daytrade --dry-run       حساب فقط، بلا أوامر
  python -m agent.daytrade --report        تقرير كامل الآن، بلا أوامر
  python -m agent.daytrade --candidates    قائمة الأسهم لمراجعة الأخبار قبل الافتتاح
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path

import requests

from . import config, state as st
from .broker import Broker
from .daystrategy import daily_context, dt_check_exit, dt_meta, evaluate_intraday, market_ok
from .news import red_flags
from .strategy import check_exit as swing_check_exit
from .universe import is_crypto, key, load_stocks

ROOT = Path(__file__).resolve().parent.parent
TRADES_CSV = ROOT / "trades.csv"
REPORT_MD = ROOT / "latest_report.md"
CANDIDATES_JSON = ROOT / "candidates.json"
REVIEW_JSON = ROOT / "news_review.json"
FIELDS = ["date_utc", "symbol", "side", "notional_usd", "qty", "reason", "order_id", "status"]


def ny_now(now: dt.datetime) -> dt.datetime:
    return now.astimezone(st.NY)


def session_start_utc(now: dt.datetime) -> str:
    n = ny_now(now)
    return n.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(
        dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def completed_bars(series: list[tuple[str, object]], now: dt.datetime) -> list:
    """شموع 5 دقائق المكتملة فقط (بدأت قبل 5 دقائق على الأقل)."""
    cutoff = now - dt.timedelta(minutes=5)
    out = []
    for t, bar in series:
        ts = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        if ts <= cutoff:
            out.append(bar)
    return out


def load_review(today: str) -> dict:
    try:
        data = json.loads(REVIEW_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if data.get("date") == today else {}


def log_trades(rows: list[dict]) -> None:
    import csv
    new = not TRADES_CSV.exists()
    with TRADES_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


class Trader:
    def __init__(self, broker: Broker, dry: bool = False, report_only: bool = False):
        self.b = broker
        self.dry = dry
        self.report_only = report_only
        self.day = ""
        self.universe: list[str] = []
        self.daily: dict[str, dict] = {}
        self.news_checked: dict[str, list[str]] = {}
        self.notes: list[str] = []
        self.considered: list[str] = []
        self.last_scan = ""

    # ───────── تحضير اليوم (مرة واحدة) ─────────
    def prepare_day(self, today: str) -> None:
        if self.day == today:
            return
        self.day = today
        self.notes, self.considered, self.news_checked = [], [], {}
        stocks, source = load_stocks()
        ok = self.b.tradable(stocks + config.METALS)
        stocks = [s for s in stocks if ok.get(s)]
        metals = [m for m in config.METALS if ok.get(m)]
        self.universe = stocks + metals
        self.stocks = set(stocks)
        bars = self.b.daily_bars(self.universe + [config.MARKET_INDEX], days=60)
        self.daily = {}
        for s, series in bars.items():
            ctx = daily_context([b for d, b in series if d < today])
            if ctx:
                self.daily[s] = ctx
        self.review = load_review(today)
        self.blocked = dict(self.review.get("blocked", {}))
        self.notes.append(f"القائمة: {len(stocks)} سهماً متوافقاً من {source} + الذهب والفضة.")
        if self.review.get("view"):
            self.notes.append(f"تحليل الأخبار قبل الافتتاح: {self.review['view']}")
        if self.blocked:
            self.notes.append("ممنوع اليوم (أخبار/أرباح): " + "، ".join(
                f"{s} ({r})" for s, r in list(self.blocked.items())[:12]))
        if today in config.FOMC_DECISION_DATES:
            self.notes.append("اليوم قرار الفيدرالي — لا صفقات جديدة.")

    # ───────── تمريرة واحدة ─────────
    def run_once(self, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now(dt.timezone.utc)
        today = st.today_ny(now)
        hhmm = ny_now(now).strftime("%H:%M")
        market_open = bool(self.b.clock().get("is_open"))
        if not market_open and not (self.dry or self.report_only):
            return {"closed": True, "actions": []}
        can_trade = (market_open or self.dry) and not self.report_only

        acct = self.b.account()
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            return {"closed": True, "actions": ["⛔ الحساب موقوف من الوسيط."]}

        state = st.load()
        before = json.dumps(state, sort_keys=True)
        equity_acct = float(acct["equity"])
        if "anchor_equity" not in state:
            state["anchor_equity"], state["anchor_date"] = equity_acct, today
        state.setdefault("positions", {})
        if state.get("dt_day") != today:
            state["dt_day"], state["dt_new_today"] = today, 0

        positions = self.b.positions()
        invested = sum(float(p["market_value"]) for p in positions.values())
        equity = config.BUDGET_USD + (equity_acct - state["anchor_equity"])
        cash = max(0.0, min(equity - invested, float(acct["cash"])))

        actions, rows = [], []

        def record(symbol, side, reason, res, notional=None, qty=None):
            status, oid = ("dry-run", "") if self.dry else (res.get("status", ""), res.get("id", ""))
            rows.append({"date_utc": now.isoformat(timespec="seconds"), "symbol": symbol, "side": side,
                         "notional_usd": notional if notional is not None else "",
                         "qty": qty if qty is not None else "", "reason": reason,
                         "order_id": oid, "status": status})
            amount = f"${notional:,.2f}" if notional is not None else f"{qty} وحدة"
            actions.append(f"- {'🟢 شراء' if side == 'buy' else '🔴 بيع'} **{symbol}** {amount} — {reason} → `{status}`")

        # 1) الخروج — كل دقيقة
        freed = 0.0
        for k_, p in list(positions.items()):
            sym = next((c for c in config.CRYPTO if key(c) == k_), p["symbol"])
            price, mv, avg = float(p["current_price"]), float(p["market_value"]), float(p["avg_entry_price"])
            meta = state["positions"].get(k_)
            if not meta:
                ctx = self.daily.get(sym)
                meta = dt_meta(avg, ctx["atr"] if ctx else price * 0.03, "غير معروف", today)
                meta["filled"] = True
            elif not meta.get("filled"):
                if meta.get("mode") == "dt":
                    meta = {**dt_meta(avg, meta["atr"], meta["setup"], meta["entry_date"]), "filled": True}
                else:
                    meta["filled"] = True
            days = st.trading_days_between(meta["entry_date"], today)
            if meta.get("mode") == "dt":
                reason, meta = dt_check_exit(meta, price, days)
            elif config.CLOSE_OLD_SWING:   # قرار أبو خالد: تحرير المبلغ للتداول السريع
                reason = "إغلاق صفقة من الطريقة القديمة لتحرير المبلغ للتداول السريع (قرار أبو خالد)"
            else:   # صفقات المضاربة السابقة تُدار بقواعدها حتى تُغلق
                reason, meta = swing_check_exit(meta, price, days)
            if reason and can_trade:
                pnl = (price - meta["entry"]) * float(p["qty"])
                res = {} if self.dry else self.b.close(sym)
                record(sym, "sell", f"{reason} (ربح/خسارة ≈ ${pnl:+,.2f})", res, qty=p["qty"])
                freed += mv
                state["positions"].pop(k_, None)
                del positions[k_]
            else:
                state["positions"][k_] = meta
        for k_ in list(state["positions"]):
            m = state["positions"][k_]
            if k_ not in positions and (m.get("filled") or m["entry_date"] < today):
                state["positions"].pop(k_)

        # 2) الدخول — مع كل شمعة 5 دقائق جديدة
        scan_slot = ny_now(now).strftime("%H:") + f"{ny_now(now).minute // 5 * 5:02d}"
        want_scan = (self.dry or (can_trade and scan_slot != self.last_scan))
        if want_scan:
            self.last_scan = scan_slot
            self.prepare_day(today)
            slots = min(config.DT_MAX_OPEN - len(positions),
                        config.DT_MAX_NEW_PER_DAY - state["dt_new_today"])
            if (slots > 0 and config.DT_ENTRY_START <= hhmm <= config.DT_ENTRY_END
                    and today not in config.FOMC_DECISION_DATES
                    and not self.review.get("block_new_entries")) or self.dry:
                actions += self.scan_and_buy(now, hhmm, today, state, positions, equity,
                                             cash + freed, max(slots, 0), record)

        if not self.dry and json.dumps(state, sort_keys=True) != before:
            st.save(state)
        if rows and not self.dry:
            log_trades(rows)
        self.state, self.positions, self.equity, self.cash = state, positions, equity, cash + freed
        return {"closed": False, "actions": actions}

    def scan_and_buy(self, now, hhmm, today, state, positions, equity, cash, slots, record):
        start = session_start_utc(now)
        if dt.datetime.fromisoformat(start.replace("Z", "+00:00")) >= now:
            self.regime = "الجلسة لم تبدأ بعد"
            return []
        bars = self.b.intraday_bars(self.universe + [config.MARKET_INDEX], start)
        spy5 = completed_bars(bars.get(config.MARKET_INDEX, []), now)
        healthy, regime = market_ok(spy5)
        spy_ctx = self.daily.get(config.MARKET_INDEX)
        spy_chg = (spy5[-1].c / spy_ctx["prev_close"] - 1) if spy5 and spy_ctx else 0.0
        self.regime = regime

        signals = []
        for s in self.universe:
            if key(s) in positions or s in self.blocked:
                continue
            if not healthy and s in self.stocks:
                continue
            sig = evaluate_intraday(s, completed_bars(bars.get(s, []), now), self.daily.get(s), hhmm, spy_chg)
            if sig:
                signals.append(sig)
        signals.sort(key=lambda x: x.score, reverse=True)

        # فلتر الأخبار (مرة لكل سهم في اليوم)
        top = signals[:max(slots * 2, 6)]
        need = [x.symbol for x in top if x.symbol not in self.news_checked]
        if need:
            got = self.b.news(need, config.NEWS_LOOKBACK_DAYS)
            for s in need:
                self.news_checked[s] = got.get(key(s), [])
        out = []
        available = cash - equity * config.CASH_BUFFER
        bought = 0
        for sig in top:
            if bought >= slots:
                break
            flags = red_flags(self.news_checked.get(sig.symbol, []))
            if flags:
                self.considered.append(f"- ⛔ {sig.symbol}: خبر سلبي — «{flags[0][:80]}»")
                continue
            size = min(config.DT_POSITION_USD, available)
            if size < 10:
                self.notes.append("النقد المتاح لا يكفي صفقة جديدة.")
                break
            res = {} if self.dry else self.b.buy(sig.symbol, round(size, 2))
            record(sig.symbol, "buy", f"{sig.setup}: {sig.reason}", res, notional=round(size, 2))
            self.considered.append(f"- ✅ {hhmm} {sig.symbol} ({sig.setup}): {sig.reason}")
            if not self.dry and res.get("id"):
                state["positions"][key(sig.symbol)] = dt_meta(sig.price, sig.atr_d, sig.setup, today)
                state["dt_new_today"] += 1
            available -= size
            bought += 1
        return out

    # ───────── التقرير ─────────
    def report_lines(self, now: dt.datetime) -> list[str]:
        today = st.today_ny(now)
        state, positions = self.state, self.positions
        pnl = self.equity - config.BUDGET_USD
        lines = [f"# تقرير المتداول السريع — {today} ({ny_now(now):%H:%M} نيويورك)", ""]
        if self.dry:
            lines.append("**وضع التجربة: لم تُرسل أي أوامر.**\n")
        lines += [
            "## المحفظة",
            f"- رأس المال: ${config.BUDGET_USD:,.2f} ← القيمة الآن **${self.equity:,.2f}** "
            f"({pnl:+,.2f}$ / {pnl / config.BUDGET_USD:+.2%})",
            f"- النقد: ${self.cash:,.2f}",
            f"- صفقات جديدة اليوم: {state.get('dt_new_today', 0)} من {config.DT_MAX_NEW_PER_DAY}",
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
        todays = []
        if TRADES_CSV.exists():
            import csv
            with TRADES_CSV.open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if st.today_ny(dt.datetime.fromisoformat(r["date_utc"])) == today:
                        side = "🟢 شراء" if r["side"] == "buy" else "🔴 بيع"
                        amt = f"${float(r['notional_usd']):,.2f}" if r["notional_usd"] else f"{r['qty']} وحدة"
                        todays.append(f"- {side} **{r['symbol']}** {amt} — {r['reason']}")
        lines += ["", "## صفقات اليوم"] + (todays or ["- لا صفقات بعد."])
        if self.considered:
            lines += ["", "## قرارات اليوم"] + self.considered[-12:]
        notes = list(self.notes)
        if getattr(self, "regime", ""):
            notes.append(f"حالة السوق الآن: {self.regime}.")
        if notes:
            lines += ["", "## ملاحظات"] + [f"- {n}" for n in notes]
        lines += ["", "_حساب تجريبي. التداول السريع عالي المخاطر ولا يوجد ضمان ربح. الأسهم من مكوّنات "
                      "مؤشر S&P 500 الشرعي؛ أي توزيعات أرباح تُستلم تحتاج تطهيراً._"]
        return lines

    def write_report(self, now: dt.datetime) -> None:
        text = "\n".join(self.report_lines(now)) + "\n"
        REPORT_MD.write_text(text, encoding="utf-8")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "w", encoding="utf-8") as f:
                f.write(text)
        print(text, flush=True)


# ───────── الحفظ في المستودع ─────────

def git_save(message: str) -> None:
    files = [f for f in ("trades.csv", "latest_report.md", "state.json") if (ROOT / f).exists()]
    if not files:
        return
    run = lambda *a: subprocess.run(a, cwd=ROOT, capture_output=True, text=True)  # noqa: E731
    run("git", "add", *files)
    if run("git", "diff", "--cached", "--quiet").returncode == 0:
        return
    run("git", "commit", "-m", message)
    for _ in range(3):
        run("git", "pull", "--rebase", "-q")
        if run("git", "push", "-q").returncode == 0:
            return
        time.sleep(5)


def redispatch() -> None:
    """حد GitHub ست ساعات للتشغيل: نشغّل تشغيلاً جديداً يكمل الجلسة."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/trade.yml/dispatches",
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                  json={"ref": "main"}, timeout=30)


def candidates(b: Broker, now: dt.datetime) -> None:
    today = st.today_ny(now)
    stocks, source = load_stocks()
    bars = b.daily_bars(stocks, days=40)
    activity = []
    for s, series in bars.items():
        ctx = daily_context([x for d, x in series if d < today])
        if ctx:
            last = [x for d, x in series if d < today][-1]
            activity.append((last.c * last.v * ctx["atr"] / last.c, s))
    activity.sort(reverse=True)
    CANDIDATES_JSON.write_text(json.dumps({
        "date": today, "mode": "daytrade", "source": source,
        "universe": stocks, "most_active": [s for _, s in activity[:30]],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"candidates.json: {len(stocks)} سهماً، والأنشط {len(activity[:30])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--candidates", action="store_true")
    args = ap.parse_args()

    b = Broker()
    if args.candidates:
        candidates(b, dt.datetime.now(dt.timezone.utc))
        return

    t = Trader(b, dry=args.dry_run, report_only=args.report)
    if not args.loop:
        now = dt.datetime.now(dt.timezone.utc)
        res = t.run_once(now)
        if res["closed"]:
            print("السوق مغلق — لا شيء.")
            return
        t.write_report(now)
        return

    started = time.monotonic()
    last_save = 0.0
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        try:
            res = t.run_once(now)
        except Exception as e:  # noqa: BLE001 — خطأ عابر في الشبكة لا يوقف الجلسة
            print(f"خطأ عابر: {type(e).__name__}: {e}", flush=True)
            time.sleep(config.DT_LOOP_SECONDS)
            continue
        if res["closed"]:
            # قبل الافتتاح بأقل من 45 دقيقة: ننتظر بدل الخروج
            nxt = b.clock().get("next_open", "")
            try:
                wait = (dt.datetime.fromisoformat(nxt.replace("Z", "+00:00")) - now).total_seconds()
            except ValueError:
                wait = 1e9
            if 0 < wait < 45 * 60 and time.monotonic() - started < config.DT_LOOP_MAX_MINUTES * 60:
                time.sleep(min(wait + 5, 300))
                continue
            print("السوق مغلق — نهاية التشغيل.", flush=True)
            break
        if res["actions"] or time.monotonic() - last_save > 15 * 60:
            t.write_report(now)
            git_save(f"Trading {now:%Y-%m-%dT%H:%M}")
            last_save = time.monotonic()
        if time.monotonic() - started > config.DT_LOOP_MAX_MINUTES * 60:
            t.write_report(now)
            git_save(f"Trading {now:%Y-%m-%dT%H:%M}")
            redispatch()
            print("انتهت مدة التشغيل — بدأ تشغيل جديد يكمل الجلسة.", flush=True)
            return
        time.sleep(max(5, config.DT_LOOP_SECONDS - (time.monotonic() - started) % config.DT_LOOP_SECONDS))
    try:
        t.write_report(dt.datetime.now(dt.timezone.utc))
    except Exception:  # noqa: BLE001
        pass
    git_save(f"Session end {dt.datetime.now(dt.timezone.utc):%Y-%m-%d}")


if __name__ == "__main__":
    main()
