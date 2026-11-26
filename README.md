# Open Co-Work

![python](https://img.shields.io/badge/python-3.10%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS-lightgrey) ![BYOK](https://img.shields.io/badge/API%20keys-optional%20(BYOK)-orange) ![cost](https://img.shields.io/badge/default%20cost-%240-brightgreen)

**An open-source AI DevOps co-worker that lives in your terminal. The LLM only translates; real tools do the work.**

Say *"commit my work"*, *"why is my disk full?"* or *"plan: deploy my app"*. Open Co-Work turns that into real `git`, `docker`, cloud-CLI and OS calls, runs them under a safety layer, and explains the **real** output. It never fakes a result: if a tool fails you see the actual error.

- **Free-first**: works with zero keys (offline mode / local Ollama), free keys (Groq, Gemini, Cerebras, ...) or paid keys (Claude, OpenAI) if you add them.
- **Fast failover**: 5 s timeout, instant provider switch on 429/timeout, circuit breaker.
- **Safe by default**: dangerous commands are blocked, destructive ones ask first, `--dry-run`, redacted audit log.

## Quick Start

### 1. Install

```bash
git clone https://github.com/teddymacharia354-code/Open-cowork.git ~/open-cowork
cd ~/open-cowork

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python agent.py --test
```

The explicit clone destination keeps the installation path consistent on case-sensitive filesystems. The virtual environment keeps project dependencies isolated from your system Python.

### 2. Choose how to launch Open Co-Work

You can use either launch method. They both run the same `agent.py` application; they do not create separate agent implementations.

**Option A — `cowork`: one-shot tasks or the interactive REPL**

Make the launcher available as a command:

```bash
chmod +x ~/open-cowork/cowork
grep -qxF 'export PATH="$HOME/open-cowork:$PATH"' ~/.bashrc 2>/dev/null || \\
echo 'export PATH="$HOME/open-cowork:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Run a one-shot task:

```bash
cowork "Check my Docker containers"
cowork "Analyze this project"
cowork --dry-run "Clean up unused Docker resources"
cowork --test
```

Run it without a task to enter the interactive experience:

```bash
cowork
```

**Option B — `chat.sh`: launch the interactive experience directly**

```bash
cd ~/open-cowork
chmod +x chat.sh
./chat.sh
```

Both interactive launch methods enter the REPL already implemented in `agent.py`. One Python process creates one Router and one Agent, then reuses them for every message until you exit. This avoids restarting the application and repeating provider startup checks after each message. Conversation context stays in memory for that session; it is not automatically restored when you close and reopen the application.

### 3. Configure an LLM (optional)

Add any free key to `.env` (e.g. `GROQ_API_KEY=...`) or run `ollama pull qwen2.5:7b` for a local model. With neither, the agent still works in **offline mode** (direct commands such as `git status`, `/tool ...`, and a keyword intent matcher). Check your setup any time with `python agent.py --test`.

**Working-directory behaviour:** the launchers locate `agent.py` relative to their own file, but they do not change your current directory. Commands and file operations therefore continue to use the directory from which you launched Open Co-Work.

## Features

Run inside the REPL, or one-shot: `python agent.py "docker ps"`. Add `--dry-run` to preview.

### Git actions
| You say | What runs |
|---|---|
| "status" / "what changed?" | `git status -sb`, `git diff [--staged]` |
| "show last 5 commits" | `git log -n5` |
| "list branches" / "create branch x" / "switch to x" | `git branch`, `git checkout [-b]` |
| "pull" / "fetch" | `git pull`, `git fetch --all --prune` (secure auth, see below) |
| "stage everything" | `git add` |
| **"commit my work"** | stages (`-A` if asked), **auto-generates the message from the staged diff** via the LLM (falls back to a heuristic when no LLM), then `git commit` |
| "push" / "force push" | `git push`; force uses `--force-with-lease` and asks first |
| "clone URL" | `git clone` (private https repos use the credential flow) |
| "stash my changes" / "pop stash" | `git stash push/pop/list/apply/drop` |
| "undo last commit" / "hard reset" | `git reset --soft/mixed/hard` (hard asks first) |
| "init a repo" / "make a .gitignore" | `git init`, `.gitignore` (secrets/logs + python/node/go/terraform) |
| "create a GitHub repo" | `gh repo create --source=. [--push]` |
| "git auth status" | which auth methods exist (never prints secrets) |

### Docker actions
`docker ps/images/logs --tail/stats/inspect`, `start/stop/restart/rm` (rm asks), `exec`, `build`, `run`, `compose up/down/ps/logs/build/pull`, `system prune` (**preview + confirm**), `volume/network ls/prune`.
- **"are my containers healthy?"** -> `docker_health` classifies running / unhealthy / crashed (non-zero exit) / restarting.
- **"fix it"** -> `docker_fix` shows the last logs, restarts crashed/unhealthy containers and verifies they came back.

### Cloud actions
- **AWS** (boto3 if installed, else the `aws` CLI; works only if *you* configured credentials, never required): EC2 list/start/stop/reboot, S3 ls, month-to-date cost + projection, security-group audit (flags `0.0.0.0/0` on SSH/DB ports), EBS (flags unattached/unencrypted), CloudWatch logs.
- **Generic**: public IP, open-port check, ping, HTTP health check, and **deploy** = `git pull` + `docker compose up -d --build` + verify (+ optional health URL).
- **DigitalOcean / Hetzner / GCP** through `doctl` / `hcloud` / `gcloud` when installed; otherwise "not installed, install with: ...".

### System actions
- `system_overview`: disk, memory, CPU load, uptime, top processes, failed systemd units, listening ports, zombies, threshold alerts.
- `toolchain`: git, docker, python, node, terraform, aws, gh, kubectl, ... with install hints for what's missing.
- Processes: list, **kill (confirm)**, "who is using port 8080?".
- Logs: file tail, `journalctl`, docker logs.
- Cleanup: `apt autoremove`, journal vacuum, docker prune, old `/tmp` files, each with a preview.
- **Watch mode** (`/watch`): every 60 s alerts when disk > 90 %, memory > 90 % or a container is down.
- systemd: create/enable/disable a service. Cron: list/add entries.

### File / create actions
`ls`, `cat` (secrets redacted), `search` (ripgrep/grep), `create_file`, `edit_file` (**backup** in `.open-cowork/backups/` + diff), `delete` (confirm, protected paths refused), `move`, `copy`, `chmod`, find large files.
- **Scaffold**: Flask, FastAPI, Node Express, static site, Dockerfile, docker-compose.yml, `.github/workflows/ci.yml`, Terraform `main.tf`, README.md (never overwrites without `overwrite=true`).
- **Create -> run -> auto-fix**: `run_with_autofix` runs a command; on failure the LLM patches the offending file (backup kept) and retries, **max 3 times**.
- **Tests / deps**: `pytest`, `npm test`, `go test` with real output; `pip`/`npm` install; `apt install` asks first.

### Chat / plan actions
- `chat: how does a blue/green deploy work?` (or any question needing no tool) talks without tools.
- `plan: deploy my app` -> the LLM writes a numbered plan of real tool calls -> you confirm -> steps run one by one with progress; a failed step asks whether to continue.
- Workspace memory (`.open-cowork/memory.json`): current project dir, last repo, last deploy.

## LLM Providers

Providers are plugins: any `providers/*.py` exposing `PROVIDER_NAME`, `ENV_KEY` and `call(messages, tools, complexity)` is auto-loaded. Models are **auto-discovered** where an API exists (nothing hard-coded except last-resort fallbacks).

| Provider | Free? | Needs key? | Models | Speed | How to enable |
|---|---|---|---|---|---|
| Groq | free tier | yes | discovered (gpt-oss, qwen, llama, deepseek, kimi...; audio/guard models filtered; fallback `openai/gpt-oss-20b`) | very fast | `GROQ_API_KEY` |
| Google Gemini | free tier | yes | discovered via ListModels (2.0/2.5/1.5 flash) | fast | `GEMINI_API_KEY` |
| Cerebras | free tier | yes | llama-3.3-70b, qwen-3-32b, gpt-oss-120b (discovered) | very fast | `CEREBRAS_API_KEY` |
| Together AI | free-tier models only | yes | zero-priced chat models | medium | `TOGETHER_API_KEY` |
| Mistral | free tier | yes | mistral-small, codestral | medium | `MISTRAL_API_KEY` |
| DeepSeek | free credits / cheap | yes | deepseek-chat | medium | `DEEPSEEK_API_KEY` |
| HuggingFace | free credits | yes | Qwen / Llama / gpt-oss via router | medium | `HF_TOKEN` |
| OpenRouter | `:free` models only (paid with `OPENROUTER_ALLOW_PAID=true`) | yes | tool-capable `:free` models | varies | `OPENROUTER_API_KEY` |
| Ollama | $0 local | no | auto-detected via `/api/tags` | hardware | install Ollama, `ollama pull qwen2.5:7b` |
| LM Studio / llama.cpp | $0 local | no | whatever is loaded | hardware | start server; llama.cpp: `LMSTUDIO_BASE_URL=http://localhost:8080/v1` |
| Anthropic Claude | paid (optional) | yes | haiku (simple), sonnet (complex) | fast | `ANTHROPIC_API_KEY` |
| OpenAI | paid (optional) | yes | gpt-4o-mini, gpt-5-mini | fast | `OPENAI_API_KEY` |

### How failover works
1. At startup all configured providers are pinged **concurrently**; the healthy set is cached 60 s (refreshed in the background).
2. 5 s hard deadline per request (`LLM_TIMEOUT`); local models get `LLM_LOCAL_TIMEOUT`. Timeout -> instant failover.
3. **429** -> provider in cooldown for 30 s, next provider immediately, no sleeping.
4. **401/403/400** -> provider marked *fatal* for the session. (Two refinements: a 400 for "model does not support tools" bans only that model; Groq's `tool_use_failed` counts as transient.)
5. Transient 5xx/malformed -> **one** immediate retry, then next provider.
6. Log format: `[LLM: groq] failed (429) -> next [LLM: gemini]`. Worst-case switch is about 5 s (< 7 s).

State is a circuit breaker: `cooldown` dict, `fatal` set, `last_used` map (see `/providers`).
With `LLM_COMPLEXITY_ROUTING=true`, simple requests use the first (fastest free) provider with a small model; planning/coding/debugging prefer Claude/GPT when keys exist, otherwise Groq's largest / Gemini 2.5.

> Note: free cloud models can exceed 5 s on long prompts. Raise `LLM_TIMEOUT` if a provider keeps timing out.

## Config

Everything is optional. Full list in [`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `LLM_ORDER` | `groq,gemini,cerebras,ollama,openrouter` | provider order; other configured providers are appended (free before paid) |
| `LLM_PRIMARY` | | forces one provider first |
| `LLM_COMPLEXITY_ROUTING` | `true` | simple -> fast free; complex -> strongest |
| `LLM_TIMEOUT` / `LLM_LOCAL_TIMEOUT` | `5` / `120` | seconds |
| `LLM_COOLDOWN` / `LLM_HEALTH_TTL` | `30` / `60` | seconds |
| `<PROVIDER>_API_KEY`, `HF_TOKEN` | | free/paid keys (see table) |
| `<PROVIDER>_MODEL` | | pin a model, e.g. `GROQ_MODEL`, `OLLAMA_MODEL` |
| `OLLAMA_HOST`, `LMSTUDIO_BASE_URL` | localhost defaults | local endpoints |
| `OPENROUTER_ALLOW_PAID` | `false` | allow non-free OpenRouter models |
| `GIT_USERNAME`, `GIT_TOKEN`, `GIT_CRED_HOSTS` | | env git auth (own risk), host allow-list |
| `GIT_SAVE_PLAINTEXT` | `false` | save prompted creds to `.env` instead of keyring |
| `WATCH_INTERVAL`, `WATCH_DISK_PCT`, `WATCH_MEM_PCT` | `60`, `90`, `90` | watch mode |

`LLM_ORDER` examples:

```bash
LLM_ORDER=groq,gemini,cerebras,openrouter       # free-only (cloud)
LLM_ORDER=groq,gemini,claude                    # free first, Claude as the paid backstop (set ANTHROPIC_API_KEY)
LLM_ORDER=ollama,lmstudio                       # local-only, nothing leaves your machine
```

## Git Auth

For https remotes the lookup order is:
1. **OS keyring** (service `open-cowork-git`, per host)
2. **`GIT_USERNAME` / `GIT_TOKEN`** from `.env`, only sent to hosts in `GIT_CRED_HOSTS` (default GitHub, GitLab, Bitbucket)
3. your configured **git credential helper** (`git credential fill`, non-interactive)
4. **Prompt**: `Push needs auth. Enter GitHub username:` then `Enter token/password (hidden):` (getpass). Used for this operation only, then `Save to keyring for next time? [y/N]`.

Credentials are passed to git through a temporary `GIT_ASKPASS` helper and `-c credential.helper=` (never on the command line) and are redacted from output and logs. SSH remotes are detected and use your ssh-agent/keys; `git_auth_status` lists the keys found. GitHub PATs, GitLab tokens (`glpat-...`) and generic passwords are supported.

> **Warning:** putting `GIT_TOKEN` in `.env` stores it in **plaintext**. That is at your own risk. `.env` is gitignored: **never commit it**. Prefer the keyring. `GIT_SAVE_PLAINTEXT=true` writes prompted credentials to `.env` (chmod 600) and is off by default.

## Safety

- **Hard block** (never runs): `rm -rf /` and other system/home roots, `mkfs`, `dd if=`, fork bombs, writes to raw disks, `chmod/chown` on `/`.
- **Confirm first**: `rm`, `docker system/volume/... prune`, `docker rm/rmi/kill`, SQL `drop`, `git push --force`, `git reset --hard`, `git clean -f`, `kill`, `apt remove/purge/autoremove`, `terraform destroy`, `find -delete`, reboot/shutdown. Non-interactive sessions decline.
- **`--dry-run`** (or `/dry-run`): every tool prints what WOULD run and executes nothing (so read-only inspection is not run either).
- **Audit log** `logs/audit.log`: `timestamp | tool | command | exit=N`, with tokens, passwords and API keys redacted (also covers values of any `*_KEY/_TOKEN/_SECRET/_PASSWORD` env var).
- `.env` is gitignored; `.env.example` has empty values. No keys are ever committed.

## Architecture

```
 User ──► agent.py (REPL: chat / plan / confirm / dry-run / memory)
            │
            ▼
        llm.py  LLM Router = TRANSLATOR ONLY
        (discovery, health cache, 429 cooldown, fatal set, failover)
            │  providers/*.py  (groq gemini cerebras together mistral deepseek
            │                   huggingface openrouter ollama lmstudio claude openai)
            ▼  tool calls (JSON)
        tools/registry.py ──► tools/_core.py  (blocklist · confirm · dry-run · audit · redaction)
            │
            ├─► git.py ───► git / gh / keyring
            ├─► docker.py ► docker / compose
            ├─► cloud.py ─► boto3|aws · doctl · hcloud · gcloud · HTTP/sockets
            ├─► system.py · process.py ─► OS (df, ps, ss, systemd, cron, journal)
            ├─► files.py · scaffold.py · dev.py ─► filesystem, tests, installs
            ▼
        real output  ──►  LLM  ──►  human answer     (no LLM? offline.py + /tool + raw commands)
```

## Commands

`/help` `/tools [cat]` `/status` `/providers [-r]` `/model [prov[:model]]` `/plan <goal>` `/dry-run [on|off]` `/clear` `/quit`; extras: `/cd <dir>`, `/watch [s]`, `/tool <name> k=v`, `chat: ...`, `!<shell cmd>`.
Flags: `--dry-run`, `--test` (PASS/FAIL/SKIP table of providers, tools, git auth, docker, system), `--no-llm`, `--help` (lists every tool).

## Roadmap

Kubernetes (`kubectl`/`helm`) tools · Terraform plan/apply wrappers · streaming responses · richer Windows support · Slack/Discord alerts for watch mode · more providers (Cohere, xAI, Fireworks).

## Contributing

PRs welcome. A new tool is one decorated function in `tools/` (auto-loaded); a new provider is one file in `providers/`. Rules: no mocks, every tool runs a real subprocess/API call, route subprocesses through `tools._core.run_cmd`, type hints and docstrings, run `python agent.py --test` before opening a PR.

## License

MIT, see [LICENSE](LICENSE).
