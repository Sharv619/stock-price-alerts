from app import notifier


class HttpResponse:
    def __init__(self, data=None, error=None):
        self._data = data or {}
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._data


def test_whatsapp_missing_credentials_does_not_call_network(monkeypatch):
    monkeypatch.setattr(notifier, "WHAPI_TOKEN", None)
    monkeypatch.setattr(notifier.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network called")))

    assert notifier.send_whatsapp("+61400000000", "test") is False


def test_whatsapp_adapter_payload_and_success(monkeypatch):
    monkeypatch.setattr(notifier, "WHAPI_TOKEN", "secret")
    monkeypatch.setattr(notifier, "WHAPI_URL", "https://example.invalid/")
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return HttpResponse({"sent": True})

    monkeypatch.setattr(notifier.httpx, "post", post)

    assert notifier.send_whatsapp("+61400000000", "hello") is True
    assert calls[0][0][0] == "https://example.invalid/messages/text"
    assert calls[0][1]["json"] == {
        "to": "61400000000",
        "body": "hello",
        "type": "text",
    }
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret"


def test_whatsapp_adapter_failure_is_contained(monkeypatch):
    monkeypatch.setattr(notifier, "WHAPI_TOKEN", "secret")
    monkeypatch.setattr(
        notifier.httpx,
        "post",
        lambda *a, **k: HttpResponse(error=RuntimeError("offline")),
    )

    assert notifier.send_whatsapp("+61400000000", "hello") is False


def test_email_missing_credentials_does_not_open_smtp(monkeypatch):
    monkeypatch.setattr(notifier, "GMAIL_USER", None)
    monkeypatch.setattr(notifier, "GMAIL_APP_PASSWORD", None)
    monkeypatch.setattr(notifier.smtplib, "SMTP", lambda *a, **k: (_ for _ in ()).throw(AssertionError("SMTP opened")))

    assert notifier.send_email("to@example.com", "subject", "body") is False


def test_email_adapter_uses_tls_login_and_send(monkeypatch):
    monkeypatch.setattr(notifier, "GMAIL_USER", "from@example.com")
    monkeypatch.setattr(notifier, "GMAIL_APP_PASSWORD", "app-password")
    events = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            events.append(("open", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append(("close",))

        def starttls(self):
            events.append(("tls",))

        def login(self, user, password):
            events.append(("login", user, password))

        def send_message(self, message):
            events.append(("send", message["To"], message["Subject"], message.get_payload()))

    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)

    assert notifier.send_email("to@example.com", "subject", "body") is True
    assert events == [
        ("open", "smtp.gmail.com", 587, 30),
        ("tls",),
        ("login", "from@example.com", "app-password"),
        ("send", "to@example.com", "subject", "body"),
        ("close",),
    ]
