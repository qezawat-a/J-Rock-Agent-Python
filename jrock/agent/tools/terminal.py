from __future__ import annotations

import asyncio
import re

from ..context import AgentContext

TIMEOUT = 600
MAX_OUT = 6000

# Patterns that destroy the machine or fork-bomb it. These are refused outright
# unless the owner explicitly opts in with /config set allow_dangerous_commands.
DENY = [
    re.compile(r"\brm\s+(-[a-z]*\s+)*-\w*f\w*\s+/(?:\s|$)"),
    re.compile(r"\bmkfs(\.\w+)?\b"),
    re.compile(r"\bdd\b.*\bof=/dev/(sd|nvme|hd|vd)"),
    re.compile(r">\s*/dev/(sd|nvme|hd|vd)"),
    re.compile(r":\s*\(\s*\)\s*\{"),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r"\bchmod\s+-R\s+777\s+/\s*$"),
]


async def run(args: dict, ctx: AgentContext) -> str:
    cmd = args.get("command", "").strip()
    if not cmd:
        return "No command given."
    if not ctx.settings.terminal_allowed:
        return "Terminal access is disabled in /config."
    if not ctx.settings.allow_dangerous_commands:
        for rx in DENY:
            if rx.search(cmd):
                return (f"Refused: this command matches a destructive pattern "
                        f"({rx.pattern}). The owner can allow it with "
                        "/config set allow_dangerous_commands true")
    cwd = str(ctx.workspace) if ctx.workspace else None
    ok = await ctx.ask_permission(f"terminal: `{cmd[:200]}`")
    if not ok:
        return "User denied the terminal command."
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()   # reap, or the child lingers as a zombie
            return f"Command timed out after {TIMEOUT}s."
    except Exception as e:
        return f"Failed to run: {e}"
    text = out.decode("utf-8", "replace")
    if len(text) > MAX_OUT:
        text = text[:MAX_OUT] + f"\n... [truncated, exit={proc.returncode}]"
    return f"exit={proc.returncode}\n{text}"
