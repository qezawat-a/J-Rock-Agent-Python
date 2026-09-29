"""Sending long text and generated media back to the chat."""
from __future__ import annotations

import html
import re
from pathlib import Path

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

CHUNK = 3800

# Tags the bot emits itself. Everything else is escaped so that model output
# or file contents can never break Telegram's HTML parser.
ALLOWED = ("b", "i", "u", "s", "code", "pre")
_KEEP = re.compile(r"&lt;(/?)(" + "|".join(ALLOWED) + r")&gt;")


def split(text: str, size: int = CHUNK) -> list[str]:
    if len(text) <= size:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        block = para if not cur else cur + "\n\n" + para
        if len(block) > size:
            if cur:
                out.append(cur)
            while len(para) > size:
                out.append(para[:size])
                para = para[size:]
            cur = para
        else:
            cur = block
    if cur:
        out.append(cur)
    return out


def esc(text: str) -> str:
    """Escape for HTML, but keep the bot's own formatting tags intact."""
    out = html.escape(str(text), quote=False)
    return _KEEP.sub(r"<\1\2>", out)


async def _send(bot: Bot, chat_id: int, part: str) -> None:
    """Send one chunk as HTML; fall back to plain text if Telegram rejects it."""
    try:
        await bot.send_message(chat_id, esc(part), parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True)
    except TelegramError:
        # malformed markup (usually from model output) - never drop the message
        await bot.send_message(chat_id, str(part), disable_web_page_preview=True)


async def send_long(bot: Bot, chat_id: int, text: str) -> None:
    for part in split(str(text or "(empty)")):
        await _send(bot, chat_id, part)


async def deliver(bot: Bot, chat_id: int, payload: dict) -> None:
    """payload keys: photo | voice | video | document (file paths)."""
    try:
        if "photo" in payload:
            await bot.send_photo(chat_id, Path(payload["photo"]))
        elif "voice" in payload:
            await bot.send_voice(chat_id, Path(payload["voice"]))
        elif "video" in payload:
            await bot.send_video(chat_id, Path(payload["video"]))
        elif "document" in payload:
            await bot.send_document(chat_id, Path(payload["document"]))
    except Exception as e:
        await bot.send_message(chat_id, f"Could not deliver the file: {e}")
