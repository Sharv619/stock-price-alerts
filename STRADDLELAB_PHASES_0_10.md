# Stock Price Alerts + StraddleLab: Phases 0–10

**Project handoff document — 20 August 2026**  
**Scope:** Stock-alert platform and long-straddle research/paper-trading MVP  
**Safety status:** **PAPER TRADING ONLY — NO LIVE ORDERS ARE PLACED**

---

## Page 1 of 3 — Platform foundation and deterministic strategy core

### Product and architecture overview

The repository began as a Streamlit and FastAPI stock-price alert application and
was extended into StraddleLab without replacing the original workflow. The deployed
system has two workspaces:

1. **Stock Alerts** — creates inclusive above/below price alerts, checks them on an
   APScheduler interval, and sends enabled WhatsApp or email notifications.
2. **StraddleLab** — constructs, validates, calculates, simulates, approves, opens,
   monitors, exits, reviews, and exports simulated long-straddle positions.

FastAPI is the backend boundary, Streamlit is the presentation layer, SQLite stores
alerts and StraddleLab audit records, and DhanHQ is used only for market data. Pure
StraddleLab modules do not import FastAPI, Streamlit, SQLAlchemy, schedulers,
notifiers, or provider SDKs. Monetary calculations use `Decimal`; deterministic
functions receive dates and timestamps explicitly rather than reading the clock.

### Phase 0 — Stable stock alerts and DhanHQ migration

The legacy mixed Angel/Kite runtime was replaced by a single DhanHQ REST integration
with yfinance fallback. `app/dhan_auth.py` performs official PIN/TOTP authentication,
caches the 24-hour access token in the ignored `.dhan_token` file, serializes token
refresh, and retries once after authorization failure. Passive status inspection does
not authenticate or make a network request.

`app/market_feed.py` normalizes prices into `{symbol: price | None}`. NSE `.NS`
symbols are mapped to Dhan security IDs and batched into one LTP call per checker
cycle. Missing instruments, malformed responses, provider failures, and exhausted
authentication retries fall back per symbol to yfinance. `.BO`, Indian indices, US
stocks, and crypto remain yfinance-only. The checker fetches duplicate tickers once
per cycle and preserves existing alert semantics:

- `above` triggers when `current_price >= target_price`;
- `below` triggers when `current_price <= target_price`;
- alerts can notify again while true, but at most once per configured cooldown;
- failed price or notification calls do not crash the scheduler;
- the scheduler runs only stock checks and a pre-market Dhan token-refresh job.

The original `Alert` schema and CRUD API remain intact: `POST /alerts`, `GET /alerts`,
`DELETE /alerts/{id}`, `GET /price/{ticker}`, and `GET /health`. WhatsApp uses Whapi,
email uses Gmail SMTP, and all notification calls are mocked in tests.

### Phase 1 — Long-straddle domain and calculation engine

`app/straddle/domain.py` defines immutable `OptionLeg`, `Strategy`, and
`StrategyCalculation` values. A supported strategy contains one BUY CALL and one BUY
PUT with the same underlying, strike, expiry, and quantity. The pure engine in
`app/straddle/engine.py` retains defensive structural invariants and exposes version
`STRADDLE_ENGINE_VERSION = "1.0.0"` from `app/straddle/version.py`.

For call premium `C`, put premium `P`, strike `K`, units per leg `Q`, spot `S`, expiry
spot `X`, fees `F`, and slippage `L`, the implemented formulas are:

```text
combined_premium       = C + P
total_cost             = (C + P) × Q
upper_break_even       = K + (C + P)
lower_break_even       = K - (C + P)
call_payoff            = max(X - K, 0) × Q
put_payoff             = max(K - X, 0) × Q
net_pnl                = call_payoff + put_payoff - total_cost - F - L
maximum_loss           = total_cost + F + L
required_move_points   = C + P
required_move_percent  = (C + P) ÷ S × 100
```

The acceptance strategy (`K=24300`, `C=180`, `P=160`, `Q=75`) therefore costs
`25500`, has break-evens at `23960` and `24640`, and has a maximum loss of `25500`
before costs.

### Phase 2 — Rules and validation engine

`app/straddle/rules.py` adds immutable `RuleDefinition`, `RuleResult`, and
`RulesConfig` models. Results have a stable ID, typed `ERROR/WARNING/INFO` severity,
blocking flag, acknowledgement flag, message, and deterministic structured evidence.
Thresholds are supplied explicitly through configuration.

The stable registry contains 22 rules:

