"""الاتصال بـ Alpaca عبر واجهتها البرمجية (REST)."""

from __future__ import annotations

import datetime as dt
import os
import uuid

import requests

from . import config
from .indicators import Bar
from .universe import is_crypto, key


class Broker:
    def __init__(self) -> None:
        api_key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not api_key or not secret:
            raise SystemExit("المفاتيح غير موجودة: أضف ALPACA_API_KEY و ALPACA_SECRET_KEY في GitHub Secrets.")
        if not config.TRADING_BASE_URL.startswith("https://paper-api."):
            raise SystemExit("حماية: الوكيل مضبوط للحساب التجريبي فقط.")
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret})

    def _get(self, base: str, path: str, **params):
        r = self.s.get(f"{base}{path}", params=params, timeout=30)
        if r.status_code in (401, 403):
            raise SystemExit(f"رفض الدخول ({r.status_code}): تأكد أن المفتاحين من نفس المرة ومن الحساب التجريبي.")
        r.raise_for_status()
        return r.json()

    # ───────── الحساب ─────────
    def clock(self) -> dict:
        return self._get(config.TRADING_BASE_URL, "/v2/clock")

    def account(self) -> dict:
        return self._get(config.TRADING_BASE_URL, "/v2/account")

    def positions(self) -> dict[str, dict]:
        return {key(p["symbol"]): p for p in self._get(config.TRADING_BASE_URL, "/v2/positions")}

    def tradable(self, symbols: list[str]) -> dict[str, bool]:
        """يرجع {الرمز: قابل للكسور والتداول}."""
        assets = self._get(config.TRADING_BASE_URL, "/v2/assets", status="active", asset_class="us_equity")
        ok = {a["symbol"]: bool(a.get("tradable")) and bool(a.get("fractionable")) for a in assets}
        return {s: ok.get(s, False) for s in symbols}

    # ───────── البيانات ─────────
    def daily_bars(self, symbols: list[str], days: int = 330) -> dict[str, list[tuple[str, Bar]]]:
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        out: dict[str, list[tuple[str, Bar]]] = {}
        stocks = [s for s in symbols if not is_crypto(s)]
        crypto = [s for s in symbols if is_crypto(s)]
        for i in range(0, len(stocks), 100):
            self._collect(out, "/v2/stocks/bars", stocks[i:i + 100], start,
                          feed="iex", adjustment="all")
        if crypto:
            self._collect(out, "/v1beta3/crypto/us/bars", crypto, start)
        return out

    def _collect(self, out, path, symbols, start, **extra):
        token = None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                      "limit": 10000, **extra}
            if token:
                params["page_token"] = token
            data = self._get(config.DATA_BASE_URL, path, **params)
            for sym, bars in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend(
                    (b["t"][:10], Bar(b["o"], b["h"], b["l"], b["c"], b.get("v", 0))) for b in bars)
            token = data.get("next_page_token")
            if not token:
                break

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        stocks = [s for s in symbols if not is_crypto(s)]
        crypto = [s for s in symbols if is_crypto(s)]
        for i in range(0, len(stocks), 100):
            data = self._get(config.DATA_BASE_URL, "/v2/stocks/trades/latest",
                             symbols=",".join(stocks[i:i + 100]), feed="iex")
            out.update({s: float(t["p"]) for s, t in (data.get("trades") or {}).items()})
        if crypto:
            data = self._get(config.DATA_BASE_URL, "/v1beta3/crypto/us/latest/trades",
                             symbols=",".join(crypto))
            out.update({s: float(t["p"]) for s, t in (data.get("trades") or {}).items()})
        return out

    def news(self, symbols: list[str], days: int) -> dict[str, list[str]]:
        start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        names = [s.replace("/", "") if is_crypto(s) else s for s in symbols]
        try:
            data = self._get(config.DATA_BASE_URL, "/v1beta1/news",
                             symbols=",".join(names), start=start, limit=50)
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, list[str]] = {}
        for n in data.get("news", []):
            for s in n.get("symbols", []):
                out.setdefault(s, []).append(n.get("headline", ""))
        return out

    # ───────── الأوامر ─────────
    def buy(self, symbol: str, notional: float) -> dict:
        body = {
            "symbol": symbol, "side": "buy", "type": "market",
            "time_in_force": "gtc" if is_crypto(symbol) else "day",
            "notional": f"{notional:.2f}",
            "client_order_id": f"sw-{uuid.uuid4().hex[:20]}",
        }
        r = self.s.post(f"{config.TRADING_BASE_URL}/v2/orders", json=body, timeout=30)
        if not r.ok:
            return {"id": "", "status": f"rejected: {r.status_code} {r.text[:200]}"}
        return r.json()

    def close(self, symbol: str) -> dict:
        """بيع المركز كاملاً."""
        r = self.s.delete(f"{config.TRADING_BASE_URL}/v2/positions/{key(symbol)}", timeout=30)
        if not r.ok:
            return {"id": "", "status": f"rejected: {r.status_code} {r.text[:200]}"}
        return r.json()
