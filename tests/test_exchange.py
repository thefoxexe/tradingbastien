from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bot.config import Mode, Settings
from bot.exchange.client import BybitClient, ExchangeError, make_session
from bot.exchange.instruments import DEFAULT_RULES, InstrumentRules
from bot.exchange.permissions import ForbiddenPermissionError, check_api_key
from bot.exchange.stream import MarketStream
from bot.models import Side
from tests.fakes import INSTRUMENT, FakeBybitError, FakeSession


def client(session: FakeSession) -> BybitClient:
    return BybitClient(session, sleep=lambda _: None)


# ---------------------------------------------------------------- permissions
GOOD = {
    "readOnly": 0,
    "permissions": {"ContractTrade": ["Order", "Position"], "Spot": [], "Wallet": []},
    "ips": ["1.2.3.4"],
    "expiredAt": "2099-01-01T00:00:00Z",
}


def test_key_ok() -> None:
    res = check_api_key(GOOD, require_ip=True)
    assert res.ip_bound and not res.warnings


@pytest.mark.parametrize(
    "perms",
    [
        {"Wallet": ["Withdraw"]},
        {"Wallet": ["AccountTransfer"]},
        {"Wallet": ["SubMemberTransfer"]},
        {"Spot": ["SpotTrade"]},
        {"ContractTrade": ["Order", "Position", "Something"]},
        {"Options": ["OptionsTrade"]},
    ],
)
def test_key_forbidden_permissions(perms: dict[str, list[str]]) -> None:
    info = {**GOOD, "permissions": {**GOOD["permissions"], **perms}}  # type: ignore[dict-item]
    with pytest.raises(ForbiddenPermissionError):
        check_api_key(info, require_ip=False)


def test_key_read_only_refused() -> None:
    with pytest.raises(ForbiddenPermissionError, match="lecture seule"):
        check_api_key({**GOOD, "readOnly": 1}, require_ip=False)


def test_live_key_requires_ip() -> None:
    info = {**GOOD, "ips": ["*"]}
    with pytest.raises(ForbiddenPermissionError, match="IP"):
        check_api_key(info, require_ip=True)
    assert not check_api_key(info, require_ip=False).ip_bound


def test_key_expiry_warning() -> None:
    now = datetime(2098, 12, 25, tzinfo=UTC)
    assert check_api_key(GOOD, require_ip=True, now=now).warnings
    assert check_api_key({**GOOD, "expiredAt": "garbage"}, require_ip=True).expires_at is None
    assert check_api_key({**GOOD, "expiredAt": ""}, require_ip=True).expires_at is None


# ---------------------------------------------------------------- instruments
def test_instrument_rounding() -> None:
    rules = InstrumentRules.from_bybit(INSTRUMENT)
    assert rules.floor_qty(0.0089) == Decimal("0.008")
    assert rules.floor_qty(1000) == Decimal(100)
    assert rules.round_price(60000.04) == Decimal("60000")
    assert rules.round_stop(58800.03, long=True) == Decimal("58800.1")
    assert rules.round_stop(61200.07, long=False) == Decimal("61200")
    assert rules.is_tradeable(Decimal("0.001"), 60000)
    assert not rules.is_tradeable(Decimal("0.0001"), 60000)
    assert not rules.is_tradeable(Decimal("0.001"), 1000)  # notionnel 1 < 5
    assert set(DEFAULT_RULES) == {"BTCUSDT", "ETHUSDT"}


# ---------------------------------------------------------------- client
def test_retry_then_success() -> None:
    s = FakeSession()
    s.fail_next = [FakeBybitError("rate limit", 10006), FakeBybitError("network", None)]
    assert client(s).equity() == 1000.0


def test_non_retryable_error_raises() -> None:
    s = FakeSession()
    s.fail_next = [FakeBybitError("bad param", 10001)]
    with pytest.raises(ExchangeError) as exc:
        client(s).equity()
    assert exc.value.code == 10001


def test_retry_exhausted() -> None:
    s = FakeSession()
    s.fail_next = [FakeBybitError("down", 503)] * 5
    with pytest.raises(ExchangeError):
        client(s).equity()


def test_retcode_error_and_retry() -> None:
    class S(FakeSession):
        n = 0

        def get_wallet_balance(self, **kw: object) -> dict[str, object]:
            self.n += 1
            if self.n == 1:
                return {"retCode": 10006, "retMsg": "rate"}
            return {"retCode": 10001, "retMsg": "bad"}

    with pytest.raises(ExchangeError, match="bad"):
        client(S()).equity()


def test_equity_fallbacks() -> None:
    class S(FakeSession):
        payload: dict[str, object] = {}

        def get_wallet_balance(self, **kw: object) -> dict[str, object]:
            return {"retCode": 0, "result": self.payload}

    s = S()
    s.payload = {"list": [{"totalEquity": "", "coin": [{"coin": "USDT", "equity": "42"}]}]}
    assert client(s).equity() == 42
    s.payload = {"list": []}
    with pytest.raises(ExchangeError):
        client(s).equity()
    s.payload = {"list": [{"totalEquity": "", "coin": []}]}
    with pytest.raises(ExchangeError):
        client(s).equity()


