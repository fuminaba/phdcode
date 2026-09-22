import asyncio
import logging
from typing import Optional

from telegram import Bot
from telegram.error import BadRequest

from .base import ChannelFeedback

log = logging.getLogger(__name__)


class TelegramFeedback(ChannelFeedback):
    """Telegram's ChannelFeedback: sends one message on start(), edits it
    on progress(), and keeps Telegram's chat-action indicator ("typing",
    "upload_document", ...) alive for the whole duration via a background
    task -- a single send_chat_action only lasts ~5 seconds, nowhere near
    enough for a multi-minute document pipeline.
    """

    def __init__(
            self,
            bot: Bot,
            chat_id: int | str,
            action: str = "typing",
            keep_alive_interval: float = 4.0,
    ):
        self._bot = bot
        self._chat_id = chat_id
        self._action = action
        self._keep_alive_interval = keep_alive_interval
        self._message_id: Optional[int] = None
        self._keep_alive_task: Optional[asyncio.Task] = None

    async def start(self, label: str) -> None:
        message = await self._bot.send_message(chat_id=self._chat_id, text=f"⏳ {label}")
        self._message_id = message.message_id
        self._keep_alive_task = asyncio.create_task(self._keep_alive_loop())

    async def progress(self, label: str, fraction: float) -> None:
        if self._message_id is None:
            return
        percent = round(max(0.0, min(1.0, fraction)) * 100)
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id, message_id=self._message_id,
                text=f"⏳ {label} ({percent}%)",
            )
        except BadRequest as e:
            # Telegram raises if the new text is identical to the current
            # one (two stages landing on the same rounded %), or on rare
            # races -- neither is worth surfacing.
            if "message is not modified" not in str(e).lower():
                log.warning(f"Couldn't update progress message: {e}")

    async def finish(self, label: Optional[str] = None) -> None:
        if self._keep_alive_task is not None:
            self._keep_alive_task.cancel()
            self._keep_alive_task = None
        if self._message_id is None:
            return
        message_id, self._message_id = self._message_id, None
        try:
            if label:
                await self._bot.edit_message_text(chat_id=self._chat_id, message_id=message_id, text=label)
            else:
                await self._bot.delete_message(chat_id=self._chat_id, message_id=message_id)
        except BadRequest as e:
            log.warning(f"Couldn't finalize progress message: {e}")

    async def _keep_alive_loop(self) -> None:
        try:
            while True:
                await self._bot.send_chat_action(chat_id=self._chat_id, action=self._action)
                await asyncio.sleep(self._keep_alive_interval)
        except asyncio.CancelledError:
            pass
