from __future__ import annotations

import re
from pathlib import Path

from ..context import AgentContext

MAX_READ = 20000
MAX_SCAN = 1_000_000          # skip files larger than this when searching
IGNORES = {".git", "__pycache__", "node_modules", ".venv", "venv", "data/sessions"}


def _base(ctx: AgentContext) -> Path:
    return (Path(ctx.workspace) if ctx.workspace else Path.cwd()).resolve()


def _safe(ctx: AgentContext, path: str) -> tuple[Path | None, str | None]:
    """Resolve a path and keep it inside the workspace.

    The workspace is a boundary, not just a default directory: without this
    check a single approval lets the agent read or write anywhere on the host.
    The owner can opt out with /config set allow_outside_workspace true.
    """
    base = Path(ctx.workspace) if ctx.workspace else Path.cwd()
    p = Path(path).expanduser()
    p = p if p.is_absolute() else base / p
    p = p.resolve()
    if ctx.settings.allow_outside_workspace:
        return p, None
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None, (f"Refused: {p} is outside the workspace {base.resolve()}. "
                      "The owner can allow this with "
                      "/config set allow_outside_workspace true")
    return p, None


async def read_file(args: dict, ctx: AgentContext) -> str:
    p, err = _safe(ctx, args.get("path", ""))
    if err:
        return err
    if not p.exists():
        return f"Not found: {p}"
    if p.is_dir():
        return f"{p} is a directory."
    if p.stat().st_size > 5_000_000:
        return f"{p} is too large to read ({p.stat().st_size} bytes)."
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return f"Error: {e}"
    if len(text) > MAX_READ:
        text = text[:MAX_READ] + "\n... [truncated]"
    return text


async def write_file(args: dict, ctx: AgentContext) -> str:
    p, err = _safe(ctx, args.get("path", ""))
    if err:
        return err
    ok = await ctx.ask_permission(f"write file: {p}")
    if not ok:
        return "User denied the write."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args.get("content", ""))
    return f"Wrote {len(args.get('content',''))} chars to {p}."


async def edit_file(args: dict, ctx: AgentContext) -> str:
    p, err = _safe(ctx, args.get("path", ""))
    if err:
        return err
    ok = await ctx.ask_permission(f"edit file: {p}")
    if not ok:
        return "User denied the edit."
    if not p.exists():
        return f"Not found: {p}"
    text = p.read_text(errors="replace")
    old = args.get("old", "")
    if old not in text:
        return "'old' string not found in file."
    p.write_text(text.replace(old, args.get("new", ""), 1))
    return f"Edited {p}."


async def list_dir(args: dict, ctx: AgentContext) -> str:
    p, err = _safe(ctx, args.get("path", "."))
    if err:
        return err
    if not p.exists():
        return f"Not found: {p}"
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
    lines = []
    for e in entries[:200]:
        if any(part in IGNORES for part in e.parts):
            continue
        lines.append(f"{e.name}/" if e.is_dir() else e.name)
    return "\n".join(lines) or "(empty)"


async def search(args: dict, ctx: AgentContext) -> str:
    q = args.get("query", "")
    root, err = _safe(ctx, args.get("path", "."))
    if err:
        return err
    try:
        rx = re.compile(q)
    except re.error:
        rx = None
    hits: list[str] = []
    for f in root.rglob("*"):
        if not f.is_file() or any(part in IGNORES for part in f.parts):
            continue
        if len(hits) > 200:
            break
        try:
            if f.stat().st_size > MAX_SCAN:
                continue
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if (rx.search(line) if rx else q in line):
                    hits.append(f"{f}:{i}: {line.strip()[:200]}")
                    break
        except Exception:
            continue
    return "\n".join(hits) or "No matches."
