"""فلترة الأخبار + تحليل اختياري بالذكاء الاصطناعي للأخبار والاقتصاد."""

from __future__ import annotations

import json
import os
import re

import requests

from . import config


def red_flags(headlines: list[str]) -> list[str]:
    hits = []
    for h in headlines:
        low = h.lower()
        for word in config.NEWS_RED_FLAGS:
            if word in low:
                hits.append(h)
                break
    return hits


def ai_review(candidates: list[dict], market_context: str) -> tuple[dict[str, tuple[bool, str]], str]:
    """يرسل المرشحين وأخبارهم إلى Claude ليوافق أو يرفض. يرجع ({الرمز: (موافق، السبب)}، رأي السوق).

    إذا لم يوجد ANTHROPIC_API_KEY يرجع فارغاً ويعتمد الوكيل على القواعد فقط.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or not candidates:
        return {}, ""
    prompt = (
        "You are a cautious swing-trading risk reviewer (holding period 1-7 trading days, long only, "
        "no leverage). For each candidate decide APPROVE or REJECT based on the recent headlines, "
        "upcoming macro risk (Fed, CPI/jobs data, tariffs, geopolitics, supply-chain/logistics issues), "
        "and earnings risk within the holding window. Reject when the news flow is clearly negative or a "
        "binary event (earnings, FDA decision, court ruling) is likely within 7 days. "
        "Answer ONLY with JSON: {\"market_view\": \"<one sentence in Arabic>\", "
        "\"decisions\": [{\"symbol\": \"...\", \"approve\": true|false, \"reason\": \"<short Arabic>\"}]}\n\n"
        f"Market context: {market_context}\n\nCandidates:\n{json.dumps(candidates, ensure_ascii=False)}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": config.AI_MODEL, "max_tokens": 1500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        out = {d["symbol"]: (bool(d.get("approve")), d.get("reason", ""))
               for d in data.get("decisions", []) if "symbol" in d}
        return out, data.get("market_view", "")
    except Exception as e:  # noqa: BLE001
        return {}, f"تعذّر تحليل الذكاء الاصطناعي ({type(e).__name__}) — اعتمدنا على القواعد فقط."
