"""
autonomous/capitol_trades/ticker_selector.py
============================================
Fetches recent stock trades disclosed by US Congress members via the
Quiver Quantitative / Capitol Trades public API and returns a ranked list
of tickers to analyse.

Data source: https://www.quiverquant.com/sources/congresstrading
API docs:    https://api.quiverquant.com/beta/live/congresstrading

The module applies a configurable scoring model that weights:
  - Recency of the disclosure (more recent = higher score)
  - Number of distinct politicians buying the same ticker
  - Transaction type (purchases only, or purchases + sales)
  - Optional: filter to a curated list of high-performing politicians

Usage
-----
    from autonomous.capitol_trades.ticker_selector import CapitolTradesSelector

    selector = CapitolTradesSelector(api_key="YOUR_QUIVER_KEY")
    tickers = selector.get_top_tickers(top_n=5, buys_only=True)
    # Returns: ['NVDA', 'MSFT', 'AAPL', ...]
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Politicians with historically strong market-beating track records.
# Source: Quiver Quant performance rankings & public reporting.
# Add or remove names here to customise your "smart money" filter.
# ──────────────────────────────────────────────────────────────────────────────
HIGH_PERFORMERS: List[str] = [
    "Michael McCaul",
    "Nancy Pelosi",
    "Dan Crenshaw",
    "Josh Gottheimer",
    "Marjorie Taylor Greene",
    "Tommy Tuberville",
    "David Rouzer",
    "Ro Khanna",
    "Brian Mast",
    "Pete Sessions",
]

QUIVER_BASE_URL = "https://api.quiverquant.com/beta"


class CapitolTradesSelector:
    """
    Queries the Quiver Quantitative Congress Trading API and returns a
    ranked list of tickers suitable for TradingAgents analysis.

    Parameters
    ----------
    api_key : str
        Quiver Quantitative API key. Falls back to the environment variable
        ``QUIVER_API_KEY`` if not provided.
    lookback_days : int
        How many calendar days back to scan for disclosures. Default 30.
    high_performers_only : bool
        If True, only consider trades from politicians in HIGH_PERFORMERS.
        If False, consider all disclosed trades. Default True.
    min_transaction_size : str
        Minimum transaction size bracket to include. Quiver reports sizes as
        strings like "$1,001 - $15,000", "$15,001 - $50,000", etc.
        Set to None to include all sizes. Default None.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        lookback_days: int = 30,
        high_performers_only: bool = True,
        min_transaction_size: Optional[str] = None,
    ):
        self.api_key = api_key or os.getenv("QUIVER_API_KEY", "")
        self.lookback_days = lookback_days
        self.high_performers_only = high_performers_only
        self.min_transaction_size = min_transaction_size

        if not self.api_key:
            logger.warning(
                "No Quiver API key provided. Set QUIVER_API_KEY env var or pass "
                "api_key= to CapitolTradesSelector. Falling back to demo data."
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────────────

    def get_top_tickers(
        self,
        top_n: int = 10,
        buys_only: bool = True,
    ) -> List[str]:
        """
        Return the top-N tickers by disclosure score.

        Parameters
        ----------
        top_n : int
            Maximum number of tickers to return.
        buys_only : bool
            If True, only include Purchase transactions.
            If False, include both purchases and sales (sales may signal
            insider knowledge of a downturn).

        Returns
        -------
        List[str]
            Ticker symbols ordered by descending score.
        """
        trades = self._fetch_trades()
        if not trades:
            logger.warning("No trades fetched — returning empty ticker list.")
            return []

        scores = self._score_tickers(trades, buys_only=buys_only)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        tickers = [ticker for ticker, _ in ranked[:top_n]]
        logger.info(
            "Capitol Trades top-%d tickers (buys_only=%s): %s",
            top_n, buys_only, tickers,
        )
        return tickers

    def get_recent_trades_summary(self, buys_only: bool = True) -> List[Dict]:
        """
        Return a human-readable list of recent trade disclosures.

        Returns
        -------
        List[Dict]
            Each dict has keys: ticker, politician, transaction, amount, date, party.
        """
        trades = self._fetch_trades()
        result = []
        for t in trades:
            tx = t.get("Transaction", "")
            if buys_only and "purchase" not in tx.lower():
                continue
            result.append({
                "ticker": t.get("Ticker", ""),
                "politician": t.get("Representative", ""),
                "transaction": tx,
                "amount": t.get("Amount", ""),
                "date": t.get("TransactionDate", ""),
                "party": t.get("Party", ""),
            })
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _fetch_trades(self) -> List[Dict]:
        """Fetch raw trade data from Quiver Quant API."""
        if not self.api_key:
            return self._demo_trades()

        headers = {
            "Accept": "application/json",
            "Authorization": f"Token {self.api_key}",
        }
        url = f"{QUIVER_BASE_URL}/live/congresstrading"

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            all_trades = resp.json()
        except requests.RequestException as e:
            logger.error("Failed to fetch Capitol Trades data: %s", e)
            return self._demo_trades()

        # Filter by lookback window
        cutoff = datetime.now() - timedelta(days=self.lookback_days)
        filtered = []
        for trade in all_trades:
            date_str = trade.get("TransactionDate", "")
            try:
                trade_date = datetime.strptime(date_str, "%Y-%m-%d")
                if trade_date >= cutoff:
                    filtered.append(trade)
            except ValueError:
                continue

        # Filter to high-performer politicians if requested
        if self.high_performers_only:
            filtered = [
                t for t in filtered
                if any(
                    hp.lower() in t.get("Representative", "").lower()
                    for hp in HIGH_PERFORMERS
                )
            ]

        logger.info(
            "Fetched %d Capitol Trades disclosures (last %d days, high_performers_only=%s)",
            len(filtered), self.lookback_days, self.high_performers_only,
        )
        return filtered

    def _score_tickers(
        self, trades: List[Dict], buys_only: bool
    ) -> Dict[str, float]:
        """
        Score each ticker based on:
          - +2.0 per unique politician buying it
          - +1.0 per repeat purchase by same politician
          - Recency bonus: trades in last 7 days get +1.5, last 14 days +1.0
          - High-performer bonus: +1.0 if politician is in HIGH_PERFORMERS
        """
        now = datetime.now()
        scores: Dict[str, float] = {}
        seen: Dict[str, set] = {}  # ticker -> set of politicians who bought

        for trade in trades:
            tx = trade.get("Transaction", "")
            if buys_only and "purchase" not in tx.lower():
                continue

            ticker = trade.get("Ticker", "").upper().strip()
            if not ticker or len(ticker) > 5:
                continue

            politician = trade.get("Representative", "")
            date_str = trade.get("TransactionDate", "")

            try:
                trade_date = datetime.strptime(date_str, "%Y-%m-%d")
                days_ago = (now - trade_date).days
            except ValueError:
                days_ago = 30

            if ticker not in scores:
                scores[ticker] = 0.0
                seen[ticker] = set()

            # New politician buying this ticker
            if politician not in seen[ticker]:
                scores[ticker] += 2.0
                seen[ticker].add(politician)
            else:
                scores[ticker] += 1.0

            # Recency bonus
            if days_ago <= 7:
                scores[ticker] += 1.5
            elif days_ago <= 14:
                scores[ticker] += 1.0

            # High-performer bonus
            if any(hp.lower() in politician.lower() for hp in HIGH_PERFORMERS):
                scores[ticker] += 1.0

        return scores

    def _demo_trades(self) -> List[Dict]:
        """
        Fallback demo data when no API key is configured.
        Represents a realistic set of recent disclosures for testing.
        """
        logger.info("Using demo Capitol Trades data (no API key configured).")
        today = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        return [
            {"Ticker": "NVDA", "Representative": "Michael McCaul", "Transaction": "Purchase", "Amount": "$50,001 - $100,000", "TransactionDate": today, "Party": "R"},
            {"Ticker": "NVDA", "Representative": "Nancy Pelosi", "Transaction": "Purchase", "Amount": "$100,001 - $250,000", "TransactionDate": today, "Party": "D"},
            {"Ticker": "MSFT", "Representative": "Josh Gottheimer", "Transaction": "Purchase", "Amount": "$15,001 - $50,000", "TransactionDate": week_ago, "Party": "D"},
            {"Ticker": "AAPL", "Representative": "Dan Crenshaw", "Transaction": "Purchase", "Amount": "$1,001 - $15,000", "TransactionDate": week_ago, "Party": "R"},
            {"Ticker": "AMZN", "Representative": "Ro Khanna", "Transaction": "Purchase", "Amount": "$50,001 - $100,000", "TransactionDate": today, "Party": "D"},
            {"Ticker": "TSLA", "Representative": "Tommy Tuberville", "Transaction": "Purchase", "Amount": "$15,001 - $50,000", "TransactionDate": week_ago, "Party": "R"},
            {"Ticker": "META", "Representative": "Nancy Pelosi", "Transaction": "Purchase", "Amount": "$250,001 - $500,000", "TransactionDate": today, "Party": "D"},
            {"Ticker": "GOOGL", "Representative": "Brian Mast", "Transaction": "Purchase", "Amount": "$15,001 - $50,000", "TransactionDate": week_ago, "Party": "R"},
        ]
