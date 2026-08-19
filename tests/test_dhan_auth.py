import json
import threading
import time
from datetime import datetime, timedelta, timezone

from app import dhan_auth


class Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


def clear_config(monkeypatch):
    for name in ("DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET"):
        monkeypatch.delenv(name, raising=False)


def set_config(monkeypatch):
    monkeypatch.setenv("DHAN_CLIENT_ID", "1000000001")
    monkeypatch.setenv("DHAN_PIN", "123456")
    monkeypatch.setenv("DHAN_TOTP_SECRET", "JBSWY3DPEHPK3PXP")


def test_missing_configuration_has_passive_unauthenticated_status(monkeypatch, tmp_path):
    clear_config(monkeypatch)
    monkeypatch.setattr(dhan_auth, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(dhan_auth.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network called")))

    assert dhan_auth.dhan_status() == {
        "configured": False,
        "authenticated": False,
        "token_expiry": None,
    }


def test_expired_saved_token_is_not_authenticated(monkeypatch, tmp_path):
    set_config(monkeypatch)
    token_file = tmp_path / "token"
    token_file.write_text(json.dumps({
        "access_token": "expired",
        "expiry": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    }))
    monkeypatch.setattr(dhan_auth, "TOKEN_FILE", token_file)

    assert dhan_auth.dhan_status()["authenticated"] is False
    assert dhan_auth.dhan_status()["token_expiry"] is None


def test_refresh_is_cached_and_persisted_once(monkeypatch, tmp_path):
    set_config(monkeypatch)
    token_file = tmp_path / "token"
    monkeypatch.setattr(dhan_auth, "TOKEN_FILE", token_file)
    monkeypatch.setattr(dhan_auth.pyotp.TOTP, "now", lambda self: "654321")
    expiry = datetime.now(timezone.utc) + timedelta(hours=24)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"accessToken": "fresh-token", "expiryTime": expiry.isoformat()})

    monkeypatch.setattr(dhan_auth.httpx, "post", post)

    assert dhan_auth.get_access_token() == "fresh-token"
    assert dhan_auth.get_access_token() == "fresh-token"
    assert len(calls) == 1
    assert calls[0][1]["params"]["totp"] == "654321"
    assert json.loads(token_file.read_text())["access_token"] == "fresh-token"
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_concurrent_cache_miss_generates_one_token(monkeypatch, tmp_path):
    set_config(monkeypatch)
    monkeypatch.setattr(dhan_auth, "TOKEN_FILE", tmp_path / "token")
    expiry = datetime.now(timezone.utc) + timedelta(hours=24)
    calls = 0
    calls_lock = threading.Lock()

    def post(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.03)
        return Response({"accessToken": "shared-token", "expiryTime": expiry.isoformat()})

    monkeypatch.setattr(dhan_auth.httpx, "post", post)
    barrier = threading.Barrier(6)
    results = []

    def worker():
        barrier.wait()
        results.append(dhan_auth.get_access_token())

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert results == ["shared-token"] * 6
    assert calls == 1
