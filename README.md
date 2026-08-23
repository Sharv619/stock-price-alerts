# 📈 Stock Price Alerts + StraddleLab

Single-user local app: set a target price on any supported ticker,
get a WhatsApp and/or email alert when the price crosses it.

StraddleLab adds a local, auditable long-straddle research and paper-trading
workflow: construct, validate, calculate, simulate expiry payoff, approve,
open a simulated position, monitor, exit, review, and export the journal.

> **PAPER TRADING ONLY. NO LIVE ORDERS ARE PLACED.** Dhan is used only for
> market data. StraddleLab contains no broker order execution integration.

- **Backend**: FastAPI + APScheduler (price check every 60 s, configurable)
- **Frontend**: Streamlit dashboard
- **Data**: DhanHQ for NSE stocks with yfinance fallback; yfinance for BSE, indices, US stocks, and crypto
- **Notifications**: Whapi (WhatsApp) + Gmail SMTP (email)
- **Options data**: DhanHQ option chains for NIFTY and BANKNIFTY
- **Storage**: SQLite (`alerts.db`) with append-only StraddleLab journal

## Quick start

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env   # fill in credentials
uvicorn main:app --port 8600            # terminal 1
streamlit run dashboard.py --server.port 8620   # terminal 2
```

Open http://localhost:8620 — full instructions in [SETUP.md](SETUP.md).

Use the dashboard sidebar to switch between **Stock Alerts** and
**StraddleLab**.

> DhanHQ tokens are generated automatically with the configured TOTP and last
> 24 hours. See [SETUP.md](SETUP.md) for details.

## Layout

| File | Purpose |
|------|---------|
| `app/database.py` | SQLAlchemy model + CRUD for alerts |
| `app/price_checker.py` | Batched market-feed price fetch + alert evaluation |
| `app/dhan_auth.py` | DhanHQ TOTP token load and refresh |
| `app/market_feed.py` | DhanHQ/yfinance normalized price provider |
| `app/notifier.py` | Whapi WhatsApp + Gmail SMTP senders |
| `app/scheduler.py` | APScheduler background loop |
| `app/straddle/` | Pure strategy domain, Dhan options adapter, paper lifecycle, persistence, API, and UI presentation |
| `main.py` | FastAPI stock-alert routes plus the `/straddle/*` router |
| `dashboard.py` | Streamlit UI |

## Roadmap

Phase 11 is documented as a proposed, separately deployable historical-replay
microservice within this monorepository. It will ingest user-uploaded NSE CSVs and
replay a recurring four-leg NIFTY calendar straddle without sharing the alert
database or runtime. It is not implemented yet. See
[Phase 11 — NSE Calendar Replay Microservice](docs/PHASE_11_CALENDAR_REPLAY_MICROSERVICE.md).

## StraddleLab scope and limitations

- Long BUY straddles only: matching CALL and PUT, same strike/expiry/quantity.
- Supported option underlyings: `NIFTY` and `BANKNIFTY`.
- Dhan credentials and a valid market-data entitlement are required for live
  option chains. Startup still succeeds without credentials and reports the
  provider as unavailable.
- Scenario simulation is an expiry payoff surface, not a forecast.
- Greeks and IV are displayed when supplied but are not calculated.
- Monitoring is manual in the MVP; the scheduler never opens or closes paper
  positions.
- Exports are deterministic JSON and append `EXPORT_CREATED` to the journal.

## Tests

```bash
python -m pytest -q
```
