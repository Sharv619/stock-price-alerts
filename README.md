# 📈 Stock Price Alerts

Single-user local app: set a target price on any Yahoo Finance ticker,
get a WhatsApp and/or email alert when the price crosses it.

- **Backend**: FastAPI + APScheduler (price check every 60 s, configurable)
- **Frontend**: Streamlit dashboard
- **Data**: yfinance (free, no API key)
- **Notifications**: Whapi (WhatsApp) + Gmail SMTP (email)
- **Storage**: SQLite (`alerts.db`)

## Quick start

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env   # fill in credentials
uvicorn main:app --port 8600            # terminal 1
streamlit run dashboard.py --server.port 8620   # terminal 2
```

Open http://localhost:8620 — full instructions in [SETUP.md](SETUP.md).

## Layout

| File | Purpose |
|------|---------|
| `app/database.py` | SQLAlchemy model + CRUD for alerts |
| `app/price_checker.py` | yfinance price fetch + alert evaluation |
| `app/notifier.py` | Whapi WhatsApp + Gmail SMTP senders |
| `app/scheduler.py` | APScheduler background loop |
| `main.py` | FastAPI routes (`/alerts`, `/price/{ticker}`, `/health`) |
| `dashboard.py` | Streamlit UI |