- **Structure:** `STR-001` underlying mismatch, `STR-002` strike mismatch,
  `STR-003` expiry mismatch, `STR-004` quantity mismatch, `STR-005` non-BUY leg,
  `STR-006` negative premium, `STR-007` expired leg, and `STR-008` missing leg.
- **Data:** `DATA-001` hard stale, `DATA-002` warning stale, `DATA-003` missing
  provider timestamp, `DATA-004` missing required prices, and `DATA-005` missing
  Greeks.
- **Risk:** `RISK-IV-001`, `RISK-IV-002`, `RISK-THETA-001`, and `RISK-MOVE-001`.
- **Position:** `POS-001` one-leg exit, `POS-002` stale refresh, `POS-003` closed
  mutation, `POS-004` excessive exit quantity, and `POS-005` short-straddle request.

`RISK-IV-002` and `POS-001` structurally require acknowledgement. Hard staleness
emits `DATA-001` only; warning staleness applies only after the warning boundary and
through the hard boundary. The rules evaluate supplied facts only—Greeks, IV, theta,
prices, events, and timestamps are never fetched or generated here.

### Phase 3 — Provider-neutral strategy construction

`app/straddle/strategy_service.py` defines immutable `OptionContract`,
`OptionChainSnapshot`, `ConstructionIssue`, and `StrategyConstructionResult` models.
`recommend_atm_strike()` selects only from supplied strikes, minimizes absolute
distance from spot, and chooses the lower strike on an exact tie. Manual override is
allowed when the selected strike exists in the supplied chain.

Construction filters by underlying and expiry, finds exactly one CALL and PUT for the
selected strike, invokes Phase 2 structural and market-data rules, and calls Phase 1
calculation only if no result blocks progression. Missing legs surface `STR-008`;
unavailable strikes and duplicate logical contracts produce deterministic `CON-001`
through `CON-004` construction issues. The service does not invent contracts or know
which provider produced the snapshot.

<div style="page-break-after: always;"></div>

## Page 2 of 3 — Simulation, approval, lifecycle, monitoring, and audit

### Phase 4 — Deterministic expiry scenario simulation

`app/straddle/scenario.py` evaluates a valid strategy across any non-empty ordered
sequence of non-negative `Decimal` underlying prices. Caller order and duplicate
prices are preserved. For each price, the simulator calls the Phase 1 engine rather
than maintaining a second payoff implementation. Each immutable `ScenarioPoint`
contains underlying price, call payoff, put payoff, gross P&L, and net P&L.

`ScenarioConfig` supplies explicit large- and small-move percentages for five ordered
presets: `large_fall`, `small_fall`, `flat`, `small_rally`, and `large_rally`. Prices
are not rounded silently. The surface represents **expiry payoff only**—it contains no
forecast, probability, Black-Scholes valuation, theta curve, IV crush, or Monte Carlo
model. For the reference strategy, expiry P&L is `0` at `23960`, `-25500` at `24300`,
and `0` at `24640` before fees and slippage.

### Phase 5 — Approval and paper-position lifecycle

`app/straddle/paper_trading.py` defines typed immutable strategy states (`DRAFT`,
`VALIDATED`, `READY_FOR_PAPER`, `ARCHIVED`) and paper-position states (`OPEN`,
`PARTIALLY_CLOSED`, `CLOSED`). Approval checks every Phase 2 result: blocking results
prevent approval; acknowledgement-required warnings must be acknowledged by stable
rule ID; ordinary warnings and information do not block. Approval never opens a
position automatically.

`open_paper_position()` requires an approved strategy and caller-supplied identifier
and timestamp. Its entry snapshot preserves strategy, strike, expiry, exact entry
premiums, original quantity, entry fees/slippage, and remaining quantities. No UUID,
clock, broker account, or order call is generated internally.

Partial and full exits return new position objects. A one-leg exit invokes `POS-001`
and requires explicit acknowledgement; excessive quantity invokes `POS-004`; any
mutation of a closed position invokes `POS-003`. `CLOSED` is terminal. Realised P&L
is calculated as:

```text
call realised = (call exit price - call entry premium) × closed call units
put realised  = (put exit price  - put entry premium)  × closed put units
net realised  = cumulative call realised + cumulative put realised
                - entry fees - entry slippage
                - cumulative exit fees - cumulative exit slippage
```

The acceptance exit at CALL `250`, PUT `100`, quantity `75`, fees `100`, and slippage
`50` produces call realised `5250`, put realised `-4500`, gross `750`, and net `600`.
Lifecycle operations return typed `TradeEvent` values but do not persist them directly.

### Phase 6 — Paper-position monitoring

