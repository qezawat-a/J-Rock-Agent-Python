"""Shared fixtures: in-memory settings, stub LLM, no-network context."""
import asyncio, time
from pathlib import Path

from jrock.config import Settings
from jrock.agent.context import AgentContext
from jrock.agent.memory import Memory
from jrock.agent.sessions import SessionStore
from jrock.agent.soul import SoulStore
from jrock.agent.skills import SkillStore


class StubLLM:
    """Replays a scripted list of assistant messages, then echoes."""

    def __init__(self, script=None, echo="stub-ok"):
        self.script = list(script or [])
        self.echo = echo
        self.seen: list[dict] = []

    async def chat(self, messages, tools=None, **kw):
        self.seen.append({"messages": messages, "tools": tools})
        if self.script:
            return self.script.pop(0)
        return {"role": "assistant", "content": self.echo}

    async def list_models(self, provider=None):
        return ["stub-model-a", "stub-model-b"]


def tool_call(name, args, call_id="c1"):
    import json
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                           "function": {"name": name,
                                        "arguments": json.dumps(args)}}]}


def make_ctx(llm=None, **over):
    s = Settings()
    s.auto_approve_on_edit = True
    s.tg_allowed_ids = []
    for k, v in over.items():
        setattr(s, k, v)
    sent, delivered = [], []

    async def notify(t): sent.append(t)
    async def confirm(w): return True
    async def deliver(p): delivered.append(p)

    ss = SessionStore()
    ctx = AgentContext(
        settings=s, llm=llm or StubLLM(), user_id=4242, chat_id=4242,
        notify=notify, confirm=confirm, deliver=deliver,
        workspace=Path.cwd(), memory=Memory(), sessions=ss, mcp=None,
        skills=SkillStore(), soul=SoulStore(), session_id=ss.create(4242, "test"))
    ctx.sent, ctx.delivered = sent, delivered
    return ctx
