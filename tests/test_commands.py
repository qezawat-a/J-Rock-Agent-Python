"""Telegram command layer, permission gates and the approval flow."""
import asyncio, datetime, types

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from jrock.bot import handlers
from jrock.bot.confirm import Approvals
from jrock.config import Settings
from jrock.llm.client import LLMClient
from jrock.agent.memory import Memory
from jrock.agent.sessions import SessionStore
from jrock.agent.soul import SoulStore
from jrock.agent.skills import SkillStore
from jrock.agent.mcp import MCPManager
from jrock.main import AppCore

UID = 900001


class FakeBot:
    username = "jrock_test_bot"
    def get_me(self): return types.SimpleNamespace(username="jrock_test_bot")
    async def send_message(self, chat_id, text, **kw): self.sent.append(str(text))
    async def send_photo(self, *a, **k): pass
    async def send_voice(self, *a, **k): pass
    async def send_video(self, *a, **k): pass
    async def send_document(self, *a, **k): pass
    async def answer_callback_query(self, *a, **k): return True
    async def edit_message_reply_markup(self, *a, **k): return True


class FakeCtx:
    """Mimics ContextTypes.DEFAULT_TYPE closely enough for the handlers."""
    def __init__(self, core, bot):
        self.application = types.SimpleNamespace(
            bot_data={"core": core},
            create_task=lambda c: asyncio.get_event_loop().create_task(c))
        self.args = []
        self._bot = bot
    @property
    def bot(self): return self._bot


@pytest.fixture
def env():
    bot = FakeBot(); bot.sent = []
    core = AppCore.__new__(AppCore)
    core.settings = Settings()
    core.settings.tg_allowed_ids = [UID]
    core.settings.owner_id = UID
    core.settings.api_keys = {"openai": "sk-unit-test-secret"}
    core.settings.max_agent_steps = 5
    core.llm = LLMClient(core.settings)
    core.memory = Memory(); core.sessions = SessionStore()
    core.souls = SoulStore(); core.skills = SkillStore()
    core.mcp = MCPManager(); core.approvals = Approvals()
    core.running = {}; core.active_session = {}; core.resume = {}
    core.bot = bot
    return core, FakeCtx(core, bot), bot


def call(handler, text, ctx, uid=UID):
    async def go():
        parts = text.split()
        ctx.args = parts[1:] if parts and parts[0].startswith("/") else []
        m = Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                    chat=Chat(id=uid, type="private"),
                    from_user=User(id=uid, first_name="T", is_bot=False), text=text)
        m.set_bot(ctx.bot)
        return await handler(Update(update_id=1, message=m), ctx)
    return asyncio.run(go())


def test_help_and_status_commands(env):
    core, ctx, bot = env
    call(handlers.cmd_start, "/start", ctx)
    assert "J-Rock" in bot.sent[-1]
    call(handlers.cmd_harness, "/harness", ctx)
    assert "harness" in bot.sent[-1].lower()
    call(handlers.cmd_tools, "/tools", ctx)
    assert "terminal_run" in bot.sent[-1]
    call(handlers.cmd_compat, "/compat", ctx)
    assert "Compatibility" in bot.sent[-1]
    call(handlers.cmd_team, "/team", ctx)
    assert "build" in bot.sent[-1]


def test_settings_are_persisted(env):
    core, ctx, bot = env
    call(handlers.cmd_thinking, "/thinking high", ctx)
    assert core.settings.thinking == "high"
    call(handlers.cmd_provider, "/provider anthropic", ctx)
    assert core.settings.provider == "anthropic"
    call(handlers.cmd_set, "/set model gpt-4o", ctx)
    assert core.settings.model == "gpt-4o"
    call(handlers.cmd_learning, "/learning off", ctx)
    assert core.settings.learning is False
    call(handlers.cmd_autocompat, "/auto-compat on", ctx)
    assert core.settings.auto_compat is True


def test_config_never_leaks_keys(env):
    core, ctx, bot = env
    call(handlers.cmd_config, "/config", ctx)
    assert "sk-unit-test-secret" not in bot.sent[-1]
    assert "set" in bot.sent[-1]


def test_soul_memory_sessions(env):
    core, ctx, bot = env
    call(handlers.cmd_soul, "/soul", ctx)
    assert "fable-5.1" in bot.sent[-1]
    call(handlers.cmd_memory, "/memory add likes terse output", ctx)
    call(handlers.cmd_memory, "/memory terse", ctx)
    assert "terse" in bot.sent[-1]
    # a real session exists once the user has talked to the agent
    core.sessions.create(UID, "earlier chat")
    call(handlers.cmd_sessions, "/sessions", ctx)
    assert "earlier chat" in bot.sent[-1]
    call(handlers.cmd_resume, "/resume-session last", ctx)
    assert "Resumed" in bot.sent[-1]


def test_api_key_owner_only(env):
    core, ctx, bot = env
    call(handlers.cmd_set_api_key, "/set-api-key openai sk-new-1", ctx)
    assert core.settings.api_keys["openai"] == "sk-new-1"
    core.settings.owner_id = 999999
    core.settings.tg_allowed_ids = [999999, UID]
    call(handlers.cmd_set_api_key, "/set-api-key openai sk-hack", ctx)
    assert "Owner only" in bot.sent[-1]
    assert core.settings.api_keys["openai"] == "sk-new-1"


def test_outsiders_are_rejected(env):
    core, ctx, bot = env
    call(handlers.cmd_harness, "/harness", ctx, uid=UID + 7)
    assert "private" in bot.sent[-1]


def test_app_connector_roundtrip(env):
    core, ctx, bot = env
    call(handlers.cmd_app_connector,
         "/app_connector weather https://api.example.com KEY1", ctx)
    call(handlers.cmd_app_connector, "/app_connector", ctx)
    assert "weather" in bot.sent[-1]


def test_approve_button_resolves_the_future(env):
    core, ctx, bot = env
    async def go():
        task = asyncio.get_event_loop().create_task(
            core.approvals.request(bot, UID, "terminal: ls"))
        await asyncio.sleep(0.05)
        assert "Approval needed" in bot.sent[-1]
        token = list(core.approvals._pending)[0]
        cq = CallbackQuery(id="1", chat_instance="ci", data="appr:" + token,
                           from_user=User(id=UID, first_name="T", is_bot=False))
        cq.set_bot(bot)
        u = Update(update_id=2, callback_query=cq); u.set_bot(bot)
        await core.approvals.handler().callback(u, ctx)
        return await asyncio.wait_for(task, 5)
    assert asyncio.run(go()) is True
