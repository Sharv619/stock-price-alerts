from app import dhan_auth, scheduler


def test_scheduler_has_only_price_check_and_dhan_refresh_jobs():
    jobs = {job.id: job for job in scheduler.scheduler.get_jobs()}

    assert set(jobs) == {"price_check", "dhan_token_refresh"}
    assert jobs["price_check"].max_instances == 1
    assert jobs["price_check"].coalesce is True
    assert jobs["dhan_token_refresh"].max_instances == 1
    assert jobs["dhan_token_refresh"].coalesce is True
    assert str(jobs["dhan_token_refresh"].trigger.timezone) == "Asia/Kolkata"


def test_scheduled_refresh_is_noop_without_credentials(monkeypatch):
    for name in ("DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dhan_auth, "refresh_token", lambda: (_ for _ in ()).throw(AssertionError("refreshed")))

    dhan_auth.scheduled_token_refresh()
