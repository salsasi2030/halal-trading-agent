"""الاتصال بـ Alpaca عبر واجهتها البرمجية (REST)."""

from __future__ import annotations

import datetime as dt
import os
import uuid

import requests

from . import config


class Broker:
    def __init__(self) -> None:
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not key or not secret:
            raise SystemExit("المفاتيح غير موجودة: أضف ALPACA_API_KEY و ALPACA_SECRET_KEY في GitHub Secrets.")
        if not config.TRADING_BASE_URL.startswith("https://paper-api."):
            raise SystemExit("حماية: الوكيل مضبوط للحساب التجريبي فقط.")
        self.s = requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def _get(self, base: str, path: str, **params):
        r = self.s.get(f"{base}{path}", params=params, timeout=30)
        if r.status_code == 401 or r.status_code == 403:
            raise SystemExit(f"رفض الدخول ({r.status_code}): تأكد أن المفتاحين من نفس المرة ومن الحساب التجريبي.")
        r.raise_for_status()
        return r.json()

    def clock(self) -> dict:
        return self._get(config.TRADING_BASE_URL, "/v2/clock")

    def account(self) -> dict:
        return self._get(config.TRADING_BASE_URL, "/v2/account")

    def positions(self) -> list[dict]:
        return self._get(config.TRADING_BASE_URL, "/v2/positions")

    def asset(self, symbol: str) -> dict:
        return self._get(config.TRADING_BASE_URL, f"/v2/assets/{symbol}")

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        data = self._get(config.DATA_BASE_URL, "/v2/stocks/trades/latest",
                         symbols=",".join(symbols), feed="iex")
        return {s: float(t["p"]) for s, t in (data.get("trades") or {}).items()}

    def recent_stop_loss_symbols(self, days: int) -> set[str]:
        after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        orders = self._get(config.TRADING_BASE_URL, "/v2/orders",
                           status="closed", after=after, limit=500, direction="desc")
        return {
            o["symbol"] for o in orders
            if o.get("side") == "sell"
            and (o.get("client_order_id") or "").startswith(config.STOP_LOSS_TAG)
            and o.get("status") == "filled"
        }

    def orders_today(self) -> list[dict]:
        """أوامر الوكيل منذ بداية اليوم (بتوقيت نيويورك تقريباً = 05:00 UTC)."""
        now = dt.datetime.now(dt.timezone.utc)
        start = now.replace(hour=5, minute=0, second=0, microsecond=0)
        if now < start:
            start -= dt.timedelta(days=1)
        orders = self._get(config.TRADING_BASE_URL, "/v2/orders",
                           status="all", after=start.isoformat(), limit=500)
        return [o for o in orders
                if o.get("symbol") in config.TARGET_WEIGHTS
                and o.get("status") not in ("canceled", "rejected", "expired")]

    def submit(self, symbol: str, side: str, *, qty: float | None = None,
               notional: float | None = None, tag: str = "") -> dict:
        if symbol not in config.TARGET_WEIGHTS:
            raise SystemExit(f"حماية: {symbol} خارج القائمة البيضاء — رُفض الأمر.")
        body = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": f"{tag}{uuid.uuid4().hex[:20]}",
        }
        if notional is not None:
            body["notional"] = f"{notional:.2f}"
        else:
            body["qty"] = f"{qty:g}"
        r = self.s.post(f"{config.TRADING_BASE_URL}/v2/orders", json=body, timeout=30)
        if not r.ok:
            return {"id": "", "status": f"rejected: {r.status_code} {r.text[:200]}"}
        return r.json()
