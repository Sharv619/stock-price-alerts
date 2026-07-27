"""FastAPI backend for the stock price alert app."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import database, kite_auth
from app.price_checker import get_current_price, last_check
from app.scheduler import is_running, start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Stock Price Alerts", lifespan=lifespan)


class AlertCreate(BaseModel):
    ticker: str = Field(..., min_length=1)
    target_price: float = Field(..., gt=0)
    condition: str = Field(..., pattern="^(above|below)$")
    whatsapp_on: bool = False
    email_on: bool = False
    phone: str | None = None
    email: str | None = None


@app.post("/alerts")
def create_alert(alert: AlertCreate):
    if alert.whatsapp_on and not alert.phone:
        raise HTTPException(422, "phone required when whatsapp_on")
    if alert.email_on and not alert.email:
        raise HTTPException(422, "email required when email_on")
    if not alert.whatsapp_on and not alert.email_on:
        raise HTTPException(422, "enable at least one notification channel")
    if get_current_price(alert.ticker) is None:
        raise HTTPException(422, f"unknown ticker: {alert.ticker}")
    return database.create_alert(
        ticker=alert.ticker,
        target_price=alert.target_price,
        condition=alert.condition,
        whatsapp_on=alert.whatsapp_on,
        email_on=alert.email_on,
        phone=alert.phone,
        email=alert.email,
    )


@app.get("/alerts")
def list_alerts():
    return database.get_all_alerts()


@app.delete("/alerts/{alert_id}")
def remove_alert(alert_id: int):
    if not database.delete_alert(alert_id):
        raise HTTPException(404, "alert not found")
    return {"deleted": alert_id}


@app.get("/price/{ticker}")
def price(ticker: str):
    p = get_current_price(ticker)
    if p is None:
        raise HTTPException(404, f"no price data for {ticker}")
    return {"ticker": ticker.upper(), "price": p}


@app.get("/kite/login")
def kite_login():
    """Redirect to the Kite OAuth login page."""
    try:
        return RedirectResponse(kite_auth.get_login_url())
    except RuntimeError as e:
        raise HTTPException(
            500, f"{e}. Set KITE_API_KEY / KITE_API_SECRET in .env."
        )


@app.get("/kite/callback", response_class=HTMLResponse)
def kite_callback(request_token: str | None = None, status: str | None = None):
    """OAuth redirect target — exchange the request_token for an access token."""
    if status != "success" or not request_token:
        return HTMLResponse(
            "<h3>❌ Kite login failed</h3>"
            "<p>No request token returned. "
            '<a href="/kite/login">Try again</a>.</p>',
            status_code=400,
        )
    try:
        kite_auth.login(request_token)
    except Exception as e:
        return HTMLResponse(
            f"<h3>❌ Kite login failed</h3><p>{e}</p>"
            '<p><a href="/kite/login">Try again</a>.</p>',
            status_code=500,
        )
    return HTMLResponse(
        "<h3>✅ Kite authenticated</h3>"
        "<p>Token saved — close this tab / return to the dashboard.</p>"
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "scheduler_running": is_running(),
        "last_check": last_check["time"],
        "alerts_checked": last_check["checked"],
        "active_alerts": len(database.get_active_alerts()),
        "kite": kite_auth.kite_status(),
    }
