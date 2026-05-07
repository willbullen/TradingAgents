"""
autonomous/orchestrator.py
===========================
The Autonomous Trading Orchestrator — the "outer controller" that drives the
entire system end-to-end without human intervention.

Architecture
------------
  Layer 1 (THIS FILE): Orchestrator
    - Selects tickers via Capitol Trades (politician copy-trading) and/or
      a static watchlist
    - Drives TradingAgents for each ticker (fully headless, no CLI)
    - Routes decisions to the correct execution layer

  Layer 2: TradingAgents Core
    - Multi-agent LLM analysis (Fundamentals, Sentiment, News, Technical)
    - Bull/Bear debate → Research Manager → Trader → Risk Team → Portfolio Manager
    - Returns structured PortfolioDecision (Buy/Overweight/Hold/Underweight/Sell)

  Layer 3: Execution
    - AlpacaBridge: translates ratings to market/limit orders
    - TrailingStopMonitor: ratchets stop-loss floors upward as prices rise
    - WheelStrategy: manages CSP/CC options income cycle

Scheduling
----------
Run this script on a cron schedule for full autonomy:

  # Daily pre-market analysis (9:00 AM Mon-Fri)
  0 9 * * 1-5 cd /path/to/TradingAgents && python -m autonomous.orchestrator

  # Trailing stop monitor every 5 minutes during market hours
  */5 9-16 * * 1-5 cd /path/to/TradingAgents && python -m autonomous.orchestrator --mode stops

Usage
-----
  # Full run: analyse + execute
  python -m autonomous.orchestrator

  # Analyse only (no orders placed)
  python -m autonomous.orchestrator --mode analyse

  # Update trailing stops only
  python -m autonomous.orchestrator --mode stops

  # Wheel strategy cycle check only
  python -m autonomous.orchestrator --mode wheel

  # Use a static watchlist instead of Capitol Trades
  python -m autonomous.orchestrator --tickers NVDA MSFT AAPL

  # Dry run (no real orders, full logging)
  python -m autonomous.orchestrator --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

# ──────────────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────────────
LOG_DIR = Path.home() / ".tradingagents" / "logs" / "orchestrator"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        ),
    ],
)
logger = logging.getLogger("orchestrator")

# ──────────────────────────────────────────────────────────────────────────────
# Default static watchlist (used when Capitol Trades is not configured or
# as a supplement to it)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_WATCHLIST: List[str] = [
    "NVDA",   # AI / semiconductors
    "MSFT",   # Cloud / AI infrastructure
    "AAPL",   # Consumer tech
    "AMZN",   # E-commerce / cloud
    "META",   # Social / AI
    "GOOGL",  # Search / AI
    "TSLA",   # EV / energy
    "SPY",    # S&P 500 benchmark
]

# Tickers to always run the Wheel Strategy on (must be optionable stocks)
WHEEL_WATCHLIST: List[str] = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
]


class AutonomousOrchestrator:
    """
    The top-level controller for the autonomous trading system.

    Parameters
    ----------
    mode : str
        Operating mode: 'full', 'analyse', 'stops', 'wheel'.
    use_capitol_trades : bool
        If True, fetch tickers from Capitol Trades (requires QUIVER_API_KEY).
    static_tickers : list, optional
        Override ticker list. If provided, Capitol Trades is skipped.
    dry_run : bool
        If True, run analysis but do not place any real orders.
    paper : bool
        If True, use Alpaca paper trading endpoint.
    top_n_capitol : int
        Number of top Capitol Trades tickers to analyse.
    llm_provider : str
        LLM provider for TradingAgents ('openai', 'google', 'anthropic', etc.)
    deep_model : str
        Model name for deep thinking agents.
    quick_model : str
        Model name for quick thinking agents.
    """

    def __init__(
        self,
        mode: str = "full",
        use_capitol_trades: bool = True,
        static_tickers: Optional[List[str]] = None,
        dry_run: bool = False,
        paper: bool = True,
        top_n_capitol: int = 5,
        llm_provider: str = "openai",
        deep_model: str = "gpt-4o",
        quick_model: str = "gpt-4o-mini",
    ):
        self.mode = mode
        self.use_capitol_trades = use_capitol_trades
        self.static_tickers = static_tickers
        self.dry_run = dry_run
        self.paper = paper
        self.top_n_capitol = top_n_capitol
        self.llm_provider = llm_provider
        self.deep_model = deep_model
        self.quick_model = quick_model

        # Results accumulator for this run
        self.run_results: List[Dict] = []

    # ──────────────────────────────────────────────────────────────────────────
    # Main entry points
    # ──────────────────────────────────────────────────────────────────────────

    def run(self) -> List[Dict]:
        """Execute the orchestrator in the configured mode."""
        logger.info("=" * 70)
        logger.info("Autonomous Trading Orchestrator — mode=%s | dry_run=%s | paper=%s",
                    self.mode, self.dry_run, self.paper)
        logger.info("=" * 70)

        if self.mode in ("full", "analyse"):
            self._run_analysis_and_execution()

        if self.mode in ("full", "stops"):
            self._run_trailing_stop_monitor()

        if self.mode in ("full", "wheel"):
            self._run_wheel_strategy()

        self._save_run_report()
        return self.run_results

    # ──────────────────────────────────────────────────────────────────────────
    # Layer 1 → Layer 2 → Layer 3 pipeline
    # ──────────────────────────────────────────────────────────────────────────

    def _run_analysis_and_execution(self) -> None:
        """Select tickers → run TradingAgents → execute decisions."""
        tickers = self._select_tickers()
        if not tickers:
            logger.warning("No tickers selected — skipping analysis.")
            return

        logger.info("Tickers selected for analysis: %s", tickers)

        # Initialise TradingAgents (lazy — only done once)
        ta = self._init_trading_agents()
        bridge = self._init_alpaca_bridge()
        trade_date = str(date.today())

        for ticker in tickers:
            logger.info("─" * 50)
            logger.info("Analysing %s on %s ...", ticker, trade_date)

            try:
                result = self._analyse_ticker(ta, bridge, ticker, trade_date)
                self.run_results.append(result)
                logger.info(
                    "[%s] Decision: %s | Action: %s | %s",
                    ticker,
                    result.get("rating"),
                    result.get("execution", {}).get("action", "n/a"),
                    result.get("execution", {}).get("message", ""),
                )
            except Exception as e:
                logger.error("Error processing %s: %s", ticker, e, exc_info=True)
                self.run_results.append({"ticker": ticker, "status": "error", "message": str(e)})

    def _analyse_ticker(
        self, ta, bridge, ticker: str, trade_date: str
    ) -> Dict:
        """
        Run TradingAgents for a single ticker and execute the decision.

        Returns a full result dict with analysis + execution details.
        """
        # ── Layer 2: TradingAgents analysis ──────────────────────────────────
        final_state, rating = ta.propagate(ticker, trade_date)

        # Extract structured decision fields
        final_decision_text = final_state.get("final_trade_decision", "")
        price_target = self._extract_price_target(final_decision_text)
        stop_loss = self._extract_stop_loss(final_decision_text)
        position_sizing = self._extract_position_sizing(final_decision_text)
        time_horizon = self._extract_time_horizon(final_decision_text)

        logger.info(
            "[%s] TradingAgents → rating=%s | target=%s | stop=%s | sizing=%s",
            ticker, rating, price_target, stop_loss, position_sizing,
        )

        result = {
            "ticker": ticker,
            "date": trade_date,
            "rating": rating,
            "price_target": price_target,
            "stop_loss": stop_loss,
            "position_sizing": position_sizing,
            "time_horizon": time_horizon,
            "full_decision": final_decision_text[:500],  # truncated for log
        }

        # ── Layer 3: Execution ────────────────────────────────────────────────
        if self.mode == "analyse" or self.dry_run:
            result["execution"] = {"action": "dry_run", "message": "Analysis only — no order placed"}
        else:
            execution = bridge.execute_decision(
                ticker=ticker,
                rating=rating,
                entry_price=price_target,
                stop_loss=stop_loss,
                position_sizing=position_sizing,
                investment_thesis=final_decision_text,
            )
            result["execution"] = execution

            # Add to wheel strategy if eligible
            if rating in ("Buy", "Overweight", "Hold"):
                if ticker in WHEEL_WATCHLIST:
                    wheel = self._init_wheel_strategy()
                    wheel_result = wheel.add_ticker(
                        ticker=ticker,
                        tradingagents_rating=rating,
                        price_target=price_target,
                        time_horizon=time_horizon,
                    )
                    result["wheel"] = wheel_result

        return result

    def _run_trailing_stop_monitor(self) -> None:
        """Update trailing stop-loss orders for all open positions."""
        logger.info("Running trailing stop monitor ...")
        monitor = self._init_trailing_stop_monitor()
        stop_results = monitor.run()
        for r in stop_results:
            logger.info("[TrailingStop] %s", r)
        self.run_results.append({"mode": "stops", "results": stop_results})

    def _run_wheel_strategy(self) -> None:
        """Run the wheel strategy cycle check for all managed tickers."""
        logger.info("Running wheel strategy cycle check ...")
        wheel = self._init_wheel_strategy()
        wheel_results = wheel.run_cycle()
        for r in wheel_results:
            logger.info("[Wheel] %s", r)
        self.run_results.append({"mode": "wheel", "results": wheel_results})

    # ──────────────────────────────────────────────────────────────────────────
    # Ticker selection
    # ──────────────────────────────────────────────────────────────────────────

    def _select_tickers(self) -> List[str]:
        """
        Build the ticker list for this run.

        Priority:
          1. CLI-provided static tickers (if any)
          2. Capitol Trades top picks (if QUIVER_API_KEY is set)
          3. Default watchlist (fallback)
        """
        if self.static_tickers:
            logger.info("Using CLI-provided tickers: %s", self.static_tickers)
            return [t.upper() for t in self.static_tickers]

        tickers = []

        if self.use_capitol_trades:
            from autonomous.capitol_trades.ticker_selector import CapitolTradesSelector
            selector = CapitolTradesSelector(
                api_key=os.getenv("QUIVER_API_KEY"),
                lookback_days=14,
                high_performers_only=True,
            )
            capitol_tickers = selector.get_top_tickers(
                top_n=self.top_n_capitol,
                buys_only=True,
            )
            if capitol_tickers:
                logger.info("Capitol Trades tickers: %s", capitol_tickers)
                tickers.extend(capitol_tickers)

                # Log the human-readable summary
                summary = selector.get_recent_trades_summary(buys_only=True)
                logger.info("Recent Capitol Trades disclosures:")
                for trade in summary[:10]:
                    logger.info(
                        "  %s | %s (%s) | %s | %s",
                        trade["date"], trade["politician"], trade["party"],
                        trade["ticker"], trade["amount"],
                    )

        # Supplement with default watchlist (deduplicated)
        for t in DEFAULT_WATCHLIST:
            if t not in tickers:
                tickers.append(t)

        return tickers[:10]  # cap at 10 per run to manage API costs

    # ──────────────────────────────────────────────────────────────────────────
    # Component initialisers
    # ──────────────────────────────────────────────────────────────────────────

    def _init_trading_agents(self):
        """Initialise TradingAgentsGraph with configured LLM provider."""
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG

        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = self.llm_provider
        config["deep_think_llm"] = self.deep_model
        config["quick_think_llm"] = self.quick_model
        config["max_debate_rounds"] = 1
        config["max_risk_discuss_rounds"] = 1
        config["data_vendors"] = {
            "core_stock_apis": "yfinance",
            "technical_indicators": "yfinance",
            "fundamental_data": "yfinance",
            "news_data": "yfinance",
        }

        logger.info(
            "Initialising TradingAgents (provider=%s, deep=%s, quick=%s)",
            self.llm_provider, self.deep_model, self.quick_model,
        )
        return TradingAgentsGraph(debug=False, config=config)

    def _init_alpaca_bridge(self):
        """Initialise the Alpaca execution bridge."""
        from autonomous.execution.alpaca_bridge import AlpacaBridge
        return AlpacaBridge(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
            paper=self.paper,
        )

    def _init_trailing_stop_monitor(self):
        """Initialise the trailing stop monitor."""
        from autonomous.execution.trailing_stop_monitor import TrailingStopMonitor
        return TrailingStopMonitor(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
            trail_pct=float(os.getenv("TRAIL_PCT", "0.05")),
            paper=self.paper,
        )

    def _init_wheel_strategy(self):
        """Initialise the wheel strategy manager."""
        from autonomous.strategies.wheel_strategy import WheelStrategy
        return WheelStrategy(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
            paper=self.paper,
            dte=int(os.getenv("WHEEL_DTE", "30")),
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Decision text parsers
    # ──────────────────────────────────────────────────────────────────────────

    def _extract_price_target(self, decision_text: str) -> Optional[float]:
        """Extract price target from TradingAgents decision markdown."""
        import re
        match = re.search(r"\*\*Price Target\*\*[:\s]+\$?([\d,]+\.?\d*)", decision_text)
        if match:
            return float(match.group(1).replace(",", ""))
        return None

    def _extract_stop_loss(self, decision_text: str) -> Optional[float]:
        """Extract stop-loss level from TradingAgents decision markdown."""
        import re
        match = re.search(r"stop.loss[:\s]+\$?([\d,]+\.?\d*)", decision_text, re.IGNORECASE)
        if match:
            return float(match.group(1).replace(",", ""))
        return None

    def _extract_position_sizing(self, decision_text: str) -> Optional[str]:
        """Extract position sizing guidance from TradingAgents decision markdown."""
        import re
        match = re.search(r"(\d+(?:\.\d+)?%\s+of\s+portfolio)", decision_text, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    def _extract_time_horizon(self, decision_text: str) -> Optional[str]:
        """Extract time horizon from TradingAgents decision markdown."""
        import re
        match = re.search(r"\*\*Time Horizon\*\*[:\s]+([^\n]+)", decision_text)
        if match:
            return match.group(1).strip()
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Reporting
    # ──────────────────────────────────────────────────────────────────────────

    def _save_run_report(self) -> None:
        """Save the full run results to a JSON report file."""
        report_path = LOG_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        try:
            with open(report_path, "w") as f:
                json.dump(
                    {
                        "run_date": str(date.today()),
                        "mode": self.mode,
                        "dry_run": self.dry_run,
                        "paper": self.paper,
                        "results": self.run_results,
                    },
                    f,
                    indent=2,
                    default=str,
                )
            logger.info("Run report saved to: %s", report_path)
        except Exception as e:
            logger.warning("Could not save run report: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Autonomous Trading Orchestrator — TradingAgents + Capitol Trades + Alpaca"
    )
    parser.add_argument(
        "--mode",
        choices=["full", "analyse", "stops", "wheel"],
        default="full",
        help="Operating mode (default: full)",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        help="Static ticker list (overrides Capitol Trades)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyse only — do not place any orders",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use live trading (default: paper trading)",
    )
    parser.add_argument(
        "--no-capitol",
        action="store_true",
        help="Disable Capitol Trades ticker selection",
    )
    parser.add_argument(
        "--provider",
        default=os.getenv("LLM_PROVIDER", "openai"),
        help="LLM provider (default: openai)",
    )
    parser.add_argument(
        "--deep-model",
        default=os.getenv("DEEP_MODEL", "gpt-4o"),
        help="Deep thinking model (default: gpt-4o)",
    )
    parser.add_argument(
        "--quick-model",
        default=os.getenv("QUICK_MODEL", "gpt-4o-mini"),
        help="Quick thinking model (default: gpt-4o-mini)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Number of Capitol Trades tickers to analyse (default: 5)",
    )

    args = parser.parse_args()

    orchestrator = AutonomousOrchestrator(
        mode=args.mode,
        use_capitol_trades=not args.no_capitol,
        static_tickers=args.tickers,
        dry_run=args.dry_run,
        paper=not args.live,
        top_n_capitol=args.top_n,
        llm_provider=args.provider,
        deep_model=args.deep_model,
        quick_model=args.quick_model,
    )

    results = orchestrator.run()

    # Print summary table
    print("\n" + "=" * 70)
    print("AUTONOMOUS TRADING RUN SUMMARY")
    print("=" * 70)
    for r in results:
        if "ticker" in r:
            exec_info = r.get("execution", {})
            print(
                f"  {r['ticker']:<8} | {r.get('rating', 'N/A'):<12} | "
                f"{exec_info.get('action', 'n/a'):<8} | {exec_info.get('message', '')}"
            )
    print("=" * 70)


if __name__ == "__main__":
    main()
