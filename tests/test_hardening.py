"""Regression tests for the hardening pass: provider format, sandbox,
approval binding, memory bounds and message splitting."""
import asyncio
import json

import pytest

from jrock.agent.context import AgentContext
from jrock.agent.memory import Memory
from jrock.agent.tools import fs as F, terminal as T, web as W
from jrock.bot import media
from jrock.bot.confirm import Approvals
from jrock.config import Settings
from jrock.llm import client as C
from tests.conftest import StubLLM


# --------------------------------------------------------------- anthropic
def test_to_anthropic_pairs_tool_use_with_tool_result():
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "file_list", "arguments": '{"path": "."}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "file_list",
         "content": "a.py\nb.py"},
    ]
    system, convo = C._to_anthropic(messages)
    assert system == "be brief"
    assert convo[0]["role"] == "user"
    use = convo[1]["content"][0]
    assert use["type"] == "tool_use" and use["id"] == "call_1"
    assert use["input"] == {"path": "."}
    result = convo[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "call_1"
    # no empty text blocks anywhere - Anthropic rejects them
    for m in convo:
        for block in m["content"]:
            if block["type"] == "text":
                assert block["text"].strip()


def test_to_anthropic_drops_empty_turns():
    _, convo = C._to_anthropic([
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "hello"},
    ])
    assert convo == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeClient:
    body = None

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.body = json
        return _Resp({"content": [{"type": "text", "text": "ok"}]})


def _anthropic_body(monkeypatch, thinking, max_tokens=4096):
    monkeypatch.setattr(C.httpx, "AsyncClient", _FakeClient)
    s = Settings()
    s.provider = "anthropic"
    s.api_keys = {"anthropic": "test-key"}
    llm = C.LLMClient(s)
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}],
                         thinking=thinking, max_tokens=max_tokens))
    return _FakeClient.body


def test_anthropic_thinking_drops_temperature_and_fits_budget(monkeypatch):
    body = _anthropic_body(monkeypatch, "high")
    assert "temperature" not in body, "thinking rejects a custom temperature"
    assert body["thinking"]["budget_tokens"] < body["max_tokens"]


def test_anthropic_without_thinking_keeps_temperature(monkeypatch):
    body = _anthropic_body(monkeypatch, "off")
    assert "thinking" not in body and "temperature" in body


# ------------------------------------------------------------------ sandbox
def _ctx(tmp_path, **over):
    s = Settings()
    s.tg_allowed_ids = []
    s.auto_approve_on_edit = True
    for k, v in over.items():
        setattr(s, k, v)
    return AgentContext(settings=s, llm=StubLLM(), user_id=1, chat_id=1,
                        workspace=tmp_path)


def test_fs_refuses_paths_outside_the_workspace(tmp_path):
    ctx = _ctx(tmp_path)
    out = asyncio.run(F.read_file({"path": "/etc/passwd"}, ctx))
    assert "Refused" in out and "outside the workspace" in out
    out = asyncio.run(F.write_file({"path": "/tmp/evil", "content": "x"}, ctx))
    assert "Refused" in out


def test_fs_allows_paths_inside_the_workspace(tmp_path):
    ctx = _ctx(tmp_path)
    out = asyncio.run(F.write_file({"path": "notes/t.txt", "content": "hi"}, ctx))
    assert "Wrote" in out
    assert (tmp_path / "notes" / "t.txt").read_text() == "hi"


def test_fs_escape_hatch_is_opt_in(tmp_path):
    ctx = _ctx(tmp_path, allow_outside_workspace=True)
    out = asyncio.run(F.list_dir({"path": "/tmp"}, ctx))
    assert "Refused" not in out


# ----------------------------------------------------------------- terminal
def test_terminal_refuses_destructive_commands(tmp_path):
    ctx = _ctx(tmp_path, auto_approve_on_edit=True)
    for cmd in ("rm -rf /", "mkfs.ext4 /dev/sda1", ":(){ :|:& };:", "shutdown -h now"):
        out = asyncio.run(T.run({"command": cmd}, ctx))
        assert out.startswith("Refused"), cmd


def test_terminal_still_runs_normal_commands(tmp_path):
    ctx = _ctx(tmp_path, auto_approve_on_edit=True)
    assert "exit=0" in asyncio.run(T.run({"command": "echo ok"}, ctx))


