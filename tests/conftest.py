import pytest


@pytest.fixture
def alert_factory():
    def make(**overrides):
        alert = {
            "id": 1,
            "ticker": "TCS.NS",
            "target_price": 100.0,
            "condition": "above",
            "whatsapp_on": False,
            "email_on": False,
            "phone": None,
            "email": None,
            "last_notified": None,
        }
        alert.update(overrides)
        return alert

    return make
