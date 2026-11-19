"""الاختبار على التاريخ: نفس قواعد المتداول على بيانات السنوات السابقة.

التشغيل:  python -m agent.backtest --years 4
النتيجة:  backtest_report.md (الأداء مقابل الاحتفاظ بصندوق SPUS)

الافتراضات (متحفظة):
- الإشارة تُحسب على شموع الأمس المكتملة، والدخول بسعر افتتاح اليوم.
- إذا لمس السعر الوقف والهدف في نفس اليوم نفترض الوقف أولاً.
- الفجوة تحت الوقف تُنفّذ بسعر الافتتاح (خسارة أكبر من المخطط).
- لا يشمل فلتر الأخبار ولا مراجعة الذكاء الاصطناعي (لا توجد أخبار تاريخية)، ولا رسوم.
- قائمة الأسهم هي مكوّنات SPUS الحالية (انحياز بقاء: أسهم اليوم الناجحة).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .indicators import Bar
from .strategy import evaluate, market_is_healthy, position_size

WINDOW = 260  # آخر 260 شمعة تكفي لكل المؤشرات وتسرّع الحساب


@dataclass
class Trade:
    symbol: str
    setup: str
    entry_date: str
    entry: float
    exit_date: str = ""
    exit: float = 0.0
    qty: float = 0.0
    reason: str = ""

    @property
    def pnl(self) -> float:
        return (self.exit - self.entry) * self.qty


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    curve: list[tuple[str, float]] = field(default_factory=list)


def run(bars: dict[str, list[tuple[str, Bar]]], index_sym: str, stocks: set[str],
        start: str, budget: float = config.BUDGET_USD) -> Result:
    """محرك الاختبار — دالة صافية."""
    idx = {d: i for i, (d, _) in enumerate(bars[index_sym])}
    dates = [d for d, _ in bars[index_sym] if d >= start]
    pos_of = {s: {d: i for i, (d, _) in enumerate(series)} for s, series in bars.items()}
    plain = {s: [b for _, b in series] for s, series in bars.items()}

    cash = budget
    open_: dict[str, dict] = {}
    res = Result()

    for d in dates:
        # 1) الخروج
        for s in list(open_):
            i = pos_of[s].get(d)
            if i is None:
                continue
            p, b = open_[s], plain[s][i]
            px, why = None, ""
            if b.o <= p["stop"]:
                px, why = b.o, "فجوة تحت الوقف"
            elif b.l <= p["stop"]:
                px, why = p["stop"], "وقف"
            elif b.o >= p["target"]:
                px, why = b.o, "فجوة فوق الهدف"
            elif b.h >= p["target"]:
                px, why = p["target"], "هدف"
            else:
                p["days"] += 1
                if p["days"] >= config.MAX_HOLD_DAYS:
                    px, why = b.c, "انتهاء المدة"
                elif b.h >= p["entry"] + config.BREAKEVEN_ATR * p["atr"]:
                    p["stop"] = max(p["stop"], p["entry"])
            if px is not None:
                t = p["trade"]
                t.exit, t.exit_date, t.reason = px, d, why
                cash += px * t.qty
                res.trades.append(t)
                del open_[s]

        # 2) الدخول
        slots = config.MAX_POSITIONS - len(open_)
        equity = cash + sum(o["trade"].qty * plain[s][pos_of[s][d]].o
                            for s, o in open_.items() if d in pos_of[s]) if open_ else cash
        if slots > 0:
            ii = idx[d]
            healthy, _ = market_is_healthy(plain[index_sym][max(0, ii - WINDOW):ii])
            signals = []
            for s, series in plain.items():
                if s == index_sym or s in open_ or (not healthy and s in stocks):
                    continue
                i = pos_of[s].get(d)
                if i is None or i < 201:
                    continue
                sig = evaluate(s, series[max(0, i - WINDOW):i])
                if sig:
                    signals.append((sig, series[i].o))
            signals.sort(key=lambda x: x[0].score, reverse=True)
            available = cash - equity * config.CASH_BUFFER
            for sig, open_px in signals[:slots]:
                size = position_size(equity, available, open_px, sig.atr)
                if size < 10:
                    continue
                qty = size / open_px
                cash -= size
                available -= size
                open_[sig.symbol] = {
                    "trade": Trade(sig.symbol, sig.setup, d, open_px, qty=qty),
                    "entry": open_px, "atr": sig.atr, "days": 0,
                    "stop": open_px - config.STOP_ATR * sig.atr,
                    "target": open_px + config.TARGET_ATR * sig.atr,
                }

        # 3) قيمة المحفظة بسعر الإغلاق
        mtm = cash
        for s, o in open_.items():
            i = pos_of[s].get(d)
            last = plain[s][i].c if i is not None else o["entry"]
            mtm += o["trade"].qty * last
        res.curve.append((d, mtm))
    return res


def max_drawdown(values: list[float]) -> float:
    peak, worst = values[0] if values else 0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak:
            worst = min(worst, v / peak - 1)
    return worst


def stats(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_w = sum(t.pnl for t in wins)
    gross_l = -sum(t.pnl for t in losses)
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_win": gross_w / len(wins) if wins else 0,
        "avg_loss": -gross_l / len(losses) if losses else 0,
        "profit_factor": gross_w / gross_l if gross_l else float("inf"),
        "pnl": sum(t.pnl for t in trades),
        "avg_days": 0,
    }


def report(res: Result, bench: list[tuple[str, float]], budget: float, years: float,
           stocks: set[str]) -> list[str]:
    vals = [v for _, v in res.curve]
    final = vals[-1] if vals else budget
    total = final / budget - 1
    cagr = (final / budget) ** (1 / years) - 1 if years > 0 and final > 0 else 0
    b0, b1 = (bench[0][1], bench[-1][1]) if bench else (1, 1)
    b_total = b1 / b0 - 1
    b_cagr = (b1 / b0) ** (1 / years) - 1 if years > 0 else 0
    b_dd = max_drawdown([v for _, v in bench])

    def row(name, ts):
        st = stats(ts)
        if not st["n"]:
            return f"| {name} | 0 | — | — | — | — | — |"
        pf = "∞" if st["profit_factor"] == float("inf") else f"{st['profit_factor']:.2f}"
        return (f"| {name} | {st['n']} | {st['win_rate']:.0%} | ${st['avg_win']:.2f} | "
                f"${st['avg_loss']:.2f} | {pf} | ${st['pnl']:+,.2f} |")

    crypto = {c for c in config.CRYPTO}
    metals = set(config.METALS)
    reasons: dict[str, int] = {}
    for t in res.trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    period = f"{res.curve[0][0]} → {res.curve[-1][0]}" if res.curve else "—"

    lines = [
        "# الاختبار على التاريخ",
        "",
        f"الفترة: {period} (~{years:.1f} سنة) · رأس المال ${budget:,.0f} · القواعد الحالية في `agent/config.py`",
        "",
        "## النتيجة مقابل الاحتفاظ بصندوق SPUS",
        "",
        "| | المتداول | SPUS (شراء واحتفاظ) |",
        "|---|---:|---:|",
        f"| العائد الكلي | {total:+.1%} | {b_total:+.1%} |",
        f"| العائد السنوي | {cagr:+.1%} | {b_cagr:+.1%} |",
        f"| أسوأ هبوط من القمة | {max_drawdown(vals):.1%} | {b_dd:.1%} |",
        f"| القيمة النهائية | ${final:,.2f} | ${budget * (1 + b_total):,.2f} |",
        "",
        "## الصفقات",
        "",
        "| الفئة | العدد | نسبة الربح | متوسط الربح | متوسط الخسارة | معامل الربح | الصافي |",
        "|---|---:|---:|---:|---:|---:|---:|",
        row("الكل", res.trades),
        row("ارتداد", [t for t in res.trades if t.setup == "ارتداد"]),
        row("اختراق", [t for t in res.trades if t.setup == "اختراق"]),
        row("أسهم", [t for t in res.trades if t.symbol in stocks]),
        row("ذهب وفضة", [t for t in res.trades if t.symbol in metals]),
        row("كريبتو", [t for t in res.trades if t.symbol in crypto]),
        "",
        "## أسباب الخروج",
        "",
    ] + [f"- {k}: {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])] + [
        "",
        "## حدود هذا الاختبار",
        "",
        "- لا يشمل فلتر الأخبار ومراجعة الذكاء الاصطناعي ولا الرسوم ولا انزلاق الأسعار.",
        "- قائمة الأسهم هي مكوّنات SPUS الحالية، فالنتيجة متفائلة قليلاً (أسهم نجحت حتى اليوم).",
        "- بيانات IEX اليومية قد تختلف قليلاً عن أسعار السوق المجمّعة.",
        "- الأداء الماضي لا يضمن المستقبل.",
    ]
    return lines


VARIANTS: list[tuple[str, dict]] = [
    ("القواعد الحالية", {}),
    ("هدف أقرب 1.5×ATR", {"TARGET_ATR": 1.5}),
    ("الهدف القديم 3×ATR", {"TARGET_ATR": 3.0}),
    ("وقف أضيق 1.5×ATR", {"STOP_ATR": 1.5}),
    ("وقف أوسع 2.5×ATR", {"STOP_ATR": 2.5}),
    ("مدة أقصر 5 أيام", {"MAX_HOLD_DAYS": 5}),
]


def run_variants(bars, index_sym, stocks, start, years, bench) -> list[str]:
    """يشغّل كل نسخة من القواعد على نفس البيانات ويقارن بينها."""
    rows = []
    for name, overrides in VARIANTS:
        saved = {k: getattr(config, k) for k in overrides}
        try:
            for k, v in overrides.items():
                setattr(config, k, v)
            res = run(bars, index_sym, stocks, start)
        finally:
            for k, v in saved.items():
                setattr(config, k, v)
        vals = [v for _, v in res.curve]
        final = vals[-1] if vals else config.BUDGET_USD
        cagr = (final / config.BUDGET_USD) ** (1 / years) - 1 if years > 0 and final > 0 else 0
        st = stats(res.trades)
        pf = st.get("profit_factor", 0)
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        rows.append((cagr, f"| {name} | {cagr:+.1%} | ${final:,.0f} | {max_drawdown(vals):.1%} | "
                           f"{st['n']} | {st.get('win_rate', 0):.0%} | {pf_s} |"))
        print(f"done: {name}", flush=True)
    b_cagr = (bench[-1][1] / bench[0][1]) ** (1 / years) - 1 if bench and years > 0 else 0
    best = max(rows, key=lambda r: r[0])
    return [
        "# مقارنة نسخ القواعد على التاريخ",
        "",
        f"نفس البيانات ونفس رأس المال (${config.BUDGET_USD:,.0f})، الفترة ~{years:.1f} سنة. "
        f"للمرجع فقط: الاحتفاظ بأسهم نفس القائمة (SPUS) أعطى {b_cagr:+.1%} سنوياً.",
        "",
        "| النسخة | العائد السنوي | القيمة النهائية | أسوأ هبوط | الصفقات | نسبة الربح | معامل الربح |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ] + [r for _, r in rows] + [
        "",
        f"**الأفضل عائداً:** {best[1].split('|')[1].strip()}",
        "",
        "تنبيه: أفضل نسخة على الماضي ليست بالضرورة الأفضل مستقبلاً. لا يتغير شيء في القواعد إلا بموافقة أبو خالد.",
    ]


def main() -> None:
    import datetime as dt

    from .broker import Broker
    from .universe import load_stocks

    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=4)
    ap.add_argument("--variants", action="store_true", help="مقارنة عدة نسخ من القواعد")
    args = ap.parse_args()

    b = Broker()
    stocks, _ = load_stocks()
    ok = b.tradable(stocks + config.METALS)
    stocks = [s for s in stocks if ok.get(s)]
    universe = stocks + [m for m in config.METALS if ok.get(m)] + config.CRYPTO
    days = int(args.years * 365) + 400
    bars = b.daily_bars(universe + [config.MARKET_INDEX, "SPUS"], days=days)
    today = dt.date.today().isoformat()
    bars = {s: [(d, x) for d, x in v if d < today] for s, v in bars.items() if v}

    spus = bars.pop("SPUS", [])
    index_dates = [d for d, _ in bars[config.MARKET_INDEX]]
    start = index_dates[min(len(index_dates) - 1, 210)]   # فترة تسخين للمؤشرات
    years = (dt.date.fromisoformat(index_dates[-1]) - dt.date.fromisoformat(start)).days / 365.25

    bench = [(d, x.c) for d, x in spus if d >= start]
    if args.variants:
        lines = run_variants(bars, config.MARKET_INDEX, set(stocks), start, years, bench)
        Path(__file__).resolve().parent.parent.joinpath("variants_report.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        return
    res = run(bars, config.MARKET_INDEX, set(stocks), start)
    lines = report(res, bench, config.BUDGET_USD, years, set(stocks))
    Path(__file__).resolve().parent.parent.joinpath("backtest_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
