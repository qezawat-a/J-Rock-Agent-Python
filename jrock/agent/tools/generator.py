from __future__ import annotations

import base64
import subprocess
import urllib.parse
import uuid
from pathlib import Path

import httpx

from ..context import AgentContext

OUT = Path(__file__).resolve().parents[3] / "data" / "workspace"
OUT.mkdir(parents=True, exist_ok=True)


def _out(stem: str, ext: str) -> Path:
    """Unique output path, so a repeat prompt does not overwrite the last file."""
    return OUT / f"{stem}_{uuid.uuid4().hex[:8]}{ext}"


async def image(args: dict, ctx: AgentContext) -> str:
    prompt = args.get("prompt", "")
    size = args.get("size", "1024x1024")
    key = ctx.settings.api_keys.get("openai", "")
    path = _out("img", ".png")
    note = ""
    if key:
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(
                    "https://api.openai.com/v1/images/generations",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": "gpt-image-1", "prompt": prompt, "size": size},
                )
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            b64 = r.json()["data"][0].get("b64_json")
            if b64:
                path.write_bytes(base64.b64decode(b64))
                if ctx.deliver:
                    await ctx.deliver({"photo": str(path)})
                return f"Image saved: {path}"
            raise RuntimeError("response contained no image data")
        except Exception as e:
            # Surface the reason instead of silently pretending it worked.
            note = f"(OpenAI image failed: {e}; used the free generator) "
    url = ("https://image.pollinations.ai/prompt/"
           f"{urllib.parse.quote(prompt)}?width=1024&nologo=true")
    try:
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code >= 400 or not r.content:
            return f"{note}Image generation failed: HTTP {r.status_code}."
        path.write_bytes(r.content)
        if ctx.deliver:
            await ctx.deliver({"photo": str(path)})
        return f"{note}Image saved: {path}"
    except Exception as e:
        return f"{note}Image generation failed: {e}"


async def tts(args: dict, ctx: AgentContext) -> str:
    text = args.get("text", "")
    path = _out("tts", ".mp3")
    key = ctx.settings.api_keys.get("openai", "")
    note = ""
    if key:
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": "tts-1", "voice": "alloy", "input": text},
                )
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            path.write_bytes(r.content)
            if ctx.deliver:
                await ctx.deliver({"voice": str(path)})
            return f"Voice saved: {path}"
        except Exception as e:
            note = f"(OpenAI TTS failed: {e}; tried edge-tts) "
    try:
        proc = subprocess.run(["edge-tts", "--text", text, "--write-media", str(path)],
                              capture_output=True, timeout=180)
        if proc.returncode == 0 and path.exists():
            if ctx.deliver:
                await ctx.deliver({"voice": str(path)})
            return f"{note}Voice saved: {path}"
    except FileNotFoundError:
        return "TTS needs an OpenAI key, or edge-tts installed (pip install edge-tts)."
    return f"{note}TTS failed."


async def video(args: dict, ctx: AgentContext) -> str:
    prompt = args.get("prompt", "")
    key = ctx.settings.fal_key or ctx.settings.api_keys.get("fal", "")
    if not key:
        return "Video generation needs a fal.ai key: /set-api-key fal <key>"
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                "https://queue.fal.run/fal-ai/kling-video/v1.6/standard/text-to-video",
                headers={"Authorization": f"Key {key}"},
                json={"prompt": prompt, "duration": "5"},
            )
            info = r.json()
        status = info.get("status", "")
        if status == "IN_QUEUE":
            return "Video queued - run this command again in a minute to fetch it."
        video_url = info.get("video", {}).get("url")
        if not video_url:
            return f"Video request status: {status}"
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as c2:
            data = await c2.get(video_url)
        path = _out("vid", ".mp4")
        path.write_bytes(data.content)
        if ctx.deliver:
            await ctx.deliver({"video": str(path)})
        return f"Video saved: {path}"
    except Exception as e:
        return f"Video generation failed: {e}"


async def document(args: dict, ctx: AgentContext) -> str:
    """Create a file (code/doc/text) with LLM content and attach it to chat."""
    prompt = args.get("prompt", "")
    filename = args.get("filename", "output.txt")
    kind = args.get("kind", "codes")
    instr = {
        "codes": "Write complete, runnable code.",
        "docs": "Write well-structured documentation in Markdown.",
        "files": "Write the requested file content.",
    }.get(kind, "Write the file content.")
    msg = await ctx.llm.chat(
        [{"role": "user",
          "content": f"{instr}\n\nRequest: {prompt}\n\nReturn only the file body."}],
        temperature=0.2, max_tokens=6000)
    body = (msg.get("content") or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1].rsplit("```", 1)[0]
    path = OUT / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    if ctx.deliver:
        await ctx.deliver({"document": str(path)})
    return f"Saved: {path}"
