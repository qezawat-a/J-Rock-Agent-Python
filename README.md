# J-Rock

A full AI agent that lives on Telegram: tool-calling loop, real terminal and
file access with per-action approval, persistent memory that dreams and
learns, personas (souls), skills, MCP servers, build/plan sub-agents, and
generators for image, voice, video and files.

Python 3.11+. Single process, SQLite storage, no external services required
beyond an LLM key.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill TG_BOT_TOKEN, TG_ALLOWED_IDS, provider + key
python run.py
```

`TG_ALLOWED_IDS` is your Telegram user id (get it from @userinfobot). The
first id is the owner: only the owner can change keys, config and
auto-approve.

## Commands

| Command | What it does |
| --- | --- |
| `/harness` | runtime status: model, tools, MCP, skills, safety switches |
| `/thinking off\|low\|medium\|high` | reasoning effort where the model supports it |
| `/models`, `/set model <name>` | list and pick models |
| `/provider [name]` | show or switch provider (openai, anthropic, deepseek, groq, openrouter, google, local) |
| `/set_api_key <provider> <key>` | store a key (owner) |
| `/skills`, `/skills add <path\|url>` | install a skill (a folder with SKILL.md, or a .md file) |
| `/mcp`, `/mcp add <name> <command...\|json>` | MCP servers over stdio or HTTP |
| `/soul`, `/soul add <name> <file\|text>`, `/set soul <name>` | personas; default `fable-5.1` |
| `/memory [query]`, `/memory add <fact>`, `/dream` | long-term memory + nightly-style consolidation |
| `/learning on\|off` | turn corrections in chat into applied lessons |
| `/deepsearch <query>` | multi-source research with citations |
| `/bugfixes <description>` | locate, fix and verify a bug |
| `/code_review [path]` | structured review: critical/high/medium/low with fixes |
| `/team`, `/agents build <task>`, `/agents plan <task>` | sub-agents; build writes, plan is read-only |
| `/generator image\|tts\|video\|files\|codes\|docs <prompt>` | generated media and files, delivered to the chat |
| `/app_connector <name> <url> [key]` | give the agent an HTTP API to call |
| `/tools` | every tool the agent can use |
| `/auto_approve_on_edit on\|off` | skip the approval buttons for terminal/file writes |
| `/auto_compat on\|off`, `/compat` | compatibility layer for non-tool-calling models |
| `/sessions`, `/resume_session <id\|last>` | conversation history |
| `/config`, `/config set max_agent_steps 40` | settings |
| `/quit` | stop the running task |

Plain messages go to the agent. Upload a file or photo and it is saved to
`data/workspace/uploads` and referenced automatically.

Telegram only allows letters, digits and underscores in command names, so the
canonical spellings are `code_review`, `auto_compat`, `resume_session` and
`set_api_key`. The hyphenated forms (`/code-review`, `/auto-compat`,
`/resume-session`, `/set-api-key`) are accepted as aliases.

## Safety model

* Terminal and file writes always ask for approval in chat unless
  `/auto_approve_on_edit on` is set by the owner.
* The filesystem tools are confined to the workspace. Reading or writing
  outside it is refused unless the owner sets `allow_outside_workspace`.
* Obvious machine-destroying commands (`rm -rf /`, `mkfs`, fork bombs) are
  refused unless the owner sets `allow_dangerous_commands`.
* `web_fetch` refuses localhost, private-network and cloud-metadata addresses.
* Only the user who triggered an action can answer its approval prompt.
* Only ids in `TG_ALLOWED_IDS` can talk to the bot.
* Secrets are printed masked by `/config`.

The agent runs as the user that started the bot. If you give it a real
machine, run it as an unprivileged user or in a container - it can do
anything the OS user can.

## Layout

```
jrock/
  config.py            settings (data/config.json + .env)
  llm/                 provider registry + async client
  agent/
    core.py            the tool-calling loop
    context.py         AgentContext (notify / confirm / deliver)
    memory.py          facts, lessons, /dream
    sessions.py        transcripts, resume
    soul.py skills.py  personas and skills
    mcp.py             stdio + HTTP MCP client
    team.py            build/plan sub-agents
    research.py        deep search
    workflows.py       bugfix / code-review
    harness.py         /harness report
    tools/             terminal, fs, web, generators, app_call
  bot/                 Telegram handlers, approvals, media
  main.py              wiring and polling
data/                  runtime: souls, skills, sessions, memory, workspace
```

## Docker

```bash
docker build -t jrock .
docker run -d --name jrock --env-file .env -v "$PWD/data:/app/data" jrock
```
