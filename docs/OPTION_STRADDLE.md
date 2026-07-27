# Option Straddle Feature — Design Notes

Design reference only. No code has been written for this yet.

## What a straddle is

Buy 1 call + 1 put, same strike, same expiry, same underlying. Bet on a big
move, either direction. Max loss = combined premium paid (if underlying lands
exactly on strike at expiry). Breakevens: `strike ± combined_premium`.

## What "supporting straddles" means for this app

- Track an open straddle position (ticker, strike, expiry, entry premiums).
- Pull live CE/PE prices, compute current combined value, P&L, breakevens.
- Alert (WhatsApp/email) when P&L crosses a threshold — same delivery path
  as the existing single-price alerts.

## What Kite Connect already gives for free

`kiteconnect==5.2.0` (already a dependency) provides:

- **`kite.instruments("NFO")`** — full instrument dump as a list of dicts:
  `tradingsymbol`, `strike` (float), `expiry` (date), `instrument_type`
  (`CE`/`PE`/`FUT`), `lot_size` (int), `instrument_token` (int), `name`,
  `segment`, `exchange`. There is **no dedicated option-chain endpoint** —
  you get this flat dump and must filter/group it yourself (by `name` +
  `expiry` + `strike`) to assemble a chain.
- **`kite.quote()` / `kite.ltp()` / `kite.ohlc()`** — identical call shape to
  equities, just pass `"NFO:<tradingsymbol>"` instead of `"NSE:<symbol>"`.
  For options/futures the response additionally includes `oi`,
  `oi_day_high`, `oi_day_low` (open interest) on top of the usual
  `last_price`, `volume`, `depth`, etc.
- **`kite.historical_data(..., oi=True)`** — historical OI series.
- **`kite.order_margins()`** — margin estimate for the option legs (useful
  for showing capital required, separate from P&L math).

**Not provided anywhere in the library:** implied volatility, or any Greeks
(delta/gamma/theta/vega). Confirmed by source inspection — no such fields or
methods exist in `connect.py` / `ticker.py`.

## What needs custom code

- **Option-chain assembly** — group the raw NFO dump by underlying + expiry
  + strike into a usable CE/PE chain.
- **Straddle selection logic** — if auto-picking, find the strike nearest
  current spot (ATM detection).
- **P&L / breakeven math** — combined premium tracking,
  `payoff = |spot − strike| − combined_premium`, breakevens at
  `strike ± combined_premium`.
- **Greeks / IV** — entirely absent from Kite; would need a new dependency
  (`py_vollib` or hand-rolled Black-Scholes via `scipy` + `numpy`) plus an
  assumed risk-free rate. None of `scipy`/`numpy`/`py_vollib`/`mibian` are
  currently in `requirements.txt`.

## Open design decisions (not resolved here)

**1. Manual tracker vs. auto ATM finder**

| | Manual tracker | Auto ATM finder |
|---|---|---|
| Input | User enters ticker, strike, expiry, entry premiums | User enters ticker + expiry only |
| Build cost | Low — just live-price lookup + P&L math | Higher — needs full chain assembly + ATM-detection logic |
| Recommended for v1 | Yes | Later enhancement |

**2. Greeks/IV — skip vs. include**

| | Skip for v1 | Include delta/theta/vega |
|---|---|---|
| Dependency | None | New: `py_vollib` or `scipy`+`numpy` |
| Value | Covers P&L/breakeven, which drives the alert | Useful for judging IV-crush risk, but adds build + maintenance cost |
| Recommended for v1 | Yes | Later enhancement |

## How it would slot into the existing architecture

Reusing exact patterns already in the codebase:

- **`app/database.py`** — new `Straddle` model + `create_/get_/delete_`
  functions, following the existing `Alert` model (`app/database.py:17-45`)
  and its manual `PRAGMA table_info` migration shim.
- **`app/kite_auth.py`** — reuse `get_kite_client()` / `refresh_client()`
  singleton getters; same try/except-then-refresh pattern as
  `price_checker._get_kite_price`.
- **New `app/straddle_pricer.py`** — instrument-dump caching, chain
  assembly, live price fetch, P&L/breakeven calc. Mirrors the structure of
  `app/price_checker.py`.
- **`main.py`** — new `POST /straddles`, `GET /straddles`,
  `DELETE /straddles/{id}` routes + a `StraddleCreate` Pydantic model, same
  shape as the existing `/alerts` routes.
- **`app/scheduler.py`** — either extend `check_all_alerts()` to also
  evaluate straddles, or add a second
  `scheduler.add_job(check_all_straddles, "interval", ...)`.
- **`app/notifier.py`** — `send_whatsapp` / `send_email` reused unchanged;
  just needs a new message-formatting function alongside `format_message`.
- **`dashboard.py`** — new "Straddle Tracking" section following the
  existing three-part pattern: add-form, live table with P&L/breakevens,
  sidebar status.

## New dependency note

`requirements.txt` currently has no options-math library
(`fastapi`, `uvicorn`, `kiteconnect==5.2.0`, `streamlit`, `apscheduler`,
`sqlalchemy`, `python-dotenv`, `httpx`, `yfinance`). A new dependency is
only needed if Greeks/IV are added later — not for the manual-tracker,
P&L-only v1.
