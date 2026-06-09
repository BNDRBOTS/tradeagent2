"""
Entry point. Full startup sequence:
1. Logging
2. Env validation
3. DB init
4. Web server starts FIRST — Railway health check responds immediately.
   Previously uvicorn started after the backtest, so the health check timed
   out and Railway served a 502 before anyone could see the dashboard.
5. Backtest runs in a thread (asyncio.to_thread) so the event loop stays free
   and uvicorn can serve the dashboard/health check during the backtest run.
6. DRY_RUN mode: gate failures are non-fatal. Dashboard shows degraded status.
   Bot watches markets but places no orders. No real money, no hard exit.
7. LIVE mode: gate failures halt bot tasks but keep the dashboard alive so you
   can see exactly what failed before connecting real funds.
8. asyncio.gather: uvicorn web server + WS bot + daily reset + balance sync + kill monitor
   Kill monitor watches store.kill_event; when set, cancels bot tasks while
   keeping web server live.
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
from dashboard.persistence import init_db
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

    # Init SQLite
    await init_db()

    rest = CryptoComRestClient()

    # ── Start web server FIRST ────────────────────────────────────────────────
    # Railway health check hits /api/health. The old code started uvicorn only
    # after the backtest completed — which meant a 502 if the health check fired
    # before backtest finished, and a dead domain if the gate failed.
    # Starting uvicorn here means the health check responds in under 1 second
    # regardless of how long the backtest takes.
    port = int(os.environ.get("PORT", 8080))
    uvi_config = uvicorn.Config(dashboard_app, host="0.0.0.0", port=port,
                                log_level="warning", loop="none")
    uvi_server = uvicorn.Server(uvi_config)
    web_task = asyncio.create_task(uvi_server.serve(), name="web")
    logger.info("Dashboard starting on port %d — health check active", port)

    # Let uvicorn bind its socket before backtest starts
    await asyncio.sleep(1.0)

    # ── Backtest gate ─────────────────────────────────────────────────────────
    # asyncio.to_thread runs the synchronous backtest (which uses time.sleep
    # internally for rate-limit pacing) in a thread pool so the event loop
    # stays free. Uvicorn can serve requests during the entire backtest run.
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
            # Real money is connected. Do not start bot tasks.
            # Keep the dashboard alive so you can see what failed.
            logger.critical(
                "LIVE MODE: startup gate failed — bot halted. "
                "Dashboard is live at port %d. Review backtest panel before "
                "connecting funds.", port
            )

            async def _stub_kill_monitor() -> None:
                await store.kill_event.wait()

            await asyncio.gather(web_task, _stub_kill_monitor(), return_exceptions=True)
            return

        else:
            # DRY_RUN: no real money, no real orders regardless.
            # Gate failures are shown in the dashboard backtest panel.
            # The bot runs in observation mode — it watches the market,
            # evaluates signals, logs what it would do, but never submits orders.
            logger.warning(
                "DRY_RUN: strategies below backtest threshold — running in "
                "observation mode. Dashboard shows degraded status. "
                "No orders will be placed."
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

    # Bot tasks — cancelled by kill switch, web_task stays alive
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