def test_market_endpoints() -> None:
    s = FakeSession()
    s.klines["BTCUSDT"] = [[str(i), "1", "2", "0.5", "1.5", "10", "15"] for i in (1, 2, 3)]
    c = client(s)
    assert [r[0] for r in c.klines("BTCUSDT", "240", start_ms=1, end_ms=3)] == ["1", "2", "3"]
    assert c.funding_history("BTCUSDT", 0, 10) == []
    assert c.mark_price("BTCUSDT") == 60000
    assert c.server_time().year == 2023
    assert c.api_key_info()["readOnly"] == 0
    assert c.instruments(["BTCUSDT"])["BTCUSDT"].qty_step == Decimal("0.001")
    assert c.open_orders() == []
    assert c.executions("BTCUSDT", "x") == []
    assert c.closed_pnl("BTCUSDT") == []


def test_empty_market_results() -> None:
    class S(FakeSession):
        def get_instruments_info(self, **kw: object) -> dict[str, object]:
            return {"retCode": 0, "result": {"list": []}}

        def get_tickers(self, **kw: object) -> dict[str, object]:
            return {"retCode": 0, "result": {"list": []}}

    with pytest.raises(ExchangeError):
        client(S()).instruments(["BTCUSDT"])
    with pytest.raises(ExchangeError):
        client(S()).mark_price("BTCUSDT")


def test_open_with_stop_attaches_stop() -> None:
    s = FakeSession()
    c = client(s)
    c.open_with_stop("BTCUSDT", Side.LONG, Decimal("0.008"), Decimal(58800), "id1")
    name, kw = s.calls[-1]
    assert name == "place_order"
    assert kw["stopLoss"] == "58800"
    assert kw["slTriggerBy"] == "MarkPrice"
    assert kw["tpslMode"] == "Full"
    pos = c.positions()[0]
    assert pos.side is Side.LONG and pos.stop == 58800 and pos.qty == 0.008


def test_open_duplicate_link_id_is_idempotent() -> None:
    s = FakeSession()
    c = client(s)
    c.open_with_stop("BTCUSDT", Side.LONG, Decimal("0.008"), Decimal(58800), "id1")
    assert c.open_with_stop("BTCUSDT", Side.LONG, Decimal("0.008"), Decimal(58800), "id1") == "id1"


def test_open_rejects_missing_stop() -> None:
    with pytest.raises(ExchangeError):
        client(FakeSession()).open_with_stop("BTCUSDT", Side.LONG, Decimal(1), Decimal(0), "x")


def test_open_other_errors_propagate() -> None:
    s = FakeSession()
    s.fail_next = [FakeBybitError("insufficient", 110007)]
    with pytest.raises(ExchangeError):
        client(s).open_with_stop("BTCUSDT", Side.SHORT, Decimal(1), Decimal(61000), "x")


def test_close_stop_cancel_and_setup() -> None:
    s = FakeSession()
    c = client(s)
    c.open_with_stop("BTCUSDT", Side.SHORT, Decimal("0.01"), Decimal(61000), "id")
    c.set_stop("BTCUSDT", Decimal(60500))
    assert c.positions()[0].stop == 60500
    c.close_market("BTCUSDT", Side.SHORT, 0.01)
    assert c.positions() == []
    c.cancel_all()
    c.setup_symbol("BTCUSDT", 3)  # 110043 ignoré


def test_setup_symbol_propagates_real_error() -> None:
    s = FakeSession()
    s.fail_next = [FakeBybitError("forbidden", 10005)]
    with pytest.raises(ExchangeError):
        client(s).setup_symbol("BTCUSDT", 3)


def test_no_withdraw_or_transfer_method() -> None:
    names = {n.lower() for n in dir(BybitClient)}
    assert not any(w in n for n in names for w in ("withdraw", "transfer"))


def test_make_session(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ExchangeError):
        make_session(Settings())
    monkeypatch.setenv("MODE", "demo")
    monkeypatch.setenv("BYBIT_API_KEY", "k")
    monkeypatch.setenv("BYBIT_API_SECRET", "s")
    s = Settings()
    assert s.mode is Mode.DEMO
    session = make_session(s)
    assert "api-demo.bybit.com" in session.endpoint  # type: ignore[attr-defined]


# ---------------------------------------------------------------- stream
def test_stream_staleness() -> None:
    now = [0.0]
    st = MarketStream(["BTCUSDT"], clock=lambda: now[0])
    assert st.is_stale() and st.seconds_since_last() is None
    st.on_message({"data": {"symbol": "BTCUSDT", "markPrice": "60000"}})
    assert not st.is_stale() and st.price("BTCUSDT") == 60000
    now[0] = 61
    assert st.is_stale()
    st.on_message({"data": {}})
    assert not st.is_stale()
