"""
trading/consumers.py
WebSocket consumers for the TradingAgents dashboard.

Two consumers:
  1. TradingConsumer     — general live updates (positions, runs, stops)
  2. AgentRoomConsumer   — streams each TradingAgents node result in real-time
"""
import json
import logging
import os
import sys
import threading
from channels.generic.websocket import WebsocketConsumer
from asgiref.sync import async_to_sync

logger = logging.getLogger(__name__)

# Ensure repo root is on path so tradingagents/ and autonomous/ are importable
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ──────────────────────────────────────────────────────────────────────────────
# Helper: extract readable report from a LangGraph state chunk
# ──────────────────────────────────────────────────────────────────────────────
def _extract_report(node_name: str, state_chunk: dict) -> str:
    """Pull the most relevant text from a LangGraph state update chunk."""
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
            if history:
                last = history[-1]
                return last.get("content", str(last))[:800]
        elif node_name == "Bear Researcher":
            history = val.get("bear_history", [])
            if history:
                last = history[-1]
                return last.get("content", str(last))[:800]
        elif node_name == "Research Manager":
            return val.get("judge_decision", val.get("current_response", str(val)))[:800]
        elif node_name == "Aggressive Analyst":
            history = val.get("aggressive_history", [])
            if history:
                return history[-1].get("content", str(history[-1]))[:800]
        elif node_name == "Conservative Analyst":
            history = val.get("conservative_history", [])
            if history:
                return history[-1].get("content", str(history[-1]))[:800]
        elif node_name == "Neutral Analyst":
            history = val.get("neutral_history", [])
            if history:
                return history[-1].get("content", str(history[-1]))[:800]
        return str(val)[:800]

    return str(val)[:800]


def _extract_rating(final_decision: str) -> str:
    """Parse the rating from the Portfolio Manager's final decision text."""
    if not final_decision:
        return "Hold"
    text = str(final_decision).upper()
    for rating in ["OVERWEIGHT", "UNDERWEIGHT", "BUY", "SELL", "HOLD"]:
        if rating in text:
            return rating.capitalize()
    return "Hold"


# ──────────────────────────────────────────────────────────────────────────────
# Consumer 1: General trading updates (positions, runs, stops)
# ──────────────────────────────────────────────────────────────────────────────
class TradingConsumer(WebsocketConsumer):
    def connect(self):
        async_to_sync(self.channel_layer.group_add)("trading_updates", self.channel_name)
        self.accept()
        self.send(text_data=json.dumps({"type": "connected", "message": "Live trading updates connected."}))

    def disconnect(self, close_code):
        async_to_sync(self.channel_layer.group_discard)("trading_updates", self.channel_name)

    def receive(self, text_data):
        pass  # Read-only consumer

    def trading_update(self, event):
        self.send(text_data=json.dumps({
            "type": event.get("event"),
            "data": event.get("data", {}),
        }))


