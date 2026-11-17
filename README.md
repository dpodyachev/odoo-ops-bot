# Odoo ops bot — monitoring and routine operations for Odoo in Telegram

A single-file Telegram bot (`bot.py`, Python standard library only) that watches Odoo servers over SSH
and runs the routine operations an Odoo admin does every week: health checks, logs, cron and queue_job
status, who is working right now, an AI analysis of the server state, a prod → stage copy of the database,
`git pull` + module update with rollback.

Bot interface: Russian and Ukrainian (per chat). In production on Odoo 14 (systemd) and Odoo 16 (Docker),
PostgreSQL on the same host or on a separate one (with a streaming replica).

## Commands

| Command | What it does |
|---|---|
| `/health` | disk, RAM, load average and CPU/iowait, Odoo service, PostgreSQL (connections, replication lag when the DB is on a separate host), nginx (`nginx -t`, ports 80/443, 5xx and 502/504 in the last 5000 requests, error.log for the last hour), Odoo ports, HTTP check of `/web/login`, log size and error count |
| `/log [N\|all]` | last lines of the Odoo log: up to 100 as text, more as a file, `all` — the whole current log as .gz |
| `/errors` | last 10 ERROR/CRITICAL/FATAL; scanner noise and serialization retries are filtered out and only counted |
| `/jobs` | cron (last/next run, ▶ running now via `FOR UPDATE SKIP LOCKED`, ⚠ overdue) and queue_job (running, queue, done, failed) |
| `/users [all]` | who was in Odoo in the last 24 h, who is online, who changed which documents |
| `/analyze` | all of the above + tracebacks → `claude -p` **without tools** → verdict, causes, next steps; compares with the previous analysis |
| `/backup` | prod → stage: new DB `<prefix>DD.MM.YY`, filestore, neutralization, switch stage, update modules, check. Or (mode `systemd`) start the server's own backup unit |
| `/pull` | `git pull` in all repositories from `addons_path`; shows new commits and affected modules; remembers the previous commits |
| `/update` | `click-odoo-update` — only modules whose code changed; Odoo keeps running; restart button at the end |
| `/restore` | roll the code back to the state before one of the last `/pull` |
| `/2stage` | stage DB from scratch: stop stage, `pg_basebackup` from the master, start as a standalone DB, neutralize (all cron, all queue jobs, mail servers, `web.base.url`), start Odoo, check |

Server is an argument (`/log stage 50`, `/errors both`) or a button.
`/backup`, `/pull`, `/update`, `/restore`, `/2stage` — only for `ADMIN_CHAT_IDS` / `BACKUP_CHAT_IDS` (private chats only),
always a plan first and a confirmation button.

## Install

Requirements on the bot host: Python 3 (standard library only), `ssh`, network access to the servers and to `api.telegram.org`.
For `/analyze` — [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude` in `PATH`).
Nothing is installed on the Odoo servers: the bot runs fixed shell scripts over SSH.

```bash
git clone <this repo> odoo-bot-client1 && cd odoo-bot-client1
cp .env.example .env && chmod 600 .env          # bot token, allowed chats, language
cp targets.example.json targets.json             # servers: ssh, url, paths, service, db, ports
python3 bot.py --cli health prod                 # check without Telegram
./run.sh start                                   # background + auto-restart; ./run.sh status | stop | restart
```

1. Create a bot with [@BotFather](https://t.me/BotFather) → `MONITOR_BOT_TOKEN`. **One bot per client**: a mistake in a chat id
   must not send one client's data to another client's chat.
2. Write to the bot; the refusal is logged as `denied chat_id=…` in `bot.log` — put that id into `ALLOWED_CHAT_IDS`.
3. SSH: a dedicated key per client, a non-root user with read access to logs and `sudo -n` only for what you enable
   (`systemctl status`, `nginx -t`; `/update` and `/2stage` need more — see comments in `targets.example.json`).
4. Docker: `deploy/docker-compose.yml`.

## Security

- User input never reaches the shell: commands are fixed scripts, arguments are validated by regex (numbers, module names, SHA, DB names).
- Access by chat id; destructive commands only from private chats, with a plan and a confirmation button that expires in 30 min.
- `/analyze` runs Claude with no tools: the model only reads the data it is given.
- Passwords are never read by the bot: `/2stage` takes the replication connection string from the DB host into a temporary `pgpass` (600) on that host.
- Logs go to the chat as files, never as public links. Keep the bot chats private: logs and `/users` contain personal data.
- `.env` and `targets.json` are in `.gitignore`. CI runs gitleaks on every push and weekly over the full history.

## CI (`.github/workflows/ci.yml`)

| Job | Why |
|---|---|
| `lint` | Python syntax, ruff (only real errors), ShellCheck for `run.sh` and `ops/*.sh`, examples are valid JSON with no real hosts, the bot loads with the example configs |
| `secrets` | gitleaks over the whole history (+ a Telegram token rule in `.gitleaks.toml`) |

The workflow token is read-only (`permissions: contents: read`), checkout does not keep credentials, there are no secrets and no deploy.
Dependabot (`.github/dependabot.yml`) opens a PR when the actions get new versions.
