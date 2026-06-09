# TradeAgent

---

## What it does

Connects to Crypto.com Exchange and trades two instruments automatically:

- **BTC/USD perpetual** — momentum strategy. Enters long positions when the daily trend, momentum indicators, volume, and signal quality all align.
- **ETH/USD perpetual** — mean-reversion strategy. Enters long or short positions when price breaks outside its normal range and snaps back.

Every time it starts up, it runs a 90-day historical test on both strategies. If either strategy fails its quality threshold, the bot will not place real orders until the issue is resolved. This gate exists to prevent trading during market conditions where the strategies have no edge.

The bot runs 24/7 on Railway and exposes a live dashboard at its public URL showing open positions, account balance, risk guards, closed trade history, and a halt button.

---

## Accounts you need

1. **Crypto.com Exchange** —`[exchange.crypto.com](https://exchange.crypto.com)`(this is the trading platform, not the Crypto.com mobile app)
2. **Railway** — `[railway.app](https://railway.app)` (where the bot runs)
3. **GitHub** — `[github.com](https://github.com)` (connects your code to Railway)

---

## Setup

### 1. Crypto.com Exchange account

Go to **exchange.crypto.com** → Sign Up → verify your email.

### 2. Enable 2FA on Crypto.com Exchange

Required before you can create API keys.

Top right → your name/icon → **Security** → **Two-Factor Authentication** → turn on → follow the prompts using an authenticator app (Google Authenticator works).

### 3. Enable derivatives trading on Crypto.com Exchange

Top right → your name/icon → **Manage Account** → look for a **Derivatives** or **Futures** tab → accept the terms. You must do this before the bot can trade BTC/USD-PERP or ETH/USD-PERP.

### 4. Fund your account

Deposit USDT into your Crypto.com Exchange account. The default starting capital the bot uses is $20. Deposit at least that amount.

### 5. Create API keys on Crypto.com Exchange

Top right → your name/icon → **Manage Account** → **API Management** tab → **Create a new API key**

- **Label:** anything (e.g. tradebot)
- **Can Read:** on (default)
- **Enable Trading:** on
- **Enable Withdrawal:** leave OFF — never turn this on for a bot
- **IP Whitelist:** leave blank
- Enter your 2FA code when prompted

Two values appear: **API Key** and **Secret Key**.

⚠️ Copy both immediately and save them somewhere safe. The Secret Key is shown once only. If you leave the page without copying it, you must create a new one.

### 6. Set up Railway Volume (permanent storage)

Without this, trade history and everything the bot learns is wiped every time you push a code update.

Go to your Railway project → click your bot service → **Volumes** tab → **New Volume**

- Mount Path: `/data`
- Click **Create**

### 7. Add environment variables to Railway

Go to your Railway project → click your bot service → **Variables** tab

Add each of these. Click **New Variable**, enter the name on the left and the value on the right:

| Variable | Value |
|---|---|
| `CRYPTOCOM_API_KEY` | your API Key from step 5 |
| `CRYPTOCOM_API_SECRET` | your Secret Key from step 5 |
| `RAILWAY_VOLUME_MOUNT_PATH` | `/data` |
| `DRY_RUN` | `true` |
| `ACCOUNT_CAPITAL` | your starting balance as a number, e.g. `20` |

Leave `DRY_RUN` as `true` until you have confirmed the dashboard loads and the bot is connecting to the exchange correctly. In this mode the bot watches the market and logs everything it would do, but places no real orders.

### 8. Push the code to GitHub

Make sure the following files are in your GitHub repo and up to date (these are the files that were changed most recently):

- `main.py` — root of the repo
- `bot_engine.py` — root of the repo
- `dashboard/persistence.py` — inside the dashboard folder

Railway detects the push and rebuilds automatically.

### 9. Verify

Wait about 60 seconds after pushing. Open your Railway service URL. The dashboard should load. You will see:

- A **Strategy Validation** panel showing backtest results for BTC and ETH
- BTC and ETH engine status (both should show **Watching**)
- Account balance
- Risk guards (all green)

If you see a 502 error, wait another 30 seconds — the backtest runs at startup and takes a minute or two.

### 10. Go live

When you are ready to trade with real money:

1. Go to Railway → **Variables** → change `DRY_RUN` from `true` to `false`
2. Confirm `ACCOUNT_CAPITAL` matches your actual USDT balance on the exchange
3. Railway redeploys automatically

---

## Environment variables — full list

