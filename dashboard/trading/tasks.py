"""
trading/tasks.py
Celery tasks that wrap the autonomous/ modules.
All schedules are managed via django_celery_beat (stored in DB).
"""
import logging
import os
import sys
from datetime import datetime, timezone

from celery import shared_task
from django.conf import settings
from django.utils import timezone as dj_timezone

logger = logging.getLogger(__name__)

# Ensure the repo root is on the path so autonomous/ is importable
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ──────────────────────────────────────────────────────────────────────────────
# Helper: push a live update to the WebSocket group
# ──────────────────────────────────────────────────────────────────────────────

def _ws_push(event_type: str, data: dict):
    """Send a message to the 'trading' channel group for live dashboard updates."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            "trading_updates",
            {"type": "trading.update", "event": event_type, "data": data},
        )
    except Exception as e:
        logger.warning("WebSocket push failed: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# Task 1: Full daily analysis run
# ──────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, name="trading.run_daily_analysis")
def run_daily_analysis(self, mode="full", dry_run=False, tickers=None):
    """
    Run the full autonomous orchestrator.
    Scheduled: 9:00 AM Mon–Fri (set via django_celery_beat admin).
    """
    from trading.models import OrchestratorRun, TradeDecision

    run = OrchestratorRun.objects.create(mode=mode, status="running")
    _ws_push("run_started", {"run_id": run.id, "mode": mode})

    try:
        from autonomous.orchestrator import AutonomousOrchestrator

        orch = AutonomousOrchestrator(
            mode=mode,
            use_capitol_trades=True,
            static_tickers=tickers,
            dry_run=dry_run,
            paper=settings.ALPACA_PAPER,
            llm_provider=settings.LLM_PROVIDER,
            deep_model=settings.DEEP_MODEL,
            quick_model=settings.QUICK_MODEL,
        )

        # Inject credentials into env for autonomous modules
        os.environ["ALPACA_API_KEY"] = settings.ALPACA_API_KEY
        os.environ["ALPACA_SECRET_KEY"] = settings.ALPACA_SECRET_KEY
        os.environ["QUIVER_API_KEY"] = settings.QUIVER_API_KEY
        os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY

        results = orch.run()

        # Persist decisions to DB
        decisions_saved = 0
        orders_placed = 0
        tickers_done = []

        for r in results:
            if "ticker" not in r:
                continue
            tickers_done.append(r["ticker"])
            exec_info = r.get("execution", {})
            action = exec_info.get("action", "")
            if action not in ("dry_run", "hold", "error", "no_position"):
                orders_placed += 1

            TradeDecision.objects.create(
                ticker=r["ticker"],
                trade_date=r.get("date", datetime.now().date()),
                rating=r.get("rating", "Error"),
                price_target=r.get("price_target"),
                stop_loss=r.get("stop_loss"),
                position_sizing=r.get("position_sizing", ""),
                time_horizon=r.get("time_horizon", ""),
                investment_thesis=r.get("full_decision", ""),
                execution_action=action,
                execution_message=exec_info.get("message", ""),
                order_id=exec_info.get("order_id", ""),
                source="orchestrator",
            )
            decisions_saved += 1
            _ws_push("decision_made", {"ticker": r["ticker"], "rating": r.get("rating")})

        run.status = "success"
        run.tickers_analysed = tickers_done
        run.decisions_made = decisions_saved
        run.orders_placed = orders_placed
        run.completed_at = dj_timezone.now()
        run.duration_seconds = (run.completed_at - run.started_at).total_seconds()
        run.save()

        _ws_push("run_complete", {"run_id": run.id, "decisions": decisions_saved})
        return {"status": "success", "decisions": decisions_saved}

    except Exception as exc:
        run.status = "error"
        run.error_message = str(exc)
        run.completed_at = dj_timezone.now()
        run.save()
        _ws_push("run_error", {"run_id": run.id, "error": str(exc)})
        logger.exception("Orchestrator run failed: %s", exc)
        raise self.retry(exc=exc, countdown=300, max_retries=2)


# ──────────────────────────────────────────────────────────────────────────────
# Task 2: Sync live Alpaca positions
# ──────────────────────────────────────────────────────────────────────────────

@shared_task(name="trading.sync_alpaca_positions")
def sync_alpaca_positions():
    """
    Pull live positions from Alpaca and update the Position model.
    Scheduled: every 1 minute during market hours.
    """
    from trading.models import Position

    try:
        import alpaca_trade_api as tradeapi
        base_url = "https://paper-api.alpaca.markets" if settings.ALPACA_PAPER else "https://api.alpaca.markets"
        api = tradeapi.REST(settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY, base_url, api_version="v2")
        positions = api.list_positions()

        # Load trailing stop state
        import json
        from pathlib import Path
        stop_state_file = Path.home() / ".tradingagents" / "trailing_stops.json"
        stop_state = {}
        if stop_state_file.exists():
            with open(stop_state_file) as f:
                stop_state = json.load(f)

        synced_tickers = set()
        for pos in positions:
            ticker = pos.symbol
            synced_tickers.add(ticker)
            Position.objects.update_or_create(
                ticker=ticker,
                defaults={
                    "qty": float(pos.qty),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "current_price": float(pos.current_price),
                    "market_value": float(pos.market_value),
                    "unrealised_pl": float(pos.unrealized_pl),
                    "unrealised_pl_pct": float(pos.unrealized_plpc),
                    "stop_loss_floor": stop_state.get(ticker),
                    "side": pos.side,
                    "last_synced": dj_timezone.now(),
                },
            )

        # Remove positions that are no longer open
        Position.objects.exclude(ticker__in=synced_tickers).delete()

        _ws_push("positions_updated", {"count": len(synced_tickers)})
        return {"synced": len(synced_tickers)}

    except Exception as e:
        logger.warning("Alpaca position sync failed: %s", e)
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# Task 3: Update trailing stops
# ──────────────────────────────────────────────────────────────────────────────

@shared_task(name="trading.update_trailing_stops")
def update_trailing_stops():
    """
    Ratchet trailing stop floors upward for all open positions.
    Scheduled: every 5 minutes during market hours.
    """
    try:
        from autonomous.execution.trailing_stop_monitor import TrailingStopMonitor
        monitor = TrailingStopMonitor(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            trail_pct=settings.TRAIL_PCT,
            paper=settings.ALPACA_PAPER,
        )
        results = monitor.run()
        raised = sum(1 for r in results if r.get("action") == "raised")
        _ws_push("stops_updated", {"raised": raised, "total": len(results)})
        return {"raised": raised, "total": len(results)}
    except Exception as e:
        logger.warning("Trailing stop update failed: %s", e)
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# Task 4: Wheel strategy cycle
# ──────────────────────────────────────────────────────────────────────────────

@shared_task(name="trading.run_wheel_cycle")
def run_wheel_cycle():
    """
    Check all wheel positions and open/close contracts as needed.
    Scheduled: 9:15 AM Mon–Fri.
    """
    from trading.models import WheelContract

    try:
        from autonomous.strategies.wheel_strategy import WheelStrategy
        wheel = WheelStrategy(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
            paper=settings.ALPACA_PAPER,
            dte=settings.WHEEL_DTE,
        )
        results = wheel.run_cycle()

        for r in results:
            if r.get("action") in ("sold_put", "dry_run_csp"):
                WheelContract.objects.create(
                    ticker=r["ticker"],
                    stage="csp",
                    strike=r.get("strike", 0),
                    expiry=r.get("expiry", "2099-01-01"),
                    premium_collected=r.get("premium", r.get("estimated_premium", 0)),
                    order_id=r.get("order_id", ""),
                    status="open",
                )
            elif r.get("action") in ("sold_call", "dry_run_cc"):
                WheelContract.objects.create(
                    ticker=r["ticker"],
                    stage="cc",
                    strike=r.get("strike", 0),
                    expiry=r.get("expiry", "2099-01-01"),
                    premium_collected=r.get("premium", r.get("estimated_premium", 0)),
                    cost_basis=r.get("cost_basis"),
                    order_id=r.get("order_id", ""),
                    status="open",
                )
            elif r.get("action") == "assigned":
                WheelContract.objects.filter(
                    ticker=r["ticker"], stage="csp", status="open"
                ).update(status="assigned", closed_at=dj_timezone.now())
            elif r.get("action") == "called_away":
                WheelContract.objects.filter(
                    ticker=r["ticker"], stage="cc", status="open"
                ).update(
                    status="called_away",
                    closed_at=dj_timezone.now(),
                    cycle_profit=r.get("cycle_profit"),
                )

        _ws_push("wheel_updated", {"actions": len(results)})
        return {"actions": len(results)}
    except Exception as e:
        logger.warning("Wheel cycle failed: %s", e)
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# Task 5: Fetch Capitol Trades disclosures
# ──────────────────────────────────────────────────────────────────────────────

@shared_task(name="trading.fetch_capitol_trades")
def fetch_capitol_trades():
    """
    Refresh the politician trade disclosure feed.
    Scheduled: 8:00 AM daily.
    """
    from trading.models import CapitolTrade

    try:
        from autonomous.capitol_trades.ticker_selector import CapitolTradesSelector
        selector = CapitolTradesSelector(
            api_key=settings.QUIVER_API_KEY,
            lookback_days=30,
        )
        trades = selector.get_recent_trades_summary(buys_only=False)
        saved = 0
        for t in trades:
            obj, created = CapitolTrade.objects.get_or_create(
                politician=t.get("politician", ""),
                ticker=t.get("ticker", ""),
                disclosure_date=t.get("date", datetime.now().date()),
                transaction_type=t.get("transaction", "Purchase"),
                defaults={
                    "party": t.get("party", "")[:1],
                    "chamber": t.get("chamber", ""),
                    "amount": t.get("amount", ""),
                    "transaction_date": t.get("transaction_date"),
                },
            )
            if created:
                saved += 1

        _ws_push("capitol_trades_updated", {"new": saved})
        return {"saved": saved}
    except Exception as e:
        logger.warning("Capitol Trades fetch failed: %s", e)
        return {"error": str(e)}
