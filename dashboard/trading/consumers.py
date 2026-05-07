import json
from channels.generic.websocket import AsyncWebsocketConsumer


class TradingConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer that pushes live trading updates to the dashboard."""

    async def connect(self):
        await self.channel_layer.group_add("trading_updates", self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("trading_updates", self.channel_name)

    async def trading_update(self, event):
        """Receive a message from the channel layer and forward to WebSocket."""
        await self.send(text_data=json.dumps({
            "event": event["event"],
            "data": event["data"],
        }))
