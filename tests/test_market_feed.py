import json

import pytest

from app import market_feed


class Response:
    def __init__(self, data=None, status_code=200, content=b""):
        self._data = data
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def reset_instrument_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(market_feed, "_symbols", None)
    monkeypatch.setattr(market_feed, "INSTRUMENT_FILE", tmp_path / "instruments.csv")
    monkeypatch.setattr(market_feed, "MAP_FILE", tmp_path / "map.json")


def test_instrument_csv_maps_only_nse_equities(monkeypatch):
    market_feed.INSTRUMENT_FILE.write_text(
        "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID\n"
        "NSE,E,TCS,11536\n"
        "NSE,E,RELIANCE,2885\n"
        "BSE,E,TCS,532540\n"
        "NSE,D,TCS-FUT,999\n"
    )

    assert market_feed._load_instruments() == {"TCS": "11536", "RELIANCE": "2885"}
    assert json.loads(market_feed.MAP_FILE.read_text())["TCS"] == "11536"


def test_multiple_nse_symbols_use_one_ltp_request(monkeypatch):
    monkeypatch.setattr(market_feed, "_symbols", {"TCS": "11536", "RELIANCE": "2885"})
    monkeypatch.setattr(market_feed.dhan_auth, "get_access_token", lambda force_refresh=False: "token")
    monkeypatch.setattr(market_feed.dhan_auth, "_config", lambda: ("client", "pin", "secret"))
    posts = []

    def post(url, **kwargs):
        posts.append((url, kwargs))
        return Response({"data": {"NSE_EQ": {
            "11536": {"last_price": "4520.25"},
            "2885": {"last_price": 3001},
        }}})

    monkeypatch.setattr(market_feed.httpx, "post", post)
    monkeypatch.setattr(market_feed, "_yahoo", lambda symbol: (_ for _ in ()).throw(AssertionError(symbol)))

    prices = market_feed.get_prices(["tcs.ns", "RELIANCE.NS", "TCS.NS"])

    assert prices == {"TCS.NS": 4520.25, "RELIANCE.NS": 3001.0}
    assert len(posts) == 1
    assert posts[0][1]["json"] == {"NSE_EQ": [11536, 2885]}


def test_401_refreshes_once_and_retries_once(monkeypatch):
    monkeypatch.setattr(market_feed, "_symbols", {"TCS": "11536"})
    auth_calls = []

    def token(force_refresh=False):
        auth_calls.append(force_refresh)
        return "new" if force_refresh else "old"

    monkeypatch.setattr(market_feed.dhan_auth, "get_access_token", token)
    monkeypatch.setattr(market_feed.dhan_auth, "_config", lambda: ("client", "pin", "secret"))
    responses = [Response(status_code=401), Response({"data": {"NSE_EQ": {"11536": {"last_price": 101}}}})]
    monkeypatch.setattr(market_feed.httpx, "post", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(market_feed, "_yahoo", lambda symbol: (_ for _ in ()).throw(AssertionError(symbol)))

    assert market_feed.get_prices(["TCS.NS"]) == {"TCS.NS": 101.0}
    assert auth_calls == [False, True]
    assert responses == []


@pytest.mark.parametrize("failure", ["missing", "malformed", "network", "auth"])
def test_dhan_failures_fall_back_per_symbol(monkeypatch, failure):
    monkeypatch.setattr(market_feed, "_symbols", {} if failure == "missing" else {"TCS": "11536"})
    monkeypatch.setattr(market_feed.dhan_auth, "_config", lambda: ("client", "pin", "secret"))
    monkeypatch.setattr(market_feed.dhan_auth, "get_access_token", lambda force_refresh=False: "token")
    yahoo_calls = []
    monkeypatch.setattr(market_feed, "_yahoo", lambda symbol: yahoo_calls.append(symbol) or 99.5)

    if failure == "malformed":
        monkeypatch.setattr(market_feed.httpx, "post", lambda *a, **k: Response({"unexpected": True}))
    elif failure == "network":
        monkeypatch.setattr(market_feed.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    elif failure == "auth":
        monkeypatch.setattr(market_feed.dhan_auth, "get_access_token", lambda force_refresh=False: (_ for _ in ()).throw(RuntimeError("no auth")))

    assert market_feed.get_prices(["TCS.NS"]) == {"TCS.NS": 99.5}
    assert yahoo_calls == ["TCS.NS"]


def test_exhausted_401_retry_falls_back(monkeypatch):
    monkeypatch.setattr(market_feed, "_symbols", {"TCS": "11536"})
    monkeypatch.setattr(market_feed.dhan_auth, "get_access_token", lambda force_refresh=False: "token")
    monkeypatch.setattr(market_feed.dhan_auth, "_config", lambda: ("client", "pin", "secret"))
    posts = []
    monkeypatch.setattr(market_feed.httpx, "post", lambda *a, **k: posts.append(1) or Response(status_code=401))
    monkeypatch.setattr(market_feed, "_yahoo", lambda symbol: 88.0)

    assert market_feed.get_prices(["TCS.NS"]) == {"TCS.NS": 88.0}
    assert len(posts) == 2


@pytest.mark.parametrize("symbol", ["RELIANCE.BO", "^NSEI", "^BSESN", "AAPL", "BTC-USD"])
def test_non_nse_routes_are_yfinance_only(monkeypatch, symbol):
    monkeypatch.setattr(market_feed, "_load_instruments", lambda: (_ for _ in ()).throw(AssertionError("Dhan metadata called")))
    monkeypatch.setattr(market_feed.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Dhan LTP called")))
    monkeypatch.setattr(market_feed, "_yahoo", lambda value: 42.0)

    assert market_feed.get_prices([symbol]) == {symbol: 42.0}