`app/straddle/monitoring.py` accepts an OPEN or PARTIALLY_CLOSED position and an
immutable `PositionMarketSnapshot`; it never fetches data. Remaining CALL and PUT
quantities are valued independently, so a partial one-leg exit is handled correctly:

```text
call current value = current call price × remaining call quantity
put current value  = current put price  × remaining put quantity
combined value     = call current value + put current value

unrealised call = (current call price - entry call premium) × remaining call units
unrealised put  = (current put price  - entry put premium)  × remaining put units
unrealised P&L  = unrealised call + unrealised put
total P&L       = stored realised P&L + unrealised P&L
```

Break-even distances are signed: `upper_break_even - spot` and
`spot - lower_break_even`. If all leg Greeks are supplied, delta, gamma, theta, and
vega are quantity-weighted across remaining units; otherwise aggregates remain
`None` and `DATA-005` is informational. No Greek or IV is calculated.

Fresh data is valued normally. Warning-stale data is valued with `DATA-002`. Hard
stale or price-incomplete data is unusable; a compatible caller-supplied last-known-
good snapshot may be preserved explicitly with `used_last_known_good=True`,
`POS-002`/data evidence, and recalculated total P&L using current realised P&L. Without
fallback, monitoring fails without fabricating or erasing a valuation. Successful
refreshes emit `MARKET_SNAPSHOT_APPLIED`; closed positions cannot be monitored.

### Phase 7 — SQLite persistence and append-only journal

`app/straddle/models.py` and `app/straddle/persistence.py` isolate SQLAlchemy from all
pure modules. New tables are created safely alongside the existing `alerts` table:

- `straddle_strategy` and `straddle_option_leg`;
- `straddle_strategy_calculation` and `straddle_risk_assessment`;
- `straddle_strategy_approval` and `straddle_warning_acknowledgement`;
- `straddle_paper_position` and append-oriented `straddle_position_snapshot`;
- append-only `straddle_trade_event`.

Money uses SQLAlchemy `Numeric`, not binary floating point. Evidence and event payloads
use deterministic JSON serialization for `Decimal`, datetime, date, enum, mappings,
and sequences; domain objects are never pickled. Timestamps are UTC-aware and supplied
by callers.

Calculations, risk assessments, snapshots, and events append new history rather than
overwriting previous records. The repository intentionally provides
`append_trade_event()` and no normal event update/delete API. Opening, monitoring, and
closing operations pair state changes with their journal events in transactions so a
failure rolls back the entire unit of work. Entry fields cannot be silently changed,
and closed-position mutation is rejected. Ordered journal reads can reconstruct a
lifecycle from `STRATEGY_CREATED` through validation, opening, snapshots, partial exits,
closure, notes, and exports without relying on mutable position columns.

<div style="page-break-after: always;"></div>

## Page 3 of 3 — Market-data integration, product workflow, operations, and verification

### Phase 8 — Provider-neutral options market data

`app/straddle/market_data.py` defines the small `OptionsMarketDataProvider` protocol:
`get_underlyings()`, `get_expiries()`, and `get_option_chain()`. Typed errors distinguish
authentication failure, unavailable instrument metadata, unknown underlying, unknown
expiry, quote failure, and malformed provider responses.

`app/straddle/providers/dhan_options.py` implements the protocol with official Dhan REST
market-data endpoints. It normalizes NIFTY and BANKNIFTY metadata, CE/PE into CALL/PUT,
expiry, strike, lot size, provider symbol, and security ID. Provider identifiers remain
at the adapter boundary; strategy construction receives existing provider-neutral
`OptionChainSnapshot` and `OptionContract` values.

Instrument metadata is persisted in `.dhan_instruments.csv` plus deterministic cache
metadata with a 24-hour TTL. Expired contracts can be filtered using an explicit
evaluation date. One option-chain request retrieves the selected expiry rather than
one request per contract. Provider and receipt timestamps remain distinct. Contracts
without usable premiums are not fabricated, duplicates are handled deterministically,
and there is deliberately no yfinance fallback for Indian option premiums. ATM
selection remains exclusively in the Phase 3 service.

### Phase 9 — FastAPI and Streamlit MVP workflow

`app/straddle/application.py` is the thin orchestration layer connecting provider,
domain services, persistence, and journal. `app/straddle/api.py` exposes the bounded
paper-workflow routes under `/straddle`:

