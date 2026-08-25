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
- **Backtest engine** — replay daily bars through momentum / crypto-momentum /
  crypto-volatility logic with ATR stops.
- **Self-improvement optimizer** — a backtest-driven, multi-threaded parameter
  search that walks the trading parameters toward proven, *out-of-sample*
  improvements while guarding against single-symbol over-fitting.
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
the **best & worst 10 closed trades of all time**, broker + uptime, and provides
**Restart** / **Stop** buttons.

Reading the cards:

- **Current Equity** = **Cash** + **Positions Value** (the market value of open
  holdings), so those three always reconcile.
- **Total Return** is measured against the *first ever* equity snapshot, not the
  visible slice of the chart. Hover the card to see the baseline.
- **Uptime** counts the current trader process (`y d h m`); it resets to zero on
  every restart. Hover for the supervisor's uptime and the restart count.
- The equity chart has **7 / 14 / 30 days** and **All** buttons (the choice is
  remembered per browser). The window only affects the graph — every headline
  figure stays all-time. Behind it, `GET /api/metrics?days=N` limits the curve.
- The **Strategy Performance** table at the bottom lists every configured
  strategy with `active` / `disabled` status plus its buys, closed trades, win
  rate, realized and unrealized P/L since the last **Reset**. Realized P/L is
  credited to the strategy that *opened* the position, not to the exit that
  closed it (`crypto_stop`, `crypto_take_profit`), so it is a fair basis for
  deciding which algo to improve or switch off.

> The dashboard binds to loopback only. Do **not** expose it publicly without an
> authenticating reverse proxy in front.

---

## Backtesting

```powershell
# Equity momentum (default) — the window is selected in months
python -m trader.backtest --symbols AAPL,MSFT,NVDA --months 12

# Crypto strategies replay the same engine with their own ranking logic
python -m trader.backtest --symbols BTC/USD,ETH/USD --strategy crypto_momentum --months 6
python -m trader.backtest --symbols BTC/USD,ETH/USD --strategy crypto_volatility --months 3

# --days is still accepted for an exact calendar-day window
python -m trader.backtest --symbols AAPL,MSFT,NVDA --days 90
```

`--months` and `--days` are mutually exclusive; with neither, the last
**12 months** are replayed. The resolved window is printed before the run, and
if it is too short for the strategy's warm-up (e.g. a 1-month window against a
60-day momentum lookback) you get an explicit "expect no trades" warning instead
of a silent flat result.

Prints total return, number of trades, win rate, and max drawdown. The
backtester reuses the exact indicator / ATR code paths used in live trading.
`--strategy` selects which family's ranking logic is replayed (see
`STRATEGY_RANKERS` in `trader/backtest.py`).

### Where does the backtest data come from?

From the **active broker adapter** — `Backtester.run()` calls
`broker.get_bars(...)`, so with `broker.provider: alpaca` the bars are Alpaca
Market Data, using the feed set in `alpaca.data_feed`. No separate data vendor
is needed:

| | Alpaca **Basic** (free) | Algo Trader Plus ($99/mo) |
|---|---|---|
| Historical range | since **2016** | since 2016 |
| Recency limit | latest **15 minutes** withheld (SIP) | none |
| Equity coverage | IEX only | all US exchanges |
| Rate limit | 200 calls/min | 10,000 calls/min |
| Crypto history | unrestricted | unrestricted |

So the free plan does *not* cap you at 15 minutes of history — it withholds the
*most recent* 15 minutes and gives you years of daily bars. Two consequences the
adapter now handles for you (`_clamp_history_window` in
`trader/brokers/alpaca.py`):

- a `start` earlier than 2016 is clamped, with a warning, instead of silently
  returning nothing;
- on `data_feed: sip` the request `end` is pulled back behind the 15-minute
  cutoff so a Basic-plan account gets data instead of a subscription error.
  `iex` has no recency restriction and is left untouched.

Symbols that come back with **zero** bars are now logged as a warning, and a
subscription/403 rejection prints an actionable hint naming `alpaca.data_feed`.

