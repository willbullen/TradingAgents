from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/trading/$", consumers.TradingConsumer.as_asgi()),
    re_path(r"ws/agents/$", consumers.AgentRoomConsumer.as_asgi()),
]