| Variable | Default | Description |
|---|---|---|
| `CRYPTOCOM_API_KEY` | — | Your Crypto.com Exchange API key. Required when DRY_RUN is false. |
| `CRYPTOCOM_API_SECRET` | — | Your Crypto.com Exchange secret key. Required when DRY_RUN is false. |
| `RAILWAY_VOLUME_MOUNT_PATH` | — | Set to `/data` after creating a Railway Volume. Trade history saves here permanently. |
| `DRY_RUN` | `true` | `true` = watch only, no real orders. `false` = live trading. |
| `ACCOUNT_CAPITAL` | `20` | Starting balance in USD. Should match your actual exchange balance. |
| `RISK_PCT_PER_TRADE` | `0.02` | Fraction of balance risked per trade. 0.02 = 2%. |
| `MAX_TRADES_PER_DAY` | `3` | Maximum trades allowed in a 24-hour period. |
| `DAILY_DRAWDOWN_LIMIT` | `0.10` | Bot stops trading for the day if losses hit this fraction of balance. 0.10 = 10%. |
| `MIN_ACCOUNT_BALANCE` | `12` | Bot stops trading if balance drops below this dollar amount. |
| `LOG_LEVEL` | `INFO` | `INFO` for normal operation. `DEBUG` for verbose output. |
| `BACKTEST_DAYS` | `90` | Days of history used in the startup validation test. |
| `MIN_WIN_RATE` | `0.52` | Minimum win rate the strategy must show in backtest to be allowed to trade. |
| `MIN_RR_RATIO` | `1.5` | Minimum average profit-to-loss ratio required in backtest. |
| `MIN_BACKTEST_TRADES` | `100` | Minimum number of backtest trades required for the gate to pass. |
| `BTC_INSTRUMENT` | `BTCUSD-PERP` | The BTC perpetual contract name on Crypto.com Exchange. |
| `ETH_INSTRUMENT` | `ETHUSD-PERP` | The ETH perpetual contract name on Crypto.com Exchange. |

---

## Dashboard

The dashboard is available at your Railway service URL once the bot is running. It requires no login. It shows:

- **Open Position** — direction, entry price, live gain/loss, stop and target price levels
- **BTC / ETH engines** — current state and live bid/ask prices
- **Risk Guards** — account floor, daily loss limit, trade count limit
- **Account** — balance and today's realized profit/loss
- **Strategy Validation** — results from the startup backtest
- **Closed Trades** — full history of every completed trade
- **Emergency Halt** — stops the bot immediately without cancelling your existing stop-loss and take-profit orders on the exchange

---

## How persistence works

Trade history is written to a SQLite database file at `/data/trades.db` (if `RAILWAY_VOLUME_MOUNT_PATH` is set) or `trades.db` in the local directory (if not set).

Every time the bot starts, it loads all past trades back from the database and replays them through the intelligence layer. This restores the win/loss patterns, condition weights, and regime analysis the bot has accumulated. A Railway Volume is required for this to survive redeploys — without it, the database file is wiped every time you push a code change.

---

## File structure

```
tradeagent2/
├── main.py                    # Entry point and startup sequence
├── bot_engine.py              # Live trading logic per instrument
├── Dockerfile                 # Container build instructions for Railway
├── requirements.txt           # Python dependencies
├── railway.toml               # Railway deployment configuration
├── config/
│   └── settings.py            # All configurable parameters
├── strategy/
│   ├── btc_momentum.py        # BTC momentum signal logic
│   ├── eth_mean_reversion.py  # ETH mean-reversion signal logic
│   ├── indicators.py          # Technical indicators (ATR, EMA, MACD, RSI, etc.)
│   ├── signal_scorer.py       # Signal quality scoring
│   ├── intelligence.py        # Learning layer — improves with trade history
│   └── regime_detector.py     # Market regime classification
├── backtest/
│   └── engine.py              # Startup validation backtest
├── broker/
│   ├── rest_client.py         # Crypto.com Exchange REST API
│   └── ws_client.py           # Crypto.com Exchange WebSocket (live data)
├── risk/
│   └── position_sizer.py      # Position sizing and circuit breakers
├── state/
│   └── machine.py             # Position state tracking
└── dashboard/
    ├── api.py                 # Web dashboard and API endpoints
    ├── persistence.py         # SQLite trade storage and retrieval
    └── state_store.py         # Shared in-memory state
```

---

## Risk notice

This bot trades real money when `DRY_RUN` is set to `false`. Cryptocurrency perpetual futures carry significant risk including total loss of deposited funds. The startup backtest gate and daily risk guards reduce but do not eliminate that risk. Do not deposit money you cannot afford to lose.
