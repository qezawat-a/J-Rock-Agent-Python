from __future__ import annotations

import html
import ipaddress
import re
import socket
import urllib.parse

import httpx

from ..context import AgentContext

UA = {"User-Agent": "Mozilla/5.0 (compatible; JRockBot/1.0)"}
TAGS = re.compile(r"<script.*?</script>|<style.*?</style>", re.S | re.I)


def _blocked(url: str) -> str | None:
    """Refuse URLs that point back at the host or its private network.

    Without this the fetch tool is an SSRF primitive: localhost ports, the
    cloud metadata endpoint (169.254.169.254), and anything on the LAN.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "unparseable URL"
    if parts.scheme and parts.scheme not in ("http", "https"):
        return f"scheme '{parts.scheme}' is not allowed"
    host = parts.hostname
    if not host:
        return "no host in URL"
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port)
    except socket.gaierror:
        return None  # cannot resolve here; let httpx report the real error
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return f"{host} resolves to a private address ({ip})"
    return None


def to_text(raw: str, limit: int = 8000) -> str:
    raw = TAGS.sub(" ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:limit]


async def search(args: dict, ctx: AgentContext) -> str:
    q = urllib.parse.quote(args.get("query", ""))
    try:
        async with httpx.AsyncClient(timeout=25, headers=UA, follow_redirects=True) as c:
            r = await c.get(f"https://html.duckduckgo.com/html/?q={q}")
        links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
        out = []
        for href, title in links[:8]:
            title = to_text(title, 120)
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                href = urllib.parse.unquote(m.group(1))
            out.append(f"- {title}\n  {href}")
        return "\n".join(out) or "No results."
    except Exception as e:
        return f"Search failed: {e}"


async def fetch(args: dict, ctx: AgentContext) -> str:
    url = args.get("url", "")
    why = _blocked(url)
    if why:
        return f"Refused to fetch {url}: {why}."
    try:
        async with httpx.AsyncClient(timeout=30, headers=UA, follow_redirects=True) as c:
            r = await c.get(url)
        ctype = r.headers.get("content-type", "")
        if "json" in ctype or "text" in ctype or "xml" in ctype:
            return r.text[:8000]
        return to_text(r.text)
    except Exception as e:
        return f"Fetch failed: {e}"


async def deepsearch(args: dict, ctx: AgentContext) -> str:
    from ..research import deep_research
    return await deep_research(args.get("query", ""), ctx)
