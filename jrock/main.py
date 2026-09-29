"""Entry point: wires everything and starts Telegram polling."""
from __future__ import annotations

import sys

from telegram import Bot
from telegram.ext import Application

from .agent.core import Agent
from .agent.mcp import MCPManager
from .agent.memory import Memory
from .agent.sessions import SessionStore
from .agent.skills import SkillStore
from .agent.soul import SoulStore
from .bot import handlers
from .bot.confirm import Approvals
from .config import Settings
from .llm.client import LLMClient


class AppCore:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.llm = LLMClient(self.settings)
        self.memory = Memory()
        self.sessions = SessionStore()
        self.souls = SoulStore()
        self.skills = SkillStore()
        self.mcp = MCPManager()
        self.approvals = Approvals()
        self.running: dict[int, Agent] = {}
        self.active_session: dict[int, str] = {}
        self.resume: dict[int, bool] = {}
        self.bot: Bot | None = None


async def _post_init(app: Application) -> None:
    core: AppCore = app.bot_data["core"]
    core.bot = app.bot
    if core.mcp.servers:
        report = await core.mcp.connect_all()
        print("[mcp] " + report.replace("\n", " | "))
    print(f"[jrock] ready as {app.bot.username} | "
          f"{core.settings.provider}/{core.settings.model} | "
          f"soul={core.settings.default_soul} | "
          f"users={core.settings.tg_allowed_ids or 'everyone'}")


def main() -> None:
    core = AppCore()
    if not core.settings.tg_token:
        print("TG_BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")
        sys.exit(1)
    app = Application.builder().token(core.settings.tg_token).post_init(_post_init) \
                             .build()
    app.bot_data["core"] = core
    handlers.register(app, core)
    print("[jrock] starting polling…")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
