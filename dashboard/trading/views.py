from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required
from django.utils import timezone
from datetime import timedelta
import json

from .models import TradeDecision, Position, CapitolTrade, WheelContract, OrchestratorRun


def overview(request):
    """Main dashboard overview page."""
    # Portfolio summary
    positions = Position.objects.all()
    total_value = sum(p.market_value for p in positions)
    total_pl = sum(p.unrealised_pl for p in positions)
    total_pl_pct = (total_pl / (total_value - total_pl) * 100) if (total_value - total_pl) > 0 else 0

    # Recent decisions (last 7 days)
    recent_decisions = TradeDecision.objects.filter(
        created_at__gte=timezone.now() - timedelta(days=7)
    )[:10]

    # Win rate
    closed_decisions = TradeDecision.objects.exclude(rating="Error")
    buys = closed_decisions.filter(rating__in=["Buy", "Overweight"]).count()
    total_rated = closed_decisions.count()
    win_rate = round(buys / total_rated * 100, 1) if total_rated > 0 else 0

    # Latest run
    latest_run = OrchestratorRun.objects.first()

    # Rating distribution for chart
    from django.db.models import Count
    rating_dist = list(
        TradeDecision.objects.values("rating").annotate(count=Count("rating")).order_by("-count")
    )

    # Wheel premium collected
    total_premium = sum(
        w.premium_collected for w in WheelContract.objects.all()
    )

    # Capitol trades count
    capitol_count = CapitolTrade.objects.filter(
        disclosure_date__gte=timezone.now().date() - timedelta(days=14)
    ).count()

    context = {
        "page": "overview",
        "positions": positions,
        "total_value": total_value,
        "total_pl": total_pl,
        "total_pl_pct": round(total_pl_pct, 2),
        "recent_decisions": recent_decisions,
        "win_rate": win_rate,
        "latest_run": latest_run,
        "rating_dist": json.dumps(rating_dist),
        "total_premium": total_premium,
        "capitol_count": capitol_count,
        "position_count": positions.count(),
    }
    return render(request, "trading/overview.html", context)


def positions(request):
    """Live Alpaca positions page."""
    all_positions = Position.objects.all()
    total_value = sum(p.market_value for p in all_positions)
    total_pl = sum(p.unrealised_pl for p in all_positions)

    context = {
        "page": "positions",
        "positions": all_positions,
        "total_value": total_value,
        "total_pl": total_pl,
        "position_count": all_positions.count(),
    }
    return render(request, "trading/positions.html", context)


def capitol_trades(request):
    """Capitol Trades politician disclosure feed."""
    trades = CapitolTrade.objects.all()[:100]
    buys = CapitolTrade.objects.filter(transaction_type__icontains="purchase").count()
    sells = CapitolTrade.objects.filter(transaction_type__icontains="sale").count()
    analysed = CapitolTrade.objects.filter(analysed=True).count()

    # Top politicians by trade count
    from django.db.models import Count
    top_politicians = list(
        CapitolTrade.objects.values("politician", "party")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    context = {
        "page": "capitol_trades",
        "trades": trades,
        "buys": buys,
        "sells": sells,
        "analysed": analysed,
        "top_politicians": top_politicians,
    }
    return render(request, "trading/capitol_trades.html", context)


def agent_log(request):
    """TradingAgents decision log."""
    decisions = TradeDecision.objects.all()[:200]
    runs = OrchestratorRun.objects.all()[:20]

    # Stats
    total = decisions.count()
    buy_count = decisions.filter(rating__in=["Buy", "Overweight"]).count()
    sell_count = decisions.filter(rating__in=["Sell", "Underweight"]).count()
    hold_count = decisions.filter(rating="Hold").count()

    context = {
        "page": "agent_log",
        "decisions": decisions,
        "runs": runs,
        "total": total,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "hold_count": hold_count,
    }
    return render(request, "trading/agent_log.html", context)


def wheel_strategy(request):
    """Wheel Strategy options positions."""
    open_contracts = WheelContract.objects.filter(status="open")
    closed_contracts = WheelContract.objects.exclude(status="open")[:50]

    total_premium = sum(w.premium_collected for w in WheelContract.objects.all())
    open_puts = open_contracts.filter(stage="csp").count()
    open_calls = open_contracts.filter(stage="cc").count()

    # Annualised ROI per ticker
    from django.db.models import Sum
    ticker_premiums = list(
        WheelContract.objects.values("ticker")
        .annotate(total=Sum("premium_collected"))
        .order_by("-total")
    )

    context = {
        "page": "wheel",
        "open_contracts": open_contracts,
        "closed_contracts": closed_contracts,
        "total_premium": total_premium,
        "open_puts": open_puts,
        "open_calls": open_calls,
        "ticker_premiums": ticker_premiums,
    }
    return render(request, "trading/wheel_strategy.html", context)


def settings_view(request):
    """Strategy settings and configuration."""
    from django.conf import settings as django_settings
    context = {
        "page": "settings",
        "alpaca_paper": django_settings.ALPACA_PAPER,
        "llm_provider": django_settings.LLM_PROVIDER,
        "deep_model": django_settings.DEEP_MODEL,
        "quick_model": django_settings.QUICK_MODEL,
        "trail_pct": django_settings.TRAIL_PCT * 100,
        "wheel_dte": django_settings.WHEEL_DTE,
        "alpaca_configured": bool(django_settings.ALPACA_API_KEY),
        "quiver_configured": bool(django_settings.QUIVER_API_KEY),
        "llm_configured": bool(django_settings.OPENAI_API_KEY),
    }
    return render(request, "trading/settings.html", context)


# ── API endpoints for manual triggers ────────────────────────────────────────

@require_POST
def trigger_run(request):
    """Manually trigger an analysis run."""
    from .tasks import run_daily_analysis
    data = json.loads(request.body) if request.body else {}
    mode = data.get("mode", "full")
    dry_run = data.get("dry_run", True)
    tickers = data.get("tickers")
    task = run_daily_analysis.delay(mode=mode, dry_run=dry_run, tickers=tickers)
    return JsonResponse({"task_id": task.id, "status": "queued"})


@require_POST
def trigger_sync(request):
    """Manually trigger an Alpaca position sync."""
    from .tasks import sync_alpaca_positions
    task = sync_alpaca_positions.delay()
    return JsonResponse({"task_id": task.id, "status": "queued"})


def api_positions(request):
    """JSON API for live positions (used by WebSocket fallback)."""
    positions = list(Position.objects.values())
    return JsonResponse({"positions": positions})


def api_decisions(request):
    """JSON API for recent decisions."""
    decisions = list(
        TradeDecision.objects.values(
            "ticker", "trade_date", "rating", "price_target",
            "execution_action", "created_at"
        )[:50]
    )
    return JsonResponse({"decisions": decisions})

def agent_room(request):
    """Agent Room — live view of all 8 TradingAgents agents working in real-time."""
    past_decisions = TradeDecision.objects.order_by("-created_at")[:10]
    context = {
        "page": "agent_room",
        "past_decisions": past_decisions,
    }
    return render(request, "trading/agent_room.html", context)

def landing(request):
    """Public landing page — explains the system, workflow, and how to use it."""
    return render(request, "trading/landing.html", {})
