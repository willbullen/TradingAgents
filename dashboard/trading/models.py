"""
trading/models.py
=================
Django models for the TradingAgents autonomous dashboard.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import models
from django.utils import timezone


RATING_COLOURS = {
    "Buy":         "#10B981",
    "Overweight":  "#3B82F6",
    "Hold":        "#F59E0B",
    "Underweight": "#F97316",
    "Sell":        "#EF4444",
    "Error":       "#475569",
}
WHEEL_ELIGIBLE = {"Buy", "Overweight", "Hold"}


class TradeDecision(models.Model):
    """Stores each TradingAgents analysis result — one per (ticker, trade_date)."""
    RATING_CHOICES = [
        ("Buy", "Buy"), ("Overweight", "Overweight"), ("Hold", "Hold"),
        ("Underweight", "Underweight"), ("Sell", "Sell"), ("Error", "Error"),
    ]
    ACTION_CHOICES = [
        ("buy", "Buy"), ("sell", "Sell"), ("reduce", "Reduce"),
        ("hold", "Hold"), ("dry_run", "Dry Run"), ("error", "Error"),
    ]

    ticker            = models.CharField(max_length=20, db_index=True)
    trade_date        = models.DateField(default=date.today, db_index=True)
    rating            = models.CharField(max_length=20, choices=RATING_CHOICES, default="Hold")
    price_target      = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    stop_loss         = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    position_sizing   = models.CharField(max_length=100, blank=True)
    time_horizon      = models.CharField(max_length=100, blank=True)
    investment_thesis = models.TextField(blank=True)
    execution_action  = models.CharField(max_length=20, choices=ACTION_CHOICES, blank=True)
    execution_message = models.TextField(blank=True)
    order_id          = models.CharField(max_length=100, blank=True)
    wheel_activated   = models.BooleanField(default=False)
    capitol_trade_source = models.BooleanField(default=False)
    source            = models.CharField(max_length=50, default="orchestrator")
    run               = models.ForeignKey(
        "OrchestratorRun", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="decisions",
    )
    created_at        = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["ticker", "trade_date"])]
        verbose_name = "Trade Decision"
        verbose_name_plural = "Trade Decisions"

    def __str__(self):
        return f"{self.ticker} {self.trade_date} — {self.rating}"

    @property
    def rating_colour(self) -> str:
        return RATING_COLOURS.get(self.rating, "#475569")

    @property
    def rating_color(self) -> str:
        """Alias for template compatibility."""
        return self.rating_colour

    @property
    def is_bullish(self) -> bool:
        return self.rating in {"Buy", "Overweight"}

    @property
    def is_bearish(self) -> bool:
        return self.rating in {"Sell", "Underweight"}

    @property
    def is_wheel_eligible(self) -> bool:
        return self.rating in WHEEL_ELIGIBLE


class Position(models.Model):
    """Live Alpaca position snapshot — synced every minute by Celery."""
    ticker            = models.CharField(max_length=20, unique=True, db_index=True)
    qty               = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    avg_entry_price   = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    current_price     = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    market_value      = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cost_basis        = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    unrealised_pl     = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    unrealised_pl_pct = models.DecimalField(max_digits=8,  decimal_places=4, default=0)
    stop_loss_floor   = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    side              = models.CharField(max_length=10, default="long")
    asset_class       = models.CharField(max_length=20, default="us_equity")
    last_synced       = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-market_value"]
        verbose_name = "Position"
        verbose_name_plural = "Positions"

    def __str__(self):
        return f"{self.ticker} × {self.qty} @ ${self.avg_entry_price}"

    @property
    def pl_positive(self) -> bool:
        return self.unrealised_pl >= 0

    @property
    def pl_color(self) -> str:
        return "green" if self.unrealised_pl >= 0 else "red"

    @property
    def stop_distance_pct(self) -> float | None:
        if self.stop_loss_floor and self.current_price and self.current_price > 0:
            return float((self.current_price - self.stop_loss_floor) / self.current_price * 100)
        return None

    @property
    def latest_decision(self):
        return TradeDecision.objects.filter(ticker=self.ticker).first()


class CapitolTrade(models.Model):
    """Politician trade disclosure from Quiver Quantitative."""
    PARTY_CHOICES = [("D", "Democrat"), ("R", "Republican"), ("I", "Independent")]

    politician       = models.CharField(max_length=200, db_index=True)
    party            = models.CharField(max_length=1, choices=PARTY_CHOICES, blank=True)
    chamber          = models.CharField(max_length=10, blank=True)
    ticker           = models.CharField(max_length=20, db_index=True)
    transaction_type = models.CharField(max_length=30, default="purchase")
    amount           = models.CharField(max_length=100, blank=True)
    amount_min       = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    amount_max       = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    transaction_date = models.DateField(db_index=True)
    disclosure_date  = models.DateField(null=True, blank=True)
    score            = models.FloatField(default=0.0)
    analysed         = models.BooleanField(default=False, db_index=True)
    ta_rating        = models.CharField(max_length=20, blank=True)
    fetched_at       = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-transaction_date", "-score"]
        unique_together = [("politician", "ticker", "transaction_date", "transaction_type")]
        verbose_name = "Capitol Trade"
        verbose_name_plural = "Capitol Trades"

    def __str__(self):
        return f"{self.politician} — {self.transaction_type.upper()} {self.ticker} ({self.transaction_date})"

    @property
    def is_purchase(self) -> bool:
        return "purchase" in self.transaction_type.lower()

    @property
    def party_colour(self) -> str:
        return {"D": "#3B82F6", "R": "#EF4444", "I": "#F59E0B"}.get(self.party, "#475569")

    @property
    def party_color(self) -> str:
        return self.party_colour

    @property
    def days_ago(self) -> int:
        return (date.today() - self.transaction_date).days


class WheelContract(models.Model):
    """One options contract in the Wheel Strategy lifecycle."""
    STAGE_CHOICES = [("csp", "Cash-Secured Put"), ("cc", "Covered Call")]
    STATUS_CHOICES = [
        ("open",        "Open"),
        ("expired",     "Expired Worthless"),
        ("assigned",    "Assigned"),
        ("called_away", "Called Away"),
        ("closed",      "Closed Early"),
    ]

    ticker            = models.CharField(max_length=20, db_index=True)
    stage             = models.CharField(max_length=5, choices=STAGE_CHOICES)
    strike            = models.DecimalField(max_digits=12, decimal_places=2)
    expiry            = models.DateField()
    contracts         = models.IntegerField(default=1)
    premium_collected = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cost_basis        = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status            = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    order_id          = models.CharField(max_length=100, blank=True)
    ta_rating         = models.CharField(max_length=20, blank=True)
    ta_price_target   = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    opened_at         = models.DateTimeField(default=timezone.now)
    closed_at         = models.DateTimeField(null=True, blank=True)
    cycle_profit      = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    notes             = models.TextField(blank=True)

    class Meta:
        ordering = ["-opened_at"]
        verbose_name = "Wheel Contract"
        verbose_name_plural = "Wheel Contracts"

    def __str__(self):
        return f"{self.ticker} {self.stage.upper()} ${self.strike} exp {self.expiry} [{self.status}]"

    @property
    def days_to_expiry(self) -> int:
        return (self.expiry - date.today()).days

    @property
    def is_expired(self) -> bool:
        return self.days_to_expiry < 0

    @property
    def annualised_roi(self) -> float | None:
        if self.cost_basis and self.cost_basis > 0 and self.premium_collected > 0:
            days_held = max((date.today() - self.opened_at.date()).days, 1)
            return round(float(self.premium_collected / self.cost_basis / days_held) * 365 * 100, 2)
        return None


class OrchestratorRun(models.Model):
    """Log record for each autonomous orchestrator execution."""
    STATUS_CHOICES = [
        ("running", "Running"), ("success", "Success"),
        ("error", "Error"), ("partial", "Partial Success"),
    ]
    MODE_CHOICES = [
        ("full", "Full Run"), ("analyse", "Analyse Only"),
        ("stops", "Trailing Stops"), ("wheel", "Wheel Cycle"),
        ("watchlist", "Watchlist"), ("capitol", "Capitol Trades"),
    ]

    mode             = models.CharField(max_length=20, choices=MODE_CHOICES, default="full")
    status           = models.CharField(max_length=20, choices=STATUS_CHOICES, default="running")
    strategy         = models.CharField(max_length=30, blank=True)
    tickers_analysed = models.JSONField(default=list)
    decisions_made   = models.IntegerField(default=0)
    orders_placed    = models.IntegerField(default=0)
    buy_count        = models.IntegerField(default=0)
    sell_count       = models.IntegerField(default=0)
    hold_count       = models.IntegerField(default=0)
    error_message    = models.TextField(blank=True)
    started_at       = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at     = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Orchestrator Run"
        verbose_name_plural = "Orchestrator Runs"

    def __str__(self):
        return f"Run {self.started_at.strftime('%Y-%m-%d %H:%M')} [{self.status}]"

    @property
    def duration_display(self) -> str:
        if self.duration_seconds is None:
            return "—"
        if self.duration_seconds < 60:
            return f"{self.duration_seconds:.0f}s"
        return f"{self.duration_seconds / 60:.1f}m"
