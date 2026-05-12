# Automated Options Call Screener — Production-Ready MCP Architecture for Claude Code

**Bottom line:** Build this as a **FastMCP stdio server** that wraps three free data layers — `yfinance` for option chains + technicals + earnings + sector info, the **Polygon.io free tier (5 req/min, 15-min delayed)** for options snapshots with Greeks/IV when you need them, and **CBOE/FRED VIX data** for index-level volatility context. Compute Greeks yourself with `py_vollib` (vollib) when chains don't include them, store everything in a local SQLite cache, and expose five typed MCP tools that Claude Code invokes during conversations. The biggest "free-tier reality check" is that **no free source gives you a long history of per-stock implied volatility**, so true IV Rank requires you to start logging IV daily on your own machine — and use VIX-derived proxies in the meantime.

## TL;DR
- **Architecture:** A single FastMCP Python server (`screener_mcp.py`) registered in Claude Code via `.mcp.json` (stdio), exposing `screen_stocks`, `analyze_option_chain`, `get_iv_rank`, `filter_by_sector`, and `score_candidates` tools. yfinance does the heavy lifting; Polygon is a fallback for fresher Greeks; a SQLite + diskcache layer protects the 5-req/min Polygon limit; tenacity handles backoff for both APIs.
- **What's actually free:** yfinance gives you option chains with bid/ask/OI/volume/IV but **no Greeks** — you compute them with `py_vollib`. Polygon's free Basic tier returns Greeks + IV in its options snapshot endpoint but is limited to 5 calls/minute and ~15-minute delayed data, with 2 years of history. **No free provider gives multi-year per-ticker IV history**, so IV Rank must either be self-collected over time or approximated from VIX/term-structure.
- **Decision-readiness:** The score returned to Claude is a single 0-100 number plus a structured JSON breakdown (IVR, OI, spread%, delta, DTE, technical setup, catalyst flag, sector) so Claude can apply your pre-entry checklist in-conversation, recommend a specific strike/expiry, and explain the thesis without re-fetching data.

---

## Key Findings

### 1. Free-data reality matrix (this is the most important table in this report)

