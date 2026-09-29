"""Agent loop, tools, roles, memory, sessions, safety."""
import asyncio
import json
from pathlib import Path

from jrock.agent.core import Agent, REGISTRY
from jrock.agent.memory import Memory
from jrock.agent.sessions import SessionStore
from jrock.agent.soul import SoulStore
from jrock.agent.skills import SkillStore
from jrock.agent import harness, team
from jrock.agent.tools import terminal as T, fs as F, web as W
from jrock.agent.research import _parse_list
from jrock.config import DATA_DIR
from tests.conftest import make_ctx, StubLLM, tool_call


def test_loop_runs_tools_and_returns_final_answer():
    script = [
        tool_call("terminal_run", {"command": "echo jrock-test-ok"}, "c1"),
        tool_call("file_write", {"path": "data/workspace/t.txt", "content": "hi"}, "c2"),
        tool_call("file_read", {"path": "data/workspace/t.txt"}, "c3"),
        {"role": "assistant", "content": "All done."},
    ]
    ctx = make_ctx(StubLLM(script))
    out = asyncio.run(Agent(ctx).run("do the thing"))
    tool_text = "".join(m["content"] for m in ctx.llm.seen[-1]["messages"]
                        if m.get("role") == "tool")
    assert "jrock-test-ok" in tool_text
    assert "hi" in tool_text
    assert out == "All done."
    sys0 = ctx.llm.seen[0]["messages"][0]["content"]
    assert "<soul" in sys0 and "<harness>" in sys0
    Path("data/workspace/t.txt").unlink(missing_ok=True)


def test_cancel_stops_the_loop():
    ctx = make_ctx(StubLLM([{"role": "assistant", "content": "x"}] * 10))
    a = Agent(ctx)
    a.cancel.set()
    assert asyncio.run(a.run("go")) == "Stopped by /quit."


def test_unknown_tool_is_reported_not_crashed():
    ctx = make_ctx(StubLLM([tool_call("no_such_tool", {}, "c1"),
                            {"role": "assistant", "content": "gave up"}]))
    assert asyncio.run(Agent(ctx).run("go")) == "gave up"


def test_roles_restrict_tools():
    ctx = make_ctx()
    plan, build = Agent(ctx, role="plan"), Agent(ctx, role="build")
    assert "terminal_run" not in plan.tools and "file_write" not in plan.tools
    assert "web_search" in plan.tools
    assert "terminal_run" in build.tools and "file_write" in build.tools
    assert len(Agent(ctx).tools) == len(REGISTRY)


def test_dangerous_tool_requires_approval():
    ctx = make_ctx(auto_approve_on_edit=False)
    asked = []

    async def deny(what):
        asked.append(what)
        return False

    ctx.confirm = deny
    res = asyncio.run(T.run({"command": "echo nope"}, ctx))
    assert "denied" in res.lower() and asked


def test_auto_approve_skips_the_prompt():
    ctx = make_ctx(auto_approve_on_edit=True)
    called = []

    async def never(what):
        called.append(what)
        return False

    ctx.confirm = never
    res = asyncio.run(T.run({"command": "echo yes"}, ctx))
    assert "yes" in res and not called


def test_memory_ranking_and_lessons():
    m = Memory()
    m.store(1, "user prefers concise answers")
    m.store(1, "project uses telegram bot")
    m.add_lesson(1, "always ask before deleting files")
    assert m.recall(1, "concise answers")[0] == "user prefers concise answers"
    assert m.lessons(1)[0].startswith("always ask")


def test_sessions_roundtrip():
    ss = SessionStore()
    sid = ss.create(7, "unit test")
    ss.append(sid, "user", "hello")
    ss.append(sid, "assistant", "hi")
    assert [m["role"] for m in ss.load(sid)] == ["user", "assistant"]
    assert ss.last_id(7) == sid


def test_skills_and_soul_stores():
    sk = SkillStore()
    d = DATA_DIR / "skills" / "unit-skill"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: unit-skill\ndescription: for tests\n---\nbody")
    assert "unit-skill" in sk.names() and "for tests" in sk.index()
    assert "body" in sk.read("unit-skill")
    (d / "SKILL.md").unlink(); d.rmdir()
    so = SoulStore()
    assert "fable-5.1" in so.names()
    assert len(so.get("fable-5.1")) > 200


def test_reports_render():
    ctx = make_ctx()
    assert f"Tools ({len(REGISTRY)})" in harness.status(ctx)
    assert "build" in team.team_status(ctx) and "plan" in team.team_status(ctx)


def test_html_stripper_and_query_parser():
    assert "Hello & world" in W.to_text("<script>x</script><p>Hello &amp; world</p>")
    assert _parse_list('["a b", "c d"]') == ["a b", "c d"]
    assert _parse_list("1. first q\n2. second q") == ["first q", "second q"]
