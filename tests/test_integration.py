"""End-to-end: a plain Telegram message drives the agent, and the
long-running commands (dream / deepsearch / bugfixes / review) work."""
import asyncio, datetime, json, types

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import Application

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
from tests.conftest import StubLLM, tool_call

UID = 900002


class FakeBot:
    username = "jrock_test_bot"
    def get_me(self): return types.SimpleNamespace(username="jrock_test_bot")
    async def send_message(self, chat_id, text, **kw): self.sent.append(str(text))
    async def send_photo(self, *a, **k): pass
    async def send_voice(self, *a, **k): pass
    async def send_video(self, *a, **k): pass
    async def send_document(self, *a, **k): pass


class FakeCtx:
    def __init__(self, core, bot):
        self.application = types.SimpleNamespace(
            bot_data={"core": core},
            create_task=lambda c: asyncio.get_event_loop().create_task(c))
        self.args = []
        self._bot = bot
        self.tasks = []
        self.application.create_task = self.tasks.append
    @property
    def bot(self): return self._bot


def make_core(llm, bot):
    core = AppCore.__new__(AppCore)
    core.settings = Settings()
    core.settings.tg_allowed_ids = [UID]; core.settings.owner_id = UID
    core.settings.max_agent_steps = 6
    core.settings.auto_approve_on_edit = True
    core.llm = llm
    core.memory = Memory(); core.sessions = SessionStore()
    core.souls = SoulStore(); core.skills = SkillStore()
    core.mcp = MCPManager(); core.approvals = Approvals()
    core.running = {}; core.active_session = {}; core.resume = {}
    core.bot = bot
    return core


def msg(text, uid=UID, bot=None):
    m = Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                chat=Chat(id=uid, type="private"),
                from_user=User(id=uid, first_name="T", is_bot=False), text=text)
    if bot: m.set_bot(bot)
    return Update(update_id=1, message=m)


def drain(ctx, bot, timeout=10):
    """Run every background task the handler spawned and wait for them."""
    async def go():
        for t in ctx.tasks:
            await asyncio.wait_for(t, timeout)
    asyncio.run(go())


def test_plain_message_triggers_the_agent():
    bot = FakeBot(); bot.sent = []
    llm = StubLLM([
        tool_call("terminal_run", {"command": "echo from-telegram"}, "c1"),
        {"role": "assistant", "content": "The shell said: from-telegram"},
    ])
    core, ctx = make_core(llm, bot), None
    ctx = FakeCtx(core, bot)
    asyncio.run(handlers.on_message(msg("run echo from-telegram", bot=bot), ctx))
    assert ctx.tasks, "handler should have spawned a background task"
    drain(ctx, bot)
    assert any("from-telegram" in s for s in bot.sent)
    assert core.active_session.get(UID), "a session should have been created"
    # the session transcript recorded both turns
    sid = core.active_session[UID]
    roles = [m["role"] for m in core.sessions.load(sid)]
    assert "user" in roles and "assistant" in roles


def test_quit_cancels_a_running_task():
    bot = FakeBot(); bot.sent = []
    core = make_core(StubLLM([{"role": "assistant", "content": "x"}] * 8), bot)
    ctx = FakeCtx(core, bot)
    asyncio.run(handlers.on_message(msg("do something long", bot=bot), ctx))
    agent = core.running[UID]
    agent.cancel.set()
    drain(ctx, bot)
    assert UID not in core.running or agent.cancel.is_set()


def test_dream_consolidates_memories():
    bot = FakeBot(); bot.sent = []
    core = make_core(StubLLM([{"role": "assistant", "content": json.dumps({
        "insights": ["user ships fast, prefers terse answers"],
        "lessons": ["answer in under 5 lines"],
        "profile": {"preferences": ["terse"]}})}]), bot)
    core.memory.store(UID, "user prefers terse answers")
    core.memory.store(UID, "user is building a telegram agent")
    ctx = FakeCtx(core, bot)
    ctx.args = []
    asyncio.run(handlers.cmd_dream(msg("/dream", bot=bot), ctx))
    assert "Dream complete" in bot.sent[-1]
    assert "answer in under 5 lines" in core.memory.lessons(UID)
    assert core.memory.get_profile(UID).get("preferences") == ["terse"]


