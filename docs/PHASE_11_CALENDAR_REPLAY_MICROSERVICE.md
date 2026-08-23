# Phase 11 — NSE Calendar Replay Microservice

**Status:** Proposed; not implemented  
**Architecture decision:** Build Phase 11 as an independently deployable service
inside this monorepository.  
**Safety:** Historical research and paper analysis only. No broker orders, live
execution, or fabricated market prices.

## 1. Objective

Phase 11 converts the client's spreadsheet-style monthly analysis into a
repeatable historical replay. It tests a four-leg NIFTY calendar straddle using
CSV files downloaded manually from NSE's Historical Contract-wise Price Volume
Data page.

For each monthly cycle the replay will:

1. Sell the near-expiry CALL and PUT.
2. Buy the far-expiry CALL and PUT.
3. Use one common ATM strike and quantity for all four legs.
4. Value the position from official daily closing prices.
5. Exit all four legs on the first day the selected 3% or 4% target is reached.
6. Otherwise exit all four legs at the near expiry.
7. Remain flat after an early exit until that cycle's near expiry.
8. Start the next cycle on the first complete trading day after expiry, using a
   newly selected ATM strike.

This feature is a historical replay, not a price forecast or trading
recommendation.

## 2. Why a separate microservice

The existing application is an operational alert and paper-position system. It
uses small live requests, an APScheduler loop, Dhan market data, and the
`alerts.db` SQLite database. Phase 11 has a materially different workload:

- potentially large CSV uploads and normalization;
- immutable historical datasets;
- CPU-heavy multi-month replay jobs;
- daily time-series output and CSV export;
- different retention, storage, and scaling requirements;
- no need for Dhan authentication, notifications, or the alert scheduler.

Separating it protects the existing 273-test StraddleLab baseline and prevents a
large historical import from slowing stock alerts. The service can be deployed,
scaled, restarted, and migrated to PostgreSQL independently.

The boundary is a network API. The existing application must not import Phase 11
Python modules or write its database directly. A future Streamlit `Calendar
Replay` page calls the service through `CALENDAR_REPLAY_API_URL`.

## 3. Proposed monorepo layout

```text
stock_ticker/
├── app/                         # existing alert + StraddleLab service
├── services/
│   └── calendar_replay/
│       ├── calendar_replay/
│       │   ├── api.py           # FastAPI routes and request translation
│       │   ├── config.py        # service configuration
│       │   ├── domain.py        # immutable replay values
│       │   ├── ingestion.py     # NSE CSV normalization
│       │   ├── replay.py        # deterministic replay engine
│       │   ├── persistence.py   # service-owned repositories
│       │   └── export.py        # deterministic JSON/CSV output
│       ├── tests/
│       ├── Dockerfile
│       ├── requirements.txt
│       └── README.md
└── docs/
    └── PHASE_11_CALENDAR_REPLAY_MICROSERVICE.md
```

The exact package split may evolve during implementation, but these ownership
boundaries are mandatory:

- pure domain and replay code cannot import FastAPI, SQLAlchemy, Streamlit, Dhan,
  schedulers, or provider SDKs;
- the calendar service owns its database and uploaded datasets;
- the existing service communicates with it only through versioned HTTP routes;
- no code path may place a broker order.

## 4. User-data mapping

Phase 11 receives two categories of data: uploaded market history and user-entered
replay configuration.

### 4.1 NSE market-history input

| User/NSE field | Normalized field | Replay use |
|---|---|---|
| Symbol | `underlying` | Restrict the first version to NIFTY |
| Trade date | `trade_date` | Order daily observations and choose entry/exit rows |
| Expiry date | `expiry` | Identify near and far contracts |
| CE/PE | `option_type` | Normalize to CALL/PUT |
| Strike price | `strike` | Find the common ATM strike |
| Close | `close` | Official daily valuation price |
| Underlying Value | `underlying_close` | Select ATM and chart NIFTY movement |
| Open/High/Low/LTP/Settlement | matching optional fields | Preserve for review; never silently replace Close |

If `Underlying Value` is unavailable, the user must upload a separate NIFTY
historical-close CSV. The importer will normalize known NSE heading variants,
parse dates explicitly, use `Decimal` for prices, and reject duplicate
contract/date rows.