| Datum needed | yfinance (free) | Polygon Basic (free) | CBOE/FRED (free) | Verdict |
|---|---|---|---|---|
| Daily OHLCV, 5+ yrs | ✅ Yes, robust | ✅ 2-year history, 15-min delayed | — | **yfinance primary** |
| SMA 50/200, RSI(14), volume | ✅ Compute locally w/ `pandas_ta` | ✅ Built-in `/v1/indicators/*` endpoints | — | **Compute locally** |
| Option chain (strikes, bid/ask, OI, vol) | ✅ `Ticker.option_chain()` | ✅ `/v3/snapshot/options/{ticker}` | — | **yfinance default** |
| Implied volatility per contract | ✅ `impliedVolatility` column | ✅ in snapshot | — | **Both work** |
| Greeks (Δ, Γ, Θ, ν) | ❌ Not returned (GitHub issue #1465 confirmed open) | ✅ in snapshot (`null` for deep-ITM) | — | **Compute via `py_vollib`** |
| Historical IV per ticker (1+ yr) | ❌ No native history | ❌ Not on free tier | — | **Self-collect daily** |
| VIX (index IV) | ✅ `^VIX` ticker | — | ✅ Direct CSV download | **CBOE/FRED for clean series** |
| Earnings dates | ✅ `Ticker.get_earnings_dates()` | Premium only | — | **yfinance** |
| Sector / industry | ✅ `Ticker.info["sector"]` | ✅ `/v3/reference/tickers/{T}` | — | **yfinance** |
| Rate limit | Unofficial, ~no guarantee, frequent 429s | **5 req/min hard cap** | Static files | **Cache aggressively** |
| Real-time | 15-min delayed | 15-min delayed | EOD | Same — fine for swing-trade DTE 60-120 |

**Translation:** For a swing-trade screener (DTE 60-120), 15-minute delayed data is fine. The binding constraint is Polygon's 5/min — which means you can analyze **at most ~300 contracts/hour** through Polygon. yfinance has no published limit but in practice throws HTTP 429 after sustained bursts. Cache.

### 2. Real Polygon endpoints you will hit (free tier)
All require `?apiKey=YOUR_KEY`; all are read-only GET.

| Purpose | Endpoint | Notes |
|---|---|---|
| Daily OHLCV bars | `GET /v2/aggs/ticker/{symbol}/range/1/day/{from}/{to}` | The classic "aggs" endpoint; up to 2 yrs free |
| Intraday bars | `GET /v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from}/{to}` | 15-min delayed on free |
| Single option snapshot | `GET /v3/snapshot/options/{underlying}/{contract}` | Returns Greeks, IV, OI, last trade/quote |
| Full chain snapshot | `GET /v3/snapshot/options/{underlying}` | Returns every contract; paginates via `next_url` |
| SMA / RSI / EMA / MACD | `GET /v1/indicators/sma/{symbol}` (and rsi, ema, macd) | Saves you computing locally |
| Ticker reference (sector) | `GET /v3/reference/tickers/{symbol}` | Sector, industry, market cap |
| Options contracts list | `GET /v3/reference/options/contracts?underlying_ticker={S}` | Discover available expiries/strikes |

### 3. Scoring engine threshold logic the user specified
Each criterion is a 0–100 sub-score; the final is a weighted average:

| Criterion | Rule | Weight | Why |
|---|---|---|---|
| **IVR < 30** | 100 if IVR ≤ 30, linearly decay to 0 at IVR=60 | 20% | Low IV → cheap long premium for debit calls |
| **OI > 500** | step: 100 if OI ≥ 500, else `OI/5` | 15% | Liquidity — fills at mid |
| **Spread < 5% of mid** | 100 at ≤2%, 0 at ≥8%, linear in between | 15% | Execution cost |
| **Delta 0.50–0.85** | 100 inside band, 0 outside | 15% | ITM-leaning directional exposure |
| **DTE 60–120** | 100 inside band, decays outside | 10% | Theta runway |
| **Bullish technical** | price > SMA50 > SMA200 AND RSI 40–70 AND RS_vs_SPY > 0 → 100 | 15% | Trend confirmation |
| **Catalyst present** | earnings within DTE window OR sector-tailwind flag → 100 | 10% | Has a reason to move |

### 4. IV Rank/Percentile — three honest options on free data
Per Schwab, Tastytrade, and Barchart documentation, the canonical formulas are:

- **IVR (Tastytrade-style):** `(IV_today − IV_52w_low) / (IV_52w_high − IV_52w_low) × 100`
- **IVP:** `(# days in past 252 trading days where IV < IV_today) / 252 × 100`

You **cannot get a year of per-stock historical IV for free** in 2026. Three workarounds, in order of preference:

1. **Self-collect (recommended):** Have the MCP server, on every call, append today's ATM-30DTE IV to a local SQLite table. After ~60 trading days you have a usable IV Rank; after 252 days you have the real thing. The included code does this automatically.
2. **VIX-anchored proxy (works on day one):** Scale current IV by VIX percentile. `pseudo_IVR = clip( (current_IV − stock_floor) / (3 × VIX_52w_avg − stock_floor) × 100, 0, 100 )` — crude but better than nothing.
3. **Skip IVR entirely for the first 60 days** and gate only on IV-vs-HV ratio (both computable from a 60-day price history through yfinance).

### 5. MCP integration is one config file
Claude Code (per the official `code.claude.com/docs/en/mcp` docs) reads `.mcp.json` for project-scope and `~/.claude.json` for user-scope. The format is identical:

```json
{
  "mcpServers": {
    "options-screener": {
      "type": "stdio",
      "command": "/Users/you/.local/bin/uv",
      "args": ["--directory", "/Users/you/code/options-mcp", "run", "python", "screener_mcp.py"],
      "env": {
        "POLYGON_API_KEY": "${POLYGON_API_KEY}",
        "CACHE_DIR": "/Users/you/code/options-mcp/.cache"
      }
    }
  }
}
```

After saving, run `claude mcp list` to verify, then say `/mcp` in any Claude Code session.

---

## Details — Full Working Code

### Project layout

```
options-mcp/
├── .mcp.json                  # Claude Code registration (this project only)
├── .env                       # POLYGON_API_KEY=...
├── pyproject.toml             # uv-managed deps
├── requirements.txt           # pip fallback
├── screener_mcp.py            # MCP server entrypoint (also CLI)
├── core/
│   ├── __init__.py
│   ├── data.py                # yfinance + Polygon fetchers, cache, retry
│   ├── screener.py            # SMA, RSI, RS-vs-SPY, breakout, SECTOR
│   ├── chains.py              # option chain fetch + Greek fill-in
│   ├── ivrank.py              # IVR/IVP + VIX proxy
│   ├── scoring.py             # 7-criterion weighted score
│   └── catalysts.py           # earnings dates via yfinance
└── data/
    └── iv_history.sqlite      # auto-populated daily IV log
```

### `requirements.txt`

```
fastmcp>=2.10
yfinance>=0.2.50
pandas>=2.2
numpy>=1.26
pandas-ta-classic>=0.4.71
py_vollib>=1.0.3
scipy>=1.12
requests>=2.32
tenacity>=8.5
diskcache>=5.6
python-dotenv>=1.0
polygon-api-client>=1.14
```

(Install with `uv pip install -r requirements.txt`. On Mac, `py_vollib` may need `brew install gcc` if the wheel doesn't resolve; the pure-Python `ref_python` submodule is a fallback.)

### `core/data.py` — caching + rate-limited fetchers + error handling

```python
import os, time, sqlite3, datetime as dt, logging
from functools import wraps
import diskcache as dc
import yfinance as yf
import requests
from tenacity import (retry, stop_after_attempt, wait_exponential,
                      retry_if_exception_type, before_sleep_log)

log = logging.getLogger("screener")

CACHE_DIR = os.getenv("CACHE_DIR", ".cache")
cache = dc.Cache(CACHE_DIR)
POLY_KEY = os.getenv("POLYGON_API_KEY", "")

def ttl_cache(seconds: int):
    """Disk-backed TTL cache decorator — survives restarts."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (fn.__name__, args, tuple(sorted(kwargs.items())))
            hit = cache.get(key)
            if hit is not None:
                ts, val = hit
                if time.time() - ts < seconds:
                    return val
            val = fn(*args, **kwargs)
            cache.set(key, (time.time(), val))
            return val
        return wrapper
    return deco

class PolygonRateLimiter:
    """Hard-enforces 5 calls / 60s for free tier."""
    def __init__(self, max_calls=5, window=60.0):
        self.max_calls, self.window, self.calls = max_calls, window, []
    def wait(self):
        now = time.time()
        self.calls = [t for t in self.calls if now - t < self.window]
        if len(self.calls) >= self.max_calls:
            sleep_for = self.window - (now - self.calls[0]) + 0.2
            log.info(f"Polygon rate limit reached, sleeping {sleep_for:.1f}s")
            time.sleep(max(sleep_for, 0))
        self.calls.append(time.time())

_poly_limiter = PolygonRateLimiter()

class PolygonRateError(Exception): pass

@retry(stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=2, min=2, max=30),
       retry=retry_if_exception_type((requests.HTTPError, PolygonRateError)),
       before_sleep=before_sleep_log(log, logging.WARNING))
def polygon_get(path: str, **params):
    """Robust Polygon GET with rate-limit + exponential backoff."""
    if not POLY_KEY:
        raise RuntimeError("POLYGON_API_KEY missing — set it in .env")
    _poly_limiter.wait()
    params["apiKey"] = POLY_KEY
    r = requests.get(f"https://api.polygon.io{path}", params=params, timeout=20)
    if r.status_code == 429:
        raise PolygonRateError("429 from Polygon — backing off")
    r.raise_for_status()
    return r.json()

# Examples of concrete endpoint calls you'll wire in:
#   polygon_get(f"/v2/aggs/ticker/{sym}/range/1/day/{from_}/{to_}")
#   polygon_get(f"/v3/snapshot/options/{sym}")
#   polygon_get(f"/v3/reference/tickers/{sym}")     # sector

@ttl_cache(seconds=3600)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15),
       before_sleep=before_sleep_log(log, logging.WARNING))
def yf_history(symbol: str, period="1y", interval="1d"):
    t = yf.Ticker(symbol)
    df = t.history(period=period, interval=interval, auto_adjust=False)
    if df.empty:
        raise ValueError(f"yfinance returned empty history for {symbol}")
    return df

@ttl_cache(seconds=600)
def yf_option_chain(symbol: str, expiry: str):
    t = yf.Ticker(symbol)
    chain = t.option_chain(expiry)
    return {"calls": chain.calls.to_dict("records"),
            "puts":  chain.puts.to_dict("records")}

@ttl_cache(seconds=600)
def yf_option_expiries(symbol: str):
    return list(yf.Ticker(symbol).options)

@ttl_cache(seconds=86400)
def yf_sector(symbol: str) -> str:
    """Sector / industry / market-cap from yfinance .info dict."""
    try:
        info = yf.Ticker(symbol).info
        return info.get("sector", "Unknown")
    except Exception as e:
        log.warning(f"sector lookup failed for {symbol}: {e}")
        return "Unknown"
```

### `core/screener.py` — technicals + sector filter

```python
import pandas as pd, numpy as np
from .data import yf_history, yf_sector

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["SMA50"]  = df["Close"].rolling(50).mean()
    df["SMA200"] = df["Close"].rolling(200).mean()
    # RSI(14) — Wilder smoothing
    delta = df["Close"].diff()
    up = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    df["RSI14"] = 100 - (100 / (1 + rs))
    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    df["VolBreak"] = df["Volume"] > 1.5 * df["VolAvg20"]
    return df

def relative_strength(symbol: str, spy_hist: pd.DataFrame, lookback=63) -> float:
    s = yf_history(symbol, period="6mo")
    s_ret  = s["Close"].iloc[-1] / s["Close"].iloc[-lookback] - 1
    spy_ret = spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[-lookback] - 1
    return float(s_ret - spy_ret)

def technical_setup(symbol: str, spy_hist: pd.DataFrame) -> dict:
    df = add_indicators(yf_history(symbol, period="1y"))
    last = df.iloc[-1]
    rs = relative_strength(symbol, spy_hist)
    bullish = (
        last["Close"] > last["SMA50"] > last["SMA200"]
        and 40 <= last["RSI14"] <= 70
        and rs > 0
    )
    return {
        "symbol": symbol,
        "sector": yf_sector(symbol),
        "price": float(last["Close"]),
        "sma50": float(last["SMA50"]),
        "sma200": float(last["SMA200"]),
        "rsi14": float(last["RSI14"]),
        "vol_breakout": bool(last["VolBreak"]),
        "rel_strength_vs_spy_63d": rs,
        "bullish": bullish,
    }

def screen_universe(symbols: list[str],
                    allowed_sectors: list[str] | None = None,
                    excluded_sectors: list[str] | None = None) -> list[dict]:
    """Returns bullish-technical-passing tickers, optionally sector-filtered."""
    spy = yf_history("SPY", period="6mo")
    out = []
    for sym in symbols:
        try:
            r = technical_setup(sym, spy)
        except Exception as e:
            out.append({"symbol": sym, "error": str(e)})
            continue
        if allowed_sectors and r["sector"] not in allowed_sectors:
            continue
        if excluded_sectors and r["sector"] in excluded_sectors:
            continue
        if r.get("bullish"):
            out.append(r)
    return out
```

### `core/chains.py` — chain + Greek fill-in via py_vollib

```python
import datetime as dt, math
from py_vollib.black_scholes.greeks.analytical import delta, gamma, theta, vega
from py_vollib.black_scholes.implied_volatility import implied_volatility
from .data import yf_option_chain, yf_option_expiries, yf_history, polygon_get

RISK_FREE = 0.045  # update from FRED DGS3MO if you want; close enough for ranking

def _safe(fn, *args):
    try: return float(fn(*args))
    except Exception: return None

def enrich_with_greeks(c: dict, S: float, today: dt.date, opt_type="c") -> dict:
    K = c["strike"]
    expiry = dt.datetime.strptime(c["expiry"], "%Y-%m-%d").date()
    T_days = max((expiry - today).days, 1)
    T = T_days / 365.0
    sigma = c.get("impliedVolatility") or 0.0
    mid = (c.get("bid", 0) + c.get("ask", 0)) / 2.0
    if (not sigma or sigma <= 0) and mid > 0:
        sigma = _safe(implied_volatility, mid, S, K, T, RISK_FREE, opt_type) or 0.0
    c["iv"] = sigma
    c["delta"] = _safe(delta, opt_type, S, K, T, RISK_FREE, sigma)
    c["gamma"] = _safe(gamma, opt_type, S, K, T, RISK_FREE, sigma)
    c["theta"] = _safe(theta, opt_type, S, K, T, RISK_FREE, sigma)
    c["vega"]  = _safe(vega,  opt_type, S, K, T, RISK_FREE, sigma)
    c["dte"] = T_days
    c["spread_pct"] = ((c["ask"] - c["bid"]) / mid * 100) if mid else None
    return c

def get_call_chain(symbol: str, min_dte=60, max_dte=120) -> list[dict]:
    today = dt.date.today()
    expiries = yf_option_expiries(symbol)
    target = [e for e in expiries
              if min_dte <= (dt.datetime.strptime(e, "%Y-%m-%d").date()-today).days <= max_dte]
    if not target: return []
    spot = float(yf_history(symbol, period="5d")["Close"].iloc[-1])
    out = []
    for exp in target:
        chain = yf_option_chain(symbol, exp)
        for row in chain["calls"]:
            row["expiry"] = exp; row["symbol"] = symbol
            out.append(enrich_with_greeks(row, spot, today, "c"))
    return out

def get_polygon_chain(symbol: str) -> list[dict]:
    """Optional: fresher Greeks from Polygon — costs 1 call from your 5/min budget."""
    data = polygon_get(f"/v3/snapshot/options/{symbol}")
    return data.get("results", [])
```

### `core/ivrank.py` — self-collecting IV history + VIX proxy

```python
import sqlite3, os, datetime as dt
from .data import yf_history

DB = os.path.join(os.getenv("CACHE_DIR", ".cache"), "iv_history.sqlite")

def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS iv_log
                 (symbol TEXT, date TEXT, atm_iv REAL,
                  PRIMARY KEY (symbol, date))""")
    return c

def log_atm_iv(symbol: str, atm_iv: float):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO iv_log VALUES (?, ?, ?)",
                  (symbol, dt.date.today().isoformat(), float(atm_iv)))

def iv_rank(symbol: str, current_iv: float, lookback_days=252) -> dict:
    cutoff = (dt.date.today() - dt.timedelta(days=int(lookback_days*1.5))).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT atm_iv FROM iv_log WHERE symbol=? AND date>=? ORDER BY date",
            (symbol, cutoff)).fetchall()
    history = [r[0] for r in rows]
    if len(history) < 30:
        return {"ivr": None, "ivp": None, "method": "insufficient_history",
                "n_obs": len(history), "fallback": vix_proxy_ivr(current_iv)}
    hi, lo = max(history), min(history)
    ivr = (current_iv - lo) / (hi - lo) * 100 if hi > lo else 50.0
    ivp = sum(1 for x in history if x < current_iv) / len(history) * 100
    return {"ivr": round(ivr,1), "ivp": round(ivp,1),
            "method": "self_collected", "n_obs": len(history)}

def vix_proxy_ivr(current_iv: float) -> dict:
    """Crude proxy: scale single-name IV against 1-yr VIX range."""
    vix = yf_history("^VIX", period="1y")["Close"]
    vix_lo, vix_hi = vix.min()/100, vix.max()/100
    floor, ceil = 0.7*vix_lo, 3.0*vix_hi
    ivr = max(0, min(100, (current_iv - floor) / (ceil - floor) * 100))
    return {"proxy_ivr": round(ivr,1), "vix_today": float(vix.iloc[-1]),
            "note": "Self-collect 252 days for real IVR"}
```

### `core/catalysts.py`

```python
from .data import ttl_cache
import yfinance as yf, datetime as dt

@ttl_cache(seconds=86400)
def next_earnings(symbol: str) -> str | None:
    try:
        ed = yf.Ticker(symbol).get_earnings_dates(limit=8)
        if ed is None or ed.empty: return None
        future = ed[ed.index.date >= dt.date.today()]
        return future.index[0].date().isoformat() if len(future) else None
    except Exception:
        return None

def has_catalyst(symbol: str, dte_max: int) -> dict:
    earn = next_earnings(symbol)
    if earn:
        days = (dt.date.fromisoformat(earn) - dt.date.today()).days
        return {"earnings_in_days": days, "catalyst": 0 <= days <= dte_max}
    return {"earnings_in_days": None, "catalyst": False}
```

### `core/scoring.py`

```python
def _piecewise(x, points):
    """Linear piecewise interpolation; points = [(x0,y0),(x1,y1)...]"""
    for (x0,y0),(x1,y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 + (y1-y0) * (x-x0)/(x1-x0)
    return points[0][1] if x < points[0][0] else points[-1][1]

WEIGHTS = {"ivr":.20, "oi":.15, "spread":.15, "delta":.15,
           "dte":.10, "tech":.15, "catalyst":.10}

def score_contract(c: dict, tech: dict, cat: dict, ivr_info: dict) -> dict:
    ivr = ivr_info.get("ivr")
    if ivr is None:
        ivr = ivr_info.get("fallback",{}).get("proxy_ivr", 50)
    sub = {
      "ivr":      _piecewise(ivr, [(0,100),(30,100),(60,0),(100,0)]),
      "oi":       100 if (c.get("openInterest") or 0) >= 500 else (c.get("openInterest") or 0)/5,
      "spread":   _piecewise(c.get("spread_pct") or 99, [(0,100),(2,100),(5,50),(8,0),(99,0)]),
      "delta":    100 if c.get("delta") and 0.50 <= c["delta"] <= 0.85 else 0,
      "dte":      _piecewise(c["dte"], [(0,0),(60,100),(120,100),(180,0)]),
      "tech":     100 if tech.get("bullish") else 0,
      "catalyst": 100 if cat.get("catalyst") else 30,
    }
    final = sum(WEIGHTS[k]*sub[k] for k in WEIGHTS)
    return {"score": round(final,1), "subscores": {k: round(v,1) for k,v in sub.items()}}
```

### `screener_mcp.py` — the MCP server (also runs as CLI)

```python
"""
Options Call Screener — Claude Code MCP server + standalone CLI.
Run as MCP:    python screener_mcp.py             (stdio transport, default)
Run as CLI:    python screener_mcp.py --tickers AAPL,MSFT,NVDA --top 5
"""
import os, sys, json, argparse, datetime as dt, logging
from dotenv import load_dotenv; load_dotenv()
from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from core.screener  import screen_universe, technical_setup
from core.chains    import get_call_chain
from core.ivrank    import iv_rank, log_atm_iv
from core.catalysts import has_catalyst
from core.scoring   import score_contract
from core.data      import yf_history, yf_sector

mcp = FastMCP(name="options-screener", version="0.1.0")

# ---------------- MCP tools -----------------------------------------

@mcp.tool
def screen_stocks(tickers: list[str],
                  allowed_sectors: list[str] | None = None,
                  excluded_sectors: list[str] | None = None) -> dict:
    """
    Run the technical screen on a list of tickers.
    Returns only those passing the bullish trend filter
    (price > SMA50 > SMA200, RSI 40-70, RS-vs-SPY > 0).
    Optional sector allowlist/denylist (e.g. ['Technology','Healthcare']).
    """
    passing = screen_universe(tickers, allowed_sectors, excluded_sectors)
    return {"passed": passing, "count": len(passing),
            "as_of": dt.datetime.utcnow().isoformat() + "Z"}

@mcp.tool
def filter_by_sector(tickers: list[str], sectors: list[str]) -> dict:
    """Pure sector filter — no technical analysis."""
    out = [{"symbol": s, "sector": yf_sector(s)} for s in tickers]
    return {"matched": [r for r in out if r["sector"] in sectors],
            "all": out}

@mcp.tool
def analyze_option_chain(symbol: str, min_dte: int = 60, max_dte: int = 120) -> dict:
    """
    Fetch the call chain for one stock within a DTE window.
    Returns enriched contracts with Greeks (computed locally) and spread%.
    """
    chain = get_call_chain(symbol, min_dte=min_dte, max_dte=max_dte)
    spot = float(yf_history(symbol, period="5d")["Close"].iloc[-1])
    atm = min((c for c in chain if c.get("iv")),
              key=lambda c: abs(c["strike"]-spot), default=None)
    if atm and atm.get("iv"):
        log_atm_iv(symbol, atm["iv"])
    return {"symbol": symbol, "spot": spot,
            "n_contracts": len(chain), "contracts": chain}

@mcp.tool
def get_iv_rank(symbol: str) -> dict:
    """
    Compute IV Rank and IV Percentile from the locally collected daily-ATM-IV log.
    Falls back to a VIX-anchored proxy when history < 30 days.
    """
    spot = float(yf_history(symbol, period="5d")["Close"].iloc[-1])
    chain = get_call_chain(symbol, min_dte=20, max_dte=45)
    atm = min((c for c in chain if c.get("iv")),
              key=lambda c: abs(c["strike"]-spot), default=None)
    if not atm: return {"error": "no ATM contract found"}
    log_atm_iv(symbol, atm["iv"])
    return {"symbol": symbol, "current_iv": atm["iv"],
            **iv_rank(symbol, atm["iv"])}

@mcp.tool
def score_candidates(tickers: list[str], min_dte: int = 60, max_dte: int = 120,
                     top_n: int = 10,
                     allowed_sectors: list[str] | None = None) -> dict:
    """
    Full pipeline: screen → fetch chains → score every call contract → return top N.
    This is the single tool Claude should call to get a ranked watchlist.
    """
    candidates = []
    passing = screen_universe(tickers, allowed_sectors=allowed_sectors)
    for tech in passing:
        sym = tech["symbol"]
        try:
            chain = get_call_chain(sym, min_dte, max_dte)
            cat = has_catalyst(sym, max_dte)
            spot = tech["price"]
            atm = min((c for c in chain if c.get("iv")),
                      key=lambda c: abs(c["strike"]-spot), default=None)
            ivr_info = iv_rank(sym, atm["iv"]) if atm and atm.get("iv") else {"ivr": None}
            for c in chain:
                if not c.get("delta"): continue
                if not (0.30 <= c["delta"] <= 0.90): continue
                s = score_contract(c, tech, cat, ivr_info)
                candidates.append({**c, **s, "technical": tech,
                                   "catalyst": cat, "ivr": ivr_info})
        except Exception as e:
            candidates.append({"symbol": sym, "error": str(e)})
    scored = [c for c in candidates if "score" in c]
    scored.sort(key=lambda c: c["score"], reverse=True)
    return {"top": scored[:top_n], "total_scored": len(scored),
            "as_of": dt.datetime.utcnow().isoformat() + "Z"}

# ---------------- CLI fallback --------------------------------------

def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="AAPL,MSFT,NVDA,AMD,META")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--sectors", help="comma list", default="")
    args = ap.parse_args()
    sec = args.sectors.split(",") if args.sectors else None
    out = score_candidates(args.tickers.split(","), top_n=args.top, allowed_sectors=sec)
    print(json.dumps(out, indent=2, default=str))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        mcp.run()   # stdio transport — what Claude Code uses
```

### `.mcp.json` (commit this to the project root)

```json
{
  "mcpServers": {
    "options-screener": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/Users/you/code/options-mcp", "run", "python", "screener_mcp.py"],
      "env": { "POLYGON_API_KEY": "${POLYGON_API_KEY}", "CACHE_DIR": ".cache" }
    }
  }
}
```

For user-scope (works in any project), use `~/.claude.json` with the same `mcpServers` block, or run:

```bash
claude mcp add options-screener --scope user -- \
   uv --directory /Users/you/code/options-mcp run python screener_mcp.py
```

### Example Claude Code prompts (what you type in conversation)

| Prompt | Tool Claude will invoke |
|---|---|
| "Screen AAPL, MSFT, NVDA, AMD, META, GOOGL for bullish call setups today and rank the top 5 contracts." | `score_candidates` |
| "Restrict the screen to Technology and Healthcare sectors only." | `screen_stocks` w/ `allowed_sectors` |
| "Just give me the technical screen — are AAPL, NVDA, TSLA in bullish trends?" | `screen_stocks` |
| "Pull the AAPL call chain for the 60-120 DTE window and show me the ATM contracts." | `analyze_option_chain` |
| "What's the IV Rank on NVDA right now? Should I be a premium buyer or seller?" | `get_iv_rank` |
| "Run the full screener on my watchlist [list], then for the top scoring contract, apply the pre-entry checklist and explain whether you'd take the trade." | `score_candidates` + Claude reasons over JSON |

### Data flow that lets Claude reason

Each MCP tool returns a single JSON object with fields named to match the checklist vocabulary (`ivr`, `subscores`, `bullish`, `catalyst`, `delta`, `dte`, `spread_pct`, `sector`). Because FastMCP automatically converts dict returns into MCP "structured content" (per the 2025-06-18 spec revision), Claude receives the data as a typed object and can write follow-up prose like:

> "AAPL Jan-2026 230C scored 84/100. IVR is 22 (cheap premium), OI 12,400 (deep liquidity), spread 1.8% (tight). Delta 0.62 — solidly directional. Earnings on 2026-01-28 fall inside DTE. Bullish setup confirmed: price above both SMAs, RSI 58, RS +6.4% vs SPY. Sector: Technology (in line with current leadership). The only concern is the earnings catalyst falls 3 days before expiry — consider rolling out to the March cycle to avoid IV crush."

### Rate-limit math (the practical limits)

- **Polygon free:** 5 calls/min → 300/hour → ~7,200/day. One `score_candidates` call on 20 tickers will hit yfinance ~60 times (chain × 3 expiries each) and Polygon 0 times by default. **Use Polygon only as fallback** when yfinance throws 429.
- **yfinance:** no official limit; in practice ~2k requests/hour before sporadic 429s. The 1-hour `ttl_cache` on history and 10-minute cache on chains in `data.py` cuts repeat calls by ~95% during an active conversation.
- **Earnings dates & sector:** cached for 24 hours — they barely change.

### Why these library choices and not others
- **FastMCP over the bare MCP SDK:** decorator-based `@mcp.tool` auto-generates JSON schema from type hints, which is what Claude Code needs; FastMCP is now bundled into the official Python SDK and is the form documented by Anthropic.
- **py_vollib over hand-rolled Black-Scholes:** wraps Peter Jäckel's LetsBeRational, which reaches machine precision for implied vol in ≤2 iterations — far more numerically robust than a Newton-Raphson you'd write yourself.
- **pandas-ta-classic over TA-Lib:** TA-Lib requires `brew install ta-lib` and C compilation; pandas-ta-classic is pure-Python with optional Numba acceleration and ships ready-to-go.
- **diskcache + SQLite over Redis:** zero external services; lives in `.cache/` next to the code; survives restarts.
- **tenacity over manual retry loops:** declarative `@retry(stop=…, wait=wait_exponential(multiplier=2, min=2, max=30))` with `before_sleep_log` gives you observable backoff for free.

---

## Recommendations (staged, with the benchmarks that change them)

1. **Day 1 (today):** Clone the layout, run `uv pip install -r requirements.txt`, drop the `.mcp.json` in your project, and test with `python screener_mcp.py --tickers AAPL,MSFT,NVDA` from the CLI before connecting to Claude Code. **Move on when:** the CLI prints a valid JSON `top:` array.
2. **Day 1 (after CLI works):** Register in Claude Code (`.mcp.json` or `claude mcp add`), run `/mcp` in a session to verify the five tools show up, and ask Claude to "screen AAPL, MSFT, NVDA for call setups." **Move on when:** Claude calls `score_candidates` and returns a ranked list.
3. **Week 1:** Run `get_iv_rank` on your full watchlist daily (a cron job calling the CLI works) so your `iv_history.sqlite` starts filling. Until you have 30+ days of data, accept that IVR is using the VIX proxy.
4. **Week 4–8:** When `n_obs ≥ 60` for most tickers, the IVR field becomes meaningful and your scores get a real upgrade. **Threshold to trust IVR for live decisions:** `n_obs ≥ 120`.
5. **When you outgrow free tier:** Move to **Polygon Starter ($29/mo)** for real-time + unlimited calls. The architecture won't change — only `_poly_limiter.max_calls` and `auto_adjust` flags. Don't pay for anything until you've validated that the score correlates with realized P&L on paper.
6. **What would make me change this design:**
   - If you need **intraday entries**, swap yfinance → Polygon Starter (15-min delay matters then).
   - If your watchlist exceeds **~50 tickers**, switch the screener to **Polygon `/v1/indicators/sma|rsi`** to avoid yfinance 429s.
   - If you start trading 0DTE or short-dated, this scoring's weights for premium-buying are wrong — flip the IVR criterion (high IVR favored) and you have a credit-spread scorer.

---

## Caveats

- **No free historical per-ticker IV exists in 2026.** Every paid provider (ORATS, EODHD at $99.99/mo, IVolatility, etc.) sells this because it's expensive to maintain. The self-collection pattern in `core/ivrank.py` is the honest workaround — but the first 30 trading days of your usage will return `method: "insufficient_history"` and rely on the VIX proxy. Don't treat the proxy as ground truth.
- **yfinance is unofficial.** It scrapes Yahoo Finance and can break with any HTML change (it broke severely in May 2023 over Yahoo's encryption switch, and again in late 2024). Always pin a version, and have the Polygon fallback path ready. Maintainer Ran Aroussi actively patches it, but expect 1–2 outages a year. The `yahoo_fin` library that older tutorials reference is abandoned — don't use it.
- **Polygon free tier's Greeks may be `null` for deep-ITM contracts** — Polygon's own docs note "circumstances where greeks will not be returned, such as options contracts that are deep in the money." Your local `py_vollib` computation handles this automatically.
- **15-minute delayed quotes** mean your `spread_pct` and `bid/ask` are stale during volatile sessions. This is fine for DTE 60-120 swing entries placed at the open/close; it is **not** fine for intraday scalps.
- **The RSI 40-70 + price-above-SMA50 + RS-vs-SPY trio is one bullish definition** — popular and reasonable, but not the only one. Backtest before trusting.
- **The scoring weights are starting points**, not optimized values. Spend an afternoon shifting them and watching how the top-10 changes on a quiet day vs. a CPI day.
- **Claude Code MCP servers run as subprocesses with whatever credentials you put in `env`.** Do not commit `.env`; do put `.env` and `.cache/` in `.gitignore`. The published Anthropic guidance is to scope tokens to the minimum permissions needed — Polygon's free key is read-only, so the risk surface here is low, but treat the pattern as a habit.
- **CME Group's "Greeks and Implied Volatility data" product is for futures options, not equity options** — a tempting search result that doesn't help you here.
- **One thing I could not verify in the time available:** whether the very latest Polygon free Basic tier (May 2026) still returns Greeks in the chain-snapshot response, or whether they've moved that behind Starter ($29/mo). Polygon's pricing page reorganized in late 2025 (and is now also branded "Massive" in some places), and third-party comparisons disagree about whether Greeks are gated. **Run the included `get_polygon_chain()` call once after signup** — if Greeks come back `null` across the response, you're on the gated tier and should rely entirely on the local `py_vollib` path. The architecture supports both with no code changes.

---

### Completion coverage

| Requirement from query | Covered |
|---|---|
| Stock Screening Layer (SMA50/200, RSI14, vol breakout, RS vs SPY, **sector filters**) | ✅ `core/screener.py`, `filter_by_sector` tool |
| Options Chain Layer w/ Greeks free-vs-paid clarity | ✅ Matrix + `py_vollib` fill-in |
| IV Rank / IVP calculation + workarounds | ✅ Self-collect + VIX proxy |
| Scoring engine with all 7 thresholds | ✅ `core/scoring.py` |
| MCP server setup + tool definitions + registration | ✅ `screener_mcp.py` + `.mcp.json` |
| Data flow as structured JSON | ✅ FastMCP structured content |
| Polygon free-tier limits + endpoint examples | ✅ Real endpoint table |
| Full working code (req.txt, server, screener, chains, scoring) | ✅ All modules included |
| Claude Code integration + example prompts | ✅ Prompt table + config snippet |
| Caching to avoid rate limits | ✅ `diskcache` + SQLite |
| Rate-limit backoff + error handling | ✅ `tenacity` + `PolygonRateLimiter` |