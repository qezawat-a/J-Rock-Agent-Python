"""All Telegram command handlers."""
from __future__ import annotations

import asyncio
import json
import shlex
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from ..agent import harness, research, team, workflows
from ..agent.context import AgentContext
from ..agent.core import REGISTRY, Agent
from ..agent.tools import generator, apps
from ..config import BASE_DIR, DATA_DIR, Settings
from ..llm.providers import PROVIDERS
from . import media
from .confirm import Approvals
from .permissions import allowed, is_owner

WORKSPACE_UPLOADS = DATA_DIR / "workspace" / "uploads"
WORKSPACE_UPLOADS.mkdir(parents=True, exist_ok=True)

HELP = """<b>J-Rock</b> - your AI agent. Just send me a message and I will work.

<b>Brain</b>
/harness - runtime status
/thinking off|low|medium|high
/models - list models / /set model &lt;name&gt;
/provider [name] - show or switch provider
/set_api_key &lt;provider&gt; &lt;key&gt; (owner)

<b>Persona &amp; skills</b>
/soul - show active soul / /soul add &lt;name&gt; &lt;file|text&gt;
/skills - list / /skills add &lt;path|url&gt;
/memory [query] - recall / /dream - consolidate memories
/learning on|off - learn from your feedback

<b>Work</b>
/agents build &lt;task&gt; - implement
/agents plan &lt;task&gt; - research and plan
/team - sub-agents
/bugfixes &lt;description&gt;
/code_review [path]
/deepsearch &lt;query&gt;
/generator image|tts|video|files|codes|docs &lt;prompt&gt;
/app_connector &lt;name&gt; &lt;url&gt; [key]

<b>Safety</b>
/auto_approve_on_edit on|off - skip approval prompts
/auto_compat on|off - compatibility layer
/compat - compatibility report
/tools - list tools
/mcp - MCP servers / /mcp add &lt;name&gt; &lt;command|json&gt;

<b>Sessions &amp; settings</b>
/sessions - list / /resume_session &lt;id|last&gt;
/config - settings / /quit - stop the current task
"""


# --------------------------------------------------------------- utilities
def core_of(context) -> object:
    return context.application.bot_data["core"]


async def reply(update: Update, text: str) -> None:
    await media.send_long(update.effective_message.get_bot(),
                           update.effective_chat.id, text)


def _args(context) -> list[str]:
    return [a for a in (context.args or [])]


def _on(value: str) -> bool | None:
    v = value.lower()
    if v in ("on", "true", "yes", "1", "enable", "enabled"):
        return True
    if v in ("off", "false", "no", "0", "disable", "disabled"):
        return False
    return None


def workspace_of(settings: Settings) -> Path:
    return Path(settings.workspace).expanduser().resolve() if settings.workspace \
        else BASE_DIR


def make_ctx(core, user_id: int, chat_id: int) -> AgentContext:
    bot = core.bot
    return AgentContext(
        settings=core.settings,
        llm=core.llm,
        user_id=user_id,
        chat_id=chat_id,
        notify=lambda text: media.send_long(bot, chat_id, text),
        confirm=lambda what: core.approvals.request(bot, chat_id, what, user_id),
        deliver=lambda payload: media.deliver(bot, chat_id, payload),
        chat=bot,
        workspace=workspace_of(core.settings),
        memory=core.memory,
        sessions=core.sessions,
        mcp=core.mcp,
        skills=core.skills,
        soul=core.souls,
        session_id=core.active_session.get(user_id, ""),
    )


def spawn(core, update: Update, context: ContextTypes.DEFAULT_TYPE,
          task_text: str, role: str = "general", directive: str = "") -> None:
    """Run an agent task in the background so /quit can cancel it."""
    user = update.effective_user.id
    chat_id = update.effective_chat.id
    actx = make_ctx(core, user, chat_id)
    if not actx.session_id:
        actx.session_id = core.sessions.create(user, task_text[:60])
        core.active_session[user] = actx.session_id
    agent = Agent(actx, role=role, directive=directive)
    core.running[user] = agent
    history = core.sessions.load(actx.session_id, 12) if core.resume.get(user) else []

    async def _job():
        try:
            if role == "general" or directive:
                await update.effective_message.reply_text(
                    f"🧠 {role} agent started. /quit stops it.")
            answer = await agent.run(task_text, history=history)
            core.resume[user] = True
            await media.send_long(core.bot, chat_id, answer or "Done.")
            if core.memory and core.settings.learning:
                await _auto_learn(core, user, task_text, answer)
        except asyncio.CancelledError:
            await core.bot.send_message(chat_id, "Stopped by /quit.")
        except Exception as e:
            await core.bot.send_message(chat_id, f"Agent error: {e}")
        finally:
            core.running.pop(user, None)

    context.application.create_task(_job())


