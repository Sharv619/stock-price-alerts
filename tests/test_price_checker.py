from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import price_checker


@pytest.mark.parametrize(
    ("condition", "current", "target", "expected"),
    [
        ("above", 100, 100, True),
        ("above", 99.99, 100, False),
        ("below", 100, 100, True),
        ("below", 100.01, 100, False),
        ("invalid", 100, 100, False),
    ],
)
def test_thresholds_are_inclusive(condition, current, target, expected):
    assert price_checker.check_condition(current, target, condition) is expected


@pytest.mark.parametrize("symbol", ["BTC-USD", "ETH-USD"])
def test_crypto_is_open_24_7(symbol):
    sunday = datetime(2026, 8, 16, 3, 0, tzinfo=timezone.utc)
    assert price_checker.is_market_open(symbol, sunday) is True


@pytest.mark.parametrize("symbol", ["TCS.NS", "RELIANCE.BO", "^NSEI", "^BSESN"])
def test_indian_symbols_and_indices_use_indian_hours(symbol):
    monday_open = datetime(2026, 8, 17, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
    monday_closed = datetime(2026, 8, 17, 11, 0, tzinfo=timezone.utc)  # 16:30 IST
    assert price_checker.is_market_open(symbol, monday_open) is True
    assert price_checker.is_market_open(symbol, monday_closed) is False


def test_us_symbols_use_new_york_hours():
    monday_open = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)  # 10:00 EDT
    monday_closed = datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc)  # 18:00 EDT
    assert price_checker.is_market_open("AAPL", monday_open) is True
    assert price_checker.is_market_open("AAPL", monday_closed) is False


def test_cooldown_preserves_existing_behavior(monkeypatch, alert_factory):
    monkeypatch.setattr(price_checker, "NOTIFY_COOLDOWN_MINUTES", 60)
    recent = alert_factory(last_notified=(datetime.now() - timedelta(minutes=59)).isoformat())
    old = alert_factory(last_notified=(datetime.now() - timedelta(minutes=61)).isoformat())

    assert price_checker.in_cooldown(recent) is True
    assert price_checker.in_cooldown(old) is False
    assert price_checker.in_cooldown(alert_factory(last_notified=None)) is False


def test_duplicate_tickers_are_fetched_once_per_cycle(monkeypatch, alert_factory):
    alerts = [alert_factory(id=1), alert_factory(id=2, target_price=90)]
    requested = []
    marked = []
    monkeypatch.setattr(price_checker, "get_active_alerts", lambda: alerts)
    monkeypatch.setattr(price_checker, "is_market_open", lambda symbol: True)
    monkeypatch.setattr(
        price_checker.market_feed,
        "get_prices",
        lambda symbols: requested.append(list(symbols)) or {"TCS.NS": 100.0},
    )
    monkeypatch.setattr(price_checker, "mark_notified", marked.append)
    monkeypatch.setattr(price_checker, "send_whatsapp", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    monkeypatch.setattr(price_checker, "send_email", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))

    price_checker.check_all_alerts()

    assert requested == [["TCS.NS"]]
    assert marked == [1, 2]
    assert price_checker.last_check["checked"] == 2


def test_alert_in_cooldown_is_not_sent_or_marked(monkeypatch, alert_factory):
    alert = alert_factory(
        whatsapp_on=True,
        phone="+61400000000",
        last_notified=(datetime.now() - timedelta(minutes=1)).isoformat(),
    )
    monkeypatch.setattr(price_checker, "NOTIFY_COOLDOWN_MINUTES", 60)
    monkeypatch.setattr(price_checker, "get_active_alerts", lambda: [alert])
    monkeypatch.setattr(price_checker, "is_market_open", lambda symbol: True)
    monkeypatch.setattr(price_checker.market_feed, "get_prices", lambda symbols: {"TCS.NS": 100.0})
    monkeypatch.setattr(price_checker, "send_whatsapp", lambda *a: (_ for _ in ()).throw(AssertionError("sent")))
    monkeypatch.setattr(price_checker, "mark_notified", lambda *a: (_ for _ in ()).throw(AssertionError("marked")))

    price_checker.check_all_alerts()


def test_notification_attempt_is_stamped_even_when_delivery_fails(monkeypatch, alert_factory):
    alert = alert_factory(whatsapp_on=True, phone="+61400000000")
    marked = []
    monkeypatch.setattr(price_checker, "get_active_alerts", lambda: [alert])
    monkeypatch.setattr(price_checker, "is_market_open", lambda symbol: True)
    monkeypatch.setattr(price_checker.market_feed, "get_prices", lambda symbols: {"TCS.NS": 100.0})
    monkeypatch.setattr(price_checker, "send_whatsapp", lambda *a: False)
    monkeypatch.setattr(price_checker, "mark_notified", marked.append)

    price_checker.check_all_alerts()

    assert marked == [1]


def test_evaluator_does_not_import_provider_sdks():
    source = Path(price_checker.__file__).read_text()
    assert "kiteconnect" not in source.lower()
    assert "smartapi" not in source.lower()