# ---------------------------------------------------------------- approvals
def test_approval_wiring_fails_closed(tmp_path):
    ctx = _ctx(tmp_path, auto_approve_on_edit=False)
    ctx.confirm = None
    assert asyncio.run(ctx.ask_permission("rm -rf /")) is False


def test_approval_only_answers_to_the_requester():
    state = {}

    class Bot:
        async def send_message(self, *a, **k):
            pass

    class Q:
        def __init__(self, uid, token):
            self.data = "appr:" + token
            self.from_user = type("U", (), {"id": uid})()

        async def answer(self, *a, **k):
            pass

        async def edit_message_reply_markup(self, *a, **k):
            pass

    approvals = Approvals()

    async def go():
        task = asyncio.ensure_future(
            approvals.request(Bot(), 1, "terminal: ls", user_id=100))
        await asyncio.sleep(0.05)
        token = next(iter(approvals._pending))
        update = type("Up", (), {"callback_query": Q(999, token)})()
        await approvals.handler().callback(update, None)
        assert not task.done(), "a stranger must not be able to approve"
        update.callback_query = Q(100, token)
        await approvals.handler().callback(update, None)
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(go()) is True


# ------------------------------------------------------------------- memory
@pytest.fixture
def mem(tmp_path, monkeypatch):
    """A Memory bound to a throwaway database.

    Memory() otherwise writes to the shared runtime DB, which persists
    between runs and would make these assertions order-dependent.
    """
    from jrock.agent import memory as M
    monkeypatch.setattr(M, "DB", tmp_path / "memory.db")
    monkeypatch.setattr(M, "DREAMS", tmp_path / "dreams")
    (tmp_path / "dreams").mkdir(parents=True, exist_ok=True)
    return M.Memory()


def test_memory_dedupes_and_reinforces(mem):
    m = mem
    uid = 880011
    m.store(uid, "user prefers terse replies")
    m.store(uid, "user prefers terse replies")
    assert len(m._all(uid)) == 1
    for _ in range(4):
        m.recall(uid, "terse replies")
    assert m._all(uid)[0]["score"] > 1.0, "recall should reinforce a used fact"


def test_lessons_are_deduped_and_capped(mem):
    m = mem
    uid = 880012
    for _ in range(5):
        m.add_lesson(uid, "always ask before deleting")
    assert len(m.lessons(uid, 50)) == 1
    for i in range(260):
        m.add_lesson(uid, f"lesson number {i}")
    with m._conn() as con:
        n = con.execute("SELECT COUNT(*) c FROM lessons WHERE user_id=?",
                        (uid,)).fetchone()["c"]
    assert n <= 200, "lessons table must stay bounded"


def test_dream_does_not_permanently_zero_scores(mem):
    m = mem
    uid = 880013
    m.store(uid, "a durable fact about the user")
    for _ in range(6):
        asyncio.run(m.dream(uid, StubLLM([{"role": "assistant", "content":
                                           json.dumps({"insights": [], "lessons": []})}])))
    assert m._all(uid)[0]["score"] >= 0.2


# ------------------------------------------------------------------ history
def test_resume_history_drops_orphan_tool_rows():
    from jrock.agent.core import Agent
    cleaned = Agent._clean_history([
        {"role": "assistant", "content": "an orphaned reply"},
        {"role": "tool", "content": "stale tool output"},
        {"role": "user", "content": "what did we decide"},
        {"role": "assistant", "content": ""},
    ])
    assert cleaned == [{"role": "user", "content": "what did we decide"}]


# -------------------------------------------------------------------- media
def test_long_message_split_never_cuts_a_tag():
    text = "x" * 3799 + "<b>bold text that continues past the boundary</b>"
    parts = media.split(text)
    assert len(parts) > 1
    for part in parts:
        assert part.count("<b>") == part.count("</b>")


def test_split_leaves_short_messages_alone():
    assert media.split("short <b>ok</b>") == ["short <b>ok</b>"]


# --------------------------------------------------------------------- ssrf
@pytest.mark.parametrize("url", [
    "http://localhost:8000/admin",
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/",
    "file:///etc/passwd",
])
def test_web_fetch_blocks_internal_targets(url):
    assert W._blocked(url)


def test_web_fetch_allows_public_hosts():
    assert W._blocked("https://example.com/page") is None
