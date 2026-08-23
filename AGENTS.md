# Repository Guidelines

## Project Structure & Module Organization

`main.py` starts the FastAPI backend and stock-alert scheduler; `dashboard.py` starts the Streamlit interface. Core stock-alert modules live in `app/` (`database.py`, `market_feed.py`, `price_checker.py`, `notifier.py`, and `scheduler.py`). StraddleLab is isolated under `app/straddle/`: pure domain logic and calculations sit beside orchestration, persistence, API, UI, and the Dhan adapter in `providers/`. Tests mirror these responsibilities in `tests/test_*.py`. Operational documentation is in `README.md`, `SETUP.md`, `STRADDLELAB_PHASES_0_10.md`, and `docs/`.

## Build, Test, and Development Commands

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env
uvicorn main:app --port 8600
streamlit run dashboard.py --server.port 8620
python -m pytest -q
python -m py_compile main.py dashboard.py app/straddle/*.py
```

Run the backend and dashboard in separate terminals. The test suite mocks notifications and market providers and uses temporary SQLite databases. Use the compile command as a quick syntax check. Set `API_URL` if the dashboard should use a backend on another port.

## Coding Style & Naming Conventions

Use Python 3 conventions, four-space indentation, type hints, and `snake_case` for modules, functions, and variables; use `PascalCase` for classes and enums. Keep StraddleLab domain functions deterministic: pass dates and timestamps explicitly, use `Decimal` for money, and avoid framework or provider imports in pure modules. No formatter or linter is configured, so follow the surrounding style and keep imports organized.

## Testing Guidelines

Use pytest. Name files `test_<area>.py` and tests `test_<behavior>`. Add focused unit tests for formulas and rules, plus boundary tests when changing API, persistence, or lifecycle behavior. Mock Dhan, yfinance, Whapi, Gmail, and clocks; tests must never send messages or place orders. Run `python -m pytest -q` before submitting changes.

## Commit & Pull Request Guidelines

Recent history generally follows Conventional Commits, such as `feat(straddle): add ...` and `fix(dashboard): ...`. Use an imperative, scoped subject. Pull requests should explain the behavior change, list verification commands, link relevant issues, and include screenshots for Streamlit changes. Call out schema, environment-variable, or API-contract changes explicitly.

## Security & Safety

Copy secrets only into `.env`; never commit tokens, credentials, `alerts.db`, or Dhan cache files. StraddleLab is paper trading only. Do not introduce broker order execution or fabricate unavailable option prices.
