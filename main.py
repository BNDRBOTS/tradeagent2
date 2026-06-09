"""
Entry point. Full startup sequence:
1. Logging
2. Env validation
3. DB init (+ schema migration for v2 intelligence columns)
4. Web server starts FIRST — Railway health check responds immediately
5. Backtest runs in a thread so uvicorn stays responsive during it
6. Intelligence replay — loads all past trades from SQLite and feeds them
   through each engine's intelligence layer in chronological order, exactly
   as live trades feed it. Bot resumes where it left off. Requires Railway
   Volume so the database file survives redeploys.
7. DRY_RUN mode: gate failures are non-fatal warnings, dashboard shows status
8. LIVE mode: gate failures halt bot tasks, dashboard stays alive
9. asyncio.gather: uvicorn + WS bot + daily reset + balance sync + kill monitor
"""
import asyncio
import logging
import os
import sys
import time
from typing import Dict

import uvicorn

from broker.rest_client import CryptoComRestClient
from broker.ws_client import CryptoComWSClient
from backtest.engine import run_startup_backtest
from bot_engine import InstrumentEngine
from config import settings
from dashboard.api import app as dashboard_app
from dashboard.persistence import init_db, fetch_all_trades
from dashboard.state_store import store
from risk.position_sizer import PositionSizer

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")


def _validate_env() -> None:
    if not settings.DRY_RUN:
        missing = [k for k, v in [("CRYPTOCOM_API_KEY", settings.API_KEY),
                                   ("CRYPTOCOM_API_SECRET", settings.API_SECRET)] if not v]
        if missing:
            logger.critical("Missing env vars: %s", missing)
            sys.exit(1)
    else:
        logger.warning("DRY_RUN=True — orders are simulated, not submitted")


async def _replay_intelligence(btc_engine: InstrumentEngine,
                               eth_engine: InstrumentEngine) -> None:
    """
    Load all past trades from the database in chronological order and feed
    each one through the correct engine's intelligence layer. This is
    identical to how live trades feed the intelligence engine — the engine
    cannot distinguish a replay from a live record.

    Must run after engines are created and before the WebSocket connects,
    so the intelligence state is fully restored before the first live signal.
    """
    try:
        past_trades = await fetch_all_trades()
    except Exception as exc:
        logger.warning("Intelligence replay skipped — database read failed: %s", exc)
        return

    if not past_trades:
        logger.info("Intelligence replay: no past trades found — starting fresh")
        return

    btc_count = 0
    eth_count = 0

    for trade in past_trades:
        instrument  = trade.get("instrument", "")
        exit_reason = trade.get("exit_reason", "")

        if exit_reason == "TARGET":
            outcome = "WIN"
        elif exit_reason == "STOP":
            outcome = "STOP"
        else:
            outcome = "TIMEOUT"

        replay_kwargs = dict(
            direction          = trade.get("direction", "LONG"),
            signal_conditions  = trade.get("signal_conditions") or {},
            spread_pct         = float(trade.get("spread_pct") or 0.0),
            regime_state       = trade.get("regime_state") or "MIXED",
            omega_score        = float(trade.get("omega_score") or 0.0),
            omega_tier         = trade.get("omega_tier") or "UNKNOWN",
            outcome            = outcome,
            realized_pnl       = float(trade.get("realized_pnl") or 0.0),
            entry_price        = float(trade.get("fill_price") or 0.0),
            exit_price         = float(trade.get("exit_price") or 0.0),
            # duration_minutes stored in DB; bot runs H1 candles so bars ≈ hours
            bars_held          = max(0, round(float(trade.get("duration_minutes") or 0) / 60)),
        )

        if instrument == settings.BTC_INSTRUMENT:
            btc_engine._intel.record_trade(**replay_kwargs)
            btc_count += 1
        elif instrument == settings.ETH_INSTRUMENT:
            eth_engine._intel.record_trade(**replay_kwargs)
            eth_count += 1

    logger.info(
        "Intelligence replay complete — BTC: %d trades, ETH: %d trades restored",
        btc_count, eth_count,
    )

    for engine, label in [(btc_engine, "BTC"), (eth_engine, "ETH")]:
        report = engine._intel.get_last_report()
        if report:
            logger.info(
                "  %s resumed: wr=%.1f%% load_bearers=%s consecutive_losses=%d",
                label,
                report.win_rate * 100,
                [c for c, _ in report.load_bearing_conditions[:3]],
                engine._intel.get_consecutive_losses(),
            )


async def _daily_reset_loop(sizer: PositionSizer) -> None:
    while True:
        now = time.gmtime()
        secs = (23 - now.tm_hour) * 3600 + (59 - now.tm_min) * 60 + (60 - now.tm_sec)
        await asyncio.sleep(max(secs, 1))
        sizer.reset_daily()
        snap = sizer.get_snapshot()
        store.update_risk(**snap)
        logger.info("Daily risk counters reset at UTC midnight")


