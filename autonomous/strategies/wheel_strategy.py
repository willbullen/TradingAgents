"""
autonomous/strategies/wheel_strategy.py
========================================
Implements the Wheel Strategy — a systematic options income strategy that
cycles between two stages to collect premium continuously:

  Stage 1 — Cash-Secured Put (CSP)
  ---------------------------------
  Sell a put option below the current price. You collect the premium upfront.
  - If the stock stays above the strike at expiry → keep the premium, repeat.
  - If the stock falls below the strike → you are assigned 100 shares at the
    strike price (effectively buying at a discount). Move to Stage 2.

  Stage 2 — Covered Call (CC)
  ----------------------------
  You now own 100 shares. Sell a call option above your cost basis.
  - If the stock stays below the strike at expiry → keep the premium, repeat.
  - If the stock rises above the strike → shares are called away at a profit.
    Move back to Stage 1.

This module uses TradingAgents' analysis to:
  - Select the best tickers for the wheel (strong fundamentals, stable price)
  - Choose appropriate strike prices based on TradingAgents' price targets
  - Determine expiry dates based on the recommended time horizon
  - Manage the full lifecycle: sell CSP → assignment → sell CC → called away

Requires: alpaca-trade-api with options support (Alpaca Options API)

Usage
-----
    from autonomous.strategies.wheel_strategy import WheelStrategy

    wheel = WheelStrategy(
        api_key="YOUR_KEY",
        secret_key="YOUR_SECRET",
        paper=True,
    )
    # Run a full wheel cycle check
    wheel.run_cycle()
"""

from __future__ import annotations

import logging
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yfinance as yf

logger = logging.getLogger(__name__)

WHEEL_STATE_FILE = Path.home() / ".tradingagents" / "wheel_state.json"

# ──────────────────────────────────────────────────────────────────────────────
# Strategy parameters (tunable)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_PUT_DELTA    = 0.30   # Target delta for CSP (approx 30 delta = ~30% chance of assignment)
DEFAULT_CALL_DELTA   = 0.30   # Target delta for CC (approx 30 delta = ~30% chance of being called)
DEFAULT_DTE          = 30     # Target days-to-expiry for new contracts
MIN_PREMIUM_PCT      = 0.005  # Minimum acceptable premium as % of stock price (0.5%)
ALPACA_PAPER_URL     = "https://paper-api.alpaca.markets"
ALPACA_LIVE_URL      = "https://api.alpaca.markets"


