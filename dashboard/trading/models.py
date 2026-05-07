from django.db import models
from django.utils import timezone


class TradeDecision(models.Model):
    """Stores each TradingAgents analysis run result."""
    RATING_CHOICES = [
        ("Buy", "Buy"),
        ("Overweight", "Overweight"),
        ("Hold", "Hold"),
        ("Underweight", "Underweight"),
        ("Sell", "Sell"),
        ("Error", "Error"),
    ]
    ACTION_CHOICES = [
        ("buy", "Buy"),
        ("sell", "Sell"),
        ("reduce", "Reduce"),
        ("hold", "Hold"),
        ("dry_run", "Dry Run"),
        ("error", "Error"),
    ]

    ticker = models.CharField(max_length=20)
    trade_date = models.DateField()
    rating = models.CharField(max_length=20, choices=RATING_CHOICES)
    price_target = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    stop_loss = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    position_sizing = models.CharField(max_length=50, blank=True)
    time_horizon = models.CharField(max_length=100, blank=True)
    investment_thesis = models.TextField(blank=True)
    execution_action = models.CharField(max_length=20, choices=ACTION_CHOICES, blank=True)
    execution_message = models.TextField(blank=True)
    order_id = models.CharField(max_length=100, blank=True)
    source = models.CharField(max_length=50, default="orchestrator")  # orchestrator / manual
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["ticker", "trade_date"])]

    def __str__(self):
        return f"{self.ticker} {self.trade_date} — {self.rating}"

    @property
    def rating_color(self):
        return {
            "Buy": "green", "Overweight": "blue",
            "Hold": "yellow", "Underweight": "orange", "Sell": "red",
        }.get(self.rating, "gray")


class Position(models.Model):
    """Mirrors live Alpaca positions, synced by Celery task."""
    ticker = models.CharField(max_length=20, unique=True)
    qty = models.DecimalField(max_digits=12, decimal_places=4)
    avg_entry_price = models.DecimalField(max_digits=12, decimal_places=2)
    current_price = models.DecimalField(max_digits=12, decimal_places=2)
    market_value = models.DecimalField(max_digits=14, decimal_places=2)
    unrealised_pl = models.DecimalField(max_digits=14, decimal_places=2)
    unrealised_pl_pct = models.DecimalField(max_digits=8, decimal_places=4)
    stop_loss_floor = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    side = models.CharField(max_length=10, default="long")
    last_synced = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-market_value"]

    def __str__(self):
        return f"{self.ticker} x{self.qty}"

    @property
    def pl_color(self):
        return "green" if self.unrealised_pl >= 0 else "red"


class CapitolTrade(models.Model):
    """Stores politician trade disclosures fetched from Quiver Quant."""
    PARTY_CHOICES = [("D", "Democrat"), ("R", "Republican"), ("I", "Independent")]

    politician = models.CharField(max_length=100)
    party = models.CharField(max_length=1, choices=PARTY_CHOICES, blank=True)
    chamber = models.CharField(max_length=10, blank=True)  # House / Senate
    ticker = models.CharField(max_length=20)
    transaction_type = models.CharField(max_length=20)  # Purchase / Sale
    amount = models.CharField(max_length=50, blank=True)
    disclosure_date = models.DateField()
    transaction_date = models.DateField(null=True, blank=True)
    analysed = models.BooleanField(default=False)
    ta_rating = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-disclosure_date"]
        unique_together = ["politician", "ticker", "disclosure_date", "transaction_type"]

    def __str__(self):
        return f"{self.politician} — {self.transaction_type} {self.ticker}"

    @property
    def party_color(self):
        return {"D": "blue", "R": "red", "I": "gray"}.get(self.party, "gray")


class WheelContract(models.Model):
    """Tracks active and historical Wheel Strategy contracts."""
    STAGE_CHOICES = [("csp", "Cash-Secured Put"), ("cc", "Covered Call")]
    STATUS_CHOICES = [
        ("open", "Open"),
        ("expired", "Expired Worthless"),
        ("assigned", "Assigned"),
        ("called_away", "Called Away"),
    ]

    ticker = models.CharField(max_length=20)
    stage = models.CharField(max_length=5, choices=STAGE_CHOICES)
    strike = models.DecimalField(max_digits=10, decimal_places=2)
    expiry = models.DateField()
    premium_collected = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cost_basis = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    order_id = models.CharField(max_length=100, blank=True)
    ta_rating = models.CharField(max_length=20, blank=True)
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    cycle_profit = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self):
        return f"{self.ticker} {self.stage.upper()} ${self.strike} exp {self.expiry}"

    @property
    def days_to_expiry(self):
        from datetime import date
        delta = self.expiry - date.today()
        return delta.days


class OrchestratorRun(models.Model):
    """Log of each orchestrator execution."""
    STATUS_CHOICES = [("running", "Running"), ("success", "Success"), ("error", "Error")]

    mode = models.CharField(max_length=20, default="full")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="running")
    tickers_analysed = models.JSONField(default=list)
    decisions_made = models.IntegerField(default=0)
    orders_placed = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Run {self.started_at.strftime('%Y-%m-%d %H:%M')} — {self.status}"
