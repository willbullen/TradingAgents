"""
trading/consumers.py
WebSocket consumers for the TradingAgents dashboard.
  1. TradingConsumer     — general live updates (positions, runs, stops)
  2. AgentRoomConsumer   — streams the full autonomous pipeline:
       Stage 1: Capitol Trades ticker selection
       Stage 2: Per-ticker multi-agent analysis (11 agents)
       Stage 3: Execution summary
"""
import json
import logging
import os
import sys
import threading
from channels.generic.websocket import WebsocketConsumer
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# ── Report extractor ───────────────────────────────────────
def _extract_report(node_name: str, state_chunk: dict) -> str:
    KEY_MAP = {
        "Market Analyst":       "market_report",
        "Fundamentals Analyst": "fundamentals_report",
        "News Analyst":         "news_report",
        "Social Analyst":       "sentiment_report",
        "Bull Researcher":      "investment_debate_state",
        "Bear Researcher":      "investment_debate_state",
        "Research Manager":     "investment_debate_state",
        "Trader":               "trader_investment_plan",
        "Aggressive Analyst":   "risk_debate_state",
        "Conservative Analyst": "risk_debate_state",
        "Neutral Analyst":      "risk_debate_state",
        "Portfolio Manager":    "final_trade_decision",
    }
    key = KEY_MAP.get(node_name)
    if not key:
        return str(state_chunk)[:500]
    val = state_chunk.get(key)
    if val is None:
        return "Analysis complete."
    if isinstance(val, dict):
        if node_name == "Bull Researcher":
            history = val.get("bull_history", [])
            if history: return history[-1].get("content", str(history[-1]))[:800]
        elif node_name == "Bear Researcher":
            history = val.get("bear_history", [])
            if history: return history[-1].get("content", str(history[-1]))[:800]
        elif node_name == "Research Manager":
            return val.get("judge_decision", val.get("current_response", str(val)))[:800]
        elif node_name == "Aggressive Analyst":
            history = val.get("aggressive_history", [])
            if history: return history[-1].get("content", str(history[-1]))[:800]
        elif node_name == "Conservative Analyst":
            history = val.get("conservative_history", [])
            if history: return history[-1].get("content", str(history[-1]))[:800]
        elif node_name == "Neutral Analyst":
            history = val.get("neutral_history", [])
            if history: return history[-1].get("content", str(history[-1]))[:800]
        return str(val)[:800]
    return str(val)[:800]


def _extract_rating(final_decision: str) -> str:
    if not final_decision:
        return "Hold"
    text = str(final_decision).upper()
    for rating in ["OVERWEIGHT", "UNDERWEIGHT", "BUY", "SELL", "HOLD"]:
        if rating in text:
            return rating.capitalize()
    return "Hold"


# ── Consumer 1: General trading updates ───────────────────
class TradingConsumer(WebsocketConsumer):
    def connect(self):
        async_to_sync(self.channel_layer.group_add)("trading_updates", self.channel_name)
        self.accept()
        self.send(text_data=json.dumps({"type": "connected", "message": "Live trading updates connected."}))

    def disconnect(self, close_code):
        async_to_sync(self.channel_layer.group_discard)("trading_updates", self.channel_name)

    def receive(self, text_data):
        pass

    def trading_update(self, event):
        self.send(text_data=json.dumps({
            "type": event.get("event"),
            "data": event.get("data", {}),
        }))