> Free-plan caveat: equity bars are IEX-only, so volumes are a fraction of the
> consolidated tape and daily OHLC can differ slightly. Fine for relative
> ranking and sanity checks; don't read the fills as tick-accurate. Crypto bars
> have no such limitation. (Paid vendors such as ThetaData — $40–$160/mo,
> options-focused — have no free tier and are not required here.)

---

## Self-improvement / parameter optimizer

The optimizer repeatedly backtests a strategy against a large, cached universe
of historical bars, scores each parameter set with an overfit-resistant
composite metric, and nudges the parameters toward the changes that improve
results — then periodically pushes *further* in the directions that have been
helping. Runs can be long; that is expected.

```powershell
# 1. Build (or refresh) the on-disk bar cache once. Equities are volume-ranked,
#    so --max-symbols keeps the MOST LIQUID names, not an arbitrary first N.
python -m trader.optimize --refresh-cache --max-symbols 500 --cache-only

# 2. Optimise equity momentum for 40 rounds across all CPU cores.
python -m trader.optimize --strategy momentum --rounds 40

# 3. Optimise crypto momentum, then apply the winner to config.yaml.
python -m trader.optimize --strategy crypto_momentum --rounds 40 --apply
```

### How it resists over-fitting

The user's key requirement is that the bot must **not** tune itself to replay a
single symbol's curve. Three mechanisms enforce this:

- **Portfolio, not per-symbol** — every candidate is scored across *all* cached
  symbols at once, never one symbol in isolation.
- **Walk-forward + k-fold splits** — history is chopped into consecutive time
  windows and symbols into folds; a parameter set must work across different
  periods and different symbol groups. A leading slice trains and a trailing
  slice validates, so the reported **validation** score is out-of-sample.
- **Variance penalties** — the composite objective is
  `mean_return − w·drawdown − w·stdev(per-symbol returns) − w·stdev(per-fold
  scores)`. Profit concentrated in one symbol (high per-symbol spread) or luck
  on one window (high per-fold spread) is penalised, so only a *general* edge
  scores well. Weights are tunable via `--w-drawdown`, `--w-symbol-var`,
  `--w-fold-var`.

### Compute & parallelism

Candidate and fold evaluations are spread across CPU cores with a process pool
(`--workers 0` = all cores, `1` = serial). The bar download is cached to disk
(`data/backtest_cache/bars.pkl`) so thousands of evaluations reuse one fetch.

### Tunable parameters

| Strategy            | Parameters walked                                                                 |
| ------------------- | --------------------------------------------------------------------------------- |
| `momentum`          | `fast_lookback`, `slow_lookback`, `top_n` + risk knobs                            |
| `crypto_momentum`   | `fast_ma`, `slow_ma`, `adx_min`, `top_n` + risk knobs                             |
| `crypto_volatility` | `lookback`, `low_vol_pct`, `high_vol_pct`, `top_n` + risk knobs                   |

Shared risk knobs: `atr_stop_multiplier`, `max_hold_days`, `max_risk_per_trade`,
`max_positions` (see `DEFAULT_PARAM_SPECS` in `trader/optimize/optimizer.py`).

### Outputs (under `--out-dir`, default `data/optimize`)

| File                                | Contents                                                        |
| ----------------------------------- | --------------------------------------------------------------- |
| `optimize_state_<strategy>.json`    | Resumable state + full round history (use `--resume`).          |
| `optimize_report_<strategy>.csv`    | One row per round: score, accepted?, which params changed.      |
| `config.optimized.yaml`             | The base config with the winning parameters merged in.          |

By default nothing is overwritten — adopt the result by copying
`config.optimized.yaml` over `config.yaml`, or re-run with **`--apply`** to write
it in place (the previous `config.yaml` is backed up to `config.yaml.bak`).
Secrets are never written to these files.

> The `--max-symbols` cap uses **volume ranking** everywhere (live universe
> building *and* the optimizer cache): the most liquid symbols are kept rather
> than whatever the broker happened to list first.

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
├── optimize/       # Backtest-driven self-improvement (cache, scoring, search)
├── main.py         # Trader orchestrator + APScheduler jobs
├── supervisor.py   # Watchdog + dashboard host
└── backtest.py     # Daily backtest engine (momentum / crypto replay)
```

Nothing above the broker layer imports a vendor SDK — adapters translate native
objects to/from the neutral types in `trader/brokers/types.py`.
