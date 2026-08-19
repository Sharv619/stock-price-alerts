import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import main


def test_alert_crud_routes_remain_compatible(monkeypatch):
    stored = {
        "id": 7,
        "ticker": "TCS.NS",
        "target_price": 100.0,
        "condition": "above",
        "whatsapp_on": False,
        "email_on": True,
        "phone": None,
        "email": "test@example.com",
    }
    monkeypatch.setattr(main, "get_current_price", lambda ticker: 99.0)
    monkeypatch.setattr(main.database, "create_alert", lambda **kwargs: stored)
    monkeypatch.setattr(main.database, "get_all_alerts", lambda: [stored])
    monkeypatch.setattr(main.database, "delete_alert", lambda alert_id: alert_id == 7)

    created = main.create_alert(main.AlertCreate(**{
            "ticker": "TCS.NS",
            "target_price": 100,
            "condition": "above",
            "email_on": True,
            "email": "test@example.com",
        }))
    assert created["id"] == 7
    assert main.list_alerts() == [stored]
    assert main.remove_alert(7) == {"deleted": 7}
    with pytest.raises(HTTPException) as exc:
        main.remove_alert(8)
    assert exc.value.status_code == 404


def test_alert_validation_remains_compatible(monkeypatch):
    base = {"ticker": "TCS.NS", "target_price": 100, "condition": "above"}
    with pytest.raises(HTTPException) as exc:
        main.create_alert(main.AlertCreate(**base))
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        main.create_alert(main.AlertCreate(**base, whatsapp_on=True))
    with pytest.raises(HTTPException):
        main.create_alert(main.AlertCreate(**base, email_on=True))
    with pytest.raises(ValidationError):
        main.AlertCreate(**{**base, "condition": "crosses", "email_on": True, "email": "a@b.com"})
    with pytest.raises(ValidationError):
        main.AlertCreate(**{**base, "target_price": 0, "email_on": True, "email": "a@b.com"})


def test_health_is_passive_and_generic(monkeypatch, tmp_path):
    for name in ("DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(main.dhan_auth, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(main.dhan_auth.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("auth network called")))
    monkeypatch.setattr(main.database, "get_active_alerts", lambda: [])
    monkeypatch.setattr(main, "is_running", lambda: False)

    result = main.health()

    assert result["status"] == "ok"
    assert "market_data" in result
    assert "kite" not in result
    assert result["market_data"]["dhan"]["authenticated"] is False
    assert result["market_data"]["yfinance_fallback"] is True


def test_application_starts_without_credentials_or_provider_network(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = r'''
import asyncio
import app.dhan_auth as auth
import app.market_feed as feed

def forbidden(*args, **kwargs):
    raise AssertionError("provider network called during startup")

auth.httpx.post = forbidden
feed.httpx.get = forbidden
feed.httpx.post = forbidden
import main

async def verify_startup():
    async with main.lifespan(main.app):
        response = main.health()
        assert response["market_data"]["dhan"]["configured"] is False
        assert response["scheduler_running"] is True

asyncio.run(verify_startup())
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    env["DHAN_CLIENT_ID"] = ""
    env["DHAN_PIN"] = ""
    env["DHAN_TOTP_SECRET"] = ""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / ".dhan_token").exists()
