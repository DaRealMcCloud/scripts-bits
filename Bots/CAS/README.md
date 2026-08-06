# Trading Bot

A broker-neutral, self-healing algorithmic trading bot for US equities (and
crypto where supported). It supports two brokers out of the box:

- **Alpaca** — works immediately with a free paper-trading account.
- **Interactive Brokers (IBKR)** — via the local **Client Portal Gateway** Web API.

The trading logic (strategies, risk, execution, analysis) depends only on a
neutral `BrokerClient` interface, so switching brokers is a one-line config
change.

---

## Features

- **Two brokers, one codebase** — pick with `broker.provider: alpaca | ibkr`.
- **Protective stops on every entry** — native bracket orders where supported,
  a standalone follow-up stop otherwise.
- **Crash-safe** — every order/equity snapshot is persisted to SQLite, and a
  supervisor process restarts the trader (with backoff) if it dies or stalls.
- **Decision-support layer** — a signal *aggregator* scores and ranks candidate
  trades across all enabled strategies (`strength × confidence × weight ×
  regime_fit`) and routes by detected market regime.
- **Rich strategy family** — momentum, gap-fade, quote-imbalance, trend-
  following, mean-reversion, breakout, volatility, pairs, DCA, rebalancing, and
  a pluggable ML slot.
- **Backtest engine** — replay daily bars through the momentum + ATR-stop logic.
- **Local web dashboard** — live equity curve, performance metrics, and
  start/stop/restart controls (optional).

---

## Install

Requires Python **3.11+**.

```powershell
# Core install
pip install -e .

# Choose the broker adapter you need:
pip install -e ".[alpaca]"     # Alpaca SDK
#   (IBKR needs no extra package — it uses the HTTP gateway)

# Optional extras
pip install -e ".[web]"        # web dashboard (FastAPI + Uvicorn)
pip install -e ".[ml]"         # scikit-learn ML strategy backend
pip install -e ".[dev]"        # pytest for the test suite

# full install
pip install -e ".[all]"
```


Copy the example config and edit it:

```powershell
Copy-Item config.example.yaml config.yaml
```

`config.yaml` is git-ignored — never commit secrets.

Then set your credentials. The easiest way is a `.env` file (loaded
automatically on startup via `python-dotenv`):

```powershell
Copy-Item .env.example .env
# edit .env and fill in ALPACA_API_KEY / ALPACA_SECRET_KEY
```

`.env` is git-ignored. Real shell environment variables always override `.env`,
which in turn overrides anything inlined in `config.yaml`.

---

## Broker setup

### Alpaca (default, easiest)

Set your paper keys — either in `.env` (recommended, see above) or as shell
environment variables — and leave `broker.provider: alpaca`:

```powershell
# Option A: .env file (persists, git-ignored)
#   ALPACA_API_KEY=PK...
#   ALPACA_SECRET_KEY=...

# Option B: current shell session only
$env:ALPACA_API_KEY    = "PK..."
$env:ALPACA_SECRET_KEY = "..."
```

`alpaca.mode: paper` trades against the paper endpoint out of the box.

### Interactive Brokers (IBKR)

IBKR trading goes through the **Client Portal Gateway** running locally. This is
a prerequisite you start and log into yourself — the bot never handles your IBKR
password or 2FA.

1. Download and run the Client Portal Gateway (`bin/run.bat root/conf.yaml`).
2. Open <https://localhost:5000> in a browser and log in (complete 2FA).
   Use a **paper account** (id begins with `DU`) for testing.
3. Keep that session alive; the bot tickles it automatically once running.
4. Point the bot at IBKR:

   ```yaml
   broker:
     provider: ibkr
   ```

   or set `BROKER_PROVIDER=ibkr`. Optionally set `IBKR_ACCOUNT_ID` (otherwise the
   first account from `/portfolio/accounts` is used) and `IBKR_BASE_URL`.

> IBKR has no bulk "all tradable US equities" endpoint, so you **must** list the
> symbols you want to trade under `universe.stocks`.

If the gateway is not logged in, the bot exits with code `2`; the supervisor
recognises this and backs off before retrying, so it recovers automatically once
you log in.

---

## Running

### Recommended: run under the supervisor

The supervisor keeps the trader alive (restart-on-crash, restart-on-stall) and
hosts the optional web dashboard:

```powershell
python -m trader.supervisor
```

### Run the trader directly (no watchdog)

```powershell
python -m trader.main
```

---

## Web dashboard

Enable it in `config.yaml` and install the `web` extra:

```yaml
web:
  enabled: true
  host: 127.0.0.1
  port: 8787
```

Then run the supervisor and open <http://127.0.0.1:8787>. It shows the live
equity curve, total return, annualised return, transaction count/last trade,
broker + uptime, and provides **Restart** / **Stop** buttons.

> The dashboard binds to loopback only. Do **not** expose it publicly without an
> authenticating reverse proxy in front.

---

## Backtesting

```powershell
python -m trader.backtest --symbols AAPL,MSFT,NVDA --days 365
```

Prints total return, number of trades, and win rate. The backtester reuses the
exact indicator / ATR code paths used in live trading.

---

## Configuration reference

See `config.example.yaml` for the fully-commented schema. Highlights:

