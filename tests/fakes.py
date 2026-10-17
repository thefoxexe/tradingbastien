"""Faux Bybit en mémoire pour tester client, exécution et réconciliation."""

from __future__ import annotations

from typing import Any


class FakeBybitError(Exception):
    def __init__(self, message: str, status_code: int | None) -> None:
        super().__init__(message)
        self.status_code = status_code


def ok(result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": result or {}}


INSTRUMENT = {
    "symbol": "BTCUSDT",
    "priceFilter": {"tickSize": "0.10"},
    "lotSizeFilter": {
        "qtyStep": "0.001",
        "minOrderQty": "0.001",
        "maxOrderQty": "100",
        "minNotionalValue": "5",
    },
    "leverageFilter": {"maxLeverage": "100"},
}


class FakeSession:
    """Simule une marge isolée one-way. Les stops sont posés avec l'ordre d'entrée."""

    def __init__(self, equity: float = 1000.0, mark: float = 60_000.0) -> None:
        self.equity = equity
        self.marks: dict[str, float] = {"BTCUSDT": mark, "ETHUSDT": 3000.0}
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.link_ids: set[str] = set()
        self.drop_stop = False  # simule un stop non posé par l'exchange
        self.fill_ratio = 1.0  # simule un remplissage partiel
        self.fail_next: list[Exception] = []
        self.key_info: dict[str, Any] = {
            "readOnly": 0,
            "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": [], "Spot": []},
            "ips": ["1.2.3.4"],
            "expiredAt": "2099-01-01T00:00:00Z",
        }
        self.klines: dict[str, list[list[str]]] = {}

    def _log(self, name: str, kw: dict[str, Any]) -> None:
        self.calls.append((name, kw))
        if self.fail_next:
            raise self.fail_next.pop(0)

    # -- marché / compte
    def get_server_time(self) -> dict[str, Any]:
        self._log("get_server_time", {})
        return ok({"timeNano": "1700000000000000000"})

    def get_api_key_information(self, **kw: Any) -> dict[str, Any]:
        self._log("get_api_key_information", kw)
        return ok(self.key_info)

    def get_instruments_info(self, **kw: Any) -> dict[str, Any]:
        self._log("get_instruments_info", kw)
        item = dict(INSTRUMENT, symbol=kw["symbol"])
        return ok({"list": [item]})

    def get_kline(self, **kw: Any) -> dict[str, Any]:
        self._log("get_kline", kw)
        rows = self.klines.get(kw["symbol"], [])
        start, end = kw.get("start"), kw.get("end")
        sel = [
            r
            for r in rows
            if (start is None or int(r[0]) >= start) and (end is None or int(r[0]) <= end)
        ]
        sel = sel[: kw.get("limit", 1000)]
        return ok({"list": list(reversed(sel))})

    def get_funding_rate_history(self, **kw: Any) -> dict[str, Any]:
        self._log("get_funding_rate_history", kw)
        return ok({"list": []})

    def get_tickers(self, **kw: Any) -> dict[str, Any]:
        self._log("get_tickers", kw)
        return ok({"list": [{"symbol": kw["symbol"], "markPrice": str(self.marks[kw["symbol"]])}]})

    def get_wallet_balance(self, **kw: Any) -> dict[str, Any]:
        self._log("get_wallet_balance", kw)
        return ok({"list": [{"totalEquity": str(self.equity), "coin": []}]})

    # -- positions
    def get_positions(self, **kw: Any) -> dict[str, Any]:
        self._log("get_positions", kw)
        out = []
        for sym, p in self.positions.items():
            out.append(
                {
                    "symbol": sym,
                    "side": p["side"],
                    "size": str(p["size"]),
                    "avgPrice": str(p["avgPrice"]),
                    "stopLoss": str(p.get("stopLoss") or "0"),
                    "liqPrice": str(p.get("liqPrice") or ""),
                    "markPrice": str(self.marks[sym]),
                    "unrealisedPnl": "0",
                    "leverage": "3",
                    "createdTime": "1700000000000",
                }
            )
        return ok({"list": out})

    def get_open_orders(self, **kw: Any) -> dict[str, Any]:
        self._log("get_open_orders", kw)
        return ok({"list": list(self.orders)})

    def get_executions(self, **kw: Any) -> dict[str, Any]:
        self._log("get_executions", kw)
        return ok({"list": []})

    def get_closed_pnl(self, **kw: Any) -> dict[str, Any]:
        self._log("get_closed_pnl", kw)
        return ok({"list": []})

    def place_order(self, **kw: Any) -> dict[str, Any]:
        self._log("place_order", kw)
        link = kw.get("orderLinkId")
        if link:
            if link in self.link_ids:
                raise FakeBybitError("OrderLinkedID is duplicate", 110072)
            self.link_ids.add(link)
        sym = kw["symbol"]
        qty = float(kw["qty"])
        if kw.get("reduceOnly"):
            pos = self.positions.get(sym)
            if pos:
                pos["size"] = round(pos["size"] - qty, 8)
                if pos["size"] <= 0:
                    del self.positions[sym]
            return ok({"orderId": "close"})
        mark = self.marks[sym]
        filled = round(qty * self.fill_ratio, 3)
        lev = 3
        liq = mark * (1 - 1 / lev) if kw["side"] == "Buy" else mark * (1 + 1 / lev)
        self.positions[sym] = {
            "side": kw["side"],
            "size": filled,
            "avgPrice": mark,
            "stopLoss": None if self.drop_stop else float(kw["stopLoss"]),
            "liqPrice": liq,
        }
        return ok({"orderId": "o1"})

    def cancel_all_orders(self, **kw: Any) -> dict[str, Any]:
        self._log("cancel_all_orders", kw)
        self.orders.clear()
        return ok()

    def set_trading_stop(self, **kw: Any) -> dict[str, Any]:
        self._log("set_trading_stop", kw)
        self.positions[kw["symbol"]]["stopLoss"] = float(kw["stopLoss"])
        return ok()

    def set_leverage(self, **kw: Any) -> dict[str, Any]:
        self._log("set_leverage", kw)
        raise FakeBybitError("leverage not modified", 110043)

    def switch_margin_mode(self, **kw: Any) -> dict[str, Any]:
        self._log("switch_margin_mode", kw)
        return ok()

    def switch_position_mode(self, **kw: Any) -> dict[str, Any]:
        self._log("switch_position_mode", kw)
        return ok()
