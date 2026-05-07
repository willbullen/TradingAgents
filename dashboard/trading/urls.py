from django.urls import path
from . import views

urlpatterns = [
    path("", views.overview, name="overview"),
    path("positions/", views.positions, name="positions"),
    path("capitol-trades/", views.capitol_trades, name="capitol_trades"),
    path("agent-log/", views.agent_log, name="agent_log"),
    path("wheel/", views.wheel_strategy, name="wheel_strategy"),
    path("settings/", views.settings_view, name="settings"),
    path("agent-room/", views.agent_room, name="agent_room"),
    # API
    path("api/trigger-run/", views.trigger_run, name="trigger_run"),
    path("api/trigger-sync/", views.trigger_sync, name="trigger_sync"),
    path("api/positions/", views.api_positions, name="api_positions"),
    path("api/decisions/", views.api_decisions, name="api_decisions"),
]