# ── Consumer 2: Agent Room ─────────────────────────────────
class AgentRoomConsumer(WebsocketConsumer):
    """
    Streams the full autonomous pipeline to the Agent Room page.

    Protocol (client → server):
      { action: "run_full_pipeline" }   ← runs Capitol Trades + all tickers
      { action: "run_analysis", ticker: "NVDA" }  ← single ticker (legacy)

    Protocol (server → client):
      Capitol Trades stage:
        { type: "capitol_start" }
        { type: "capitol_ticker", ticker, politician, trade_type, amount, date }
        { type: "capitol_done", tickers: [...] }

      Per-ticker analysis:
        { type: "ticker_start", ticker }
        { type: "agent_start",  ticker, agent }
        { type: "agent_done",   ticker, agent, content }
        { type: "agent_error",  ticker, agent, content }
        { type: "ticker_done",  ticker, rating, action, price_target, stop_loss, thesis }

      Pipeline complete:
        { type: "pipeline_complete", tickers: [...] }
        { type: "run_error", content }
    """

    def connect(self):
        self.accept()
        self._send_json({"type": "connected", "message": "Agent Room connected."})

    def disconnect(self, close_code):
        pass

    def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return
        action = data.get("action")
        if action == "run_full_pipeline":
            strategy = data.get("strategy", "capitol")
            manual_tickers = data.get("tickers", None)
            t = threading.Thread(target=self._run_full_pipeline, args=(strategy, manual_tickers), daemon=True)
            t.start()
        elif action == "run_analysis":
            ticker = data.get("ticker", "").strip().upper()
            if ticker:
                t = threading.Thread(target=self._run_single_ticker, args=(ticker,), daemon=True)
                t.start()

    def _send_json(self, payload: dict):
        try:
            self.send(text_data=json.dumps(payload))
        except Exception:
            pass

    # ── Full pipeline: Capitol Trades → per-ticker analysis ──
    def _run_full_pipeline(self, strategy='capitol', manual_tickers=None):
        from django.conf import settings
        import time

        if strategy == 'watchlist' and manual_tickers:
            # Watchlist mode — use provided tickers directly
            tickers_data = [{'ticker': t, 'politician': 'Watchlist', 'trade_type': 'Analysis',
                              'amount': '—', 'date': 'Today'} for t in manual_tickers]
        elif strategy == 'wheel' and manual_tickers:
            # Wheel mode — use selected Alpaca positions
            tickers_data = [{'ticker': t, 'politician': 'Wheel Position', 'trade_type': 'Options Analysis',
                              'amount': '—', 'date': 'Today'} for t in manual_tickers]
        else:
            # Capitol Trades mode — try real API, fall back to demo
            tickers_data = self._fetch_capitol_trades(settings)
            if not tickers_data:
                tickers_data = self._demo_capitol_trades()

        # Stage 1: stream ticker selection
        self._send_json({"type": "capitol_start"})
        time.sleep(0.5)
        for item in tickers_data:
            self._send_json({
                "type":       "capitol_ticker",
                "ticker":     item["ticker"],
                "politician": item["politician"],
                "trade_type": item["trade_type"],
                "amount":     item.get("amount", "Disclosed"),
                "date":       item.get("date", "Recent"),
            })
            time.sleep(0.8)

        ticker_list = [d["ticker"] for d in tickers_data]
        self._send_json({"type": "capitol_done", "tickers": ticker_list})
        time.sleep(0.6)

        # Stage 2: run agents on each ticker
        for ticker in ticker_list:
            self._send_json({"type": "ticker_start", "ticker": ticker})
            result = self._run_ticker_analysis(ticker, settings)
            self._send_json({
                "type":         "ticker_done",
                "ticker":       ticker,
                "rating":       result.get("rating", "Hold"),
                "action":       result.get("action", "hold"),
                "price_target": result.get("price_target"),
                "stop_loss":    result.get("stop_loss"),
                "thesis":       result.get("thesis", ""),
            })
            time.sleep(0.5)

        self._send_json({"type": "pipeline_complete", "tickers": ticker_list})

    def _fetch_capitol_trades(self, settings) -> list:
        """Try to fetch real Capitol Trades data via Quiver Quantitative."""
        try:
            import requests
            api_key = getattr(settings, "QUIVER_API_KEY", "")
            if not api_key or api_key == "preview":
                return []
            headers = {"Authorization": f"Token {api_key}"}
            resp = requests.get(
                "https://api.quiverquant.com/beta/live/congresstrading",
                headers=headers, timeout=10
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            # Score and filter: recent buys only, top politicians
            TOP_POLITICIANS = {
                "Nancy Pelosi", "Michael McCaul", "Josh Gottheimer",
                "Dan Crenshaw", "Ro Khanna", "Brian Mast",
            }
            seen = set()
            results = []
            for trade in data[:50]:
                politician = trade.get("Representative", "")
                ticker = trade.get("Ticker", "").upper()
                trade_type = trade.get("Transaction", "")
                if (ticker and ticker not in seen and
                        "purchase" in trade_type.lower() and
                        any(p.lower() in politician.lower() for p in TOP_POLITICIANS)):
                    seen.add(ticker)
                    results.append({
                        "ticker":     ticker,
                        "politician": politician,
                        "trade_type": "Buy",
                        "amount":     trade.get("Range", "Disclosed"),
                        "date":       trade.get("TransactionDate", "Recent"),
                    })
                if len(results) >= 4:
                    break
            return results
        except Exception as e:
            logger.warning(f"Capitol Trades fetch failed: {e}")
            return []

    def _demo_capitol_trades(self) -> list:
        """Demo Capitol Trades data when API key not available."""
        import time
        return [
            {"ticker": "NVDA", "politician": "Nancy Pelosi",     "trade_type": "Buy", "amount": "$500K–$1M",   "date": "May 5, 2026"},
            {"ticker": "MSFT", "politician": "Josh Gottheimer",  "trade_type": "Buy", "amount": "$250K–$500K", "date": "May 4, 2026"},
            {"ticker": "AMZN", "politician": "Michael McCaul",   "trade_type": "Buy", "amount": "$100K–$250K", "date": "May 3, 2026"},
        ]

    def _run_ticker_analysis(self, ticker: str, settings) -> dict:
        """Run the full 11-agent TradingAgents pipeline on a single ticker."""
        os.environ.setdefault("OPENAI_API_KEY", getattr(settings, "OPENAI_API_KEY", ""))
        api_key = getattr(settings, "OPENAI_API_KEY", "preview")
        if api_key == "preview" or not api_key:
            return self._run_demo_ticker(ticker)
        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            from tradingagents.default_config import DEFAULT_CONFIG
            import datetime
            config = {**DEFAULT_CONFIG}
            config["llm_provider"]    = getattr(settings, "LLM_PROVIDER", "openai")
            config["deep_think_llm"]  = getattr(settings, "DEEP_MODEL", "gpt-4o")
            config["quick_think_llm"] = getattr(settings, "QUICK_MODEL", "gpt-4o-mini")
            config["online_tools"]    = True
            ta = TradingAgentsGraph(debug=False, config=config)
            trade_date = datetime.date.today().strftime("%Y-%m-%d")
            final_report = None
            for chunk in ta.graph.stream(
                {"company_of_interest": ticker, "trade_date": trade_date},
                {"recursion_limit": 60},
            ):
                for node_name, state_update in chunk.items():
                    self._send_json({"type": "agent_start", "ticker": ticker, "agent": node_name})
                    report = _extract_report(node_name, state_update)
                    self._send_json({"type": "agent_done", "ticker": ticker, "agent": node_name, "content": report})
                    if node_name == "Portfolio Manager":
                        final_report = report
            rating = _extract_rating(final_report)
            action_map = {"Buy": "buy", "Overweight": "buy", "Hold": "hold", "Underweight": "reduce", "Sell": "sell"}
            return {
                "rating":       rating,
                "action":       action_map.get(rating, "hold"),
                "price_target": None,
                "stop_loss":    None,
                "thesis":       final_report or "",
            }
        except Exception as e:
            logger.error(f"TradingAgents pipeline error for {ticker}: {e}", exc_info=True)
            self._send_json({"type": "run_error", "content": str(e)})
            return {"rating": "Hold", "action": "hold", "thesis": str(e)}

    def _run_demo_ticker(self, ticker: str) -> dict:
        """Demo mode: simulates agent progression without real LLM keys."""
        import time
        DEMO_REPORTS = {
            "Market Analyst":       f"RSI(14): 58 — bullish momentum. MACD crossed above signal 3 sessions ago. Price above 20/50/200-day MAs. Volume 15% above average on up days. Bollinger Bands expanding — breakout forming.",
            "Fundamentals Analyst": f"Revenue growth +22% YoY. Gross margin 74.8% — expanding. FCF yield 3.2%. P/E 28x vs sector 31x — discount. Net cash position. Beat earnings 6 consecutive quarters.",
            "News Analyst":         f"Positive: AI infrastructure spending tailwind. Positive: New product cycle. Neutral: Fed rate path uncertainty. Risk: EU regulatory scrutiny (limited exposure). Net: positive news flow.",
            "Social Analyst":       f"Reddit bullish sentiment 68%. Twitter mentions +34% WoW. StockTwits bull/bear 2.8:1. 14 institutional upgrades vs 3 downgrades. Short interest 2.1% — low.",
            "Bull Researcher":      f"Compelling entry: strong momentum + expanding fundamentals + AI tailwind. Management beat guidance 6 consecutive quarters. Valuation discount to peers unjustified. Price target: +28% over 12 months.",
            "Bear Researcher":      f"Valuation elevated — multiple compression risk in risk-off environment. Competition intensifying. Insider selling accelerated over 90 days. AI capex cycle could decelerate faster than consensus.",
            "Research Manager":     f"Bull case more compelling. Bear valuation concern valid but overstated. Investment Plan: Initiate long. Entry on 3-5% pullback. Position size: 4-5% of portfolio. Horizon: 6-12 months.",
            "Trader":               f"Trade Proposal — BUY\n• Entry: Market open\n• Position: 4% of portfolio\n• Target: +25% from entry\n• Stop loss: -8% trailing\n• Risk/reward: 3.1:1",
            "Aggressive Analyst":   f"3.1:1 R/R is attractive. Recommend 6% position given setup strength. Stop at -8% appropriate. Add on 52-week high breakout. Upside scenario: +40% if AI cycle accelerates.",
            "Conservative Analyst": f"Reduce to 3% position. Macro uncertainty + beta 1.4. Tighten stop to -5%. Keep sector exposure below 15%.",
            "Neutral Analyst":      f"4% position at -8% stop within normal parameters. Max drawdown contribution: 0.32% — acceptable. Move stop to breakeven after +10% gain.",
            "Portfolio Manager":    f"FINAL DECISION — {ticker}\n\nRATING: BUY ✓\n\nApproved.\n• Entry: 4% at market\n• Target: +25% (12-month)\n• Stop: -8% trailing\n• Thesis: Strong fundamentals + momentum + AI tailwind = asymmetric upside\n\nOrder authorised for execution.",
        }
        agents_in_order = [
            "Market Analyst", "Fundamentals Analyst", "News Analyst", "Social Analyst",
            "Bull Researcher", "Bear Researcher", "Research Manager",
            "Trader", "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
            "Portfolio Manager",
        ]
        for agent in agents_in_order:
            self._send_json({"type": "agent_start", "ticker": ticker, "agent": agent})
            time.sleep(2.2)
            self._send_json({"type": "agent_done", "ticker": ticker, "agent": agent,
                             "content": DEMO_REPORTS.get(agent, "Analysis complete.")})
            time.sleep(0.2)
        return {
            "rating":       "Buy",
            "action":       "buy",
            "price_target": None,
            "stop_loss":    None,
            "thesis":       DEMO_REPORTS["Portfolio Manager"],
        }

    # ── Legacy single-ticker support ──────────────────────
    def _run_single_ticker(self, ticker: str):
        from django.conf import settings
        self._send_json({"type": "capitol_start"})
        import time; time.sleep(0.3)
        self._send_json({"type": "capitol_ticker", "ticker": ticker,
                         "politician": "Manual Entry", "trade_type": "Analysis",
                         "amount": "—", "date": "Today"})
        time.sleep(0.5)
        self._send_json({"type": "capitol_done", "tickers": [ticker]})
        time.sleep(0.4)
        self._send_json({"type": "ticker_start", "ticker": ticker})
        result = self._run_ticker_analysis(ticker, settings)
        self._send_json({
            "type":         "ticker_done",
            "ticker":       ticker,
            "rating":       result.get("rating", "Hold"),
            "action":       result.get("action", "hold"),
            "price_target": result.get("price_target"),
            "stop_loss":    result.get("stop_loss"),
            "thesis":       result.get("thesis", ""),
        })
        self._send_json({"type": "pipeline_complete", "tickers": [ticker]})