class WheelStrategy:
    """
    Manages the full Wheel Strategy lifecycle for a set of tickers.

    The strategy is driven by TradingAgents analysis:
      - Only run the wheel on tickers rated Hold, Overweight, or Buy
        (we want stocks we'd be happy owning if assigned)
      - Use TradingAgents' price_target to inform strike selection
      - Use TradingAgents' time_horizon to select expiry dates

    Parameters
    ----------
    api_key : str
        Alpaca API key.
    secret_key : str
        Alpaca secret key.
    paper : bool
        Use paper trading endpoint (default True).
    put_delta : float
        Target delta for cash-secured puts (default 0.30).
    call_delta : float
        Target delta for covered calls (default 0.30).
    dte : int
        Target days-to-expiry for new contracts (default 30).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        paper: bool = True,
        put_delta: float = DEFAULT_PUT_DELTA,
        call_delta: float = DEFAULT_CALL_DELTA,
        dte: int = DEFAULT_DTE,
    ):
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self.paper = paper
        self.put_delta = put_delta
        self.call_delta = call_delta
        self.dte = dte
        self.base_url = ALPACA_PAPER_URL if paper else ALPACA_LIVE_URL
        self._api = None

        WHEEL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.state: Dict = self._load_state()

    # ──────────────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────────────

    def run_cycle(self, tickers: Optional[List[str]] = None) -> List[Dict]:
        """
        Run a full wheel cycle check for all managed tickers.

        For each ticker:
          - If in Stage 1 (no shares): check if existing CSP has expired/assigned,
            or open a new CSP if none is active.
          - If in Stage 2 (shares held): check if existing CC has expired/called,
            or open a new CC if none is active.

        Parameters
        ----------
        tickers : list, optional
            Tickers to process. If None, uses all tickers in current wheel state.

        Returns
        -------
        List[Dict]
            Status report for each ticker processed.
        """
        if tickers:
            for t in tickers:
                if t.upper() not in self.state:
                    self.state[t.upper()] = {"stage": "csp", "contracts": []}

        results = []
        for ticker, ticker_state in self.state.items():
            result = self._process_ticker(ticker, ticker_state)
            results.append(result)

        self._save_state()
        return results

    def add_ticker(
        self,
        ticker: str,
        tradingagents_rating: str,
        price_target: Optional[float] = None,
        time_horizon: Optional[str] = None,
    ) -> Dict:
        """
        Add a ticker to the wheel strategy.

        Only accepts tickers rated Buy, Overweight, or Hold by TradingAgents
        (we must be comfortable owning the stock if assigned).

        Parameters
        ----------
        ticker : str
            Stock ticker symbol.
        tradingagents_rating : str
            TradingAgents portfolio rating for this ticker.
        price_target : float, optional
            TradingAgents price target — used to set call strike.
        time_horizon : str, optional
            TradingAgents time horizon — used to select expiry.

        Returns
        -------
        dict
            Result of adding the ticker.
        """
        rating = tradingagents_rating.strip().title()
        if rating in ("Sell", "Underweight"):
            return {
                "ticker": ticker,
                "action": "rejected",
                "message": f"Wheel strategy skipped: TradingAgents rated {ticker} as {rating}. "
                           "Only run the wheel on stocks you'd be happy owning.",
            }

        ticker = ticker.upper()
        self.state[ticker] = {
            "stage": "csp",
            "rating": rating,
            "price_target": price_target,
            "time_horizon": time_horizon,
            "contracts": [],
            "cost_basis": None,
            "total_premium_collected": 0.0,
            "added_date": datetime.now().strftime("%Y-%m-%d"),
        }
        self._save_state()
        logger.info("[Wheel] Added %s to wheel (rating=%s, target=%s)", ticker, rating, price_target)
        return {
            "ticker": ticker,
            "action": "added",
            "stage": "csp",
            "message": f"Added {ticker} to wheel strategy. Will sell cash-secured puts.",
        }

    def get_wheel_status(self) -> List[Dict]:
        """Return a summary of all active wheel positions."""
        summary = []
        for ticker, state in self.state.items():
            current_price = self._get_current_price(ticker)
            summary.append({
                "ticker": ticker,
                "stage": state.get("stage", "csp"),
                "rating": state.get("rating", "Unknown"),
                "current_price": current_price,
                "cost_basis": state.get("cost_basis"),
                "price_target": state.get("price_target"),
                "total_premium_collected": state.get("total_premium_collected", 0.0),
                "active_contracts": len(state.get("contracts", [])),
            })
        return summary

    def calculate_wheel_metrics(self, ticker: str) -> Dict:
        """
        Calculate key metrics for a wheel position.

        Returns annualised return on capital, break-even price, and
        total premium collected to date.
        """
        state = self.state.get(ticker.upper(), {})
        current_price = self._get_current_price(ticker)
        cost_basis = state.get("cost_basis") or current_price
        total_premium = state.get("total_premium_collected", 0.0)
        added_date_str = state.get("added_date", datetime.now().strftime("%Y-%m-%d"))

        try:
            added_date = datetime.strptime(added_date_str, "%Y-%m-%d")
            days_active = max((datetime.now() - added_date).days, 1)
        except ValueError:
            days_active = 30

        # Break-even = cost_basis - total_premium_collected (per share)
        premium_per_share = total_premium / 100  # options contracts = 100 shares
        break_even = cost_basis - premium_per_share

        # Annualised return on capital
        if cost_basis and cost_basis > 0:
            roi = (total_premium / (cost_basis * 100)) * (365 / days_active)
        else:
            roi = 0.0

        return {
            "ticker": ticker,
            "cost_basis": cost_basis,
            "current_price": current_price,
            "break_even": round(break_even, 2),
            "total_premium_collected": total_premium,
            "days_active": days_active,
            "annualised_roi_pct": round(roi * 100, 2),
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Stage processors
    # ──────────────────────────────────────────────────────────────────────────

    def _process_ticker(self, ticker: str, state: Dict) -> Dict:
        """Route to the correct stage handler."""
        stage = state.get("stage", "csp")
        if stage == "csp":
            return self._process_csp_stage(ticker, state)
        elif stage == "cc":
            return self._process_cc_stage(ticker, state)
        else:
            return {"ticker": ticker, "stage": stage, "action": "unknown_stage"}

    def _process_csp_stage(self, ticker: str, state: Dict) -> Dict:
        """
        Stage 1: Cash-Secured Put management.

        Check if an active CSP has expired or been assigned.
        If no active contract, open a new CSP.
        """
        current_price = self._get_current_price(ticker)
        if not current_price:
            return {"ticker": ticker, "stage": "csp", "action": "error", "message": "Could not fetch price"}

        active_contracts = state.get("contracts", [])

        # Check for expired/assigned contracts
        for contract in list(active_contracts):
            expiry = datetime.strptime(contract["expiry"], "%Y-%m-%d")
            if datetime.now() >= expiry:
                if current_price < contract["strike"]:
                    # Assigned — transition to Stage 2
                    logger.info("[Wheel] %s CSP assigned at %.2f", ticker, contract["strike"])
                    state["stage"] = "cc"
                    state["cost_basis"] = contract["strike"] - (contract["premium"] / 100)
                    state["contracts"] = []
                    return {
                        "ticker": ticker,
                        "stage": "csp→cc",
                        "action": "assigned",
                        "strike": contract["strike"],
                        "cost_basis": state["cost_basis"],
                        "message": f"CSP assigned. Now own 100 shares at cost basis {state['cost_basis']:.2f}. Moving to covered call stage.",
                    }
                else:
                    # Expired worthless — keep premium, open new CSP
                    premium = contract.get("premium", 0)
                    state["total_premium_collected"] = state.get("total_premium_collected", 0) + premium
                    state["contracts"] = []
                    logger.info("[Wheel] %s CSP expired worthless. Premium kept: $%.2f", ticker, premium)

        # No active contract — open a new CSP
        if not state.get("contracts"):
            return self._open_csp(ticker, state, current_price)

        return {
            "ticker": ticker,
            "stage": "csp",
            "action": "monitoring",
            "message": f"Active CSP in place. Current price: {current_price:.2f}",
        }

    def _process_cc_stage(self, ticker: str, state: Dict) -> Dict:
        """
        Stage 2: Covered Call management.

        Check if an active CC has expired or shares have been called away.
        If no active contract, open a new covered call.
        """
        current_price = self._get_current_price(ticker)
        if not current_price:
            return {"ticker": ticker, "stage": "cc", "action": "error", "message": "Could not fetch price"}

        active_contracts = state.get("contracts", [])

        for contract in list(active_contracts):
            expiry = datetime.strptime(contract["expiry"], "%Y-%m-%d")
            if datetime.now() >= expiry:
                if current_price > contract["strike"]:
                    # Called away — shares sold at strike, cycle complete
                    profit = contract["strike"] - state.get("cost_basis", contract["strike"])
                    premium = contract.get("premium", 0)
                    total_profit = (profit * 100) + premium
                    state["total_premium_collected"] = state.get("total_premium_collected", 0) + premium
                    state["stage"] = "csp"
                    state["cost_basis"] = None
                    state["contracts"] = []
                    logger.info("[Wheel] %s CC called away. Cycle profit: $%.2f", ticker, total_profit)
                    return {
                        "ticker": ticker,
                        "stage": "cc→csp",
                        "action": "called_away",
                        "strike": contract["strike"],
                        "cycle_profit": round(total_profit, 2),
                        "message": f"Shares called away at {contract['strike']:.2f}. Cycle profit: ${total_profit:.2f}. Returning to CSP stage.",
                    }
                else:
                    # Expired worthless — keep premium, open new CC
                    premium = contract.get("premium", 0)
                    state["total_premium_collected"] = state.get("total_premium_collected", 0) + premium
                    state["contracts"] = []
                    logger.info("[Wheel] %s CC expired worthless. Premium kept: $%.2f", ticker, premium)

        # No active contract — open a new covered call
        if not state.get("contracts"):
            return self._open_covered_call(ticker, state, current_price)

        return {
            "ticker": ticker,
            "stage": "cc",
            "action": "monitoring",
            "message": f"Active covered call in place. Current price: {current_price:.2f}",
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Order placement
    # ──────────────────────────────────────────────────────────────────────────

    def _open_csp(self, ticker: str, state: Dict, current_price: float) -> Dict:
        """
        Sell a cash-secured put.

        Strike selection:
          - Default: current_price * (1 - put_delta * 0.5) ≈ slightly OTM
          - If TradingAgents provided a price_target below current price,
            use that as the strike (we'd be happy buying at that level)
        """
        price_target = state.get("price_target")
        if price_target and price_target < current_price:
            strike = self._round_to_strike(price_target)
        else:
            # ~5-10% OTM put
            strike = self._round_to_strike(current_price * (1 - self.put_delta * 0.33))

        expiry = self._next_expiry(self.dte)
        estimated_premium = self._estimate_put_premium(current_price, strike, self.dte)

        # Check minimum premium threshold
        if estimated_premium < current_price * MIN_PREMIUM_PCT * 100:
            return {
                "ticker": ticker,
                "stage": "csp",
                "action": "skipped",
                "message": f"Premium too low (${estimated_premium:.2f}). Skipping CSP for {ticker}.",
            }

        api = self._get_api()
        if api is None:
            # Dry run — record the intended trade
            contract = {
                "type": "put",
                "strike": strike,
                "expiry": expiry,
                "premium": estimated_premium,
                "opened": datetime.now().strftime("%Y-%m-%d"),
            }
            state["contracts"] = [contract]
            logger.info(
                "[Wheel] DRY RUN: Sell CSP %s %s P%.0f @ $%.2f",
                ticker, expiry, strike, estimated_premium,
            )
            return {
                "ticker": ticker,
                "stage": "csp",
                "action": "dry_run_csp",
                "strike": strike,
                "expiry": expiry,
                "estimated_premium": estimated_premium,
                "message": f"DRY RUN: Would sell {ticker} {expiry} ${strike:.0f} Put for ~${estimated_premium:.2f} premium",
            }

        try:
            # Alpaca options order (requires options trading enabled on account)
            option_symbol = self._build_option_symbol(ticker, expiry, "P", strike)
            order = api.submit_order(
                symbol=option_symbol,
                qty=1,
                side="sell",
                type="limit",
                limit_price=round(estimated_premium / 100, 2),  # per-share price
                time_in_force="day",
            )
            contract = {
                "type": "put",
                "strike": strike,
                "expiry": expiry,
                "premium": estimated_premium,
                "order_id": order.id,
                "opened": datetime.now().strftime("%Y-%m-%d"),
            }
            state["contracts"] = [contract]
            logger.info("[Wheel] Sold CSP %s %s P%.0f (order=%s)", ticker, expiry, strike, order.id)
            return {
                "ticker": ticker,
                "stage": "csp",
                "action": "sold_put",
                "strike": strike,
                "expiry": expiry,
                "premium": estimated_premium,
                "order_id": order.id,
                "message": f"Sold {ticker} {expiry} ${strike:.0f} Put for ${estimated_premium:.2f} premium",
            }
        except Exception as e:
            logger.error("[Wheel] Failed to sell CSP for %s: %s", ticker, e)
            return {"ticker": ticker, "stage": "csp", "action": "error", "message": str(e)}

    def _open_covered_call(self, ticker: str, state: Dict, current_price: float) -> Dict:
        """
        Sell a covered call above the cost basis.

        Strike selection:
          - Default: current_price * (1 + call_delta * 0.33) ≈ slightly OTM
          - If TradingAgents provided a price_target above current price,
            use that as the strike (we'd be happy selling at that level)
        """
        cost_basis = state.get("cost_basis", current_price)
        price_target = state.get("price_target")

        if price_target and price_target > current_price:
            strike = self._round_to_strike(price_target)
        else:
            # ~5-10% OTM call, but always above cost basis
            strike = max(
                self._round_to_strike(current_price * (1 + self.call_delta * 0.33)),
                self._round_to_strike(cost_basis * 1.02),  # at least 2% above cost basis
            )

        expiry = self._next_expiry(self.dte)
        estimated_premium = self._estimate_call_premium(current_price, strike, self.dte)

        if estimated_premium < current_price * MIN_PREMIUM_PCT * 100:
            return {
                "ticker": ticker,
                "stage": "cc",
                "action": "skipped",
                "message": f"Premium too low (${estimated_premium:.2f}). Skipping CC for {ticker}.",
            }

        api = self._get_api()
        if api is None:
            contract = {
                "type": "call",
                "strike": strike,
                "expiry": expiry,
                "premium": estimated_premium,
                "opened": datetime.now().strftime("%Y-%m-%d"),
            }
            state["contracts"] = [contract]
            logger.info(
                "[Wheel] DRY RUN: Sell CC %s %s C%.0f @ $%.2f",
                ticker, expiry, strike, estimated_premium,
            )
            return {
                "ticker": ticker,
                "stage": "cc",
                "action": "dry_run_cc",
                "strike": strike,
                "expiry": expiry,
                "estimated_premium": estimated_premium,
                "cost_basis": cost_basis,
                "message": f"DRY RUN: Would sell {ticker} {expiry} ${strike:.0f} Call for ~${estimated_premium:.2f} premium",
            }

        try:
            option_symbol = self._build_option_symbol(ticker, expiry, "C", strike)
            order = api.submit_order(
                symbol=option_symbol,
                qty=1,
                side="sell",
                type="limit",
                limit_price=round(estimated_premium / 100, 2),
                time_in_force="day",
            )
            contract = {
                "type": "call",
                "strike": strike,
                "expiry": expiry,
                "premium": estimated_premium,
                "order_id": order.id,
                "opened": datetime.now().strftime("%Y-%m-%d"),
            }
            state["contracts"] = [contract]
            logger.info("[Wheel] Sold CC %s %s C%.0f (order=%s)", ticker, expiry, strike, order.id)
            return {
                "ticker": ticker,
                "stage": "cc",
                "action": "sold_call",
                "strike": strike,
                "expiry": expiry,
                "premium": estimated_premium,
                "order_id": order.id,
                "message": f"Sold {ticker} {expiry} ${strike:.0f} Call for ${estimated_premium:.2f} premium",
            }
        except Exception as e:
            logger.error("[Wheel] Failed to sell CC for %s: %s", ticker, e)
            return {"ticker": ticker, "stage": "cc", "action": "error", "message": str(e)}

    # ──────────────────────────────────────────────────────────────────────────
    # Utility helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _get_current_price(self, ticker: str) -> Optional[float]:
        """Fetch current price via yfinance."""
        try:
            data = yf.Ticker(ticker).fast_info
            return float(data.last_price)
        except Exception as e:
            logger.warning("[Wheel] Could not fetch price for %s: %s", ticker, e)
            return None

    def _estimate_put_premium(self, stock_price: float, strike: float, dte: int) -> float:
        """
        Rough Black-Scholes approximation for put premium.
        Uses a simplified model: premium ≈ (OTM distance + time value).
        For production, replace with live options chain data.
        """
        import math
        otm_distance = max(stock_price - strike, 0)
        time_value = stock_price * 0.20 * math.sqrt(dte / 365) * 0.4  # ~20% IV assumption
        premium_per_share = max(otm_distance * 0.1 + time_value, stock_price * 0.003)
        return round(premium_per_share * 100, 2)  # contract = 100 shares

    def _estimate_call_premium(self, stock_price: float, strike: float, dte: int) -> float:
        """Rough call premium estimate (same simplified model as put)."""
        import math
        otm_distance = max(strike - stock_price, 0)
        time_value = stock_price * 0.20 * math.sqrt(dte / 365) * 0.4
        premium_per_share = max(otm_distance * 0.1 + time_value, stock_price * 0.003)
        return round(premium_per_share * 100, 2)

    def _next_expiry(self, target_dte: int) -> str:
        """Find the nearest standard options expiry (3rd Friday) >= target_dte days out."""
        target_date = datetime.now() + timedelta(days=target_dte)
        # Find the 3rd Friday of the target month
        year, month = target_date.year, target_date.month
        first_day = datetime(year, month, 1)
        first_friday = first_day + timedelta(days=(4 - first_day.weekday()) % 7)
        third_friday = first_friday + timedelta(weeks=2)
        if third_friday < target_date:
            # Move to next month
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
            first_day = datetime(year, month, 1)
            first_friday = first_day + timedelta(days=(4 - first_day.weekday()) % 7)
            third_friday = first_friday + timedelta(weeks=2)
        return third_friday.strftime("%Y-%m-%d")

    def _round_to_strike(self, price: float) -> float:
        """Round a price to the nearest standard options strike increment."""
        if price < 25:
            increment = 0.5
        elif price < 200:
            increment = 1.0
        elif price < 500:
            increment = 5.0
        else:
            increment = 10.0
        return round(round(price / increment) * increment, 2)

    def _build_option_symbol(self, ticker: str, expiry: str, option_type: str, strike: float) -> str:
        """
        Build an OCC option symbol: TICKER + YYMMDD + C/P + 8-digit strike.
        Example: AAPL240119C00185000
        """
        dt = datetime.strptime(expiry, "%Y-%m-%d")
        date_str = dt.strftime("%y%m%d")
        strike_str = f"{int(strike * 1000):08d}"
        return f"{ticker}{date_str}{option_type}{strike_str}"

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

    def _load_state(self) -> Dict:
        """Load persisted wheel state from disk."""
        if WHEEL_STATE_FILE.exists():
            try:
                with open(WHEEL_STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_state(self) -> None:
        """Persist current wheel state to disk."""
        try:
            with open(WHEEL_STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.warning("[Wheel] Could not save state: %s", e)