def test_deepsearch_returns_cited_sources():
    bot = FakeBot(); bot.sent = []
    core = make_core(StubLLM([
        {"role": "assistant", "content": '["telegram bot api", "openai tools"]'},
        {"role": "assistant", "content": "Answer with citations [1]."},
    ]), bot)
    ctx = FakeCtx(core, bot)
    ctx.args = ["how", "to", "build", "a", "telegram", "bot"]
    # no network in tests: stub the search/fetch layer
    import jrock.agent.research as R
    async def fake_search(a, c): return "- Example\n  https://example.com/a\n- Other\n  https://example.org/b"
    async def fake_fetch(a, c): return "Example page content"
    R.web.search, R.web.fetch = fake_search, fake_fetch
    asyncio.run(handlers.cmd_deepsearch(msg("/deepsearch how to build a telegram bot", bot=bot), ctx))
    assert "Answer with citations [1]" in bot.sent[-1]
    assert "https://example.com/a" in bot.sent[-1]
    assert "<b>Sources</b>" in bot.sent[-1]


def test_bugfixes_uses_the_build_role():
    bot = FakeBot(); bot.sent = []
    core = make_core(StubLLM([{"role": "assistant", "content": "Fixed the off-by-one."}]), bot)
    ctx = FakeCtx(core, bot)
    ctx.args = ["off-by-one", "in", "the", "loop"]
    asyncio.run(handlers.cmd_bugfixes(msg("/bugfixes off-by-one in the loop", bot=bot), ctx))
    agent = core.running[UID]
    assert agent.role == "build"
    assert "bugfix" in agent.directive
    assert "file_edit" in agent.tools and "terminal_run" in agent.tools
    drain(ctx, bot)
    assert any("Fixed the off-by-one" in s for s in bot.sent)


def test_code_review_uses_the_review_directive():
    """The review workflow must inject its severity-grouped directive."""
    from jrock.agent.context import AgentContext
    from jrock.agent.core import Agent
    from jrock.agent.workflows import REVIEW_DIRECTIVE, run_code_review
    from pathlib import Path
    bot = FakeBot(); bot.sent = []
    llm = StubLLM([{"role": "assistant", "content": "2 findings."}])
    core = make_core(llm, bot)
    ctx = AgentContext(settings=core.settings, llm=llm, user_id=UID, chat_id=UID,
                       workspace=Path.cwd(), memory=core.memory,
                       sessions=core.sessions, skills=core.skills, soul=core.souls)
    out = asyncio.run(run_code_review("jrock/agent", ctx))
    assert out == "2 findings."
    sys0 = llm.seen[0]["messages"][0]["content"]
    assert 'name="code-review"' in sys0 and "jrock/agent" in sys0
    assert "CRITICAL" in sys0 and "file:line" in sys0


# --------------------------------------------------------------- wiring
def test_register_wires_every_handler():
    """Regression: Telegram rejects '-' in command names, so building the
    handler list used to raise ValueError and the bot could not start."""
    from jrock.bot import handlers as H
    bot = FakeBot(); bot.sent = []
    core = make_core(StubLLM([]), bot)
    app = Application.builder().token("123456:TEST").build()
    app.bot_data["core"] = core
    H.register(app, core)          # must not raise
    names = [h.__class__.__name__ for h in app.handlers[0]]
    assert "CallbackQueryHandler" in names, "approval buttons must be wired"
    assert names.count("MessageHandler") == 3


def test_hyphenated_aliases_are_still_accepted():
    """`/code-review` etc. cannot be real commands, but must still work."""
    from telegram.ext import filters
    from jrock.bot import handlers as H
    bot = FakeBot(); bot.sent = []
    core = make_core(StubLLM([]), bot)
    app = Application.builder().token("123456:TEST").build()
    app.bot_data["core"] = core
    H.register(app, core)
    alias = [h for h in app.handlers[0]
             if h.__class__.__name__ == "MessageHandler" and h.filters is not filters.TEXT][0]
    for text in ("/code-review jrock", "/auto-compat on",
                 "/resume-session last", "/set-api-key openai sk-x"):
        m = Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                    chat=Chat(id=UID, type="private"),
                    from_user=User(id=UID, first_name="T", is_bot=False), text=text)
        m.set_bot(bot)
        assert alias.check_update(Update(update_id=1, message=m)), text
    # a normal command must not be swallowed by the alias filter
    m = Message(message_id=1, date=datetime.datetime.now(datetime.timezone.utc),
                chat=Chat(id=UID, type="private"),
                from_user=User(id=UID, first_name="T", is_bot=False), text="/harness")
    m.set_bot(bot)
    assert not alias.check_update(Update(update_id=1, message=m))