```text
GET  /straddle/underlyings
GET  /straddle/expiries/{underlying}
GET  /straddle/chain/{underlying}/{expiry}
POST /straddle/strategies/construct
POST /straddle/strategies/{id}/approve
POST /straddle/strategies/{id}/paper-position
POST /straddle/strategies/{id}/simulate
GET  /straddle/positions
GET  /straddle/positions/{id}
POST /straddle/positions/{id}/refresh
POST /straddle/positions/{id}/exit
GET  /straddle/positions/{id}/journal
GET  /straddle/positions/{id}/export
GET  /straddle/health
```

Routes translate expected domain/provider failures into stable, safe responses and do
not duplicate formulas, rules, ATM selection, or lifecycle transitions. Raw stack
traces and credentials are not returned.

The Streamlit sidebar preserves the Stock Alerts workspace and adds StraddleLab. The
StraddleLab screen explicitly guides **Construct → Validate → Calculate → Simulate →
Approve → Paper Trade → Monitor → Exit → Review**. It displays stable rule IDs and
acknowledgement controls, calculation metrics and engine version, a clearly labelled
expiry-payoff table, separate approval/open actions, live paper valuation, partial or
full simulated exits, snapshots, and the append-only journal. It repeatedly states
**PAPER TRADING ONLY — NO LIVE ORDERS ARE PLACED** and does not rely solely on colour
for P&L meaning.

### Phase 10 — Export, health, deployment, and hardening

Position export produces deterministic audit JSON containing strategy and legs,
calculations and engine version, risk assessments, acknowledgements, entry state,
monitoring snapshots, realised result, and ordered journal. Export does not mutate
history and appends `EXPORT_CREATED` where appropriate.

`GET /health` reports scheduler, alert, Dhan/yfinance, database, and StraddleLab status;
`GET /straddle/health` provides focused provider/cache readiness. Health is passive: it
does not authenticate, refresh tokens, download metadata, mutate positions, or trade.
Credential-free startup succeeds and reports Dhan/options as unavailable. Stock prices
can still use yfinance fallback; constructing NIFTY/BANKNIFTY options requires valid
Dhan market-data access.

The FastAPI backend is deployed as a Render Web Service with:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

The Streamlit deployment points to
`https://stock-price-alerts-api.onrender.com`; `API_URL` can override this. The
dashboard uses a cached shared `httpx.Client` connection pool to prevent socket
exhaustion during reruns. Render free instances may sleep, and SQLite on an ephemeral
filesystem is not suitable for durable production records unless a persistent disk is
attached. The scheduler performs valuation-independent stock checks only; it never
opens, refreshes, or closes paper positions automatically.

### Configuration and operation

```bash
# Install and run locally
pip install -r requirements.txt --break-system-packages
cp .env.example .env
uvicorn main:app --port 8600
streamlit run dashboard.py --server.port 8620

# Verify
python -m pytest -q
python -m py_compile main.py dashboard.py app/straddle/*.py
```

Optional environment variables include `DHAN_CLIENT_ID`, `DHAN_PIN`, and
`DHAN_TOTP_SECRET`; `WHAPI_TOKEN`/`WHAPI_URL`; `GMAIL_USER` and
`GMAIL_APP_PASSWORD`; `CHECK_INTERVAL_SECONDS`; `NOTIFY_COOLDOWN_MINUTES`; and
`API_URL`. Secrets belong in local `.env`, Render environment variables, or Streamlit
secrets and must never be committed.

### Final verification and known limitations

The current repository verification is **273 tests passed with 3 known SQLAlchemy
`Query.get()` deprecation warnings**. Tests cover stock-alert regression behavior,
authentication, routing/fallback, notifications with mocks, all deterministic strategy
layers, Dhan normalization with mocked HTTP, isolated temporary SQLite persistence,
API contracts, credential-free startup, safety scans, and an end-to-end mocked paper
lifecycle from construction through `EXPORT_CREATED`. No test sends a real message,
places an order, or requires the user's `alerts.db`.

Known limitations are intentional MVP boundaries:

- long BUY straddles only; no short-straddle or live execution;
- NIFTY/BANKNIFTY option chains require Dhan; no fabricated yfinance options fallback;
- manual paper-position refresh only; no scheduler-driven monitoring;
- no pre-expiry model, Black-Scholes, probabilities, Greeks generation, IV/theta model,
  historical backtest, or Monte Carlo forecast;
- no PostgreSQL migration, multi-user authorization, or production-grade distributed
  job/state infrastructure;
- Render free-tier sleep and ephemeral SQLite storage can interrupt checks or lose data
  across instance replacement unless hosting is upgraded appropriately.

The completed Phase 0–10 outcome is an auditable, deterministic, provider-neutral
long-straddle research and paper-trading MVP layered safely onto the original stock
alert application. Dhan remains market-data-only, all position actions are simulated,
and live broker order execution is absent.
