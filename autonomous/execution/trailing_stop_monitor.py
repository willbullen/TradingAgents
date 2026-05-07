"""
autonomous/execution/trailing_stop_monitor.py
==============================================
Monitors all open Alpaca positions and automatically adjusts trailing
stop-loss orders as prices move in the trader's favour.

How it works
------------
For each open position:
  1. Fetch the current market price.
  2. Calculate the trailing stop floor = current_price * (1 - trail_pct).
  3. If the new floor is HIGHER than the existing stop, cancel the old stop
     and submit a new one at the higher level (locking in more profit).
  4. If the price has already fallen through the stop, the existing GTC stop
     order will have triggered automatically — no action needed.

This script is designed to be run on a cron schedule (e.g. every 5 minutes
during market hours) or called directly from the orchestrator.

Usage
-----
    from autonomous.execution.trailing_stop_monitor import TrailingStopMonitor

    monitor = TrailingStopMonitor(
        api_key="YOUR_KEY",
        secret_key="YOUR_SECRET",
        trail_pct=0.05,   # 5% trailing stop
        paper=True,
    )
    monitor.run()
"""

from __future__ import annotations

import logging
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TRAIL_PCT = 0.05       # 5% trailing stop by default
STATE_FILE = Path.home() / ".tradingagents" / "trailing_stops.json"

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL  = "https://api.alpaca.markets"


class TrailingStopMonitor:
    """
    Monitors open positions and ratchets stop-loss orders upward as
    prices rise, protecting accumulated profits.

    Parameters
    ----------
    api_key : str
        Alpaca API key.
    secret_key : str
        Alpaca secret key.
    trail_pct : float
        Trailing distance as a fraction of current price (default 0.05 = 5%).
    paper : bool
        Use paper trading endpoint if True (default).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        trail_pct: float = DEFAULT_TRAIL_PCT,
        paper: bool = True,
    ):
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self.trail_pct = trail_pct
        self.paper = paper
        self.base_url = ALPACA_PAPER_URL if paper else ALPACA_LIVE_URL
        self._api = None

        # Load persisted stop state
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.stop_state: Dict[str, float] = self._load_state()

    # ──────────────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────────────

    def run(self) -> List[Dict]:
        """
        Check all open positions and update trailing stops where needed.

        Returns
        -------
        List[Dict]
            One entry per position checked, with update status.
        """
        api = self._get_api()
        if api is None:
            logger.warning("[TrailingStop] Alpaca not configured — dry run.")
            return [{"status": "dry_run", "message": "Alpaca credentials not set"}]

        try:
            positions = api.list_positions()
        except Exception as e:
            logger.error("[TrailingStop] Failed to fetch positions: %s", e)
            return [{"status": "error", "message": str(e)}]

        results = []
        for position in positions:
            result = self._process_position(api, position)
            results.append(result)

        self._save_state()
        logger.info(
            "[TrailingStop] Checked %d positions at %s",
            len(positions), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        return results

    def set_initial_stop(self, ticker: str, entry_price: float) -> None:
        """
        Record the initial stop floor for a newly opened position.
        Called by AlpacaBridge after a buy order is filled.
        """
        initial_stop = entry_price * (1 - self.trail_pct)
        self.stop_state[ticker.upper()] = initial_stop
        self._save_state()
        logger.info(
            "[TrailingStop] Initial stop for %s set at %.2f (entry=%.2f, trail=%.1f%%)",
            ticker, initial_stop, entry_price, self.trail_pct * 100,
        )

    def get_stop_levels(self) -> Dict[str, float]:
        """Return the current tracked stop floor for each position."""
        return dict(self.stop_state)

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _process_position(self, api, position) -> Dict:
        """Evaluate and update the trailing stop for a single position."""
        ticker = position.symbol
        current_price = float(position.current_price)
        qty = int(float(position.qty))

        # Calculate where the new stop floor should be
        new_stop = round(current_price * (1 - self.trail_pct), 2)
        existing_stop = self.stop_state.get(ticker)

        if existing_stop is None:
            # First time seeing this position — initialise from avg entry
            avg_entry = float(position.avg_entry_price)
            existing_stop = round(avg_entry * (1 - self.trail_pct), 2)
            self.stop_state[ticker] = existing_stop
            logger.info(
                "[TrailingStop] Initialised stop for %s at %.2f (entry=%.2f)",
                ticker, existing_stop, avg_entry,
            )

        # Only ratchet UP — never lower the stop
        if new_stop <= existing_stop:
            return {
                "ticker": ticker,
                "current_price": current_price,
                "stop_floor": existing_stop,
                "action": "no_change",
                "message": f"Stop unchanged at {existing_stop:.2f}",
            }

        # New stop is higher — update it
        logger.info(
            "[TrailingStop] Raising stop for %s: %.2f → %.2f (price=%.2f)",
            ticker, existing_stop, new_stop, current_price,
        )

        # Cancel existing stop orders for this ticker
        self._cancel_stop_orders(api, ticker)

        # Place new stop order at the higher level
        try:
            order = api.submit_order(
                symbol=ticker,
                qty=qty,
                side="sell",
                type="stop",
                stop_price=new_stop,
                time_in_force="gtc",
            )
            self.stop_state[ticker] = new_stop
            return {
                "ticker": ticker,
                "current_price": current_price,
                "old_stop": existing_stop,
                "new_stop": new_stop,
                "order_id": order.id,
                "action": "raised",
                "message": f"Stop raised from {existing_stop:.2f} to {new_stop:.2f}",
            }
        except Exception as e:
            logger.error("[TrailingStop] Failed to update stop for %s: %s", ticker, e)
            return {
                "ticker": ticker,
                "action": "error",
                "message": str(e),
            }

    def _cancel_stop_orders(self, api, ticker: str) -> None:
        """Cancel any open stop-sell orders for the given ticker."""
        try:
            orders = api.list_orders(status="open", symbols=[ticker])
            for order in orders:
                if order.type in ("stop", "stop_limit") and order.side == "sell":
                    api.cancel_order(order.id)
                    logger.debug("[TrailingStop] Cancelled old stop order %s for %s", order.id, ticker)
        except Exception as e:
            logger.warning("[TrailingStop] Could not cancel stop orders for %s: %s", ticker, e)

    def _get_api(self):
        """Lazily initialise the Alpaca REST client."""
        if self._api is not None:
            return self._api
        if not self.api_key or not self.secret_key:
            return None
        try:
            import alpaca_trade_api as tradeapi
            self._api = tradeapi.REST(
                self.api_key,
                self.secret_key,
                self.base_url,
                api_version="v2",
            )
            return self._api
        except ImportError:
            logger.error("alpaca-trade-api not installed. Run: pip install alpaca-trade-api")
            return None

    def _load_state(self) -> Dict[str, float]:
        """Load persisted stop floor state from disk."""
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_state(self) -> None:
        """Persist current stop floor state to disk."""
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.stop_state, f, indent=2)
        except Exception as e:
            logger.warning("[TrailingStop] Could not save state: %s", e)
