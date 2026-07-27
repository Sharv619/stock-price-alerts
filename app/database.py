"""SQLite storage for stock price alerts."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///./alerts.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, nullable=False)
    target_price = Column(Float, nullable=False)
    condition = Column(String, nullable=False)  # "above" or "below"
    whatsapp_on = Column(Boolean, default=False)
    email_on = Column(Boolean, default=False)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    triggered = Column(Boolean, default=False)  # has fired at least once
    last_notified = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "ticker": self.ticker,
            "target_price": self.target_price,
            "condition": self.condition,
            "whatsapp_on": self.whatsapp_on,
            "email_on": self.email_on,
            "phone": self.phone,
            "email": self.email,
            "triggered": self.triggered,
            "last_notified": self.last_notified.isoformat() if self.last_notified else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


Base.metadata.create_all(bind=engine)

# Older DBs predate last_notified — add it in place.
with engine.connect() as conn:
    from sqlalchemy import text

    cols = [r[1] for r in conn.execute(text("PRAGMA table_info(alerts)"))]
    if "last_notified" not in cols:
        conn.execute(text("ALTER TABLE alerts ADD COLUMN last_notified DATETIME"))
        conn.commit()


def create_alert(ticker, target_price, condition, whatsapp_on=False,
                 email_on=False, phone=None, email=None):
    ticker = ticker.strip().upper()
    with SessionLocal() as db:
        existing = db.query(Alert).filter(
            Alert.ticker == ticker,
            Alert.target_price == target_price,
            Alert.condition == condition,
        ).first()
        if existing:
            return existing.to_dict()
        alert = Alert(
            ticker=ticker,
            target_price=target_price,
            condition=condition,
            whatsapp_on=whatsapp_on,
            email_on=email_on,
            phone=phone,
            email=email,
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)
        return alert.to_dict()


def get_active_alerts():
    """All alerts — they stay active and re-notify after the cooldown."""
    with SessionLocal() as db:
        alerts = db.query(Alert).all()
        return [a.to_dict() for a in alerts]


def get_all_alerts():
    with SessionLocal() as db:
        alerts = db.query(Alert).order_by(Alert.created_at.desc()).all()
        return [a.to_dict() for a in alerts]


def mark_notified(alert_id):
    with SessionLocal() as db:
        alert = db.query(Alert).get(alert_id)
        if alert:
            alert.triggered = True
            alert.last_notified = datetime.now()
            db.commit()
            return True
        return False


# Backwards-compat alias
mark_triggered = mark_notified


def delete_alert(alert_id):
    with SessionLocal() as db:
        alert = db.query(Alert).get(alert_id)
        if alert:
            db.delete(alert)
            db.commit()
            return True
        return False
