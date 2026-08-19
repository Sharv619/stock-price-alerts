from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database


def test_database_crud_and_deduplication(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'alerts.db'}",
        connect_args={"check_same_thread": False},
    )
    testing_session = sessionmaker(bind=engine, autoflush=False)
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)

    created = database.create_alert(
        " tcs.ns ", 100.0, "above", email_on=True, email="test@example.com"
    )
    duplicate = database.create_alert(
        "TCS.NS", 100.0, "above", whatsapp_on=True, phone="+61400000000"
    )

    assert created["ticker"] == "TCS.NS"
    assert duplicate["id"] == created["id"]
    assert len(database.get_all_alerts()) == 1
    assert database.mark_notified(created["id"]) is True
    assert database.get_active_alerts()[0]["triggered"] is True
    assert database.get_active_alerts()[0]["last_notified"] is not None
    assert database.delete_alert(created["id"]) is True
    assert database.delete_alert(created["id"]) is False
    assert database.get_all_alerts() == []