async def _balance_sync_loop(rest: CryptoComRestClient, sizer: PositionSizer) -> None:
    while True:
        try:
            balance = rest.get_usdt_balance()
            if balance > 0:
                sizer.update_balance(balance)
                snap = sizer.get_snapshot()
                store.update_risk(**snap)
                logger.debug("Balance sync: %.4f USDT", balance)
        except Exception as exc:
            logger.warning("Balance sync error: %s", exc)
        await asyncio.sleep(60)


async def main() -> None:
    logger.info("=" * 70)
    logger.info("Crypto Trading Bot — startup")
    logger.info("DRY_RUN=%-5s  LOG_LEVEL=%s", settings.DRY_RUN, settings.LOG_LEVEL)
    logger.info("BTC: %-20s  ETH: %s", settings.BTC_INSTRUMENT, settings.ETH_INSTRUMENT)
    logger.info("=" * 70)

    _validate_env()
    store.set_startup(dry_run=settings.DRY_RUN)

    # DB init runs schema migration for v2 intelligence columns automatically
    await init_db()

    rest = CryptoComRestClient()

    # ── Start web server FIRST ────────────────────────────────────────────────
    port = int(os.environ.get("PORT", 8080))
    uvi_config = uvicorn.Config(dashboard_app, host="0.0.0.0", port=port,
                                log_level="warning", loop="none")
    uvi_server = uvicorn.Server(uvi_config)
    web_task = asyncio.create_task(uvi_server.serve(), name="web")
    logger.info("Dashboard starting on port %d — health check active", port)

    await asyncio.sleep(1.0)

    # ── Backtest gate ─────────────────────────────────────────────────────────
    logger.info("Running startup backtest — dashboard available during this")
    btc_result, eth_result = await asyncio.to_thread(run_startup_backtest, rest)
    store.set_backtest_results([btc_result, eth_result])

    failures = [f"[{r.instrument}] {f}"
                for r in [btc_result, eth_result]
                for f in r.gate_failures]

    if failures:
        for f in failures:
            logger.critical("  GATE FAIL: %s", f)
        if not settings.DRY_RUN:
            logger.critical(
                "LIVE MODE: startup gate failed — bot halted. "
                "Dashboard live at port %d. Review backtest panel before connecting funds.", port
            )
            async def _stub_kill_monitor() -> None:
                await store.kill_event.wait()
            await asyncio.gather(web_task, _stub_kill_monitor(), return_exceptions=True)
            return
        else:
            logger.warning(
                "DRY_RUN: strategies below backtest threshold — "
                "running in observation mode, no orders placed."
            )
    else:
        logger.info("All backtest gates passed")

    if not settings.DRY_RUN:
        try:
            rest.set_cancel_on_disconnect("CONNECTION")
            logger.info("Cancel-on-disconnect set via REST")
        except Exception as exc:
            logger.warning("REST CoD failed (non-fatal): %s", exc)

    sizer = PositionSizer()
    try:
        balance = rest.get_usdt_balance()
        if balance > 0:
            sizer.update_balance(balance)
            logger.info("Initial USDT balance: %.4f", balance)
    except Exception as exc:
        logger.warning("Initial balance fetch failed: %s", exc)
    store.update_risk(**sizer.get_snapshot())

    btc_engine = InstrumentEngine(settings.BTC_INSTRUMENT, settings.BTC_STRATEGY_CLASS, rest, sizer)
    eth_engine = InstrumentEngine(settings.ETH_INSTRUMENT, settings.ETH_STRATEGY_CLASS, rest, sizer)

    # ── Intelligence replay ───────────────────────────────────────────────────
    # After engines exist, before WebSocket connects. Restores everything the
    # bot learned from past trades so a restart doesn't erase accumulated knowledge.
    await _replay_intelligence(btc_engine, eth_engine)

    async def on_candlestick(instrument: str, candle: Dict) -> None:
        await btc_engine.on_candlestick(instrument, candle)
        await eth_engine.on_candlestick(instrument, candle)

    async def on_d1_candlestick(instrument: str, candle: Dict) -> None:
        if instrument == settings.BTC_INSTRUMENT:
            btc_engine.push_d1_candle(candle)

    async def on_book(instrument: str, book: Dict) -> None:
        await btc_engine.on_book(instrument, book)
        await eth_engine.on_book(instrument, book)

    async def on_order_update(order: Dict) -> None:
        await btc_engine.on_order_update(order)
        await eth_engine.on_order_update(order)

    ws = CryptoComWSClient(
        on_candlestick=on_candlestick,
        on_d1_candlestick=on_d1_candlestick,
        on_book=on_book,
        on_order_update=on_order_update,
    )

    bot_tasks = [
        asyncio.create_task(ws.start(),                      name="ws"),
        asyncio.create_task(_daily_reset_loop(sizer),         name="daily_reset"),
        asyncio.create_task(_balance_sync_loop(rest, sizer),  name="balance_sync"),
    ]

    async def _kill_monitor() -> None:
        await store.kill_event.wait()
        logger.warning("KILL SWITCH activated — halting bot tasks (web server stays live)")
        for t in bot_tasks:
            t.cancel()

    await asyncio.gather(web_task, _kill_monitor(), *bot_tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