async def _auto_learn(core, user_id: int, task: str, answer: str) -> None:
    """The learning loop: extract a durable lesson when the user corrected us."""
    try:
        msg = await core.llm.chat([{"role": "user", "content":
            "A user gave this task and got this answer. If the task contains a "
            "correction, a standing preference, or a rule to follow in future "
            "conversations, write ONE short lesson line starting with 'Lesson:'. "
            "Otherwise reply exactly NONE.\n\n"
            f"TASK: {task[:800]}\nANSWER: {(answer or '')[:800]}"}],
            temperature=0.1, max_tokens=120)
        out = (msg.get("content") or "").strip()
        if out and not out.startswith("NONE") and "Lesson:" in out:
            core.memory.add_lesson(user_id, out.splitlines()[0][:300])
    except Exception:
        pass


async def _guard(core, update: Update) -> bool:
    if allowed(core.settings, update.effective_user.id):
        return True
    await reply(update, "This bot is private.")
    return False


# ---------------------------------------------------------------- commands
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    await reply(update, HELP)


async def cmd_harness(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    await reply(update, harness.status(
        make_ctx(core, update.effective_user.id, update.effective_chat.id)))


async def cmd_thinking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    if not context.args:
        await reply(update, f"thinking = {core.settings.thinking} "
                            "(off | low | medium | high)")
        return
    level = context.args[0].lower()
    if level not in ("off", "low", "medium", "high"):
        await reply(update, "Use: /thinking off|low|medium|high")
        return
    core.settings.thinking = level
    core.settings.save()
    await reply(update, f"thinking = {level}")


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    s = core.settings
    args = _args(context)
    if not args:
        await reply(update, "Usage:\n"
                            "/set models\n/set model &lt;name&gt;\n"
                            "/set provider &lt;name&gt;\n/set thinking &lt;level&gt;\n"
                            "/set soul &lt;name&gt;\n/set workspace &lt;path&gt;")
        return
    key = args[0].lower()
    if key in ("models", "model", "models_list"):
        if len(args) > 1:
            s.model = args[1]
            s.save()
            await reply(update, f"model = {s.model}")
        else:
            models = await core.llm.list_models()
            pretty = "\n".join(f"- {m}" for m in models[:60])
            await reply(update, f"Models on <code>{s.provider}</code> "
                                f"(current: {s.model}):\n{pretty}")
        return
    if key == "provider":
        if len(args) < 2:
            await reply(update, f"provider = {s.provider}\nAvailable: "
                                + ", ".join(PROVIDERS))
            return
        name = args[1].lower()
        if name not in PROVIDERS:
            await reply(update, f"Unknown provider. Available: "
                                + ", ".join(PROVIDERS))
            return
        s.provider = name
        known = PROVIDERS[name]["models"]
        if known:
            s.model = known[0]
        else:
            # Providers such as 'custom' ship no static list, so ask the
            # endpoint. Falling back to the previous model beats crashing.
            try:
                discovered = await core.llm.list_models(name)
            except Exception:
                discovered = []
            s.model = discovered[0] if discovered else s.model
        s.save()
        await reply(update, f"provider = {name}, model = {s.model}"
                            + ("" if known or s.model else
                               "\nThis provider ships no model list - pick one "
                               "with /models then /set model <name>."))
        return
    if key == "thinking":
        return await cmd_thinking(update, context)
    if key == "soul":
        if len(args) < 2 or not core.souls.exists(args[1]):
            await reply(update, f"Souls: " + ", ".join(core.souls.names()))
            return
        s.default_soul = args[1]
        s.save()
        await reply(update, f"soul = {s.default_soul}")
        return
    if key == "workspace":
        if len(args) < 2:
            await reply(update, f"workspace = {workspace_of(s)}")
            return
        p = Path(args[1]).expanduser()
        if not p.exists():
            await reply(update, f"No such directory: {p}")
            return
        s.workspace = str(p.resolve())
        s.save()
        await reply(update, f"workspace = {s.workspace}")
        return
    await reply(update, "Unknown setting. Use /set to see the options.")


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    return await cmd_set(update, _with_args(context, ["models"]))


def _with_args(context, args: list[str]):
    context.args = args
    return context


async def cmd_provider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    if not context.args:
        s = core.settings
        lines = [f"provider = <code>{s.provider}</code>, model = <code>{s.model}</code>"]
        for name, meta in PROVIDERS.items():
            has_key = "yes" if core.settings.api_keys.get(name) else "no"
            lines.append(f"- {name}: tools={meta['supports_tool_calls']}, "
                         f"thinking={meta['supports_thinking']}, key set={has_key}")
        await reply(update, "\n".join(lines))
        return
    return await cmd_set(update, _with_args(context, ["provider", context.args[0]]))


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    args = _args(context)
    if not args:
        names = core.skills.names()
        await reply(update, "Skills:\n" + (core.skills.index() if names
                                           else "(none installed)") +
                     "\n\nAdd: /skills add <path to folder or .md file, or URL>")
        return
    sub = args[0].lower()
    if sub == "add" and len(args) > 1:
        src = args[1]
        if src.startswith(("http://", "https://")):
            import httpx
            target = WORKSPACE_UPLOADS / src.rsplit("/", 1)[-1]
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.get(src, follow_redirects=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(r.content)
            src = str(target)
        await reply(update, core.skills.add(src))
        return
    if sub in ("rm", "remove", "delete") and len(args) > 1:
        await reply(update, f"Removed {args[1]}" if core.skills.remove(args[1])
                    else f"No skill '{args[1]}'.")
        return
    await reply(update, core.skills.index())


async def cmd_mcp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    args = _args(context)
    if not args:
        if not core.mcp.servers:
            await reply(update, "No MCP servers.\n"
                                "Add: /mcp add &lt;name&gt; &lt;command ...&gt;  or  "
                                "/mcp add &lt;name&gt; &lt;json&gt;")
            return
        lines = ["MCP servers:"]
        for name, srv in core.mcp.servers.items():
            spec = srv.spec.get("command") or srv.spec.get("url")
            lines.append(f"- {name}: {spec} ({len(srv.tools)} tools)")
        await reply(update, "\n".join(lines))
        return
    sub = args[0].lower()
    if sub == "add" and len(args) > 2:
        name, rest = args[1], " ".join(args[2:])
        spec = None
        if rest.strip().startswith("{"):
            try:
                spec = json.loads(rest)
            except json.JSONDecodeError:
                spec = None
        if spec is None:
            parts = shlex.split(rest)
            spec = {"command": parts[0], "args": parts[1:]}
        core.mcp.add(name, spec)
        try:
            await core.mcp.servers[name].start()
            await reply(update, f"Added {name} with "
                        f"{len(core.mcp.servers[name].tools)} tools.")
        except Exception as e:
            await reply(update, f"Saved {name}, but it failed to start: {e}")
        return
    if sub in ("rm", "remove") and len(args) > 1:
        await reply(update, f"Removed {args[1]}" if core.mcp.remove(args[1])
                    else f"No MCP server '{args[1]}'.")
        return
    await reply(update, "Usage: /mcp add <name> <command ... | json> | /mcp rm <name>")


async def cmd_soul(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    args = _args(context)
    names = core.souls.names()
    if not args:
        text = core.souls.get(core.settings.default_soul)
        await reply(update, f"Active soul: <code>{core.settings.default_soul}</code>\n"
                            f"Installed: {', '.join(names)}\n\n"
                            f"<pre>{text[:2500]}</pre>")
        return
    sub = args[0].lower()
    if sub == "add" and len(args) > 1:
        name = args[1]
        body = " ".join(args[2:])
        if not body and len(args) == 2:
            return
        src = None
        if args[-1].endswith(".md") and Path(args[-1]).exists():
            src = Path(args[-1])
        elif len(args) == 3 and Path(args[2]).exists():
            src = Path(args[2])
        if src:
            core.souls.save(name, src.read_text())
        else:
            core.souls.save(name, body)
        await reply(update, f"Soul '{name}' saved. Switch with /set soul {name}")
        return
    if sub in ("use", "set") and len(args) > 1:
        if not core.souls.exists(args[1]):
            await reply(update, f"No soul '{args[1]}'. Installed: {', '.join(names)}")
            return
        core.settings.default_soul = args[1]
        core.settings.save()
        await reply(update, f"soul = {args[1]}")
        return
    if sub in ("rm", "remove") and len(args) > 1:
        await reply(update, f"Removed {args[1]}" if core.souls.delete(args[1])
                    else f"No soul '{args[1]}'.")
        return
    await reply(update, "Usage: /soul add <name> <text|file.md> | /soul use <name>")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    args = _args(context)
    uid = update.effective_user.id
    if not args:
        prof = core.memory.get_profile(uid)
        facts = core.memory.recall(uid, "", k=10)
        lessons = core.memory.lessons(uid, 5)
        await reply(update,
                    f"Profile: {json.dumps(prof) if prof else '(empty)'}\n\n"
                    f"Memories:\n" + "\n".join(f"- {f}" for f in facts) +
                    "\n\nLessons:\n" + "\n".join(f"- {x}" for x in lessons) +
                    "\n\n/memory <query> to search, /memory add <fact>, /dream to consolidate.")
        return
    sub = args[0].lower()
    if sub == "add" and len(args) > 1:
        core.memory.store(uid, " ".join(args[1:]))
        await reply(update, "Stored.")
        return
    if sub in ("rm", "forget", "delete") and len(args) > 1:
        try:
            ok = core.memory.forget(uid, int(args[1]))
        except ValueError:
            ok = False
        await reply(update, "Deleted." if ok else "Give a numeric memory id.")
        return
    hits = core.memory.recall(uid, " ".join(args))
    await reply(update, "\n".join(f"- {h}" for h in hits) or "No matches.")


async def cmd_dream(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    await update.effective_message.reply_text("💭 Dreaming (consolidating memories)…")
    uid = update.effective_user.id
    result = await core.memory.dream(uid, core.llm)
    await reply(update, result)


async def cmd_deepsearch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    q = " ".join(_args(context))
    if not q:
        await reply(update, "Usage: /deepsearch <query>")
        return
    await update.effective_message.reply_text("🔎 Researching…")
    actx = make_ctx(core, update.effective_user.id, update.effective_chat.id)
    try:
        result = await research.deep_research(q, actx)
    except Exception as e:
        result = f"Research failed: {e}"
    await reply(update, result)


async def cmd_bugfixes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    task = " ".join(_args(context))
    if not task:
        await reply(update, "Usage: /bugfixes <what is broken>")
        return
    spawn(core, update, context, task, role="build",
          directive=workflows.BUGFIX_DIRECTIVE)


async def cmd_code_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    target = " ".join(_args(context)) or str(workspace_of(core.settings))
    await update.effective_message.reply_text("🔍 Reviewing the code…")
    actx = make_ctx(core, update.effective_user.id, update.effective_chat.id)
    try:
        result = await workflows.run_code_review(target, actx)
    except Exception as e:
        result = f"Review failed: {e}"
    await reply(update, result)


async def cmd_quit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    agent = core.running.get(update.effective_user.id)
    if agent:
        agent.cancel.set()
        await reply(update, "Stopping the current task…")
    else:
        await reply(update, "Nothing is running right now.")


async def cmd_autocompat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    flag = _on(context.args[0]) if context.args else None
    if flag is None:
        await reply(update, f"auto-compat is "
                            f"{'on' if core.settings.auto_compat else 'off'}"
                            " (/auto-compat on|off)")
        return
    core.settings.auto_compat = flag
    core.settings.save()
    await reply(update, f"auto-compat = {'on' if flag else 'off'}")


async def cmd_compat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    s = core.settings
    lines = [f"<b>Compatibility</b> - auto-compat is "
             f"{'on' if s.auto_compat else 'off'}", ""]
    for name, meta in PROVIDERS.items():
        cur = " (current)" if name == s.provider else ""
        lines.append(f"- {name}{cur}: tool calls="
                     f"{'yes' if meta['supports_tool_calls'] else 'json-mode'}, "
                     f"thinking={'yes' if meta['supports_thinking'] else 'no'}, "
                     f"key={'set' if s.api_keys.get(name) else 'missing'}")
    lines.append("\nMCP servers: " +
                 (", ".join(f"{n}({len(v.tools)})" for n, v in core.mcp.servers.items())
                  if core.mcp.servers else "none"))
    lines.append("Skills: " + (", ".join(core.skills.names()) or "none"))
    await reply(update, "\n".join(lines))


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    items = core.sessions.list(update.effective_user.id)
    if not items:
        await reply(update, "No sessions yet.")
        return
    lines = ["Sessions (newest first):"]
    for sid, meta in items[:15]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta["created"]))
        lines.append(f"- <code>{sid}</code> {when} - {meta['title']}")
    lines.append("\nResume with /resume-session <id> or /resume-session last")
    await reply(update, "\n".join(lines))


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    uid = update.effective_user.id
    args = _args(context)
    if not args:
        await reply(update, "Usage: /resume-session <id|last>")
        return
    sid = core.sessions.last_id(uid) if args[0].lower() in ("last", "latest") else args[0]
    if not sid or not (DATA_DIR / "sessions" / f"{sid}.jsonl").exists():
        await reply(update, f"No session '{sid}'. Use /sessions.")
        return
    core.active_session[uid] = sid
    core.resume[uid] = True
    msgs = core.sessions.load(sid, 12)
    preview = "\n".join(f"{m['role']}: {m['content'][:120]}" for m in msgs[-4:])
    await reply(update, f"Resumed session <code>{sid}</code>. "
                        f"Last turns:\n{preview or '(empty)'}")


async def cmd_set_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    if not is_owner(core.settings, update.effective_user.id):
        await reply(update, "Owner only.")
        return
    args = _args(context)
    if len(args) < 2:
        await reply(update, "Usage: /set-api-key <provider> <key>")
        return
    name, key = args[0].lower(), args[1]
    if name in ("fal", "fal_key"):
        core.settings.fal_key = key
        core.settings.api_keys["fal"] = key
        core.settings.save()
        await reply(update, "fal.ai key saved.")
        return
    core.settings.api_keys[name] = key
    core.settings.save()
    await reply(update, f"API key for {name} saved.")


async def cmd_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    await reply(update, team.team_status(
        make_ctx(core, update.effective_user.id, update.effective_chat.id)))


async def cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    args = _args(context)
    if not args or args[0].lower() not in team.ROLES:
        await reply(update, "Usage: /agents build <task>  |  /agents plan <task>")
        return
    role = args[0].lower()
    task = " ".join(args[1:])
    if not task:
        await reply(update, f"Give the {role} agent a task.")
        return
    spawn(core, update, context, task, role=role)


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    lines = ["<b>Tools</b> (available to the main agent)"]
    for name, tool in REGISTRY.items():
        flag = " ⚠️approval" if tool.dangerous else ""
        lines.append(f"- <code>{name}</code>{flag}: {tool.description[:90]}")
    if core.mcp.tool_specs():
        lines.append("\nMCP tools: " +
                     ", ".join(t["function"]["name"]
                               for t in core.mcp.tool_specs()))
    await reply(update, "\n".join(lines))


async def cmd_app_connector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    args = _args(context)
    if not args:
        current = apps.load_apps()
        if not current:
            await reply(update, "No apps connected.\n"
                                "Add: /app_connector <name> <base_url> [api_key]")
            return
        lines = ["Connected apps:"]
        for name, cfg in current.items():
            lines.append(f"- {name}: {cfg.get('base_url')}"
                         + (" (key set)" if cfg.get("api_key") else ""))
        await reply(update, "\n".join(lines))
        return
    name = args[0]
    if len(args) < 2:
        await reply(update, "Usage: /app_connector <name> <base_url> [api_key]")
        return
    current = apps.load_apps()
    current[name] = {"base_url": args[1],
                     "api_key": args[2] if len(args) > 2 else "",
                     "header": "Authorization", "prefix": "Bearer "}
    apps.save_apps(current)
    await reply(update, f"Connected app '{name}' -> {args[1]}. "
                        "The agent can now use app_call.")


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    s = core.settings
    args = _args(context)
    if args and args[0].lower() == "set" and len(args) > 2:
        if not is_owner(s, update.effective_user.id):
            await reply(update, "Owner only.")
            return
        key, value = args[1].lower(), " ".join(args[2:])
        if key == "max_agent_steps" and value.isdigit():
            s.max_agent_steps = int(value)
        elif key == "terminal_allowed":
            flag = _on(value)
            s.terminal_allowed = True if flag is None else flag
        elif key == "allow_outside_workspace":
            flag = _on(value)
            s.allow_outside_workspace = True if flag is None else flag
        elif key == "allow_dangerous_commands":
            flag = _on(value)
            s.allow_dangerous_commands = True if flag is None else flag
        elif key == "allow_private_fetch":
            flag = _on(value)
            s.allow_private_fetch = True if flag is None else flag
        elif key == "workspace":
            s.workspace = value
        elif key == "soul":
            s.default_soul = value
        else:
            await reply(update, f"Unknown key. Try max_agent_steps, "
                                "terminal_allowed, allow_outside_workspace, "
                                "allow_dangerous_commands, allow_private_fetch, "
                                "workspace, soul.")
            return
        s.save()
        await reply(update, f"{key} = {getattr(s, key)}")
        return
    masked = {k: ("set" if v else "missing") for k, v in s.api_keys.items()}
    await reply(update, (
        "<b>Settings</b>\n"
        f"provider: {s.provider}\nmodel: {s.model}\nthinking: {s.thinking}\n"
        f"auto_compat: {s.auto_compat}\nlearning: {s.learning}\n"
        f"auto_approve_on_edit: {s.auto_approve_on_edit}\n"
        f"terminal_allowed: {s.terminal_allowed}\n"
        f"allow_outside_workspace: {s.allow_outside_workspace}\n"
        f"allow_dangerous_commands: {s.allow_dangerous_commands}\n"
        f"allow_private_fetch: {s.allow_private_fetch}\n"
        f"max_agent_steps: {s.max_agent_steps}\n"
        f"workspace: {workspace_of(s)}\n"
        f"soul: {s.default_soul}\n"
        f"allowed users: {s.tg_allowed_ids or 'everyone'}\n"
        f"api keys: {masked}\n\n"
        "/config set max_agent_steps 40"))


async def cmd_generator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    args = _args(context)
    if not args:
        await reply(update, "Usage:\n/generator image <prompt>\n"
                            "/generator tts <text>\n/generator video <prompt>\n"
                            "/generator files <prompt> [filename]\n"
                            "/generator codes <prompt> [filename.py]\n"
                            "/generator docs <prompt> [filename.md]")
        return
    kind, prompt = args[0].lower(), " ".join(args[1:])
    if not prompt:
        await reply(update, "Give me a prompt.")
        return
    actx = make_ctx(core, update.effective_user.id, update.effective_chat.id)
    await update.effective_message.reply_text(f"🎨 Generating {kind}…")
    try:
        if kind == "image":
            result = await generator.image({"prompt": prompt}, actx)
        elif kind == "tts":
            result = await generator.tts({"text": prompt}, actx)
        elif kind == "video":
            result = await generator.video({"prompt": prompt}, actx)
        elif kind in ("files", "codes", "docs"):
            filename = " ".join(args[2:]) if len(args) > 2 else f"output_{kind}.txt"
            if kind == "codes" and not filename.endswith((".py", ".js", ".ts", ".go",
                                                          ".rs", ".java", ".sh")):
                filename += ".py"
            if kind == "docs" and not filename.endswith(".md"):
                filename += ".md"
            result = await generator.document({"prompt": prompt, "filename": filename,
                                               "kind": kind}, actx)
        else:
            result = ("Unknown type. Use image, tts, video, files, codes or docs.")
    except Exception as e:
        result = f"Generator failed: {e}"
    await reply(update, result)


async def cmd_auto_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    flag = _on(context.args[0]) if context.args else None
    if flag is None:
        await reply(update, f"auto_approve_on_edit is "
                            f"{'on' if core.settings.auto_approve_on_edit else 'off'}"
                            " (/auto_approve_on_edit on|off). Terminal commands and "
                            "file edits ask for approval when off.")
        return
    if not is_owner(core.settings, update.effective_user.id):
        await reply(update, "Owner only.")
        return
    core.settings.auto_approve_on_edit = flag
    core.settings.save()
    await reply(update, f"auto_approve_on_edit = {'on' if flag else 'off'}")


async def cmd_learning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    flag = _on(context.args[0]) if context.args else None
    if flag is None:
        await reply(update, f"learning is "
                            f"{'on' if core.settings.learning else 'off'} "
                            "(/learning on|off). When on, corrections in your "
                            "messages become lessons applied in future tasks.")
        return
    core.settings.learning = flag
    core.settings.save()
    await reply(update, f"learning = {'on' if flag else 'off'}")


# ----------------------------------------------------------------- default
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    core = core_of(context)
    if not await _guard(core, update):
        return
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    if not text:
        return
    extra = ""
    if msg.document is not None:
        f = WORKSPACE_UPLOADS / (msg.document.file_name or "upload.bin")
        await msg.document.download_to_drive(custom_path=str(f))
        extra = f"\n\n[Uploaded file saved to: {f}]"
    if msg.photo:
        f = WORKSPACE_UPLOADS / f"photo_{int(time.time())}.jpg"
        await msg.photo[-1].download_to_drive(custom_path=str(f))
        extra += f"\n\n[Photo saved to: {f}]"
    spawn(core, update, context, text + extra)


def register(app: Application, core) -> None:
    ALIASES = {
        "code-review": cmd_code_review,
        "auto-compat": cmd_autocompat,
        "resume-session": cmd_resume,
        "set-api-key": cmd_set_api_key,
    }

    async def cmd_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Dispatch a hyphenated alias to its command handler."""
        parts = (update.effective_message.text or "").strip().split()
        name = parts[0].lstrip("/").split("@", 1)[0]
        handler = ALIASES.get(name)
        if handler is None:
            return
        context.args = parts[1:]
        await handler(update, context)

    handlers = [
        CommandHandler("start", cmd_start),
        CommandHandler(["help", "menu"], cmd_start),
        CommandHandler("harness", cmd_harness),
        CommandHandler("thinking", cmd_thinking),
        CommandHandler("set", cmd_set),
        CommandHandler("models", cmd_models),
        CommandHandler("provider", cmd_provider),
        CommandHandler("skills", cmd_skills),
        CommandHandler("mcp", cmd_mcp),
        CommandHandler("soul", cmd_soul),
        CommandHandler("memory", cmd_memory),
        CommandHandler("dream", cmd_dream),
        CommandHandler("deepsearch", cmd_deepsearch),
        CommandHandler("bugfixes", cmd_bugfixes),
        # Telegram command names allow only letters, digits and underscores.
        CommandHandler("code_review", cmd_code_review),
        CommandHandler("quit", cmd_quit),
        CommandHandler("auto_compat", cmd_autocompat),
        CommandHandler("compat", cmd_compat),
        CommandHandler("sessions", cmd_sessions),
        CommandHandler("resume_session", cmd_resume),
        CommandHandler("set_api_key", cmd_set_api_key),
        CommandHandler("team", cmd_team),
        CommandHandler("agents", cmd_agents),
        CommandHandler("tools", cmd_tools),
        CommandHandler("app_connector", cmd_app_connector),
        CommandHandler("config", cmd_config),
        CommandHandler("generator", cmd_generator),
        CommandHandler("auto_approve_on_edit", cmd_auto_approve),
        CommandHandler("learning", cmd_learning),
    ]
    for h in handlers:
        app.add_handler(h)
    # Keep the friendlier hyphenated spellings working: Telegram itself
    # rejects '-' in a command name, so these arrive as plain text.
    app.add_handler(MessageHandler(
        filters.Regex(r"^/(code-review|auto-compat|resume-session|set-api-key)"
                      r"(@\w+)?(\s|$)"),
        cmd_alias))
    app.add_handler(core.approvals.handler())
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(
        (filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, on_message))