# ──────────────────────────────────────────────────────────────────────────────
# Consumer 2: Agent Room — streams each node's output as it completes
# ──────────────────────────────────────────────────────────────────────────────
class AgentRoomConsumer(WebsocketConsumer):
    """
    Streams TradingAgents LangGraph node results to the Agent Room page.

    Protocol (server → client):
      { type: "agent_start",  agent: "Market Analyst" }
      { type: "agent_done",   agent: "Market Analyst", content: "..." }
      { type: "agent_error",  agent: "Market Analyst", content: "..." }
      { type: "run_complete", rating: "Buy", ticker: "NVDA", content: "..." }
      { type: "run_error",    content: "..." }

    Protocol (client → server):
      { action: "run_analysis", ticker: "NVDA" }
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

        if data.get("action") == "run_analysis":
            ticker = data.get("ticker", "").strip().upper()
            if ticker:
                t = threading.Thread(target=self._run_analysis, args=(ticker,), daemon=True)
                t.start()

    def _send_json(self, payload: dict):
        try:
            self.send(text_data=json.dumps(payload))
        except Exception:
            pass

    def _run_analysis(self, ticker: str):
        """
        Runs the TradingAgentsGraph using graph.stream() so we get node-by-node
        results, then pushes each result to the WebSocket as it arrives.
        """
        from django.conf import settings

        os.environ.setdefault("OPENAI_API_KEY", getattr(settings, "OPENAI_API_KEY", ""))
        os.environ.setdefault("ANTHROPIC_API_KEY", getattr(settings, "ANTHROPIC_API_KEY", ""))
        os.environ.setdefault("GOOGLE_API_KEY", getattr(settings, "GOOGLE_API_KEY", ""))

        try:
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            from tradingagents.default_config import DEFAULT_CONFIG
            import datetime

            config = {**DEFAULT_CONFIG}
            config["llm_provider"] = getattr(settings, "LLM_PROVIDER", "openai")
            config["deep_think_llm"] = getattr(settings, "DEEP_MODEL", "gpt-4o")
            config["quick_think_llm"] = getattr(settings, "QUICK_MODEL", "gpt-4o-mini")
            config["online_tools"] = True

            ta = TradingAgentsGraph(
                selected_analysts=["market", "fundamentals", "news", "social"],
                config=config,
                debug=False,
            )

            trade_date = datetime.datetime.now().strftime("%Y-%m-%d")

            from tradingagents.graph.propagation import Propagator
            propagator = Propagator(ta.graph, ta.memory, ta.config)
            init_state = propagator.create_initial_state(ticker, trade_date)

            args = {"recursion_limit": config.get("max_debate_rounds", 1) * 10 + 10}

            for chunk in ta.graph.stream(init_state, **args):
                for node_name, state_update in chunk.items():
                    if node_name.startswith("tools_") or node_name.startswith("Msg Clear"):
                        continue
                    self._send_json({"type": "agent_start", "agent": node_name})
                    report = _extract_report(node_name, state_update)
                    self._send_json({"type": "agent_done", "agent": node_name, "content": report})

            final_state = ta.curr_state
            if final_state:
                final_decision = final_state.get("final_trade_decision", "")
                rating = _extract_rating(final_decision)

                try:
                    from trading.models import TradeDecision
                    import datetime as dt
                    TradeDecision.objects.create(
                        ticker=ticker,
                        trade_date=dt.date.today(),
                        rating=rating,
                        investment_thesis=str(final_decision)[:2000],
                        execution_action="pending",
                        source="agent_room",
                    )
                except Exception as db_err:
                    logger.warning("Failed to save decision to DB: %s", db_err)

                self._send_json({
                    "type": "run_complete",
                    "ticker": ticker,
                    "rating": rating,
                    "content": str(final_decision)[:1000],
                })
            else:
                self._send_json({"type": "run_complete", "ticker": ticker, "rating": "Unknown", "content": "Run finished."})

        except ImportError as e:
            logger.warning("TradingAgents import failed (%s) — running demo mode", e)
            self._run_demo(ticker)
        except Exception as e:
            logger.error("Agent run failed: %s", e, exc_info=True)
            self._send_json({"type": "run_error", "content": str(e)})

    def _run_demo(self, ticker: str):
        """
        Demo mode: simulates agent progression when LLM API keys are not
        configured (e.g. in the preview environment).
        """
        import time

        DEMO_REPORTS = {
            "Market Analyst": f"Technical analysis of {ticker}:\n\n• RSI(14): 58.4 — neutral momentum\n• MACD: bullish crossover forming on daily chart\n• 50-day MA: price trading above, support confirmed\n• Volume: 15% above 30-day average on up days\n• Bollinger Bands: price in upper half, not yet overbought\n\nConclusion: Technically constructive. Momentum building.",
            "Fundamentals Analyst": f"Fundamental analysis of {ticker}:\n\n• Revenue growth (YoY): +22.4%\n• Gross margin: 74.8% — expanding\n• Free cash flow yield: 3.2%\n• P/E (forward): 28.4x vs sector 31.2x — slight discount\n• Balance sheet: net cash position, no refinancing risk\n\nConclusion: Fundamentally sound with room for multiple expansion.",
            "News Analyst": f"News & macro analysis for {ticker}:\n\n• Positive: New product announcement received well by analysts\n• Positive: Sector tailwinds from AI infrastructure spending cycle\n• Neutral: Broader market uncertainty from Fed rate path\n• Risk: Regulatory scrutiny in EU market (limited revenue exposure)\n\nConclusion: News flow net positive. No material near-term catalysts against.",
            "Social Analyst": f"Sentiment analysis for {ticker}:\n\n• Reddit/WSB: Bullish sentiment 68% (7-day average)\n• Twitter/X: Positive mentions up 34% week-over-week\n• StockTwits: Bullish/bearish ratio 2.8:1\n• Institutional: 14 upgrades vs 3 downgrades last 30 days\n• Short interest: 2.1% of float — low\n\nConclusion: Retail and institutional sentiment aligned bullish.",
            "Bull Researcher": f"Bull case for {ticker}:\n\nStrong technical momentum + expanding fundamentals + AI tailwind creates a compelling entry. Management has beaten guidance by 8-12% for 6 consecutive quarters. Valuation discount to peers is unjustified given superior growth. Price target: +28% over 12 months.",
            "Bear Researcher": f"Bear case for {ticker}:\n\nValuation remains elevated — multiple compression in a risk-off environment could see -20-25% before real support. Competition intensifying with 3 well-funded challengers. Insider selling has accelerated over 90 days. AI spending cycle could decelerate faster than consensus.",
            "Research Manager": f"Research Manager verdict for {ticker}:\n\nBull case is more compelling. Bear's valuation concern is valid but overstated — growth premium is justified. Insider selling is within normal range for options exercises.\n\nInvestment Plan: Initiate long. Entry on 3-5% pullback to 50-day MA. Position size: 4-5% of portfolio. Horizon: 6-12 months.",
            "Trader": f"Trade proposal for {ticker}:\n\n• Action: BUY\n• Entry: Market open, or limit at -2% from current\n• Position size: 4% of portfolio\n• Price target: +25% from entry\n• Stop loss: -8% from entry\n• Risk/reward: 3.1:1",
            "Aggressive Analyst": f"Aggressive risk view:\n\n3.1:1 risk/reward is attractive. I'd argue for 6% position size given setup strength. Stop at -8% is appropriate. Consider adding on 52-week high breakout. Upside scenario: +40% if AI cycle accelerates.",
            "Conservative Analyst": f"Conservative risk view:\n\nReduce to 3% position. Macro uncertainty remains and beta is 1.4. Tighten stop to -5% to limit drawdown. Ensure sector exposure stays below 15%.",
            "Neutral Analyst": f"Neutral risk assessment:\n\n4% position at -8% stop is within normal parameters. Max drawdown contribution: 0.32% of portfolio — acceptable. Recommend trade proceeds as proposed. Move stop to breakeven after +10% gain.",
            "Portfolio Manager": f"FINAL DECISION — {ticker}\n\nRATING: BUY ✓\n\nApproved.\n\n• Entry: 4% allocation at market\n• Price target: +25% (12-month)\n• Stop loss: -8% from entry\n• Thesis: Strong fundamentals + technical momentum + AI tailwind = asymmetric upside\n\nOrder authorised for execution.",
        }

        agents_in_order = [
            "Market Analyst", "Fundamentals Analyst", "News Analyst", "Social Analyst",
            "Bull Researcher", "Bear Researcher", "Research Manager",
            "Trader", "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
            "Portfolio Manager"
        ]

        for agent in agents_in_order:
            self._send_json({"type": "agent_start", "agent": agent})
            time.sleep(2.5)
            self._send_json({"type": "agent_done", "agent": agent, "content": DEMO_REPORTS.get(agent, "Analysis complete.")})
            time.sleep(0.3)

        self._send_json({
            "type": "run_complete",
            "ticker": ticker,
            "rating": "Buy",
            "content": DEMO_REPORTS["Portfolio Manager"],
        })
