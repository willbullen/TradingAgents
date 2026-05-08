<p align="center">
  <img src="assets/TauricResearch.png" style="width: 60%; height: auto;">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="./assets/wechat.png" target="_blank"><img alt="WeChat" src="https://img.shields.io/badge/WeChat-TauricResearch-brightgreen?logo=wechat&logoColor=white"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <br>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/Join_GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>

---

# TradingAgents: Autonomous Multi-Agent Trading System

**TradingAgents** is an open-source, multi-agent financial trading framework that mirrors the structure of a real-world quantitative trading firm. Originally developed by Tauric Research, this fork has been massively expanded into a **fully autonomous trading system** with a stunning real-time Django dashboard, automated Alpaca execution, and multi-strategy sourcing.

<div align="center">
🚀 <a href="#autonomous-trading-system">Autonomous System</a> | 📊 <a href="#obsidian-glass-dashboard">Dashboard</a> | ⚡ <a href="#installation--deployment">Installation</a> | 📦 <a href="#tradingagents-core-framework">Core Framework</a>
</div>

---

## Autonomous Trading System

This system runs completely headlessly via Celery Beat schedules or can be monitored and triggered manually via the real-time **Agent Room** dashboard.

The pipeline operates in three distinct stages:

### Stage 1: Strategy & Ticker Selection
Tickers are fed into the system via one of three strategy modes:
1. **Capitol Trades**: Scans legally disclosed stock purchases by high-performing US Congress members (e.g., Nancy Pelosi, Michael McCaul) using the Quiver Quantitative API.
2. **Wheel Strategy**: Scans your existing Alpaca portfolio to identify opportunities for selling cash-secured puts or covered calls.
3. **Watchlist**: A custom list of tickers you define.

### Stage 2: Multi-Agent Analysis (The LangGraph Core)
Once tickers are selected, they are passed to the 11-agent LangGraph pipeline, structured in 4 layers:
- **Market Intelligence**: Fundamentals, Sentiment, News, and Technical analysts gather and process data.
- **Debate Chamber**: Bull and Bear researchers debate the findings; a Research Manager judges the debate.
- **Execution & Risk**: A Trader proposes a specific order; three Risk Analysts (Aggressive, Conservative, Neutral) stress-test it.
- **Final Decision**: The Portfolio Manager issues the final rating: `Buy`, `Overweight`, `Hold`, `Underweight`, or `Sell`.

### Stage 3: Execution & Protection
- **Alpaca Bridge**: `Buy/Overweight` ratings trigger automated Alpaca orders with position sizing based on portfolio value.
- **Trailing Stop Monitor**: A background Celery task runs every 5 minutes during market hours, ratcheting stop-loss floors upward as prices rise to lock in profits.

---

## Obsidian Glass Dashboard

The entire system is controlled and monitored via a stunning Django + Channels real-time dashboard featuring an "Obsidian Glass" dark-mode aesthetic.

### Dashboard Pages
- **Overview**: Portfolio value, P&L curve, win rate, and recent decisions.
- **Agent Room**: Watch the 11 AI agents work in real-time via WebSocket streaming. Switch strategies, select tickers, and see reports generated node-by-node.
- **Positions**: Live Alpaca positions with active trailing stop-loss floors.
- **Capitol Trades**: Feed of recent politician disclosures and agent ratings.
- **Wheel Strategy**: Active options contracts, DTE tracking, and premium collected.
- **Agent Log**: Full historical record of every agent decision and investment thesis.

---

## Installation & Deployment

The system is fully Dockerised. It runs Django (web), Celery Worker (background tasks), Celery Beat (scheduling), Redis (message broker + WebSockets), and PostgreSQL.

### 1. Clone the repository
```bash
git clone https://github.com/willbullen/TradingAgents.git
cd TradingAgents
```

### 2. Configure credentials
Copy the example environment file and fill in your API keys:
```bash
cp .env.example .env
```
Required keys:
- `OPENAI_API_KEY` (or your preferred LLM provider)
- `ALPACA_API_KEY` & `ALPACA_SECRET_KEY` (use paper trading keys initially)
- `QUIVER_API_KEY` (for Capitol Trades data)

### 3. Start the stack
```bash
docker compose up -d --build
```

### 4. Access the Dashboard
The dashboard will be available at `http://localhost:8080`.
The default Django admin credentials are `admin` / `admin` (accessible at `/admin/`).

---

## Background Scheduling (Celery Beat)

The autonomous system requires zero manual intervention. The following schedules are automatically created on first boot and can be modified via the Django admin (`/admin/django_celery_beat/periodictask/`):

| Task | Default Schedule | Purpose |
|---|---|---|
| `run_daily_analysis` | 9:00 AM (Mon-Fri) | Runs the Capitol Trades → TradingAgents → Alpaca pipeline |
| `update_trailing_stops` | Every 5 mins (Market Hours) | Ratchets stop-loss floors upward |
| `run_wheel_cycle` | 9:15 AM (Mon-Fri) | Checks/opens options contracts for the Wheel Strategy |
| `sync_alpaca_positions` | Every 1 min (Market Hours) | Updates live portfolio data for the dashboard |
| `fetch_capitol_trades` | 8:00 AM (Daily) | Refreshes the politician disclosure feed |

---

## TradingAgents Core Framework

If you wish to use the core TradingAgents framework programmatically without the dashboard:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

# Initialize the graph
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
ta = TradingAgentsGraph(debug=True, config=config)

# Forward propagate
_, decision = ta.propagate("NVDA", "2026-05-08")
print(decision)
```

### Supported LLM Providers
The core framework supports: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen, GLM, OpenRouter, Ollama (local), and Azure OpenAI.

### Persistence and Memory
TradingAgents persists state across runs:
- **Decision Log**: Each completed run appends to `~/.tradingagents/memory/trading_memory.md`. The agents review past decisions and outcomes before making new ones.
- **Checkpoint Resume**: LangGraph saves state after each node. If a run crashes, it resumes from the last successful step.

---

## Disclaimer

**This software is for research and educational purposes only.** It is not financial advice. The autonomous execution engine will place real trades if provided with live Alpaca API keys. Always test thoroughly using Alpaca Paper Trading before risking real capital.

---

## Citation

Please reference the original Tauric Research paper if you find the core multi-agent framework useful:
```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yizhe Xiao and Jinhao Wang and Hongcheng Wu and Suotang Lin},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```