The logical identity of an option bar is:

```text
dataset + trade_date + underlying + expiry + strike + option_type
```

### 4.2 Replay configuration input

| User input | Stored configuration | Purpose |
|---|---|---|
| Initial entry date | `initial_entry_date` | Starts the first cycle |
| Near expiry | `initial_near_expiry` | CALL and PUT to sell |
| Far expiry | `initial_far_expiry` | CALL and PUT to buy |
| Capital | `capital_base` | Denominator for return percentage |
| Lot size | `lot_size` | Units per lot |
| Lots | `lots` | Position-size multiplier |
| Target | `target_percent` | Inclusive 3% or 4% exit threshold |
| Entry/exit costs | `round_trip_costs` | Strategy-level cycle costs |
| Optional strike | `initial_strike_override` | Manual valid strike instead of recommended ATM |

Derived quantity is `lot_size × lots`. Capital remains fixed between cycles so
monthly returns are comparable; profits are not compounded in this milestone.

## 5. Contract selection

On an entry date, the service will:

1. Read the NIFTY close.
2. Find strikes that contain all four required contracts on that date: near
   CALL, near PUT, far CALL, and far PUT.
3. Choose the strike with the smallest absolute distance from NIFTY.
4. Choose the lower strike on an exact distance tie.
5. Use a manual override only when all four contracts exist at that strike.

Contracts or premiums are never invented. A missing entry leg blocks that cycle.
Monthly expiries are discovered from the uploaded contracts rather than assumed
from a hard-coded weekday rule.

## 6. Deterministic replay calculations

For quantity `Q`, entry close `E`, and current close `C`:

```text
BUY leg P&L  = (C - E) × Q
SELL leg P&L = (E - C) × Q

gross_points   = sum of four signed premium movements before Q
gross_pnl      = gross_points × Q
net_pnl        = gross_pnl - round_trip_costs
return_percent = net_pnl ÷ capital_base × 100
```

The target comparison is inclusive:

```text
return_percent >= target_percent  =>  TARGET HIT
```

Target status and financial status are different fields. A `+2%` cycle against a
`3%` target is `TARGET MISSED` and `PROFIT`, not a target win.

### Acceptance example mapped from the client sheet

```text
Entry date:             29 June 2026
NIFTY close:            23,985
Common strike:          24,000
Near expiry:            24 August 2026
Far expiry:             29 September 2026
Lot size / quantity:    65 / 65

SELL near CALL:         505
SELL near PUT:          168
BUY far CALL:           738
BUY far PUT:            280
Premium credit:         673 points
Premium debit:          1,018 points
Net entry debit:        345 points = Rs 22,425
```

At the exact 3% boundary:

```text
Current near CALL:      405  -> SELL gain 100 points
Current near PUT:       100  -> SELL gain  68 points
Current far CALL:       838  -> BUY gain  100 points
Current far PUT:        300  -> BUY gain   20 points

Gross movement:         288 points
Gross P&L:              288 × 65 = Rs 18,720
Round-trip costs:       Rs 720
Net P&L:                Rs 18,000
Capital base:           Rs 600,000
Return:                 exactly 3%
Target result:          TARGET HIT
Financial result:       PROFIT
```

The entry debit is a cash-flow characteristic, not the final P&L. Daily P&L is
calculated from the signed movement of all four premiums.

## 7. Cycle and roll rules

- Evaluate complete dates in ascending order using end-of-day Close.
- Exit all four legs on the first target-hit date.
- If the target is never hit, exit all four legs using the near-expiry Close.
- Do not re-enter after an early target exit within the same monthly cycle.
- Start the following cycle on the first complete trading day after near expiry.
- Advance to the next two discovered monthly expiries and calculate a new ATM
  strike from that entry day's NIFTY close.
- Stop cleanly when another complete four-leg cycle cannot be formed.
- Do not carry an old leg into the next cycle.

Daily EOD data can identify the first closing date that meets the target, but not
the intraday time at which it may have been reached.

## 8. Missing and invalid data

Stable issue IDs will cover:

- required NSE columns missing;
- malformed number or date;
- duplicate option bar;
- missing underlying close;
- CALL or PUT missing;
- requested strike unavailable;
- near/far expiry ordering invalid;
- entry-date prices incomplete;
- intermediate date incomplete;
- near-expiry closing prices missing;
- next monthly cycle unavailable;
- capital, lot size, lots, target, or costs invalid.