| Section        | Purpose                                                        |
| -------------- | -------------------------------------------------------------- |
| `broker`       | Selects `alpaca` or `ibkr`.                                    |
| `alpaca`       | Paper/live mode and data feed.                                 |
| `ibkr`         | Gateway URL, account, keepalive, rate limit, dialog suppress.  |
| `universe`     | `stocks` (required for IBKR), `etfs`, `crypto`, volume filter. |
| `strategies`   | Per-strategy toggles/params + the `aggregator` decision brain. |
| `risk`         | Risk-per-trade, position limits, drawdown breaker, ATR stops.  |
| `web`          | Local dashboard host/port.                                     |

### Trading all available crypto

`universe.crypto` accepts an explicit list of pairs (e.g. `BTC/USD`, `ETH/USD`)
or the single sentinel `ALL/ALL` (case-insensitive). With `ALL/ALL`, the bot
auto-discovers **every tradable crypto pair** from the broker (Alpaca only —
IBKR has no crypto support) and trades all of them:

```yaml
universe:
  crypto:
    - ALL/ALL
  crypto_quote: USD        # only keep pairs quoted in this currency (dedupes USDT/USDC/BTC)
  min_crypto_volume: 0     # optional avg-daily-volume floor; 0 = off
```

- `crypto_quote` (default `USD`) filters discovered pairs to one quote currency,
  removing near-duplicates like `BTC/USDT`. Explicit lists are never filtered.
- `min_crypto_volume` (default `0`, off) applies an average-daily-volume floor to
  discovered pairs only. Crypto volume is in base-currency units (not shares), so
  magnitudes differ from the equity `min_avg_volume` — tune with care.

Environment variables always override file values: `ALPACA_API_KEY`,
`ALPACA_SECRET_KEY`, `BROKER_PROVIDER`, `IBKR_ACCOUNT_ID`, `IBKR_BASE_URL`. These
can be set in your shell or in a `.env` file (see `.env.example`), which the bot
loads automatically on startup.

### Crypto strategies (long-only, 24/7)

Crypto trades around the clock, so it runs on its own schedule and rule set,
separate from the equity strategies. On Alpaca, crypto is **spot/cash only**:

- **No shorting** — every crypto strategy is long-only; shorts are blocked at
  execution.
- **No broker-side stops or brackets**, and orders must be **GTC** (DAY is
  rejected). Protective stops are therefore enforced **in software** by the
  crypto scan loop, which market-closes a position when price hits its stop.
- **Fractional sizing** — a position can be a fraction of one unit (e.g. a
  sliver of BTC), so sizes are not floored to whole units.
- **Not force-closed at the stock-market close** by default (24/7). Set
  `risk.flatten_crypto_eod: true` to include crypto in the EOD flatten.

Enable it under `strategies.crypto` (see `config.example.yaml`):

```yaml
strategies:
  crypto:
    enabled: true              # master switch for the 24/7 crypto loop
    scan_interval_minutes: 15  # opportunity-scan cadence, all week
    stop_check_interval_minutes: 5  # tighter stop/take-profit check cadence
    crypto_order_ttl_minutes: 30  # cancel unfilled crypto orders older than N min (0=off)
    trailing_stop: false       # ratchet the software stop up as price rises
    trail_pct: 0.05
    momentum:       { enabled: true }   # dual-MA + ADX trend (TRENDING)
    mean_reversion: { enabled: false }  # z-score + RSI oversold (MEAN_REVERTING)
    breakout:       { enabled: false }  # Donchian upper-band breakout (TRENDING)
    dca:            { enabled: false, symbols: [BTC/USD, ETH/USD] }  # periodic accumulation
    volatility:     { enabled: false }  # low-vol coil setups (RANGE_BOUND)
    aggregator:     { regime_benchmark: BTC/USD }  # separate BTC/USD regime brain

risk:
  flatten_crypto_eod: false    # keep crypto open at the equity close (24/7)
  max_crypto_positions: 0      # crypto position cap, counted separately (0 = unlimited)
```

Crypto uses a **separate signal aggregator and market regime** (classified from
`BTC/USD`, not `SPY`), and crypto position counts are tracked independently from
equities via `max_crypto_positions`. The five strategies map to regimes for
regime-based routing just like the equity strategies.

---

## Testing

```powershell
pip install -e ".[dev]"
pytest
```

The suite uses an in-memory `FakeBroker` (see `trader/tests/conftest.py`), so no
network, credentials, or vendor SDKs are needed to run it.

---

## Architecture

```
trader/
├── brokers/        # Neutral DTOs + BrokerClient ABC; Alpaca & IBKR adapters
├── data/           # Universe building, fundamentals
├── strategies/     # Strategy family (all emit neutral Signals)
├── analysis/       # Indicators, regime classifier, signal aggregator, exposure
├── risk/           # Position sizing, drawdown breaker, ATR stops
├── execution/      # Order manager, portfolio, SQLite state store
├── web/            # Optional FastAPI dashboard
├── main.py         # Trader orchestrator + APScheduler jobs
├── supervisor.py   # Watchdog + dashboard host
└── backtest.py     # Daily backtest engine
```

Nothing above the broker layer imports a vendor SDK — adapters translate native
objects to/from the neutral types in `trader/brokers/types.py`.
