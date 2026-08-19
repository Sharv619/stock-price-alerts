"""Normalized market prices: DhanHQ for NSE equities, yfinance otherwise."""

import csv
import io
import json
import logging
from pathlib import Path

import httpx
import yfinance as yf

from app import dhan_auth

logger = logging.getLogger(__name__)
INSTRUMENT_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
LTP_URL = "https://api.dhan.co/v2/marketfeed/ltp"
ROOT = Path(__file__).resolve().parent.parent
INSTRUMENT_FILE = ROOT / ".dhan_instruments.csv"
MAP_FILE = ROOT / ".dhan_instrument_cache.json"
_symbols = None


def _symbol_key(symbol):
    return symbol.strip().upper()


def _load_instruments():
    global _symbols
    if _symbols is not None:
        return _symbols
    if MAP_FILE.exists():
        try:
            _symbols = json.loads(MAP_FILE.read_text())
            return _symbols
        except (OSError, ValueError):
            pass
    if not INSTRUMENT_FILE.exists():
        if not dhan_auth.dhan_status()["configured"]:
            return {}
        try:
            response = httpx.get(INSTRUMENT_URL, timeout=60)
            response.raise_for_status()
            INSTRUMENT_FILE.write_bytes(response.content)
        except Exception as exc:
            logger.warning("Dhan instrument metadata unavailable: %s", exc)
            return {}
    try:
        rows = csv.DictReader(io.StringIO(INSTRUMENT_FILE.read_text(errors="replace")))
        result = {}
        for row in rows:
            segment = (row.get("SEGMENT") or row.get("SEM_SEGMENT") or "").upper()
            exchange = (row.get("EXCH_ID") or row.get("SEM_EXM_EXCH_ID") or "").upper()
            name = row.get("SEM_TRADING_SYMBOL") or row.get("TRADING_SYMBOL") or row.get("SEM_CUSTOM_SYMBOL") or ""
            security = row.get("SEM_SMST_SECURITY_ID") or row.get("SECURITY_ID") or row.get("securityId")
            if exchange == "NSE" and segment in ("E", "EQ", "EQUITY", "NSE_EQ") and name and security:
                result[name.upper()] = str(security)
        _symbols = result
        try:
            MAP_FILE.write_text(json.dumps(result))
        except OSError:
            pass
        return result
    except (OSError, csv.Error) as exc:
        logger.warning("Could not parse Dhan instrument metadata: %s", exc)
        return {}


def _yahoo(symbol):
    try:
        price = yf.Ticker(symbol).fast_info.last_price
        return float(price) if price is not None else None
    except Exception as exc:
        logger.warning("yfinance price fetch failed for %s: %s", symbol, exc)
        return None


def get_prices(symbols):
    """Return one normalized price mapping, batching all Dhan NSE IDs."""
    symbols = list(dict.fromkeys(_symbol_key(s) for s in symbols))
    result = {s: None for s in symbols}
    nse = [s for s in symbols if s.endswith(".NS")]
    dhan_map = _load_instruments() if nse else {}
    ids = {s: dhan_map.get(s[:-3]) for s in nse}
    unresolved = set(nse)
    if ids and any(ids.values()):
        try:
            token = dhan_auth.get_access_token()
            payload = {"NSE_EQ": [int(v) for v in ids.values() if v]}
            headers = {"access-token": token, "client-id": dhan_auth._config()[0], "Content-Type": "application/json"}
            response = httpx.post(LTP_URL, headers=headers, json=payload, timeout=30)
            if response.status_code == 401:
                token = dhan_auth.get_access_token(force_refresh=True)
                headers["access-token"] = token
                response = httpx.post(LTP_URL, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json().get("data", {}).get("NSE_EQ", {})
            for symbol, security in ids.items():
                raw = data.get(str(security), {}) if security else {}
                try:
                    if raw.get("last_price") is not None:
                        result[symbol] = float(raw["last_price"])
                        unresolved.discard(symbol)
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            logger.warning("Dhan LTP request failed: %s", exc)
    for symbol in symbols:
        if symbol not in unresolved and result[symbol] is not None:
            continue
        result[symbol] = _yahoo(symbol)
    return result


def get_price(symbol):
    return get_prices([symbol]).get(_symbol_key(symbol))