An incomplete intermediate date appears as a visible gap and is excluded from
target detection. Missing entry data or required expiry-exit data blocks the
cycle. The service will not forward-fill, interpolate, or substitute another
price field silently.

## 9. Service-owned persistence

Phase 11 will not write to `alerts.db`. It owns tables equivalent to:

- `historical_dataset`;
- `historical_option_bar`;
- `historical_underlying_close`;
- `calendar_replay`;
- `calendar_replay_cycle`;
- `calendar_replay_point`.

Datasets and completed replay runs are immutable. Re-running a configuration
creates a new run instead of overwriting audit history. SQLite is acceptable for
local development; PostgreSQL and object storage are the production target for
durable uploaded datasets and concurrent runs.

## 10. Versioned API boundary

Proposed routes:

```text
GET  /v1/health
POST /v1/datasets
GET  /v1/datasets
GET  /v1/datasets/{dataset_id}
POST /v1/replays
GET  /v1/replays/{replay_id}
GET  /v1/replays/{replay_id}/cycles
GET  /v1/replays/{replay_id}/points
GET  /v1/replays/{replay_id}/export?format=json|csv
```

`POST /v1/datasets` accepts one or more manually downloaded NSE CSV files and
returns normalization/completeness results. `POST /v1/replays` references an
existing dataset and contains only replay configuration. Large runs may later be
queued behind the same API without changing the domain engine.

The existing Streamlit deployment can add a `Calendar Replay` page that calls
these routes. It must handle the service being unavailable without affecting
Stock Alerts or the existing StraddleLab workspace.

## 11. User-visible results

The Calendar Replay interface will show:

- uploaded dataset inventory and validation issues;
- available dates, expiries, strikes, and four-contract completeness;
- recommended ATM strike and any manual override;
- entry and exit premiums for every leg;
- monthly cycle summary;
- daily NIFTY Close chart;
- daily gross P&L, net P&L, and return charts;
- separate `TARGET HIT/MISSED` and `PROFIT/LOSS/FLAT` labels;
- exit date and `TARGET` or `NEAR_EXPIRY` reason;
- chart gaps for incomplete intermediate dates;
- deterministic JSON and CSV exports.

## 12. Verification and acceptance

The service receives an independent test suite covering:

- NSE heading aliases, CE/PE normalization, dates, Decimal prices, malformed
  rows, and duplicates;
- monthly-expiry discovery and four-contract intersection;
- BUY/SELL signs, quantity scaling, costs, and fixed-capital returns;
- exact 3% and 4% inclusive boundaries;
- ATM selection and lower-strike tie breaking;
- early exit, expiry fallback, flat waiting period, and monthly roll;
- target status independent from profit/loss status;
- visible intermediate gaps and blocking entry/expiry gaps;
- stopping when the next complete cycle is unavailable;
- service-owned persistence, API contracts, and deterministic JSON/CSV export.

Existing repository tests must continue to pass unchanged. Phase 11 tests must
use fixtures and temporary databases; they must not download NSE data, call Dhan,
send notifications, or place orders.

## 13. Delivery sequence

1. Scaffold the isolated service and pure domain models.
2. Implement and test NSE CSV normalization and completeness reporting.
3. Implement and test the deterministic single-cycle engine.
4. Add multi-cycle rolling and missing-data behavior.
5. Add service-owned persistence and versioned API routes.
6. Add deterministic export.
7. Integrate the existing Streamlit dashboard as an API client.
8. Add deployment configuration and complete regression verification.

Each step must be independently testable. UI work begins only after ingestion and
replay behavior are stable.

## 14. Deferred work

- automated NSE downloading;
- intraday target detection;
- live opportunity alerts;
- broker execution;
- compounding and portfolio allocation;
- detailed margin, tax, and brokerage models;
- Black-Scholes forecasts, probabilities, or AI recommendations;
- strike adjustments such as closing one PUT and selling another strike;
- multi-user authorization and production job infrastructure.

The client's PUT-adjustment example is not deterministic enough to implement yet;
it requires an explicit trigger, replacement-strike rule, quantity rule, and cost
treatment.
