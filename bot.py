#!/usr/bin/env python3
"""Telegram-бот мониторинга Odoo-серверов (по SSH). Один код для всех инстансов бота (по одному на клиента).
Всё, что относится к клиенту, — снаружи кода:
  .env          — токен бота, кто имеет доступ (chat id), язык, название, модель Claude, SSH-ключ;
  targets.json  — серверы (SSH, URL, пути, сервис, база, порты, логи nginx) и настройки /backup.
Новый клиент = новая папка с bot.py + своими .env и targets.json (см. README.md, targets.example.json).

Только стандартная библиотека Python. Команды:
  /health  [prod|stage|both]  — диск, память, статус odoo_main/postgres, порты, HTTP
  /log     [prod|stage|both]  — последние 20 строк лога Odoo
  /errors  [prod|stage|both]  — 10 последних FATAL/CRITICAL/ERROR из последних строк лога
                                 (шум интернет-сканеров отсеивается и считается отдельно)
  /analyze [prod|stage|both]  — анализ состояния через Claude (claude -p, без инструментов)

Без аргумента бот предлагает кнопки prod / stage / оба.
Проверка без Telegram:  python3 bot.py --cli health prod
"""
import html
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(os.path.join(BASE_DIR, ".env"))

TOKEN = os.environ.get("MONITOR_BOT_TOKEN", "")
ALLOWED = {c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
SSH_KEY = os.environ.get("SSH_KEY", "/root/.ssh/id_ed25519")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")
DISK_WARN = int(os.environ.get("DISK_WARN_PCT", "85"))
# Язык ответов: чаты из UK_CHAT_IDS — украинский, остальные — русский
UK_CHAT_IDS = {c.strip() for c in os.environ.get("UK_CHAT_IDS", "").split(",") if c.strip()}
# /backup — только из этих чатов (пусто — ни из каких)


def _private_ids(name):
    """Опасные команды — только из личных чатов (id > 0): в группе их мог бы запустить любой участник."""
    ids = {c.strip() for c in os.environ.get(name, "").split(",") if c.strip()}
    bad = {c for c in ids if c.startswith("-")}
    if bad:
        print(f"{name}: группы {sorted(bad)} проигнорированы — только личные чаты", file=sys.stderr, flush=True)
    return ids - bad


BACKUP_CHAT_IDS = _private_ids("BACKUP_CHAT_IDS")

_LANG = threading.local()


DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "ru")
BOT_TITLE = os.environ.get("BOT_TITLE", "Odoo")


def chat_lang(chat_id):
    return "uk" if str(chat_id) in UK_CHAT_IDS else DEFAULT_LANG


def set_lang(lang):
    _LANG.v = lang


def L(ru, uk):
    """Текст на языке текущего потока (выставляется по чату в set_lang)."""
    return uk if getattr(_LANG, "v", os.environ.get("CLI_LANG", "ru")) == "uk" else ru

# Значения по умолчанию для полей сервера; в targets.json можно переопределить любое
TARGET_DEFAULTS = {
    "service": "odoo_main",                          # systemd-юнит Odoo
    "conf": "/opt/odoo/config/odoo-server.conf",     # конфиг Odoo (из него — logfile и db_name)
    "db": "",                                        # база; пусто — db_name из конфига или единственная база
    "psql": "psql",                                  # команда psql на сервере (например, "psql -U odoo-main")
    "nginx_access": "/var/log/nginx/odoo-access.log",
    "nginx_error": "/var/log/nginx/error.log /var/log/nginx/odoo-error.log",
    "odoo_ports": "8069 8072",                       # порты Odoo для /health (первый — HTTP, для проверки на сервере)
    "filestore": "",                                 # папка filestore (нужна только /backup prod2stage)
    "odoo_bin": "/opt/odoo/odoo-bin",                # для обновления модулей в /backup prod2stage
    "jump": "",                                      # jump-хост: сначала ssh сюда, оттуда — ssh <ssh> (ключ/алиас на jump)
    "odoo_docker": "",                               # Odoo в docker: имя контейнера (статус и лог — из docker)
    "nginx_docker": "",                              # nginx в docker: имя контейнера (access — stdout, ошибки — stderr)
    "db_ssh": "",                                    # PostgreSQL на другом хосте: куда ходить за /jobs (основная база)
    "db_ssh_ro": "",                                 # ... и за /users (можно реплику); пусто — db_ssh
    "err_lines": 10000,                              # сколько последних строк лога смотреть на ошибки
    "update_cmd": "",                                # команда click-odoo-update на сервере — включает /pull /update /restore
    "git_exclude": "",                               # репозитории из addons_path, которые /pull не трогает (через пробел)
    "update_conf": "",                               # конфиг для update_cmd, если путь другой (docker: путь в контейнере); пусто — conf
    "addons_map": "",                                # docker: пути addons_path → пути на хосте, "/mnt/extra-addons=/opt/p11/extra-addons"
    "note": "",
}
CONFIG_PATH = os.path.join(BASE_DIR, "targets.json")


def _load_config():
    """targets.json: {"servers": {имя: {...}}, "backup": {...}, "tz": "...", "users_tables": [...]}.
    Старый плоский формат {имя: {...}} (только серверы) тоже понимается."""
    if not os.path.exists(CONFIG_PATH):
        sys.exit(f"нет {CONFIG_PATH} — скопируйте targets.example.json и впишите свои серверы")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    cfg = raw if "servers" in raw else {"servers": raw}
    servers = {}
    for k, v in cfg["servers"].items():
        if not re.fullmatch(r"[a-z0-9_-]+", k) or k in ("both", "all"):
            sys.exit(f"targets.json: недопустимое имя сервера {k!r} (a-z0-9_-, не both/all)")
        if not v.get("ssh") or not v.get("url"):
            sys.exit(f"targets.json: у сервера {k!r} нужны ssh и url")
        if v.get("db_ssh") and not v.get("db"):
            sys.exit(f"targets.json: у сервера {k!r} задан db_ssh — нужно и db (имя базы)")
        servers[k] = {**TARGET_DEFAULTS, "label": k.upper(), "note": v["ssh"].split("@")[-1], **v}
    if not servers:
        sys.exit("targets.json: нет серверов")
    return cfg, servers


CONFIG, TARGETS = _load_config()
# Часовой пояс сервера/PostgreSQL (в нём время в /jobs и /users; в логе Odoo — UTC)
SERVER_TZ = CONFIG.get("tz", "Europe/Kyiv")
# Таблицы, по write_date которых /users считает, кто что менял за сутки (отсутствующие пропускаются)
USERS_TABLES = CONFIG["users_tables"] if "users_tables" in CONFIG else [
    "sale_order", "purchase_order", "stock_picking", "stock_move", "mrp_production",
    "mrp_workorder", "account_move", "project_task", "res_partner", "product_template"]
# Свои метрики «сколько сделал сегодня» для /users: таблица, колонка пользователя и колонка даты (желательно с индексом).
# [{"table": "game_communications", "user_col": "user_id", "date_col": "date", "title_ru": "…", "title_uk": "…", "short": "комм"}]
USER_METRICS = CONFIG.get("user_metrics", [])
for _m in USER_METRICS:
    for _k in ("table", "user_col", "date_col"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", str(_m.get(_k, ""))):
            sys.exit(f"targets.json: user_metrics — некорректное {_k}: {_m.get(_k)!r}")

# /backup (секция "backup" в targets.json):
#   mode=prod2stage — копия базы source → dest (ops/prod2stage.sh); systemd — запуск юнита бэкапа на сервере;
#   none (по умолчанию) — команды нет
BACKUP = CONFIG.get("backup", {})
BACKUP_MODE = BACKUP.get("mode", "none")
BACKUP_NOTE_RU, BACKUP_NOTE_UK = BACKUP.get("note_ru", ""), BACKUP.get("note_uk", "")
BACKUP_UNIT, BACKUP_TIMER = BACKUP.get("unit", ""), BACKUP.get("timer", "")
BACKUP_TARGET = BACKUP.get("target", next(iter(TARGETS)))
P2S_SRC, P2S_DST = BACKUP.get("source", "prod"), BACKUP.get("dest", "stage")
P2S = f"{P2S_SRC} → {P2S_DST}"
STAGE_UPDATE_MODULES = BACKUP.get("modules", "-")
DB_PREFIX = BACKUP.get("db_prefix", "")
NEUTRALIZE = {"ru": BACKUP.get("neutralize_ru", "почта, интеграции, cron, web.base.url"),
              "uk": BACKUP.get("neutralize_uk", "пошта, інтеграції, cron, web.base.url")}


def p2s_env():
    """Параметры для ops/prod2stage.sh (переменные окружения P2S_*) — из targets.json."""
    src, dst = TARGETS[P2S_SRC], TARGETS[P2S_DST]
    return {
        "P2S_PROD_SSH": src["ssh"], "P2S_STAGE_SSH": dst["ssh"],
        "P2S_PROD_DB": BACKUP.get("source_db") or src["db"], "P2S_DB_PREFIX": DB_PREFIX,
        "P2S_BASE_URL": BACKUP.get("dest_base_url", ""), "P2S_CHECK_URL": dst["url"],
        "P2S_STAGE_CONF": dst["conf"], "P2S_SVC": dst["service"], "P2S_ODOO_BIN": dst["odoo_bin"],
        "P2S_HTTP_PORT": dst["odoo_ports"].split()[0],
        "P2S_PROD_FILESTORE": src["filestore"], "P2S_STAGE_FILESTORE": dst["filestore"],
        "P2S_DUMP_DIR": BACKUP.get("dump_dir", ""), "P2S_DB_OWNER": BACKUP.get("db_owner", "odoo"),
        "P2S_TZ": SERVER_TZ, "P2S_NEUTRALIZE_SQL": BACKUP.get("neutralize_sql", "ops/stage-neutralize.sql"),
        "STAGE_UPDATE_MODULES": STAGE_UPDATE_MODULES,
    }


def _check_backup_config():
    if BACKUP_MODE not in ("prod2stage", "systemd", "none"):
        sys.exit(f"targets.json: backup.mode={BACKUP_MODE!r} — нужно prod2stage, systemd или none")
    if BACKUP_MODE == "systemd" and (not BACKUP_UNIT or BACKUP_TARGET not in TARGETS):
        sys.exit("targets.json: для backup.mode=systemd нужны backup.unit и backup.target (имя сервера)")
    if BACKUP_MODE == "prod2stage":
        if P2S_SRC not in TARGETS or P2S_DST not in TARGETS or P2S_SRC == P2S_DST:
            sys.exit("targets.json: backup.source и backup.dest — два разных сервера из servers")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", DB_PREFIX):
            sys.exit("targets.json: backup.db_prefix — префикс имени новой базы (a-z0-9_), например erp_")
        missing = [k for k, v in p2s_env().items() if not v and k != "STAGE_UPDATE_MODULES"]
        if missing:
            sys.exit("targets.json: для backup.mode=prod2stage не заданы: " + ", ".join(missing)
                     + " (source_db, dest_base_url, dump_dir в backup; filestore у серверов)")


_check_backup_config()


def label(target):
    return TARGETS[target]["label"]


def subst(script, target):
    """Подставляет параметры сервера в шаблон удалённого скрипта (__CONF__, __SERVICE__, __DBNAME__, …).
    Маркеры вывода вида __DB__=, __CRON__ — не трогаются."""
    t = TARGETS[target]
    for k, token in (("conf", "__CONF__"), ("service", "__SERVICE__"), ("db", "__DBNAME__"), ("psql", "__PSQL__"),
                     ("nginx_access", "__NGINX_ACCESS__"), ("nginx_error", "__NGINX_ERROR__"),
                     ("odoo_ports", "__ODOO_PORTS__"), ("odoo_docker", "__ODOO_DOCKER__"),
                     ("nginx_docker", "__NGINX_DOCKER__"), ("err_lines", "__ERR_LINES__"), ("db_ssh", "__DB_SSH__"),
                     ("update_cmd", "__UPDATE_CMD__"), ("git_exclude", "__GIT_EXCLUDE__"),
                     ("addons_map", "__ADDONS_MAP__")):
        script = script.replace(token, str(t[k]))
    # update_cmd в docker пишет лог в stdout (путь --logfile хоста контейнеру не виден)
    script = script.replace("__UPD_CONF__", t["update_conf"] or t["conf"])
    return script.replace("__UPD_STDOUT__", "1" if t["odoo_docker"] else "")

# Строка уровня ERROR/CRITICAL в формате лога Odoo, либо FATAL где угодно (например, от PostgreSQL)
ERR_RE = r"^[0-9-]+ [0-9:,]+ [0-9]+ (CRITICAL|ERROR) |FATAL"
# Мусор от сканеров: TLS/HTTP2/бинарные запросы прямо на порты Odoo
NOISE_RE = r"code (400|505), message|Invalid HTTP method|Expected GET method|Invalid http version"

# Путь к логу берётся из конфига Odoo; если logfile не задан — journalctl.
# Odoo в docker (odoo_docker) — лог из docker logs (цветные коды ANSI вырезаются).
REMOTE_LOGSRC = f"""
OD="__ODOO_DOCKER__"
LOG=$(sed -n 's/^[[:space:]]*logfile[[:space:]]*=[[:space:]]*//p' __CONF__ 2>/dev/null | tail -1)
[ -n "$OD" ] && LOG=""
noansi() {{ sed -r 's/\\x1b\\[[0-9;]*m//g'; }}
logsrc() {{ if [ -n "$OD" ]; then sudo -n docker logs --tail "$1" "$OD" 2>&1 | noansi; \
elif [ -n "$LOG" ] && [ -r "$LOG" ]; then tail -n "$1" "$LOG"; \
else sudo -n journalctl -u __SERVICE__ -n "$1" --no-pager -o short-iso 2>/dev/null \
|| journalctl -u __SERVICE__ -n "$1" --no-pager -o short-iso; fi; }}
errs() {{ logsrc __ERR_LINES__ | grep -aE '{ERR_RE}'; }}
"""

REMOTE_HEALTH = REMOTE_LOGSRC + f"""
df -B1 -P / | awk 'NR==2{{print "disk_total="$2; print "disk_used="$3; print "disk_avail="$4}}'
free -b | awk '/^Mem:/{{print "mem_total="$2; print "mem_avail="$7}}'
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
echo "cpus=$(nproc)"
echo "uptime=$(uptime -p)"
if [ -n "$OD" ]; then
  S=$(sudo -n docker inspect -f '{{{{.State.Status}}}}' "$OD" 2>/dev/null || echo missing); [ "$S" = running ] && S=active
  echo "odoo=$S"
  echo "odoo_since=$(sudo -n docker inspect -f '{{{{.State.StartedAt}}}}' "$OD" 2>/dev/null | cut -c1-19 | tr T ' ') UTC"
  echo "odoo_restarts=$(sudo -n docker inspect -f '{{{{.RestartCount}}}}' "$OD" 2>/dev/null)"
else
  echo "odoo=$(systemctl is-active __SERVICE__)"
  echo "odoo_since=$(systemctl show __SERVICE__ -p ActiveEnterTimestamp --value)"
  echo "odoo_restarts=$(systemctl show __SERVICE__ -p NRestarts --value)"
fi
[ -z "__DB_SSH__" ] && echo "pg=$(systemctl is-active postgresql)"
# CPU за 1 секунду: занятость и iowait (помогает понять, откуда load)
read -r _ u n sy i w q sq st _ < /proc/stat; sleep 1; read -r _ u2 n2 sy2 i2 w2 q2 sq2 st2 _ < /proc/stat
T=$(( (u2+n2+sy2+i2+w2+q2+sq2+st2) - (u+n+sy+i+w+q+sq+st) )); [ "$T" -gt 0 ] || T=1
echo "cpu_busy=$(( 100 * ((u2+n2+sy2+q2+sq2+st2) - (u+n+sy+q+sq+st)) / T ))"
echo "cpu_iowait=$(( 100 * (w2 - w) / T ))"
echo "top_cpu=$(ps -eo pcpu,comm --sort=-pcpu --no-headers | head -3 | awk '{{printf "%s %s%%; ", $2, $1}}')"
# nginx
ND="__NGINX_DOCKER__"
if [ -n "$ND" ]; then
  S=$(sudo -n docker inspect -f '{{{{.State.Status}}}}' "$ND" 2>/dev/null || echo missing); [ "$S" = running ] && S=active
  echo "nginx=$S"
  echo "nginx_since=$(sudo -n docker inspect -f '{{{{.State.StartedAt}}}}' "$ND" 2>/dev/null | cut -c1-19 | tr T ' ') UTC"
  echo "nginx_conf=$(sudo -n docker exec "$ND" nginx -t >/dev/null 2>&1 && echo ok || echo fail)"
else
  echo "nginx=$(systemctl is-active nginx)"
  echo "nginx_since=$(systemctl show nginx -p ActiveEnterTimestamp --value)"
  echo "nginx_conf=$(sudo -n nginx -t >/dev/null 2>&1 && echo ok || echo fail)"
fi
for p in 80 443; do ss -ltn "sport = :$p" | grep -q LISTEN && echo "port_$p=up" || echo "port_$p=down"; done
A=__NGINX_ACCESS__
ngx_access() {{ if [ -n "$ND" ]; then sudo -n docker logs --tail "$1" "$ND" 2>/dev/null; else sudo -n tail -n "$1" "$A"; fi; }}
if [ -n "$ND" ] || sudo -n test -r "$A"; then
  ngx_access 5000 | awk '{{t++; if ($9 ~ /^5/) f++; if ($9 == "502" || $9 == "504") g++}} END {{print "ngx_req=" t+0; print "ngx_5xx=" f+0; print "ngx_502=" g+0}}'
  echo "ngx_from=$(ngx_access 5000 | head -1 | grep -oE '\\[[^]]+' | head -1 | tr -d '[' | cut -d' ' -f1)"
fi
SINCE=$(date -d '-60 min' '+%Y/%m/%d %H:%M')
if [ -n "$ND" ]; then ngx_err() {{ sudo -n docker logs --tail 3000 "$ND" 2>&1 >/dev/null; }}
else ngx_err() {{ sudo -n tail -q -n 3000 __NGINX_ERROR__ 2>/dev/null; }}; fi
NE=$(ngx_err | awk -v s="$SINCE" 'substr($0, 1, 16) >= s && /\\[(error|crit|alert|emerg)\\]/')
echo "ngx_err_1h=$(printf '%s\n' "$NE" | grep -c .)"
echo "ngx_upstream_1h=$(printf '%s\n' "$NE" | grep -c upstream)"
# Odoo в docker без опубликованных портов слушает только в своём сетевом namespace — смотрим LISTEN (0A) в /proc/<pid>/net
OPID=""; [ -n "$OD" ] && OPID=$(sudo -n docker inspect -f '{{{{.State.Pid}}}}' "$OD" 2>/dev/null)
for p in __ODOO_PORTS__; do
  if [ -n "$OPID" ] && [ "$OPID" != 0 ]; then
    h=$(printf '%04X' "$p")
    cat "/proc/$OPID/net/tcp" "/proc/$OPID/net/tcp6" 2>/dev/null | awk -v h=":$h" '$2 ~ h"$" && $4 == "0A" {{f=1}} END {{exit !f}}' \
      && echo "port_$p=up" || echo "port_$p=down"
  else
    ss -ltn "sport = :$p" | grep -q LISTEN && echo "port_$p=up" || echo "port_$p=down"
  fi
done
if [ -n "$OD" ]; then
  LP=$(sudo -n docker inspect -f '{{{{.LogPath}}}}' "$OD" 2>/dev/null)
  echo "logfile=docker logs $OD"
  [ -n "$LP" ] && {{ echo "log_size=$(sudo -n stat -c %s "$LP")"; echo "log_mtime=$(sudo -n stat -c %Y "$LP")"; }}
else
  echo "logfile=${{LOG:-journal}}"
  if [ -n "$LOG" ] && [ -r "$LOG" ]; then echo "log_size=$(stat -c %s "$LOG")"; echo "log_mtime=$(stat -c %Y "$LOG")"; fi
fi
E=$(errs)
echo "err_real=$(printf '%s\\n' "$E" | grep -avcE '{NOISE_RE}|^$')"
echo "err_noise=$(printf '%s\\n' "$E" | grep -acE '{NOISE_RE}')"
"""

LOG_CHAT_MAX = 100      # до стольки строк — текстом в чат, больше — файлом
LOG_LINES_MAX = 200000  # потолок для /log <N>
TG_FILE_MAX = 49 * 1024 * 1024  # лимит Telegram на отправку файла ботом — 50 МБ
TMP_DIR = os.path.join(BASE_DIR, "tmp")


def remote_log_tail(n):
    return REMOTE_LOGSRC + f"logsrc {int(n)}\n"


# docker-лог может быть огромным и без ротации — /log all берёт последние N строк
LOG_ALL_DOCKER_LINES = 1000000
REMOTE_LOG_ALL_GZ = REMOTE_LOGSRC + f"""
if [ -n "$OD" ]; then logsrc {LOG_ALL_DOCKER_LINES} | gzip -c
elif [ -n "$LOG" ] && [ -r "$LOG" ]; then gzip -c "$LOG"
else (sudo -n journalctl -u __SERVICE__ --no-pager -o short-iso 2>/dev/null || journalctl -u __SERVICE__ --no-pager -o short-iso) | gzip -c; fi
"""
# /errors: ошибки разбираются по записям (строка ERROR + до 3 строк продолжения): у «bad query» текст
# «could not serialize access» стоит на следующей строке. Serialization-ошибки (конфликт параллельных
# транзакций, Odoo сам повторяет запрос) не показываются — только их число с начала суток (__DAYUTC__, лог Odoo в UTC).
SER_RE = "serializ"
REMOTE_ERRORS = REMOTE_LOGSRC + f"""
E=$(logsrc __ERR_LINES__ | awk -v ere='{ERR_RE}' -v sre='{SER_RE}' -v d="__DAYUTC__" '
function flush() {{ if (h != "") {{ if (s) {{ if (substr(h, 1, 19) >= d) sd++ }} else print h }}; h = ""; s = 0; n = 0 }}
first == "" && /^[0-9-]+ [0-9:,]+ [0-9]+ / {{ first = substr($0, 1, 19) }}
$0 ~ ere {{ flush(); h = $0; s = (tolower($0) ~ sre); next }}
/^[0-9-]+ [0-9:,]+ [0-9]+ / {{ flush(); next }}
h != "" && ++n <= 3 && tolower($0) ~ sre {{ s = 1 }}
END {{ flush(); print "__SER__=" sd + 0 " " first }}')
printf '%s\\n' "$E" | grep -av '^__SER__=' | grep -avE '{NOISE_RE}|^$' | tail -n 10
printf '%s\\n' "$E" | grep -a '^__SER__='
echo "__NOISE__=$(printf '%s\\n' "$E" | grep -acE '{NOISE_RE}')"
"""


def _day_start_utc():
    """Начало текущих суток в SERVER_TZ, переведённое в UTC: (строка 'YYYY-MM-DD HH:MM:SS', datetime с tz)."""
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(SERVER_TZ)
    except Exception:
        tz = timezone.utc
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), tz


# Для анализа: ошибки (без шума сканеров) с контекстом трейсбека + свежий хвост лога.
REMOTE_ANALYZE_LOGS = REMOTE_LOGSRC + f"""
echo "### ОШИБКИ из последних __ERR_LINES__ строк (без шума сканеров), с 8 строками контекста, последние 200 строк"
logsrc __ERR_LINES__ | grep -avE '{NOISE_RE}' | grep -aE -A8 '{ERR_RE}' | tail -n 200
echo "### ШУМ СКАНЕРОВ (строк): $(errs | grep -acE '{NOISE_RE}')"
echo "### ХВОСТ ЛОГА, 80 строк"
logsrc 80
"""


# ---------------------------------------------------------------- SSH / проверки

def _ssh_cmd(target, host_field="ssh"):
    """Команда ssh до сервера (или до его хоста БД: host_field="db_ssh"/"db_ssh_ro").
    С jump: ssh jump 'ssh <хост> bash -s' — ключ и алиасы внутренних хостов лежат на jump-хосте."""
    t = TARGETS[target]
    host = t.get(host_field) or t["ssh"]
    base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
            "-o", "StrictHostKeyChecking=accept-new", "-i", SSH_KEY]
    if t.get("jump"):
        return base + [t["jump"], "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 "
                       + shlex.quote(host) + " bash -s"]
    return base + [host, "bash -s"]


def ssh(target, script, timeout=60, host_field="ssh"):
    script = subst(script, target)
    cmd = _ssh_cmd(target, host_field)
    try:
        p = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
    except subprocess.TimeoutExpired:
        raise RuntimeError(L(f"SSH: таймаут {timeout} с", f"SSH: тайм-аут {timeout} с"))
    if p.returncode != 0 and not p.stdout.strip():
        raise RuntimeError(L("SSH: ошибка", "SSH: помилка") + f" {p.returncode}: {p.stderr.strip()[:500]}")
    return p.stdout


def db_host(target, ro=False):
    """Где выполнять SQL: на хосте БД (db_ssh / db_ssh_ro), если он задан, иначе на самом сервере."""
    t = TARGETS[target]
    if ro and t.get("db_ssh_ro"):
        return "db_ssh_ro"
    return "db_ssh" if t.get("db_ssh") else "ssh"


def ssh_to_file(target, script, path, timeout=600):
    """Как ssh(), но stdout (бинарный) пишется в файл."""
    script = subst(script, target)
    cmd = _ssh_cmd(target)
    with open(path, "wb") as f:
        try:
            p = subprocess.run(cmd, input=script.encode(), stdout=f, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(L(f"SSH: таймаут {timeout} с", f"SSH: тайм-аут {timeout} с"))
    if p.returncode != 0:
        raise RuntimeError(L("SSH: ошибка", "SSH: помилка") + f" {p.returncode}: {p.stderr.decode(errors='replace').strip()[:500]}")


def http_check(url):
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "odoo-health-bot"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, time.time() - t0, None
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0, None
    except Exception as e:  # noqa: BLE001
        return None, time.time() - t0, str(e)[:200]


def gb(x):
    return f"{int(x) / 1024**3:.1f} ГБ"


# PostgreSQL на отдельном хосте (db_ssh): статус, соединения, репликация, диск
REMOTE_DB_HEALTH = """
P() { __PSQL__ -X -At -d postgres -c "$1" 2>/dev/null; }
[ "$(P "select 1")" = 1 ] && echo "pg=active" || echo "pg=down"
echo "pg_conn=$(P "select count(*) from pg_stat_activity")"
echo "pg_maxconn=$(P "show max_connections")"
echo "pg_recovery=$(P "select pg_is_in_recovery()")"
echo "pg_repl=$(P "select string_agg(coalesce(client_addr::text, '?') || ' ' || state || ', lag ' || coalesce(date_trunc('second', replay_lag)::text, '0'), '; ') from pg_stat_replication")"
df -B1 -P / | awk 'NR==2{print "pg_disk_pct=" int($3 * 100 / $2); print "pg_disk_avail=" $4}'
"""


def health_data(target):
    raw = ssh(target, REMOTE_HEALTH)
    if TARGETS[target]["db_ssh"]:
        try:
            raw += "\n# db host " + TARGETS[target]["db_ssh"] + "\n" + ssh(target, REMOTE_DB_HEALTH, host_field="db_ssh")
        except Exception as e:  # noqa: BLE001
            raw += f"\npg=ssh-error ({str(e)[:100]})\n"
    d = dict(l.split("=", 1) for l in raw.splitlines() if "=" in l)
    d["http_code"], d["http_time"], d["http_err"] = http_check(TARGETS[target]["url"])
    return d, raw


def fmt_health(target, d):
    ok, bad, warn = "✅", "❌", "⚠️"
    lines = []
    problems = 0

    total, used, avail = (int(d.get(k, 0) or 0) for k in ("disk_total", "disk_used", "disk_avail"))
    pct = round(used * 100 / total) if total else 0
    ico = ok if pct < DISK_WARN else (warn if pct < 95 else bad)
    problems += ico != ok
    lines.append(L(f"{ico} Диск /: {pct}% — занято {gb(used)} из {gb(total)}, свободно {gb(avail)}",
                   f"{ico} Диск /: {pct}% — зайнято {gb(used)} з {gb(total)}, вільно {gb(avail)}"))

    mt, ma = int(d.get("mem_total", 0) or 0), int(d.get("mem_avail", 0) or 0)
    mpct = round((mt - ma) * 100 / mt) if mt else 0
    ico = ok if mpct < 90 else warn
    problems += ico != ok
    lines.append(L(f"{ico} RAM: {mpct}% — доступно {gb(ma)} из {gb(mt)}",
                   f"{ico} RAM: {mpct}% — доступно {gb(ma)} з {gb(mt)}"))

    cpus = int(d.get("cpus", 1) or 1)
    la = [float(x) for x in (d.get("load") or "0 0 0").split()[:3]] + [0.0] * 3
    l1, l5, l15 = la[:3]
    busy, iow = int(d.get("cpu_busy", 0) or 0), int(d.get("cpu_iowait", 0) or 0)
    per5 = l5 / cpus
    ico = ok if per5 < 1.0 and iow < 20 else (warn if per5 < 2.0 else bad)
    problems += ico != ok
    lines.append(f"{ico} Load average 1/5/15 " + L("мин", "хв") + f": {l1:.2f} / {l5:.2f} / {l15:.2f} на {cpus} CPU "
                 f"({l1 / cpus:.0%} / {per5:.0%} / {l15 / cpus:.0%} " + L("от ядер", "від ядер") + ")")
    lines.append(L(f"   CPU сейчас: занят {busy}%, iowait {iow}%", f"   CPU зараз: зайнято {busy}%, iowait {iow}%"))
    if ico != ok and d.get("top_cpu"):
        lines.append(L("   топ по CPU: ", "   топ за CPU: ") + d['top_cpu'].strip('; '))

    s = d.get("odoo", "?")
    ico = ok if s == "active" else bad
    problems += ico != ok
    svc = TARGETS[target]["service"]
    lines.append(L(f"{ico} {svc}: {s}, с {d.get('odoo_since', '?')}, рестартов: {d.get('odoo_restarts', '?')}",
                   f"{ico} {svc}: {s}, з {d.get('odoo_since', '?')}, рестартів: {d.get('odoo_restarts', '?')}"))

    pg = d.get("pg", "?")
    ico = ok if pg == "active" else bad
    if TARGETS[target]["db_ssh"]:
        conn, mx = int(d.get("pg_conn") or 0), int(d.get("pg_maxconn") or 0)
        dpct = int(d.get("pg_disk_pct") or 0)
        if ico == ok and ((mx and conn > mx * 0.8) or dpct >= DISK_WARN):
            ico = warn
        problems += ico != ok
        lines.append(f"{ico} postgresql ({TARGETS[target]['db_ssh']}): {pg}, "
                     + L(f"соединений {conn} из {mx}, диск {dpct}% (свободно {gb(d.get('pg_disk_avail') or 0)})",
                         f"з'єднань {conn} з {mx}, диск {dpct}% (вільно {gb(d.get('pg_disk_avail') or 0)})"))
        if d.get("pg_repl"):
            lines.append(L("   репликация: ", "   реплікація: ") + d["pg_repl"])
    else:
        problems += ico != ok
        lines.append(f"{ico} postgresql: {pg}")

    ng, conf = d.get("nginx", "?"), d.get("nginx_conf", "?")
    ports = " ".join(f"{p}:{d.get(f'port_{p}', '?')}" for p in ("80", "443"))
    ico = ok if ng == "active" and conf == "ok" and "down" not in ports else bad
    problems += ico != ok
    lines.append(L(f"{ico} nginx: {ng}, с {d.get('nginx_since', '?')}, конфиг: {conf}, порты {ports}",
                   f"{ico} nginx: {ng}, з {d.get('nginx_since', '?')}, конфіг: {conf}, порти {ports}"))
    if "ngx_req" in d:
        req, e5, e502 = (int(d.get(k, 0) or 0) for k in ("ngx_req", "ngx_5xx", "ngx_502"))
        ico = ok if e502 == 0 and (req == 0 or e5 / req < 0.01) else warn
        problems += ico != ok
        lines.append(L(f"{ico} nginx, последние {req} запросов (с {d.get('ngx_from', '?')}): 5xx — {e5}, из них 502/504 — {e502}",
                       f"{ico} nginx, останні {req} запитів (з {d.get('ngx_from', '?')}): 5xx — {e5}, з них 502/504 — {e502}"))
    ne, nu = int(d.get("ngx_err_1h", 0) or 0), int(d.get("ngx_upstream_1h", 0) or 0)
    ico = ok if nu == 0 else warn
    problems += ico != ok
    lines.append(L(f"{ico} nginx error.log за час: ошибок {ne}, из них до Odoo (upstream) — {nu}",
                   f"{ico} nginx error.log за годину: помилок {ne}, з них до Odoo (upstream) — {nu}"))

    for p in TARGETS[target]["odoo_ports"].split():
        st = d.get(f"port_{p}", "?")
        ico = ok if st == "up" else bad
        problems += ico != ok
        lines.append(f"{ico} порт {p}: {st}")

    code, t, err = d["http_code"], d["http_time"], d["http_err"]
    ico = ok if code == 200 else bad
    problems += ico != ok
    lines.append(f"{ico} HTTP {TARGETS[target]['url'].split('//')[1]}: {code or err} за {t:.2f} с")

    if "log_mtime" in d:
        age = int(time.time() - int(d["log_mtime"]))
        lines.append(L(f"ℹ️ Лог: {d['logfile']} ({int(d['log_size']) / 1024**2:.0f} МБ), последняя запись {age} с назад",
                       f"ℹ️ Лог: {d['logfile']} ({int(d['log_size']) / 1024**2:.0f} МБ), останній запис {age} с тому"))
    else:
        lines.append(f"ℹ️ Лог: {d.get('logfile', '?')}")
    real, noise = int(d.get("err_real", 0) or 0), int(d.get("err_noise", 0) or 0)
    lines.append(f"{'ℹ️' if real == 0 else warn} " + L(f"Ошибок в последних {TARGETS[target]['err_lines']} строках: {real} (+ шум сканеров: {noise})",
                                                      f"Помилок в останніх {TARGETS[target]['err_lines']} рядках: {real} (+ шум сканерів: {noise})"))
    lines.append(f"ℹ️ Uptime сервера: {d.get('uptime', '?')}")

    head = (f"{ok} {label(target)}: " + L("всё в порядке", "все гаразд")) if problems == 0 \
        else f"{bad} {label(target)}: " + L("проблем", "проблем") + f" — {problems}"
    return head, "\n".join(lines)


def cut_lines(text, width=500):
    return "\n".join(l if len(l) <= width else l[:width] + " …" for l in text.splitlines())


def do_health(target):
    d, _ = health_data(target)
    head, body = fmt_health(target, d)
    return [("b", head), ("pre", body)]


ODOO_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d),\d+ \d+ (\w+) (.*)$")
LOUD_LEVELS = ("ERROR", "CRITICAL", "FATAL")


def compact_log(text):
    """Убирает дату, время, pid и уровень; уровень оставляет только для ERROR/CRITICAL/FATAL.
    Возвращает (строки, первая метка времени, последняя метка времени)."""
    rows, first, last = [], None, None
    for line in text.splitlines():
        m = ODOO_LINE_RE.match(line)
        if not m:  # продолжение трейсбека и т.п. — как есть
            rows.append(line)
            continue
        date, tm, level, rest = m.groups()
        first = first or f"{date} {tm}"
        last = f"{date} {tm}"
        rows.append(f"❗{level} {rest}" if level in LOUD_LEVELS or "FATAL" in rest else rest)
    return "\n".join(rows), first, last


def parse_count(arg):
    """None/'' -> 20; число -> число (1..LOG_LINES_MAX); all/все -> 'all'."""
    if not arg:
        return 20
    a = str(arg).lower()
    if a in ("all", "все", "всё", "всі", "усі"):
        return "all"
    if a.isdigit() and int(a) > 0:
        return min(int(a), LOG_LINES_MAX)
    return None


def _tmp_path(name):
    os.makedirs(TMP_DIR, exist_ok=True)
    return os.path.join(TMP_DIR, f"{int(time.time() * 1000)}-{name}")


def do_log(target, count=20):
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    if count == "all":
        path = _tmp_path(f"{target}-odoo-log-{stamp}.log.gz")
        ssh_to_file(target, REMOTE_LOG_ALL_GZ, path, timeout=900)
        size = os.path.getsize(path)
        if size > TG_FILE_MAX:
            os.remove(path)
            raise RuntimeError(L(f"лог в gzip весит {size / 1024**2:.0f} МБ — больше лимита Telegram (50 МБ). "
                                 f"Запросите часть: /log {target} 100000",
                                 f"лог у gzip важить {size / 1024**2:.0f} МБ — більше за ліміт Telegram (50 МБ). "
                                 f"Запросіть частину: /log {target} 100000"))
        return [("b", f"{label(target)}: " + L("весь текущий лог Odoo", "весь поточний лог Odoo") + f", gzip {size / 1024**2:.1f} МБ"),
                ("file", (path, os.path.basename(path).split("-", 1)[1]))]

    out = ssh(target, remote_log_tail(count), timeout=180).strip()
    if not out:
        return [("b", f"{label(target)}: " + L("лог пуст", "лог порожній"))]
    body, first, last = compact_log(out)
    span = ""
    if first:
        span = first if first == last else f"{first} — {last[11:] if last[:10] == first[:10] else last}"
        span = f" ({span} UTC)"
    n = len(out.splitlines())
    head = f"{label(target)}: " + L(f"последние {n} строк лога", f"останні {n} рядків лога") + span
    if count <= LOG_CHAT_MAX:
        return [("b", head), ("pre", cut_lines(body))]
    # много строк — файлом, без сокращений (полные строки с датой и уровнем)
    raw = out.encode("utf-8", errors="replace") + b"\n"
    if len(raw) > 1024 * 1024:
        import gzip
        path = _tmp_path(f"{target}-odoo-log-last{n}-{stamp}.log.gz")
        with gzip.open(path, "wb") as f:
            f.write(raw)
    else:
        path = _tmp_path(f"{target}-odoo-log-last{n}-{stamp}.log")
        with open(path, "wb") as f:
            f.write(raw)
    return [("b", head + " — файлом"), ("file", (path, os.path.basename(path).split("-", 1)[1]))]


def do_errors(target):
    day_utc, tz = _day_start_utc()
    out = ssh(target, REMOTE_ERRORS.replace("__DAYUTC__", day_utc))
    m = re.search(r"^__NOISE__=(\d+)", out, re.M)
    noise = int(m.group(1)) if m else 0
    m = re.search(r"^__SER__=(\d+) ?(.*)$", out, re.M)
    ser, first = (int(m.group(1)), m.group(2).strip()) if m else (0, "")
    out = re.sub(r"^__(NOISE|SER)__=.*$", "", out, flags=re.M).strip()
    tail = L(f"Шум сканеров (битые запросы на порты Odoo) отсеян: {noise} строк.",
             f"Шум сканерів (биті запити на порти Odoo) відсіяно: {noise} рядків.")
    since = "00:00"
    if first and first > day_utc:   # окно лога начинается позже полуночи — считаем с начала окна
        from datetime import datetime, timezone
        try:
            since = datetime.strptime(first, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(tz).strftime("%H:%M")
        except ValueError:
            pass
    tail += "\n" + L(f"Serialization (could not serialize access — конфликт параллельных транзакций, Odoo повторяет запрос) "
                     f"не показаны; сегодня с {since} ({SERVER_TZ}): {ser}.",
                     f"Serialization (could not serialize access — конфлікт паралельних транзакцій, Odoo повторює запит) "
                     f"не показано; сьогодні з {since} ({SERVER_TZ}): {ser}.")
    if not out:
        return [("b", f"✅ {label(target)}: " + L(f"в последних {TARGETS[target]['err_lines']} строках FATAL/CRITICAL/ERROR нет",
                                                   f"в останніх {TARGETS[target]['err_lines']} рядках FATAL/CRITICAL/ERROR немає")
                 + L(" (кроме serialization)", " (крім serialization)") * bool(ser)), ("text", tail)]
    return [("b", f"{label(target)}: " + L(f"последние 10 FATAL/CRITICAL/ERROR (из {TARGETS[target]['err_lines']} строк, без serialization)",
                                           f"останні 10 FATAL/CRITICAL/ERROR (з {TARGETS[target]['err_lines']} рядків, без serialization)")),
            ("pre", cut_lines(out, 700)), ("text", tail)]


# /jobs: ir_cron (что «сейчас выполняется» — строка ir_cron заблокирована воркером крона;
# проверяем через FOR UPDATE SKIP LOCKED в автокоммите, блокировка держится миллисекунды)
# и queue_job (state=started). Время — в часовом поясе PostgreSQL (SERVER_TZ).
# Общее начало для запросов в БД: база — db_name из конфига, иначе единственная база кроме postgres.
REMOTE_DB_PRELUDE = f"""
DB="__DBNAME__"
[ -z "$DB" ] && DB=$(sed -n 's/^[[:space:]]*db_name[[:space:]]*=[[:space:]]*//p' __CONF__ 2>/dev/null | tail -1)
[ "$DB" = False ] && DB=""
if [ -z "$DB" ]; then
  DBS=$(__PSQL__ -X -At -d postgres -c "select datname from pg_database where not datistemplate and datname<>'postgres'")
  [ "$(printf '%s\\n' "$DBS" | grep -c .)" = 1 ] && DB=$DBS
fi
[ -z "$DB" ] && {{ echo "__ERR__=не удалось определить базу: задайте db_name в конфиге"; exit 0; }}
echo "__DB__=$DB"
Q() {{ __PSQL__ -X -At -F $'\\t' -d "$DB" -c "$1"; }}
"""

REMOTE_JOBS = REMOTE_DB_PRELUDE + f"""
echo "__CRON__"
Q "WITH free AS (SELECT id FROM ir_cron WHERE active AND numbercall <> 0 FOR UPDATE SKIP LOCKED)
   SELECT c.cron_name, c.interval_number || ' ' || c.interval_type,
          coalesce(to_char(c.lastcall AT TIME ZONE 'UTC', 'DD.MM HH24:MI'), '—'),
          coalesce(to_char(c.nextcall AT TIME ZONE 'UTC', 'DD.MM HH24:MI'), '—'),
          (c.id NOT IN (SELECT id FROM free)),
          (c.nextcall < (now() AT TIME ZONE 'UTC') - interval '15 min')
   FROM ir_cron c WHERE c.active AND c.numbercall <> 0
   ORDER BY c.lastcall DESC NULLS LAST"
if [ "$(Q "select to_regclass('queue_job') is not null")" = t ]; then
  echo "__QSTATS__"
  Q "SELECT state, count(*) FROM queue_job GROUP BY 1 ORDER BY 1"
  echo "__QFAIL24__"
  Q "SELECT count(*) FROM queue_job WHERE state='failed' AND date_created > (now() AT TIME ZONE 'UTC') - interval '24 hours'"
  echo "__QRUN__"
  Q "SELECT coalesce(name, method_name), to_char(date_started AT TIME ZONE 'UTC', 'DD.MM HH24:MI'),
            round(extract(epoch FROM (now() - (date_started AT TIME ZONE 'UTC'))) / 60), coalesce(channel, '')
     FROM queue_job WHERE state = 'started' ORDER BY date_started LIMIT 15"
  echo "__QWAIT__"
  Q "SELECT state, coalesce(name, method_name), to_char(coalesce(eta, date_created) AT TIME ZONE 'UTC', 'DD.MM HH24:MI')
     FROM queue_job WHERE state IN ('pending', 'enqueued', 'wait_dependencies') ORDER BY date_created LIMIT 10"
  echo "__QDONE__"
  Q "SELECT to_char(date_done AT TIME ZONE 'UTC', 'DD.MM HH24:MI'), coalesce(name, method_name), round(coalesce(exec_time, 0)::numeric, 1)
     FROM queue_job WHERE state = 'done' ORDER BY date_done DESC NULLS LAST LIMIT 5"
  echo "__QFAILED__"
  Q "SELECT to_char(coalesce(date_started, date_created) AT TIME ZONE 'UTC', 'DD.MM HH24:MI'), coalesce(name, method_name),
            left(coalesce(exc_name, '') || ': ' || replace(coalesce(exc_message, ''), E'\\n', ' '), 140)
     FROM queue_job WHERE state = 'failed' ORDER BY date_created DESC LIMIT 5"
fi
"""


def _sections(out):
    sec, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"^__([A-Z0-9]+)__(?:=(.*))?$", line)
        if m:
            cur = m.group(1)
            sec.setdefault(cur, [])
            if m.group(2) is not None:
                sec[cur].append(m.group(2))
            continue
        if cur and line.strip():
            sec[cur].append(line.split("\t"))
    return sec


def _tr_name(name):
    """Odoo 16+: переводимые имена хранятся как jsonb {"en_US": …, "ru_RU": …} — берём язык чата."""
    if not name.startswith("{"):
        return name
    try:
        d = json.loads(name)
    except ValueError:
        return name
    for lang in (L("ru_RU", "uk_UA"), "en_US"):
        if d.get(lang):
            return d[lang]
    return next(iter(d.values()), name)


def _short(text, n=60):
    return text if len(text) <= n else text[: n - 1] + "…"


def do_jobs(target):
    sec = _sections(ssh(target, REMOTE_JOBS, timeout=60, host_field=db_host(target)))
    if "ERR" in sec:
        raise RuntimeError(sec["ERR"][0])
    db = sec.get("DB", ["?"])[0]
    crons = [[_tr_name(r[0])] + r[1:] for r in sec.get("CRON", []) if len(r) >= 6]
    running_cron = [r for r in crons if r[4] == "t"]
    overdue = [r for r in crons if r[5] == "t" and r[4] != "t"]
    qrun = [r for r in sec.get("QRUN", []) if len(r) >= 4]

    now_lines = [f"⏰ cron: {_short(r[0])} (" + L("запущен", "запущено") + f" ~{r[2]})" for r in running_cron]
    # job в состоянии started дольше часа — почти наверняка зависла (воркер умер, а state не сбросился)
    now_lines += [("⚠️" if float(r[2] or 0) > 60 else "🧩") + f" job: {_short(r[0])} — " + L("с", "з") + f" {r[1]}, {r[2]} "
                  + L("мин", "хв") + (f" [{r[3]}]" if r[3] else "") for r in qrun]
    if any(float(r[2] or 0) > 60 for r in qrun):
        now_lines.append(L("⚠️ — job «выполняется» больше часа: скорее всего зависла (воркер перезапускался), "
                           "Jobs Garbage Collector её не вернёт — нужен Requeue/Cancel вручную",
                           "⚠️ — job «виконується» більше години: найімовірніше зависла (воркер перезапускався), "
                           "Jobs Garbage Collector її не поверне — потрібен Requeue/Cancel вручну"))

    parts = [("b", f"⚙️ {label(target)}: " + L("задачи", "задачі") + f" (база {db})")]
    parts.append(("text", L("▶️ Сейчас выполняется:\n", "▶️ Зараз виконується:\n")
                  + ("\n".join(now_lines) if now_lines else L("ничего", "нічого"))))

    if "QSTATS" in sec:
        stats = {r[0]: r[1] for r in sec["QSTATS"] if len(r) >= 2}
        fail24 = sec.get("QFAIL24", [["0"]])[0][0]
        order = ["started", "enqueued", "pending", "wait_dependencies", "failed", "done", "cancelled"]
        st = ", ".join(f"{k}: {stats[k]}" for k in order + sorted(set(stats) - set(order)) if k in stats)
        txt = f"🧩 queue_job — {st or L('пусто', 'порожньо')}\nfailed " + L("за 24 ч", "за 24 год") + f": {fail24}"
        wait = [r for r in sec.get("QWAIT", []) if len(r) >= 3]
        if wait:
            txt += L("\n\nВ очереди (первые 10):\n", "\n\nУ черзі (перші 10):\n") + "\n".join(f"{r[2]}  {r[0]}  {_short(r[1])}" for r in wait)
        parts.append(("text", txt))
        done = [r for r in sec.get("QDONE", []) if len(r) >= 3]
        if done:
            parts.append(("pre", L("Последние выполненные job:\n", "Останні виконані job:\n") + "\n".join(f"{r[0]}  {r[2]:>6}с  {_short(r[1], 70)}" for r in done)))
        failed = [r for r in sec.get("QFAILED", []) if len(r) >= 3]
        if failed:
            parts.append(("pre", L("Последние упавшие job:\n", "Останні job з помилкою:\n") + "\n".join(f"{r[0]}  {_short(r[1], 60)}\n      {r[2]}" for r in failed)))
    else:
        parts.append(("text", L("🧩 queue_job не установлен", "🧩 queue_job не встановлено")))

    head = f"⏰ Cron: " + L("активных", "активних") + f" {len(crons)}" \
        + (f", ⚠️ " + L("просрочено", "прострочено") + f": {len(overdue)}" if overdue else "")
    rows = [L("посл.запуск  след.запуск  интервал     задача", "ост.запуск   наст.запуск  інтервал     задача")]
    for name, interval, last, nxt, run, late in crons:
        mark = "▶" if run == "t" else ("⚠" if late == "t" else " ")
        rows.append(f"{mark}{last:<11}  {nxt:<11}  {interval:<11}  {_short(name, 55)}")
    parts.append(("b", head))
    parts.append(("pre", "\n".join(rows)))
    parts.append(("text", L("▶ — выполняется сейчас, ⚠ — просрочен больше чем на 15 мин (крон не успевает или завис)",
                            "▶ — виконується зараз, ⚠ — прострочено більш ніж на 15 хв (крон не встигає або завис)")))
    return parts


# /users: активность пользователей за 24 ч. Источники:
#  - bus_presence.last_presence — пользователь был в веб-клиенте (вкладка открыта и активна);
#  - bus_presence.last_poll за 2 мин — онлайн сейчас;
#  - create/write в основных документах — кто реально вносил изменения;
#  - res_users_log — входы по логину (сессии долгие, поэтому их мало).

REMOTE_USERS = REMOTE_DB_PRELUDE + f"""
NOW="(now() AT TIME ZONE 'UTC')"
echo "__TOTAL__"
Q "SELECT count(*) FILTER (WHERE NOT share), count(*) FILTER (WHERE share) FROM res_users WHERE active"
echo "__LOGINS__"
Q "SELECT count(*), count(DISTINCT create_uid) FROM res_users_log WHERE create_date > $NOW - interval '24 hours'"
echo "__PRESENCE__"
Q "SELECT coalesce(p.name, u.login), to_char(b.last_presence AT TIME ZONE 'UTC', 'DD.MM HH24:MI'),
          (b.last_poll > $NOW - interval '2 minutes')
   FROM bus_presence b JOIN res_users u ON u.id = b.user_id LEFT JOIN res_partner p ON p.id = u.partner_id
   WHERE b.last_presence > $NOW - interval '24 hours'
   ORDER BY b.last_presence DESC"
echo "__CHANGES__"
SQL=""
for T in {' '.join(USERS_TABLES)}; do
  [ "$(Q "SELECT to_regclass('$T') IS NOT NULL")" = t ] || continue
  SQL="$SQL${{SQL:+ UNION ALL }}SELECT write_uid AS uid, '$T' AS t FROM $T WHERE write_date > $NOW - interval '24 hours'"
done
[ -n "$SQL" ] && Q "SELECT coalesce(p.name, u.login), count(*), string_agg(DISTINCT x.t, ',')
   FROM ($SQL) x JOIN res_users u ON u.id = x.uid LEFT JOIN res_partner p ON p.id = u.partner_id
   WHERE u.id <> 1 GROUP BY 1 ORDER BY 2 DESC"
echo "__ACTIONS__"
DAY="(date_trunc('day', now() AT TIME ZONE '{SERVER_TZ}') AT TIME ZONE '{SERVER_TZ}' AT TIME ZONE 'UTC')"
# mail_message без индекса по дате: первый id за сегодня — двоичным поиском по первичному ключу (~20 шагов по индексу)
MINID=$(Q "WITH RECURSIVE b(lo, hi) AS (SELECT (SELECT min(id) FROM mail_message), (SELECT max(id) + 1 FROM mail_message)
  UNION ALL SELECT CASE WHEN c THEN m + 1 ELSE lo END, CASE WHEN c THEN hi ELSE m END
  FROM (SELECT lo, hi, (lo + hi) / 2 AS m,
               coalesce((SELECT create_date FROM mail_message WHERE id >= (lo + hi) / 2 ORDER BY id LIMIT 1) < $DAY, false) AS c
        FROM b WHERE lo < hi) s)
  SELECT min(hi) FROM b")
SQL="SELECT create_uid AS uid, 'chatter' AS k FROM mail_message WHERE id >= ${{MINID:-0}} AND create_date >= $DAY"
{''.join(f"""[ "$(Q "SELECT to_regclass('{m['table']}') IS NOT NULL")" = t ] && SQL="$SQL UNION ALL SELECT {m['user_col']}, 'm{i}' FROM {m['table']} WHERE {m['date_col']} >= $DAY"
""" for i, m in enumerate(USER_METRICS))}Q "SELECT coalesce(p.name, u.login), x.k, count(*)
   FROM ($SQL) x JOIN res_users u ON u.id = x.uid LEFT JOIN res_partner p ON p.id = u.partner_id
   WHERE u.id <> 1 AND NOT u.share GROUP BY 1, 2"
"""


USERS_TOP = 40  # строк в «Действия сегодня» без параметра all


def do_users(target, extra=None):
    sec = _sections(ssh(target, REMOTE_USERS, timeout=90, host_field=db_host(target, ro=True)))
    if "ERR" in sec:
        raise RuntimeError(sec["ERR"][0])
    tot = (sec.get("TOTAL") or [["?", "?"]])[0]
    logins = (sec.get("LOGINS") or [["0", "0"]])[0]
    pres = [r for r in sec.get("PRESENCE", []) if len(r) >= 3]
    online = [r for r in pres if r[2] == "t"]
    changes = [r for r in sec.get("CHANGES", []) if len(r) >= 3]

    parts = [("b", f"👥 {label(target)}: " + L("пользователи за 24 ч", "користувачі за 24 год")
              + f" (база {sec.get('DB', ['?'])[0]})")]
    parts.append(("text", L(
        f"Были в Odoo (открытый веб-клиент): {len(pres)}\n"
        f"Онлайн сейчас: {len(online)}\n"
        f"Вносили изменения в документы: {len(changes)}\n"
        f"Входов по логину: {logins[0]} (польз.: {logins[1]}) — сессии долгие, поэтому входов мало\n"
        f"Всего активных внутренних пользователей: {tot[0]}, порталов: {tot[1]}",
        f"Були в Odoo (відкритий веб-клієнт): {len(pres)}\n"
        f"Онлайн зараз: {len(online)}\n"
        f"Вносили зміни в документи: {len(changes)}\n"
        f"Входів за логіном: {logins[0]} (корист.: {logins[1]}) — сесії довгі, тому входів мало\n"
        f"Усього активних внутрішніх користувачів: {tot[0]}, порталів: {tot[1]}")))
    if pres:
        parts.append(("pre", L(f"Последнее действие ({SERVER_TZ}):\n", f"Остання дія ({SERVER_TZ}):\n") + "\n".join(
            f"{'🟢' if r[2] == 't' else '⚪'} {r[1]}  {_short(r[0], 40)}" for r in pres)))
    acts = {}
    for r in sec.get("ACTIONS", []):
        if len(r) >= 3 and r[2].isdigit():
            acts.setdefault(r[0], {})[r[1]] = int(r[2])
    if acts:
        cols = [f"m{i}" for i in range(len(USER_METRICS))] + ["chatter"]
        shorts = [m.get("short") or m["table"][:6] for m in USER_METRICS] + [L("чаттер", "чатер")]
        rows = sorted(acts.items(), key=lambda kv: [-kv[1].get(c, 0) for c in cols])
        tot = [sum(v.get(c, 0) for v in acts.values()) for c in cols]
        head = "".join(f"{h[:7]:>8}" for h in shorts)
        top = len(rows) if extra == "all" else USERS_TOP
        body = "\n".join("".join(f"{v.get(c, 0):>8}" for c in cols) + "  " + _short(name, 30) for name, v in rows[:top])
        more = len(rows) - top
        parts.append(("pre", L(f"Действия сегодня (с 00:00, {SERVER_TZ}), пользователей: {len(rows)}\n",
                               f"Дії сьогодні (з 00:00, {SERVER_TZ}), користувачів: {len(rows)}\n")
                      + head + "\n" + body
                      + (L(f"\n… и ещё {more} (весь список: /users {target} all)",
                             f"\n… і ще {more} (увесь список: /users {target} all)") if more > 0 else "")
                      + "\n" + "".join(f"{t:>8}" for t in tot) + L("  всего", "  усього")))
        legend = [f"{s_} — {L(m.get('title_ru', m['table']), m.get('title_uk', m['table']))}"
                  for s_, m in zip(shorts, USER_METRICS)]
        legend.append(L("чаттер — записи в истории документов (mail.message): заметки, сообщения, изменения отслеживаемых полей, "
                        "выполненные активности",
                        "чатер — записи в історії документів (mail.message): нотатки, повідомлення, зміни полів, що відстежуються, "
                        "виконані активності"))
        parts.append(("text", "\n".join(legend)))
    if changes:
        parts.append(("pre", L("Изменения в документах (записей, где):\n", "Зміни в документах (записів, де):\n") + "\n".join(
            f"{r[1]:>6}  {_short(r[0], 30)}  {_short(r[2].replace('_', '.'), 60)}" for r in changes)))
    parts.append(("text", L(
        "🟢 — вкладка Odoo открыта сейчас; время — последнее действие пользователя. Изменения — по write_date "
        "в основных документах (заказы, склад, производство, счета, задачи, контрагенты, товары); "
        "служебный пользователь (uid 1) не считается, но действия cron от имени администратора попадают в его строку.",
        "🟢 — вкладка Odoo відкрита зараз; час — остання дія користувача. Зміни — за write_date "
        "в основних документах (замовлення, склад, виробництво, рахунки, задачі, контрагенти, товари); "
        "службовий користувач (uid 1) не рахується, але дії cron від імені адміністратора потрапляють у його рядок.")))
    return parts


ANALYZE_PROMPT = """Ты — опытный администратор Odoo и Linux. Ниже — снимок состояния сервера Odoo ({target}, {host}) на {now}.
Проанализируй и дай короткий ответ для Telegram. Отвечай строго {lang_name}, без markdown-таблиц и без заголовков с #:
1. Общий вердикт одной строкой (всё в порядке / есть риски / есть проблема).
2. Сравнение с прошлым анализом (если он приложен): что исправилось, что осталось, что появилось нового;
   выполнены ли рекомендованные тогда шаги — судя по данным. Учитывай, что лог мог быть ротирован,
   поэтому отсутствие старых ошибок в логе само по себе не доказывает исправление — смотри на время записей.
3. Что не так или настораживает сейчас — по пунктам, с конкретикой из данных (строки лога, числа).
4. Вероятные причины ошибок, если они есть.
5. Что сделать — конкретные шаги, от самого важного.
Не выдумывай того, чего нет в данных. Если данных мало для вывода — так и скажи. До 3000 символов.

=== ПРОШЛЫЙ АНАЛИЗ ===
{previous}

=== HEALTH СЕЙЧАС (key=value) ===
{health}
HTTP {url}: code={code}, time={time:.2f}s, error={err}

=== ЗАДАЧИ СЕЙЧАС (cron и queue_job, время {tz}; в логе время UTC) ===
{jobs}

=== ЛОГИ СЕЙЧАС ===
{logs}
"""

HISTORY_DIR = os.path.join(BASE_DIR, "history")
_analyze_locks = {t: threading.Lock() for t in TARGETS}


def last_analysis_path(target):
    return os.path.join(HISTORY_DIR, f"analyze_{target}.md")


def load_previous(target):
    path = last_analysis_path(target)
    if not os.path.exists(path):
        return "(нет — это первый анализ этого сервера)"
    with open(path, encoding="utf-8") as f:
        return f.read()[-20000:]


def save_analysis(target, now, head, health_raw, http_line, answer):
    """Последний анализ — history/analyze_<target>.md (уходит в следующий /analyze),
    копия — в архив history/<target>/<дата>.md."""
    md = (f"# Анализ {label(target)} — {now}\n\n"
          f"**Итог проверки:** {head}\n\n"
          f"## Ответ Claude\n\n{answer}\n\n"
          f"## Снимок health (key=value)\n\n```\n{health_raw}\n{http_line}\n```\n")
    arch = os.path.join(HISTORY_DIR, target)
    os.makedirs(arch, exist_ok=True)
    with open(os.path.join(arch, now.replace(" ", "_").replace(":", "") + ".md"), "w", encoding="utf-8") as f:
        f.write(md)
    tmp = last_analysis_path(target) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md)
    os.replace(tmp, last_analysis_path(target))


def do_analyze(target):
    with _analyze_locks[target]:  # два /analyze одного сервера не должны перезаписать историю друг другу
        d, raw = health_data(target)
        logs = cut_lines(ssh(target, REMOTE_ANALYZE_LOGS, timeout=90), 800)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        previous = load_previous(target)
        try:
            jobs = "\n\n".join(t for _, t in do_jobs(target))
        except Exception as e:  # noqa: BLE001
            jobs = f"(не удалось получить: {e})"
        http_line = f"HTTP {TARGETS[target]['url']}: code={d['http_code']}, time={d['http_time']:.2f}s, error={d['http_err']}"
        prompt = ANALYZE_PROMPT.format(
            lang_name=L("на русском языке", "українською мовою"),
            target=target, host=TARGETS[target]["ssh"].split("@")[-1], now=now, previous=previous,
            health=raw.strip(), url=TARGETS[target]["url"], code=d["http_code"], time=d["http_time"],
            err=d["http_err"], jobs=jobs[-15000:], logs=logs[-60000:], tz=SERVER_TZ)
        cmd = [CLAUDE_BIN, "-p", "--tools", ""]  # без инструментов: только читает присланные данные
        if CLAUDE_MODEL:
            cmd += ["--model", CLAUDE_MODEL]
        try:
            p = subprocess.run(cmd, input=mask_secrets(prompt), capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise RuntimeError(L("Claude не ответил за 300 с", "Claude не відповів за 300 с"))
        if p.returncode != 0 or not p.stdout.strip():
            raise RuntimeError(L("Claude: ошибка", "Claude: помилка") + f" {p.returncode}: {(p.stderr or p.stdout).strip()[:500]}")
        answer = p.stdout.strip()
        head, _ = fmt_health(target, d)
        save_analysis(target, now, head, raw.strip(), http_line, answer)
        prev_note = "" if previous.startswith("(нет") else L(" (сравнение с прошлым анализом)", " (порівняння з минулим аналізом)")
        return [("b", L("🤖 Анализ ", "🤖 Аналіз ") + f"{label(target)}{prev_note} — {head}"), ("text", answer)]


ACTIONS = {
    "health": (do_health, {"ru": "диск и статус Odoo", "uk": "диск і статус Odoo"}),
    "log": (do_log, {"ru": "последние строки лога (/log prod 50, /log prod all)",
                     "uk": "останні рядки лога (/log prod 50, /log prod all)"}),
    "errors": (do_errors, {"ru": "10 последних FATAL/CRITICAL/ERROR", "uk": "10 останніх FATAL/CRITICAL/ERROR"}),
    "jobs": (do_jobs, {"ru": "активные cron и queue_job", "uk": "активні cron і queue_job"}),
    "users": (do_users, {"ru": "пользователи за 24 ч и действия за сегодня (/users prod all)",
                         "uk": "користувачі за 24 год і дії за сьогодні (/users prod all)"}),
    "analyze": (do_analyze, {"ru": "анализ состояния через Claude", "uk": "аналіз стану через Claude"}),
}
HELP_TITLE = {"ru": "список команд", "uk": "список команд"}
BACKUP_TITLE = ({"ru": "запустить бэкап сейчас", "uk": "запустити бекап зараз"} if BACKUP_MODE == "systemd" else
                {"ru": f"копия базы {P2S} (новая база по дате)", "uk": f"копія бази {P2S} (нова база за датою)"})


def title(action, lang=None):
    t = ACTIONS[action][1]
    return t[lang] if lang else L(t["ru"], t["uk"])


# ---------------------------------------------------------------- Telegram

API = f"https://api.telegram.org/bot{TOKEN}/"


def tg(method, retries=3, **params):
    """Вызов Bot API. Сетевые сбои (обрыв соединения) повторяются; ошибки API (HTTPError) — нет."""
    data = json.dumps(params).encode()
    for attempt in range(retries):
        req = urllib.request.Request(API + method, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


SECRET_RES = [
    (re.compile(r"(/bot|bot)\d{6,12}:[A-Za-z0-9_-]{2,}"), r"\1***"),          # токены Telegram-ботов в URL ошибок
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+@"), r"\1***@"),                    # пароль в URL user:pass@host
    (re.compile(r"(?i)\b(password|passwd|api[_-]?key|token|secret)(\s*[=:]\s*)[^\s,;'\"]+"), r"\1\2***"),
]


def mask_secrets(text):
    """Маскирует похожие на секреты фрагменты в тексте, уходящем в чат (ошибки job, строки лога)."""
    for rx, rep in SECRET_RES:
        text = rx.sub(rep, text)
    return text


def render(parts):
    """Склеивает части в HTML-сообщения не длиннее лимита Telegram (4096)."""
    out = []
    for kind, text in parts:
        if kind == "file":
            continue
        text = mask_secrets(text)
        if kind == "b":
            out.append(f"<b>{html.escape(text)}</b>")
        elif kind == "text":
            out.append(html.escape(text))
        else:  # pre — режем по строкам, чтобы влезть в лимит
            chunk = ""
            for line in text.splitlines():
                if chunk and len(html.escape(chunk + line)) > 3500:
                    out.append(f"<pre>{html.escape(chunk)}</pre>")
                    chunk = ""
                chunk += line + "\n"
            if chunk:
                out.append(f"<pre>{html.escape(chunk)}</pre>")
    msgs, cur = [], ""
    for piece in out:
        if cur and len(cur) + len(piece) + 2 > 4000:
            msgs.append(cur)
            cur = ""
        while len(piece) > 4000:  # длинный обычный текст (ответ Claude)
            cut = piece.rfind("\n", 0, 4000)
            cut = cut if cut > 1000 else 4000
            msgs.append(piece[:cut])
            piece = piece[cut:].lstrip("\n")
        cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        msgs.append(cur)
    return msgs


def send(chat_id, text_html, **kw):
    try:
        return tg("sendMessage", chat_id=chat_id, text=text_html, parse_mode="HTML",
                  disable_web_page_preview=True, **kw)
    except urllib.error.HTTPError as e:
        print("sendMessage failed:", e.read()[:300], file=sys.stderr, flush=True)
        # запасной вариант — без разметки
        return tg("sendMessage", chat_id=chat_id, text=re.sub(r"<[^>]+>", "", html.unescape(text_html))[:4096])


def send_document(chat_id, path, filename):
    import uuid
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    for attempt in range(3):
        req = urllib.request.Request(API + "sendDocument", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def run_action(chat_id, action, targets, extra=None):
    set_lang(chat_lang(chat_id))
    try:
        _run_action(chat_id, action, targets, extra)
    except Exception as e:  # noqa: BLE001
        print(f"run_action {action} {targets} failed:", repr(e), file=sys.stderr, flush=True)


def _run_action(chat_id, action, targets, extra=None):
    fn = ACTIONS[action][0]
    for t in targets:
        status = send(chat_id, f"⏳ {html.escape(label(t))}: {html.escape(title(action).split(' (')[0])}…")
        files = []
        try:
            parts = fn(t, extra) if extra is not None else fn(t)
            files = [v for k, v in parts if k == "file"]
            msgs = render(parts)
        except Exception as e:  # noqa: BLE001
            msgs = [f"❌ <b>{label(t)}</b>: {html.escape(str(e))}"]
        try:
            tg("deleteMessage", chat_id=chat_id, message_id=status["result"]["message_id"])
        except Exception:  # noqa: BLE001
            pass
        for m in msgs:
            send(chat_id, m)
        for path, name in files:
            try:
                send_document(chat_id, path, name)
            except Exception as e:  # noqa: BLE001
                send(chat_id, "❌ " + L("не удалось отправить файл", "не вдалося надіслати файл") + f": {html.escape(str(e))}")
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass


def target_keyboard(action, extra=None):
    suf = f":{extra}" if extra is not None else ""
    row = [{"text": k, "callback_data": f"{action}:{k}{suf}"} for k in TARGETS]
    if len(TARGETS) > 1:
        row.append({"text": L("все", "всі") if len(TARGETS) > 2 else L("оба", "обидва"), "callback_data": f"{action}:both{suf}"})
    return {"inline_keyboard": [row]}


def parse_targets(arg):
    arg = (arg or "").lower()
    if arg in ("both", "оба", "обидва"):
        return list(TARGETS)
    return [arg] if arg in TARGETS else None


def parse_args(cmd, args):
    """Возвращает (targets|None, extra, error). extra — только для /log: число строк или 'all'."""
    targets, extra, rest = None, None, []
    for a in args:
        t = parse_targets(a)
        if t and targets is None:
            targets = t
        else:
            rest.append(a)
    if targets is None and len(TARGETS) == 1:
        targets = list(TARGETS)
    if cmd == "users" and rest:
        if rest[0].lower() not in ("all", "все", "всі"):
            return None, None, L(f"Не понял «{rest[0]}». Пример: /users prod all",
                                 f"Не зрозумів «{rest[0]}». Приклад: /users prod all")
        extra = "all"
    if cmd == "log":
        extra = parse_count(rest[0] if rest else None)
        if extra is None:
            return None, None, L(f"Не понял «{rest[0]}». Пример: /log prod 50 или /log prod all",
                                 f"Не зрозумів «{rest[0]}». Приклад: /log prod 50 або /log prod all")
    return targets, extra, None


# ---------------------------------------------------------------- /backup: копия source → dest (prod2stage)

PROD2STAGE = os.path.join(BASE_DIR, "ops", "prod2stage.sh")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
_backup_lock = threading.Lock()
_backup_plans = {}  # token -> {chat, new_db, modules, drop_candidates, ts}
MODULES_RE = re.compile(r"^(-|all|[a-z0-9_]+(,[a-z0-9_]+)*)$")


def _script_env():
    env = dict(os.environ)
    env["SSH_KEY"] = SSH_KEY
    if BACKUP_MODE == "prod2stage":
        env.update(p2s_env())
    return env


def backup_plan(chat_id, modules_arg):
    if BACKUP_MODE == "systemd":
        return sysd_backup_plan(chat_id)
    modules = (modules_arg or STAGE_UPDATE_MODULES or "-").strip()
    if not MODULES_RE.match(modules):
        send(chat_id, html.escape(L("Некорректный список модулей. Пример: /backup module_a,module_b или /backup all или /backup -",
                                    "Некоректний список модулів. Приклад: /backup module_a,module_b або /backup all або /backup -")))
        return
    if _backup_lock.locked():
        send(chat_id, L(f"⏳ Копирование {P2S} уже идёт.", f"⏳ Копіювання {P2S} вже йде."))
        return
    status = send(chat_id, L(f"⏳ Готовлю план копирования {P2S}…", f"⏳ Готую план копіювання {P2S}…"))
    try:
        p = subprocess.run([PROD2STAGE, "plan", "--modules", modules], capture_output=True, text=True,
                           timeout=120, env=_script_env())
    except subprocess.TimeoutExpired:
        p = None
    try:
        tg("deleteMessage", chat_id=chat_id, message_id=status["result"]["message_id"])
    except Exception:  # noqa: BLE001
        pass
    out = p.stdout if p else ""
    plan = dict(m.groups() for m in re.finditer(r"^##PLAN (\w+)=(.*)$", out, re.M))
    fails = re.findall(r"^##FAIL (.*)$", out, re.M)
    if not plan or fails:
        send(chat_id, "❌ " + html.escape(L("Не удалось составить план: ", "Не вдалося скласти план: ")
                                         + ("; ".join(fails) or (p.stderr[-500:] if p else "timeout"))))
        return
    token = os.urandom(4).hex()
    _backup_plans[token] = {"chat": chat_id, "new_db": plan["new_db"], "modules": modules,
                            "drop": plan.get("drop_candidates", ""), "ts": time.time()}
    space_ok = plan.get("space_ok") == "yes"
    mods = L("не обновлять", "не оновлювати") if modules == "-" else modules
    text = (f"<b>{L(f'Копия {P2S}', f'Копія {P2S}')}</b>\n\n"
            + html.escape(L(
                f"Новая база: {plan['new_db']} (сейчас {P2S_DST} на {plan['old_db']})\n"
                f"{P2S_SRC}: база {plan['prod_db']}, filestore {plan['prod_filestore']}\n"
                f"{P2S_DST}: свободно {plan['stage_free']}, нужно ~{plan['need']}\n"
                f"Обновить модули: {mods}\n"
                f"Базы на {P2S_DST}: {plan['stage_dbs']}\n\n"
                f"Шаги: дамп {P2S_SRC} → восстановление в новую базу → обезвреживание ({NEUTRALIZE['ru']}) → "
                f"filestore → остановка Odoo {P2S_DST}, переключение конфига, обновление модулей, повторное обезвреживание → "
                "запуск и проверка → уборка.\n"
                f"{P2S_DST} будет недоступен на время обновления модулей (несколько минут). {P2S_SRC} не останавливается, "
                f"pg_dump немного нагружает его. При ошибке {P2S_DST} вернётся на старую базу.",
                f"Нова база: {plan['new_db']} (зараз {P2S_DST} на {plan['old_db']})\n"
                f"{P2S_SRC}: база {plan['prod_db']}, filestore {plan['prod_filestore']}\n"
                f"{P2S_DST}: вільно {plan['stage_free']}, потрібно ~{plan['need']}\n"
                f"Оновити модулі: {mods}\n"
                f"Бази на {P2S_DST}: {plan['stage_dbs']}\n\n"
                f"Кроки: дамп {P2S_SRC} → відновлення в нову базу → знешкодження ({NEUTRALIZE['uk']}) → "
                f"filestore → зупинка Odoo {P2S_DST}, перемикання конфігу, оновлення модулів, повторне знешкодження → "
                "запуск і перевірка → прибирання.\n"
                f"{P2S_DST} буде недоступний під час оновлення модулів (кілька хвилин). {P2S_SRC} не зупиняється, "
                f"pg_dump трохи навантажує його. У разі помилки {P2S_DST} повернеться на стару базу.")))
    if not space_ok:
        text += "\n\n❌ " + html.escape(L(f"Не хватает места на {P2S_DST} — сначала освободите место.",
                                         f"Не вистачає місця на {P2S_DST} — спершу звільніть місце."))
        send(chat_id, text)
        return
    rows = [[{"text": L("▶️ Запустить", "▶️ Запустити"), "callback_data": f"backup:run:{token}"}]]
    if plan.get("drop_candidates"):
        rows.append([{"text": L("▶️ Запустить и удалить ", "▶️ Запустити і видалити ") + plan["drop_candidates"].replace(",", ", "),
                      "callback_data": f"backup:rundrop:{token}"}])
    rows.append([{"text": L("Отмена", "Скасувати"), "callback_data": f"backup:cancel:{token}"}])
    send(chat_id, text, reply_markup={"inline_keyboard": rows})


def backup_callback(chat_id, message_id, kind, token):
    plan = _backup_plans.pop(token, None)
    try:  # убрать кнопки с плана
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
    except Exception:  # noqa: BLE001
        pass
    if kind == "cancel":
        send(chat_id, L("Отменено.", "Скасовано."))
        return
    if BACKUP_MODE == "systemd":
        if not plan or plan["chat"] != chat_id or time.time() - plan["ts"] > 900:
            send(chat_id, L("План устарел — запустите /backup заново.", "План застарів — запустіть /backup знову."))
            return
        if not _backup_lock.acquire(blocking=False):
            send(chat_id, L("⏳ Бэкап уже идёт.", "⏳ Бекап вже йде."))
            return
        try:
            _sysd_backup_run(chat_id)
        finally:
            _backup_lock.release()
        return
    if not plan or plan["chat"] != chat_id or time.time() - plan["ts"] > 900:
        send(chat_id, L("План устарел — запустите /backup заново.", "План застарів — запустіть /backup знову."))
        return
    if not _backup_lock.acquire(blocking=False):
        send(chat_id, L(f"⏳ Копирование {P2S} уже идёт.", f"⏳ Копіювання {P2S} вже йде."))
        return
    try:
        _backup_run(chat_id, plan, plan["drop"] if kind == "rundrop" else "")
    finally:
        _backup_lock.release()


def _backup_run(chat_id, plan, drop):
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, f"prod2stage-{plan['new_db']}-{datetime.now():%Y%m%d-%H%M%S}.log")
    cmd = [PROD2STAGE, "run", "--db", plan["new_db"], "--modules", plan["modules"]] + (["--drop", drop] if drop else [])
    head = "<b>" + L(f"Копия {P2S}", f"Копія {P2S}") + f": {html.escape(plan['new_db'])}</b>\n"
    steps, infos = [], []
    msg = send(chat_id, head + "⏳ " + L("старт…", "старт…"))
    mid = msg["result"]["message_id"]
    t0 = time.time()

    def progress(final=None):
        lines = [f"{'✅' if i < len(steps) - 1 or final == 'ok' else ('❌' if final == 'fail' else '⏳')} {html.escape(st)}"
                 for i, st in enumerate(steps)]
        body = head + "\n".join(lines)
        if infos:
            body += "\n\n" + "\n".join("ℹ️ " + html.escape(x) for x in infos[-8:])
        body += f"\n\n⏱ {int(time.time() - t0) // 60} " + L("мин", "хв")
        try:
            tg("editMessageText", chat_id=chat_id, message_id=mid, text=body[:4000], parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass

    result, last_fail = None, ""
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                env=_script_env())
        for line in proc.stdout:
            log.write(line)
            log.flush()
            line = line.rstrip("\n")
            if line.startswith("##STEP "):
                steps.append(line[7:])
                progress()
            elif line.startswith("##INFO "):
                infos.append(line[7:])
                progress()
            elif line.startswith("##OK "):
                result = "ok"
                infos.append(line[5:])
            elif line.startswith("##FAIL "):
                result, last_fail = "fail", line[7:]
        rc = proc.wait()
    if result != "ok" or rc != 0:
        result = "fail"
    progress(final=result)
    mins = int(time.time() - t0) // 60
    if result == "ok":
        send(chat_id, "✅ " + html.escape(L(f"{P2S_DST} переключён на {plan['new_db']} за {mins} мин. Обезврежено: {NEUTRALIZE['ru']}.",
                                           f"{P2S_DST} перемкнено на {plan['new_db']} за {mins} хв. Знешкоджено: {NEUTRALIZE['uk']}.")))
        run_action(chat_id, "health", [P2S_DST])
    else:
        with open(log_path, encoding="utf-8") as f:
            tail = "".join(f.readlines()[-25:])
        send(chat_id, "❌ " + html.escape(L(f"Копия {P2S} не удалась: ", f"Копія {P2S} не вдалася: ") + (last_fail or f"rc={rc}"))
             + f"\n<pre>{html.escape(cut_lines(tail, 300)[-3000:])}</pre>\n" + html.escape(L("Полный лог: ", "Повний лог: ") + log_path))


# ---------------------------------------------------------------- /backup в режиме systemd: запуск юнита бэкапа

REMOTE_BACKUP_STATUS = """
U=__UNIT__; TM=__TIMER__
systemctl show "$U" -p ActiveState -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp -p ExecStart --no-pager \
  | sed 's/^ExecStart=.*path=\\([^ ;]*\\).*/ExecStart=\\1/'
echo "TimerNext=$(systemctl show "$TM" -p NextElapseUSecRealtime --value)"
echo "TimerActive=$(systemctl is-active "$TM")"
echo "LastReal=$(sudo -n journalctl -u "$U" -n 200 --no-pager -o cat 2>/dev/null | grep -E '^real' | tail -1 | awk '{print $2}')"
echo "DiskFree=$(df -h --output=avail / | tail -1 | tr -d ' ')"
"""


def _sysd_script(tpl):
    return tpl.replace("__UNIT__", BACKUP_UNIT).replace("__TIMER__", BACKUP_TIMER)


def _kv(out):
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def sysd_backup_plan(chat_id):
    if _backup_lock.locked():
        send(chat_id, L("⏳ Бэкап уже идёт.", "⏳ Бекап вже йде."))
        return
    try:
        st = _kv(ssh(BACKUP_TARGET, _sysd_script(REMOTE_BACKUP_STATUS), timeout=60))
    except Exception as e:  # noqa: BLE001
        send(chat_id, "❌ " + html.escape(str(e)))
        return
    if st.get("ActiveState") in ("active", "activating"):
        send(chat_id, L(f"⏳ {BACKUP_UNIT} уже выполняется (с {st.get('ExecMainStartTimestamp')}).",
                        f"⏳ {BACKUP_UNIT} вже виконується (з {st.get('ExecMainStartTimestamp')})."))
        return
    token = os.urandom(4).hex()
    _backup_plans[token] = {"chat": chat_id, "ts": time.time()}
    res = st.get("Result", "?")
    ok = "✅" if res == "success" and st.get("ExecMainStatus") == "0" else "❌"
    text = (f"<b>{L('Бэкап', 'Бекап')} {html.escape(label(BACKUP_TARGET))}: {html.escape(BACKUP_UNIT)}</b>\n\n" + html.escape(L(
        f"Скрипт: {st.get('ExecStart', '?')}\n"
        f"Последний запуск: {st.get('ExecMainStartTimestamp') or '—'} → {st.get('ExecMainExitTimestamp') or '—'}, "
        f"результат: {ok} {res} (код {st.get('ExecMainStatus', '?')}), дамп ~{st.get('LastReal') or '?'}\n"
        f"Следующий по расписанию ({BACKUP_TIMER}, {st.get('TimerActive')}): {st.get('TimerNext') or '—'}\n"
        f"Свободно на диске: {st.get('DiskFree', '?')}",
        f"Скрипт: {st.get('ExecStart', '?')}\n"
        f"Останній запуск: {st.get('ExecMainStartTimestamp') or '—'} → {st.get('ExecMainExitTimestamp') or '—'}, "
        f"результат: {ok} {res} (код {st.get('ExecMainStatus', '?')}), дамп ~{st.get('LastReal') or '?'}\n"
        f"Наступний за розкладом ({BACKUP_TIMER}, {st.get('TimerActive')}): {st.get('TimerNext') or '—'}\n"
        f"Вільно на диску: {st.get('DiskFree', '?')}")))
    note = L(BACKUP_NOTE_RU, BACKUP_NOTE_UK)
    if note:
        text += "\n\n⚠️ " + html.escape(note)
    rows = [[{"text": L("▶️ Запустить бэкап сейчас", "▶️ Запустити бекап зараз"), "callback_data": f"backup:run:{token}"}],
            [{"text": L("Отмена", "Скасувати"), "callback_data": f"backup:cancel:{token}"}]]
    send(chat_id, text, reply_markup={"inline_keyboard": rows})


def _sysd_backup_run(chat_id):
    head = f"<b>{L('Бэкап', 'Бекап')} {html.escape(label(BACKUP_TARGET))}: {html.escape(BACKUP_UNIT)}</b>\n"
    msg = send(chat_id, head + "⏳ " + L("запускаю…", "запускаю…"))
    mid = msg["result"]["message_id"]
    t0 = time.time()
    try:
        out = ssh(BACKUP_TARGET, f"date +%s; sudo -n systemctl start --no-block {BACKUP_UNIT} && echo started", timeout=60)
    except Exception as e:  # noqa: BLE001
        send(chat_id, "❌ " + html.escape(str(e)))
        return
    if "started" not in out:
        send(chat_id, "❌ " + html.escape(L("не удалось запустить: ", "не вдалося запустити: ") + out[-500:]))
        return
    since = out.split()[0]
    poll = (f"systemctl show {BACKUP_UNIT} -p ActiveState -p Result -p ExecMainStatus --no-pager; echo ---; "
            f"sudo -n journalctl -u {BACKUP_UNIT} --since @{since} --no-pager -o cat | tail -n 12")
    state, journal, seen_run = {}, "", False
    while time.time() - t0 < 3600:
        time.sleep(10)
        try:
            raw = ssh(BACKUP_TARGET, poll, timeout=60)
        except Exception:  # noqa: BLE001
            continue
        top, _, journal = raw.partition("---\n")
        state = _kv(top)
        act = state.get("ActiveState", "?")
        seen_run = seen_run or act in ("active", "activating", "deactivating") or bool(journal.strip())
        body = (head + f"⏳ {html.escape(act)} · ⏱ {int(time.time() - t0) // 60} " + L("мин", "хв")
                + f"\n<pre>{html.escape(cut_lines(journal.strip(), 200)[-3000:]) or '…'}</pre>")
        try:
            tg("editMessageText", chat_id=chat_id, message_id=mid, text=body[:4000], parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
        if seen_run and act in ("inactive", "failed"):
            break
    mins = int(time.time() - t0) // 60
    ok = state.get("ActiveState") == "inactive" and state.get("Result") == "success" and state.get("ExecMainStatus") == "0"
    final = (head + ("✅ " if ok else "❌ ") + html.escape(
        L(f"готово за {mins} мин, результат: {state.get('Result')}, код {state.get('ExecMainStatus')}",
          f"готово за {mins} хв, результат: {state.get('Result')}, код {state.get('ExecMainStatus')}"))
        + f"\n<pre>{html.escape(cut_lines(journal.strip(), 200)[-3000:])}</pre>")
    try:
        tg("editMessageText", chat_id=chat_id, message_id=mid, text=final[:4000], parse_mode="HTML")
    except Exception:  # noqa: BLE001
        pass
    send(chat_id, ("✅ " if ok else "❌ ") + html.escape(L(f"Бэкап {BACKUP_UNIT}: {'успешно' if ok else 'ошибка'} ({mins} мин)",
                                                        f"Бекап {BACKUP_UNIT}: {'успішно' if ok else 'помилка'} ({mins} хв)")))
    run_action(chat_id, "health", [BACKUP_TARGET])


# ---------------------------------------------------------------- /pull, /update, /restore: код аддонов и модули
# Включается для сервера, у которого в targets.json задан update_cmd (команда click-odoo-update на сервере).
# /pull    — git fetch + merge --ff-only во всех git-репозиториях из addons_path конфига Odoo (кроме git_exclude);
#            коммиты до/после пишутся в history/<target>-deploy.jsonl — к ним откатывает /restore.
# /update  — click-odoo-update: обновляет модули, у которых изменился хэш файлов (хэши хранятся в базе,
#            ir_config_parameter module_auto_update.installed_checksums). Odoo не останавливается; после
#            обновления бот предлагает перезапуск (новый Python-код подхватывается только после него).
#            Обновление идёт на сервере в фоне (nohup), лог — ~/.odoo-bot/update-<время>.log.
# /restore — выбор одного из последних pull → git reset --keep к коммитам «до» → план /update.
# Всё — только для ADMIN_CHAT_IDS (по умолчанию = BACKUP_CHAT_IDS), с подтверждением кнопкой.

ADMIN_CHAT_IDS = _private_ids("ADMIN_CHAT_IDS") or BACKUP_CHAT_IDS
DEPLOY_TARGETS = [k for k, v in TARGETS.items() if v.get("update_cmd")]
for _k in DEPLOY_TARGETS:
    if any("'" in str(TARGETS[_k][f]) for f in ("update_cmd", "git_exclude", "update_conf", "addons_map")):
        sys.exit(f"targets.json: {_k}: в update_cmd/git_exclude/update_conf/addons_map не должно быть одинарных кавычек")
DEPLOY_TITLE = {
    "pull": {"ru": "git pull аддонов (коммиты сохраняются для отката)",
             "uk": "git pull аддонів (коміти зберігаються для відкату)"},
    "update": {"ru": "обновить изменённые модули (click-odoo-update)",
               "uk": "оновити змінені модулі (click-odoo-update)"},
    "restore": {"ru": "откатить код к состоянию до pull",
                "uk": "відкотити код до стану до pull"},
}
DB_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
_deploy_locks = {t: threading.Lock() for t in TARGETS}
_dep = {}  # token -> {"chat", "target", "kind", "ts", ...}
DEP_TTL = 1800

REMOTE_GITDIRS = r'''
EXCL="__GIT_EXCLUDE__"
AMAP="__ADDONS_MAP__"
gitdirs() {
  { cat __CONF__ 2>/dev/null || sudo -n cat __CONF__; } \
    | awk '/^[[:space:]]*addons_path[[:space:]]*=/{f=1; sub(/^[^=]*=/,""); print; next} f && /^[[:space:]]/{print; next} {f=0}' \
    | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' \
    | while read -r p; do for m in $AMAP; do case "$p" in "${m%%=*}"|"${m%%=*}"/*) p="${m#*=}${p#"${m%%=*}"}";; esac; done; echo "$p"; done \
    | while read -r p; do git -C "$p" rev-parse --show-toplevel 2>/dev/null; done | sort -u \
    | while read -r t; do case " $EXCL " in *" $t "*) ;; *) echo "$t";; esac; done
}
'''
REMOTE_PULL_PLAN = REMOTE_GITDIRS + r'''
for t in $(gitdirs); do
  echo "##REPO $t"
  echo "branch=$(git -C "$t" rev-parse --abbrev-ref HEAD 2>&1)"
  echo "up=$(git -C "$t" rev-parse --abbrev-ref '@{u}' 2>/dev/null)"
  echo "head=$(git -C "$t" rev-parse HEAD)"
  echo "dirty=$(git -C "$t" status --porcelain --untracked-files=no --ignore-submodules=all | wc -l)"
  if o=$(timeout 120 git -C "$t" fetch -q 2>&1); then echo "fetch=ok"; else echo "fetch=$(printf '%s' "$o" | tr '\n' ' ' | tail -c 300)"; fi
  echo "upsha=$(git -C "$t" rev-parse '@{u}' 2>/dev/null)"
  B=$(git -C "$t" rev-list --count 'HEAD..@{u}' 2>/dev/null)
  echo "behind=$B"
  echo "ahead=$(git -C "$t" rev-list --count '@{u}..HEAD' 2>/dev/null)"
  if [ -n "$B" ] && [ "$B" != 0 ]; then
    git -C "$t" log --format='log=%h %ad %an: %s' --date=format:'%d.%m %H:%M' -n 8 'HEAD..@{u}'
    git -C "$t" diff --name-only HEAD '@{u}' | sed 's/^/chg=/'
    git -C "$t" ls-tree -r --name-only '@{u}' | grep -E '(^|/)__manifest__\.py$' | sed 's/^/mf=/'
    git -C "$t" ls-tree -r --name-only HEAD | grep -E '(^|/)__manifest__\.py$' | sed 's/^/mf=/'
  fi
done
'''
# stdin-список «каталог старый новый»: старый — ожидаемый HEAD (если HEAD уже другой — не трогаем)
REMOTE_GIT_MOVE = r'''
while read -r t old new; do
  [ -z "$t" ] && continue
  cur=$(git -C "$t" rev-parse HEAD 2>/dev/null)
  if [ "$cur" != "$old" ]; then echo "res=$t $old $cur fail HEAD_changed"; continue; fi
  # локальные правки не мешают: merge --ff-only и reset --keep сами откажут, если коммиты меняют те же файлы
  if [ "__MODE__" = pull ]; then o=$(git -C "$t" merge -q --ff-only "$new" 2>&1); else o=$(git -C "$t" reset -q --keep "$new" 2>&1); fi
  if [ $? = 0 ]; then echo "res=$t $old $(git -C "$t" rev-parse HEAD) ok"
  else echo "res=$t $old $cur fail $(printf '%s' "$o" | tr '\n' ' ' | tail -c 200)"; fi
done <<'LIST'
__LIST__
LIST
'''
# stdin-список «каталог коммит»: что будет отменено откатом
REMOTE_RESTORE_PLAN = r'''
while read -r t sha; do
  [ -z "$t" ] && continue
  echo "##REPO $t"
  echo "head=$(git -C "$t" rev-parse HEAD 2>/dev/null)"
  echo "dirty=$(git -C "$t" status --porcelain --untracked-files=no --ignore-submodules=all 2>/dev/null | wc -l)"
  git -C "$t" cat-file -e "$sha^{commit}" 2>/dev/null && echo "has=1" || echo "has=0"
  echo "undo=$(git -C "$t" rev-list --count "$sha..HEAD" 2>/dev/null)"
  git -C "$t" log --format='log=%h %ad %an: %s' --date=format:'%d.%m %H:%M' -n 8 "$sha..HEAD" 2>/dev/null
done <<'LIST'
__LIST__
LIST
'''
REMOTE_DBLIST = r'''
echo "conf=$(sed -n 's/^[[:space:]]*db_name[[:space:]]*=[[:space:]]*//p' __CONF__ 2>/dev/null | tail -1)"
__PSQL__ -X -At -d postgres -c "select datname from pg_database where not datistemplate and datname<>'postgres' order by 1" | sed 's/^/db=/'
'''
REMOTE_UPDATE_LIST = r'''
cd /tmp; LG=$(mktemp)
if [ "__UPD_STDOUT__" = 1 ]; then timeout 900 env __UPDATE_CMD__ -c __UPD_CONF__ -d '__DB__' --list-only >"$LG" 2>&1; R=$?; cp "$LG" "$LG.out"
else timeout 900 env __UPDATE_CMD__ -c __UPD_CONF__ -d '__DB__' --list-only --logfile "$LG" >"$LG.out" 2>&1; R=$?; fi; echo "rc=$R"
grep -ah 'hash changed:' "$LG" | tail -1 | sed 's/.*hash changed: /mods=/; s/[. ]*$//'
grep -ahE '^[0-9-]+ [0-9:,]+ [0-9]+ (CRITICAL|ERROR) ' "$LG" | tail -3 | cut -c1-300 | sed 's/^/err=/'
tail -n 3 "$LG.out" | cut -c1-300 | sed 's/^/out=/'
rm -f "$LG" "$LG.out"
'''
REMOTE_UPDATE_RUN = r'''
D="$HOME/.odoo-bot"; mkdir -p "$D"
if [ -f "$D/update.pid" ] && kill -0 "$(cat "$D/update.pid")" 2>/dev/null; then echo "busy=$(cat "$D/update.cur" 2>/dev/null)"; exit 0; fi
LG="$D/update-$(date +%Y%m%d-%H%M%S).log"
echo "$LG" > "$D/update.cur"
cd /tmp
if [ "__UPD_STDOUT__" = 1 ]; then C='env __UPDATE_CMD__ -c __UPD_CONF__ -d "$2" >"$1" 2>&1; echo $? > "$1.rc"'
else C='env __UPDATE_CMD__ -c __UPD_CONF__ -d "$2" --logfile "$1" >"$1.out" 2>&1; echo $? > "$1.rc"'; fi
nohup setsid bash -c "$C" _ "$LG" '__DB__' >/dev/null 2>&1 </dev/null &
echo $! > "$D/update.pid"
echo "log=$LG"
'''
REMOTE_UPDATE_POLL = r'''
LG='__LOG__'
[ -f "$LG.rc" ] && echo "rc=$(cat "$LG.rc")"
echo "errs=$(grep -acE '^[0-9-]+ [0-9:,]+ [0-9]+ (CRITICAL|ERROR) ' "$LG" 2>/dev/null)"
echo "---"
tail -n 5 "$LG" 2>/dev/null | cut -c1-220
'''
REMOTE_UPDATE_RESULT = r'''
LG='__LOG__'
echo "rc=$(cat "$LG.rc" 2>/dev/null)"
grep -ah 'hash changed:' "$LG" | tail -1 | sed 's/.*hash changed: /mods=/; s/[. ]*$//'
grep -ahE '^[0-9-]+ [0-9:,]+ [0-9]+ (CRITICAL|ERROR) ' "$LG" | cut -c1-300 | tail -n 10 | sed 's/^/err=/'
echo "warns=$(grep -acE '^[0-9-]+ [0-9:,]+ [0-9]+ WARNING ' "$LG")"
tail -n 5 "$LG.out" 2>/dev/null | cut -c1-300 | sed 's/^/out=/'
'''
REMOTE_RESTART = r'''
OD="__ODOO_DOCKER__"
if [ -n "$OD" ]; then sudo -n docker restart "$OD" >/dev/null 2>&1 && echo "restart=ok" || echo "restart=fail"
else sudo -n systemctl restart __SERVICE__ && echo "restart=ok" || echo "restart=fail"; fi
'''


def _dep_put(chat_id, target, kind, **data):
    now = time.time()
    for k in [k for k, v in _dep.items() if now - v["ts"] > DEP_TTL]:
        _dep.pop(k, None)
    tok = os.urandom(4).hex()
    _dep[tok] = {"chat": chat_id, "target": target, "kind": kind, "ts": now, **data}
    return tok


def _edit(chat_id, mid, text, kb=None):
    try:
        tg("editMessageText", chat_id=chat_id, message_id=mid, text=text[:4000], parse_mode="HTML",
           disable_web_page_preview=True, **({"reply_markup": kb} if kb else {}))
    except Exception:  # noqa: BLE001
        pass


def _del(chat_id, msg):
    try:
        tg("deleteMessage", chat_id=chat_id, message_id=msg["result"]["message_id"])
    except Exception:  # noqa: BLE001
        pass


def _hist_path(target):
    return os.path.join(HISTORY_DIR, f"{target}-deploy.jsonl")


def _hist_add(target, rec):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(SERVER_TZ))
    except Exception:  # noqa: BLE001
        now = datetime.now()
    rec = {"ts": now.strftime("%Y-%m-%d %H:%M:%S"), "target": target, **rec}
    with open(_hist_path(target), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _hist_load(target):
    try:
        with open(_hist_path(target), encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def _short_dir(path):
    return path.rstrip("/").rsplit("/", 1)[-1]


def _repos(out):
    """Разбор вывода с секциями ##REPO <dir>: {dir: {key: value, 'log': [...], 'chg': [...], 'mf': [...]}}."""
    repos, cur = {}, None
    for line in out.splitlines():
        if line.startswith("##REPO "):
            cur = repos.setdefault(line[7:].strip(), {"log": [], "chg": [], "mf": []})
        elif cur is not None and "=" in line:
            k, v = line.split("=", 1)
            if k in ("log", "chg", "mf"):
                cur[k].append(v)
            else:
                cur[k] = v.strip()
    return repos


def _changed_modules(r):
    dirs = sorted({m.rsplit("/", 1)[0] if "/" in m else "" for m in r["mf"]}, key=len, reverse=True)
    mods = set()
    for f in r["chg"]:
        for d in dirs:
            if d and f.startswith(d + "/"):
                mods.add(_short_dir(d))
                break
    return sorted(mods)


def _yes_no(tok, yes):
    return {"inline_keyboard": [[{"text": yes, "callback_data": f"dep:{tok}:run"}],
                                [{"text": L("Отмена", "Скасувати"), "callback_data": f"dep:{tok}:cancel"}]]}


def _move_list(items):
    for d, a, b in items:
        if not (PATH_RE.match(d) and SHA_RE.match(a) and SHA_RE.match(b)):
            raise RuntimeError(f"bad repo/sha: {d} {a} {b}")
    return "\n".join(f"{d} {a} {b}" for d, a, b in items)


# ---- /pull

def pull_plan(chat_id, target):
    st = send(chat_id, f"⏳ {html.escape(label(target))}: git fetch…")
    try:
        repos = _repos(ssh(target, REMOTE_PULL_PLAN, timeout=900))
    finally:
        _del(chat_id, st)
    if not repos:
        send(chat_id, "❌ " + html.escape(L("в addons_path нет git-репозиториев", "в addons_path немає git-репозиторіїв")))
        return
    lines, todo = [], []
    for d, r in repos.items():
        name = f"<b>{html.escape(_short_dir(d))}</b> <code>{html.escape(r.get('branch', '?'))}</code> {r.get('head', '')[:8]}"
        behind = int(r.get("behind") or 0)
        problems, warn = [], ""
        if r.get("dirty", "0") != "0":
            warn = "\n   ⚠️ " + html.escape(L(f"локальные правки в {r['dirty']} файлах (не коммит) — сохранятся; "
                                                "если входящие коммиты меняют эти же файлы, git откажет",
                                                f"локальні правки в {r['dirty']} файлах (не коміт) — збережуться; "
                                                "якщо вхідні коміти змінюють ці ж файли, git відмовить"))
        if r.get("fetch") != "ok":
            problems.append("fetch: " + r.get("fetch", "?"))
        if not r.get("up"):
            problems.append(L("нет upstream-ветки", "немає upstream-гілки"))
        if int(r.get("ahead") or 0):
            problems.append(L(f"{r['ahead']} локальных коммитов не в origin — ff невозможен",
                              f"{r['ahead']} локальних комітів не в origin — ff неможливий"))
        if problems:
            lines.append(f"⚠️ {name}: " + html.escape("; ".join(problems)))
        elif not behind:
            lines.append(f"✅ {name}: " + L("актуально", "актуально") + warn)
        if behind and not problems:
            mods = _changed_modules(r)
            todo.append({"dir": d, "head": r["head"], "up": r["upsha"], "behind": behind, "mods": mods})
            lines.append(f"⬇️ {name}: +{behind} " + L("коммитов", "комітів")
                         + "".join(f"\n   <code>{html.escape(x[:120])}</code>" for x in r["log"])
                         + ("\n   …" if behind > len(r["log"]) else "")
                         + ("\n   " + L("модули: ", "модулі: ") + html.escape(", ".join(mods[:30]))
                            + (L(f" … и ещё {len(mods) - 30}", f" … і ще {len(mods) - 30}") if len(mods) > 30 else "") if mods else "")
                         + warn)
    head = f"<b>/pull {html.escape(label(target))}</b>\n\n" + "\n".join(lines)
    if not todo:
        send(chat_id, head + "\n\n" + L("Подтягивать нечего.", "Підтягувати нічого."))
        return
    tok = _dep_put(chat_id, target, "pull", repos=todo)
    send(chat_id, head + "\n\n" + html.escape(L(
        "Будет выполнено git merge --ff-only до origin. Текущие коммиты сохранятся — к ним можно вернуться через /restore. "
        "Модули после этого не обновляются — это /update.",
        "Буде виконано git merge --ff-only до origin. Поточні коміти збережуться — до них можна повернутися через /restore. "
        "Модулі після цього не оновлюються — це /update.")),
         reply_markup=_yes_no(tok, L(f"⬇️ Pull ({len(todo)} репо)", f"⬇️ Pull ({len(todo)} репо)")))


def pull_run(chat_id, p):
    target = p["target"]
    items = [(r["dir"], r["head"], r["up"]) for r in p["repos"]]
    out = ssh(target, REMOTE_GIT_MOVE.replace("__MODE__", "pull").replace("__LIST__", _move_list(items)), timeout=300)
    res = [l[4:].split(" ", 4) for l in out.splitlines() if l.startswith("res=")]
    ok = [r for r in res if len(r) >= 4 and r[3] == "ok"]
    mods = sorted({m for r in p["repos"] for m in r["mods"]})
    if ok:
        _hist_add(target, {"kind": "pull", "chat": chat_id, "mods": mods,
                           "repos": [{"dir": r[0], "before": r[1], "after": r[2]} for r in ok]})
    lines = [("✅ " if len(r) >= 4 and r[3] == "ok" else "❌ ") + html.escape(
        f"{_short_dir(r[0])}: {r[1][:8]} → {r[2][:8] if len(r) > 2 else '?'}" + (f" ({' '.join(r[3:])})" if len(r) > 3 and r[3] != "ok" else ""))
        for r in res]
    kb = {"inline_keyboard": [[{"text": L("▶️ Обновить модули (/update)", "▶️ Оновити модулі (/update)"),
                                "callback_data": f"dep:go:update:{target}"}]]} if ok else None
    send(chat_id, f"<b>/pull {html.escape(label(target))}</b>\n" + "\n".join(lines or ["❌ " + html.escape(out[-500:])])
         + ("\n\n" + html.escape(L(f"Изменены модули ({len(mods)}): ", f"Змінено модулі ({len(mods)}): ") + ", ".join(mods[:40])
                                  + (" …" if len(mods) > 40 else "")) if mods and ok else ""),
         reply_markup=kb)


# ---- выбор базы

def choose_db(chat_id, target, then):
    if TARGETS[target]["db"]:
        return then(chat_id, target, TARGETS[target]["db"])
    kv = ssh(target, REMOTE_DBLIST, timeout=60)
    conf = (re.search(r"^conf=(.*)$", kv, re.M) or [None, ""])[1].strip()
    dbs = [l[3:].strip() for l in kv.splitlines() if l.startswith("db=") and DB_RE.match(l[3:].strip())]
    if len(dbs) == 1:
        return then(chat_id, target, dbs[0])
    if not dbs:
        send(chat_id, "❌ " + html.escape(L("не удалось получить список баз", "не вдалося отримати список баз")))
        return
    tok = _dep_put(chat_id, target, "db", dbs=dbs, then=then)
    rows = [[{"text": db + (L(" (в конфиге)", " (у конфігу)") if db == conf else ""), "callback_data": f"dep:{tok}:db{i}"}]
            for i, db in enumerate(dbs)]
    rows.append([{"text": L("Отмена", "Скасувати"), "callback_data": f"dep:{tok}:cancel"}])
    send(chat_id, html.escape(L(f"{label(target)}: несколько баз — какую обновлять?",
                                f"{label(target)}: кілька баз — яку оновлювати?")), reply_markup={"inline_keyboard": rows})


# ---- /update

def update_plan(chat_id, target, db):
    st = send(chat_id, html.escape(L(f"⏳ {label(target)} / {db}: считаю изменённые модули (~20 с)…",
                                     f"⏳ {label(target)} / {db}: рахую змінені модулі (~20 с)…")))
    try:
        out = ssh(target, REMOTE_UPDATE_LIST.replace("__DB__", db), timeout=960)
    finally:
        _del(chat_id, st)
    rc = (re.search(r"^rc=(\d+)", out, re.M) or [None, "?"])[1]
    m = re.search(r"^mods=(.*)$", out, re.M)
    mods = [x for x in (m[1].split(",") if m else []) if x]
    if rc != "0":
        det = "\n".join(l.split("=", 1)[1] for l in out.splitlines() if l.startswith(("err=", "out=")))
        send(chat_id, "❌ " + html.escape(L(f"click-odoo-update --list-only: код {rc}", f"click-odoo-update --list-only: код {rc}"))
             + f"\n<pre>{html.escape(det[-2500:])}</pre>")
        return
    head = f"<b>/update {html.escape(label(target))}</b> · {html.escape(db)}\n\n"
    if not mods:
        send(chat_id, head + "✅ " + html.escape(L("Хэши модулей совпадают с кодом — обновлять нечего.",
                                                   "Хеші модулів збігаються з кодом — оновлювати нічого.")))
        return
    shown = ", ".join(mods[:40]) + (L(f" … и ещё {len(mods) - 40}", f" … і ще {len(mods) - 40}") if len(mods) > 40 else "")
    note = ""
    if len(mods) > 100:
        note = "\n\n⚠️ " + L("Похоже, хэши в базе ещё не сохранены — обновятся почти все модули (как -u all), "
                             "это может занять десятки минут. После этого будут обновляться только изменённые.",
                             "Схоже, хеші в базі ще не збережені — оновляться майже всі модулі (як -u all), "
                             "це може зайняти десятки хвилин. Після цього оновлюватимуться лише змінені.")
    tok = _dep_put(chat_id, target, "update", db=db, mods=mods)
    send(chat_id, head + html.escape(L(f"Изменились ({len(mods)}): ", f"Змінилися ({len(mods)}): ") + shown + note + "\n\n" + L(
        "Odoo не останавливается, обновление идёт в фоне на сервере. В конце предложу перезапустить Odoo — "
        "новый Python-код подхватывается только после перезапуска.",
        "Odoo не зупиняється, оновлення йде у фоні на сервері. Наприкінці запропоную перезапустити Odoo — "
        "новий Python-код підхоплюється лише після перезапуску.")),
         reply_markup=_yes_no(tok, L(f"▶️ Обновить {len(mods)} модулей", f"▶️ Оновити {len(mods)} модулів")))


def update_run(chat_id, p):
    target, db = p["target"], p["db"]
    out = ssh(target, REMOTE_UPDATE_RUN.replace("__DB__", db), timeout=60)
    if out.startswith("busy="):
        send(chat_id, "⏳ " + html.escape(L("На сервере уже идёт обновление: ", "На сервері вже йде оновлення: ") + out[5:].strip()))
        return
    m = re.search(r"^log=(\S+)$", out, re.M)
    if not m or not PATH_RE.match(m[1]):
        send(chat_id, "❌ " + html.escape(L("не удалось запустить: ", "не вдалося запустити: ") + out[-500:]))
        return
    log = m[1]
    head = f"<b>/update {html.escape(label(target))}</b> · {html.escape(db)} · {len(p['mods'])} " + L("модулей", "модулів") + "\n"
    msg = send(chat_id, head + "⏳ " + L("запущено…", "запущено…"))
    mid = msg["result"]["message_id"]
    t0, rc, fails = time.time(), None, 0
    poll = REMOTE_UPDATE_POLL.replace("__LOG__", log)
    while time.time() - t0 < 6 * 3600:
        time.sleep(15)
        try:
            raw = ssh(target, poll, timeout=60)
            fails = 0
        except Exception:  # noqa: BLE001
            fails += 1
            if fails > 20:
                break
            continue
        top, _, tail = raw.partition("---\n")
        kv = _kv(top)
        rc = kv.get("rc")
        _edit(chat_id, mid, head + f"{'⏳' if rc is None else ('✅' if rc == '0' else '❌')} ⏱ {int(time.time() - t0) // 60} "
              + L("мин", "хв") + f" · ERROR: {kv.get('errs', '?')}\n<pre>{html.escape(compact_log(tail.strip()) or '…')[-3000:]}</pre>")
        if rc is not None:
            break
    mins = int(time.time() - t0) // 60
    res = ssh(target, REMOTE_UPDATE_RESULT.replace("__LOG__", log), timeout=60)
    rc = (re.search(r"^rc=(\d*)", res, re.M) or [None, ""])[1] or rc
    errs = [l[4:] for l in res.splitlines() if l.startswith("err=")]
    outs = [l[4:] for l in res.splitlines() if l.startswith("out=")]
    warns = (re.search(r"^warns=(\d+)", res, re.M) or [None, "?"])[1]
    ok = rc == "0"
    _hist_add(target, {"kind": "update", "chat": chat_id, "db": db, "mods": p["mods"], "rc": rc, "mins": mins, "log": log})
    text = head + ("✅ " if ok else "❌ ") + html.escape(
        L(f"{'готово' if ok else 'ошибка'} за {mins} мин (код {rc or '?'}), ERROR в логе: {len(errs)}{'+' if len(errs) >= 10 else ''}, WARNING: {warns}",
          f"{'готово' if ok else 'помилка'} за {mins} хв (код {rc or '?'}), ERROR у лозі: {len(errs)}{'+' if len(errs) >= 10 else ''}, WARNING: {warns}"))
    if rc is None or rc == "":
        text += "\n" + html.escape(L("Не дождался завершения — обновление может ещё идти на сервере.",
                                     "Не дочекався завершення — оновлення може ще йти на сервері."))
    if errs or (not ok and outs):
        text += f"\n<pre>{html.escape(cut_lines(chr(10).join(errs or outs), 300)[-3000:])}</pre>"
    text += "\n" + html.escape(L("Лог на сервере: ", "Лог на сервері: ") + log)
    kb = None
    if ok:
        tok = _dep_put(chat_id, target, "restart")
        text += "\n\n" + html.escape(L("Перезапустить Odoo, чтобы подхватить новый Python-код? (простой несколько секунд)",
                                       "Перезапустити Odoo, щоб підхопити новий Python-код? (простій кілька секунд)"))
        kb = {"inline_keyboard": [[{"text": L("🔄 Перезапустить Odoo", "🔄 Перезапустити Odoo"), "callback_data": f"dep:{tok}:run"}],
                                  [{"text": L("Не сейчас", "Не зараз"), "callback_data": f"dep:{tok}:cancel"}]]}
    send(chat_id, text, reply_markup=kb)


def restart_run(chat_id, p):
    target = p["target"]
    out = ssh(target, REMOTE_RESTART, timeout=180)
    if "restart=ok" not in out:
        send(chat_id, "❌ " + html.escape(L("перезапуск не удался: ", "перезапуск не вдався: ") + out[-500:]))
        return
    code = None
    for _ in range(12):
        time.sleep(5)
        code, _, _ = http_check(TARGETS[target]["url"])
        if code == 200:
            break
    _hist_add(target, {"kind": "restart", "chat": chat_id, "http": code})
    send(chat_id, ("✅ " if code == 200 else "⚠️ ") + html.escape(L(f"{label(target)}: Odoo перезапущен, сайт отвечает {code}",
                                                                   f"{label(target)}: Odoo перезапущено, сайт відповідає {code}")))
    run_action(chat_id, "health", [target])


# ---- /restore

def restore_list(chat_id, target):
    pulls = [r for r in _hist_load(target) if r.get("kind") == "pull" and r.get("repos")][-8:][::-1]
    if not pulls:
        send(chat_id, html.escape(L(f"{label(target)}: сохранённых /pull нет — откатывать не к чему.",
                                    f"{label(target)}: збережених /pull немає — відкочувати нема до чого.")))
        return
    tok = _dep_put(chat_id, target, "rpick", pulls=pulls)
    rows = []
    for i, r in enumerate(pulls):
        repos = ", ".join(f"{_short_dir(x['dir'])} {x['before'][:7]}" for x in r["repos"])
        rows.append([{"text": f"{r['ts'][8:10]}.{r['ts'][5:7]} {r['ts'][11:16]} · {repos}"[:60], "callback_data": f"dep:{tok}:r{i}"}])
    rows.append([{"text": L("Отмена", "Скасувати"), "callback_data": f"dep:{tok}:cancel"}])
    send(chat_id, html.escape(L(f"{label(target)}: к состоянию ДО какого /pull откатить код?",
                                f"{label(target)}: до стану ДО якого /pull відкотити код?")), reply_markup={"inline_keyboard": rows})


def restore_plan(chat_id, target, rec):
    lst = "\n".join(f"{x['dir']} {x['before']}" for x in rec["repos"]
                    if PATH_RE.match(x["dir"]) and SHA_RE.match(x["before"]))
    repos = _repos(ssh(target, REMOTE_RESTORE_PLAN.replace("__LIST__", lst), timeout=120))
    lines, todo = [], []
    for x in rec["repos"]:
        r = repos.get(x["dir"], {})
        name = f"<b>{html.escape(_short_dir(x['dir']))}</b>"
        undo = int(r.get("undo") or 0)
        if r.get("has") != "1":
            lines.append(f"❌ {name}: " + html.escape(L(f"коммита {x['before'][:8]} нет в репозитории", f"коміту {x['before'][:8]} немає в репозиторії")))
        elif r.get("head") == x["before"]:
            lines.append(f"✅ {name}: " + L("уже на этом коммите", "вже на цьому коміті"))
        else:
            todo.append((x["dir"], r["head"], x["before"]))
            lines.append(f"⏪ {name}: {r['head'][:8]} → {x['before'][:8]}"
                         + (L(f", отменяется коммитов: {undo}", f", скасовується комітів: {undo}") if undo else "")
                         + "".join(f"\n   <code>{html.escape(l[:120])}</code>" for l in r.get("log", []))
                         + ("\n   ⚠️ " + html.escape(L(f"локальные правки в {r['dirty']} файлах сохранятся (reset --keep); "
                                                         "если откат меняет эти же файлы — git откажет",
                                                         f"локальні правки в {r['dirty']} файлах збережуться (reset --keep); "
                                                         "якщо відкат змінює ці ж файли — git відмовить"))
                            if r.get("dirty", "0") != "0" else ""))
    head = f"<b>/restore {html.escape(label(target))}</b> · " + L("до pull ", "до pull ") + html.escape(rec["ts"]) + "\n\n" + "\n".join(lines)
    if not todo:
        send(chat_id, head + "\n\n" + L("Откатывать нечего.", "Відкочувати нічого."))
        return
    tok = _dep_put(chat_id, target, "restore", repos=todo)
    send(chat_id, head + "\n\n" + html.escape(L(
        "Будет git reset --keep к этим коммитам (коммиты остаются в origin, следующий /pull вернёт их). "
        "Затем покажу план /update — модули с изменившимся кодом обновятся. Данные в базе, изменённые миграциями, не откатываются.",
        "Буде git reset --keep до цих комітів (коміти лишаються в origin, наступний /pull поверне їх). "
        "Потім покажу план /update — модулі зі зміненим кодом оновляться. Дані в базі, змінені міграціями, не відкочуються.")),
         reply_markup=_yes_no(tok, L("⏪ Откатить код", "⏪ Відкотити код")))


def restore_run(chat_id, p):
    target = p["target"]
    out = ssh(target, REMOTE_GIT_MOVE.replace("__MODE__", "reset").replace("__LIST__", _move_list(p["repos"])), timeout=300)
    res = [l[4:].split(" ", 4) for l in out.splitlines() if l.startswith("res=")]
    ok = [r for r in res if len(r) >= 4 and r[3] == "ok"]
    if ok:
        _hist_add(target, {"kind": "restore", "chat": chat_id, "repos": [{"dir": r[0], "before": r[1], "after": r[2]} for r in ok]})
    send(chat_id, f"<b>/restore {html.escape(label(target))}</b>\n" + "\n".join(
        ("✅ " if len(r) >= 4 and r[3] == "ok" else "❌ ") + html.escape(f"{_short_dir(r[0])}: {r[1][:8]} → {r[2][:8] if len(r) > 2 else '?'}"
                                                                     + (f" ({' '.join(r[3:])})" if len(r) > 3 and r[3] != "ok" else ""))
        for r in res) or "❌ " + html.escape(out[-500:]))
    if ok:
        choose_db(chat_id, target, update_plan)


# ---- /2stage: свежая копия кластера PostgreSQL с мастера на stage (pg_basebackup), обезвреживание, запуск Odoo
# Включается настройкой "clone" у сервера в targets.json:
#   "clone": {"cluster": "16 main", "datadir": "/var/lib/postgresql/16/main",
#             "conninfo_file": "/etc/postgresql/16/main/.replica/postgresql.auto.conf",
#             "stage_url": "https://stage.example.com", "source": "prod (db-host)"}
# Строка подключения к мастеру (пользователь репликации и пароль) берётся из primary_conninfo в conninfo_file
# прямо на сервере БД — в бот, чат и лог пароль не попадает. Выполняется на хосте БД как systemd-юнит hk-2stage
# (переживает обрыв SSH и перезапуск бота). Без -R: после копии кластер стартует самостоятельной базой
# с обычными конфигами stage, догоняя WAL из копии (-X stream).

CLONE_TARGETS = [k for k, v in TARGETS.items() if isinstance(v.get("clone"), dict)]
CLONE_UNIT = "hk-2stage"
CLONE_DIR = "/var/lib/hk-2stage"
for _k in CLONE_TARGETS:
    _c = TARGETS[_k]["clone"]
    if not re.fullmatch(r"\d+ [a-z0-9_]+", _c.get("cluster", "")) or not PATH_RE.match(_c.get("datadir", "")) \
            or not PATH_RE.match(_c.get("conninfo_file", "")) or not re.fullmatch(r"https?://[A-Za-z0-9.:/_-]+", _c.get("stage_url", "")) \
            or not TARGETS[_k].get("db_ssh") or not DB_RE.match(TARGETS[_k].get("db", "")):
        sys.exit(f"targets.json: {_k}.clone — нужны cluster ('16 main'), datadir, conninfo_file, stage_url и db_ssh/db у сервера")

CLONE_NEUTRALIZE_SQL = r"""
-- Обезвреживание свежей копии prod на stage: всё, что может уйти наружу или выполниться само, — выключить.
BEGIN;
UPDATE ir_config_parameter SET value = :'stage_url' WHERE key = 'web.base.url';
UPDATE ir_cron SET active = false WHERE active;
DO $$ BEGIN
  IF to_regclass('ir_mail_server') IS NOT NULL THEN UPDATE ir_mail_server SET active = false, smtp_pass = NULL; END IF;
  IF to_regclass('fetchmail_server') IS NOT NULL THEN UPDATE fetchmail_server SET active = false, password = NULL; END IF;
  IF to_regclass('queue_job') IS NOT NULL THEN
    UPDATE queue_job SET state = 'cancelled'
     WHERE state IN ('wait_dependencies', 'pending', 'enqueued', 'started');
  END IF;
END $$;
COMMIT;
SELECT 'web.base.url=' || coalesce((SELECT value FROM ir_config_parameter WHERE key = 'web.base.url'), '-');
SELECT 'cron_active=' || count(*) FROM ir_cron WHERE active;
SELECT 'mail_servers_active=' || count(*) FROM ir_mail_server WHERE active;
"""

# Скрипт на хосте БД (root). Статус — CLONE_DIR/status (key=value), подробный лог — CLONE_DIR/log.
CLONE_RUN_SH = r"""#!/bin/bash
set -uo pipefail
W=__CDIR__; LOG=$W/log; ST=$W/status
VER=__VER__; NAME=__NAME__; DATA=__DATA__; CONNF=__CONNF__; DB=__DBNAME__; URL=__URL__
BIN=/usr/lib/postgresql/$VER/bin
START=$(date +%s); STEP=init
exec >>"$LOG" 2>&1
trap 'rm -f "$W/pgpass"' EXIT
st()   { STEP=$1; echo "$(date '+%F %T') == $1"; printf 'step=%s\nstarted=%s\n' "$1" "$START" > "$ST.tmp"; mv "$ST.tmp" "$ST"; }
fail() { echo "$(date '+%F %T') !! $1"; printf 'step=%s\nresult=fail\nmsg=%s\nstarted=%s\nended=%s\n' "$STEP" "$1" "$START" "$(date +%s)" > "$ST"; exit 1; }
asp()  { runuser -u postgres -- env PGPASSFILE="$W/pgpass" "$@"; }

st check
[ -x "$BIN/pg_basebackup" ] || fail "нет $BIN/pg_basebackup"
[ -d "$DATA" ] || fail "нет каталога $DATA"
PORT=$(pg_lsclusters -h | awk -v v="$VER" -v n="$NAME" '$1==v && $2==n {print $3}')
[ -n "$PORT" ] || fail "кластер $VER/$NAME не найден"
# primary_conninfo -> host/port/user + pgpass (пароль только в файле 600, не в аргументах процессов)
eval "$(python3 - "$CONNF" <<'PY'
import re, shlex, sys
txt = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"^\s*primary_conninfo\s*=\s*'((?:[^']|'')*)'", txt, re.M)
ci = m.group(1).replace("''", "'") if m else ""
kv = dict(re.findall(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", ci))
kv = {k: (v[1:-1].replace("\\'", "'").replace("\\\\", "\\") if v.startswith("'") else v) for k, v in kv.items()}
for k in ("host", "port", "user", "password", "sslmode"):
    print(f"SRC_{k.upper()}={shlex.quote(kv.get(k, ''))}")
PY
)"
[ -n "$SRC_HOST" ] && [ -n "$SRC_USER" ] || fail "в $CONNF нет primary_conninfo с host/user"
esc() { printf '%s' "$1" | sed 's/[\\:]/\\&/g'; }
install -m 600 -o postgres -g postgres /dev/null "$W/pgpass"
printf '%s:%s:*:%s:%s\n' "$(esc "$SRC_HOST")" "${SRC_PORT:-5432}" "$(esc "$SRC_USER")" "$(esc "$SRC_PASSWORD")" > "$W/pgpass"
export PGSSLMODE="${SRC_SSLMODE:-prefer}"
SRC="host=$SRC_HOST port=${SRC_PORT:-5432} user=$SRC_USER"
asp env PGSSLMODE="$PGSSLMODE" "$BIN/psql" -X -At "$SRC dbname=replication replication=true" -c IDENTIFY_SYSTEM >/dev/null \
  || fail "нет подключения репликации к $SRC_HOST как $SRC_USER"
echo "source: $SRC_HOST:${SRC_PORT:-5432} user=$SRC_USER; target: $VER/$NAME port $PORT $DATA"

st stop
systemctl stop "postgresql@$VER-$NAME" || pg_ctlcluster "$VER" "$NAME" stop -m fast || true
for i in $(seq 1 60); do pg_lsclusters -h | awk -v v="$VER" -v n="$NAME" '$1==v && $2==n {print $4}' | grep -q online || break; sleep 2; done
pg_lsclusters -h | awk -v v="$VER" -v n="$NAME" '$1==v && $2==n {print $4}' | grep -q online && fail "кластер не остановился"

st clean
find "$DATA" -mindepth 1 -delete || fail "не удалось очистить $DATA"
echo "free after clean: $(df -h --output=avail "$DATA" | tail -1)"

st basebackup
asp env PGSSLMODE="$PGSSLMODE" "$BIN/pg_basebackup" -h "$SRC_HOST" -p "${SRC_PORT:-5432}" -U "$SRC_USER" -w \
    -D "$DATA" -X stream -c fast -P -v || fail "pg_basebackup завершился с ошибкой"

st fix
rm -f "$DATA/standby.signal" "$DATA/recovery.signal" "$DATA/postmaster.pid"
printf '# Do not edit this file manually!\n# It will be overwritten by the ALTER SYSTEM command.\n' > "$DATA/postgresql.auto.conf"
chown postgres:postgres "$DATA/postgresql.auto.conf"; chmod 600 "$DATA/postgresql.auto.conf"

st start
systemctl start "postgresql@$VER-$NAME" || echo "systemctl start вернул ошибку — ждём восстановления по WAL"
ok=""
for i in $(seq 1 1440); do            # до 4 ч на догон WAL
  if runuser -u postgres -- "$BIN/pg_isready" -q -p "$PORT"; then ok=1; break; fi
  if ! { [ -f "$DATA/postmaster.pid" ] && kill -0 "$(head -1 "$DATA/postmaster.pid")" 2>/dev/null; }; then
    sleep 5; runuser -u postgres -- "$BIN/pg_isready" -q -p "$PORT" && { ok=1; break; }
    tail -n 30 /var/log/postgresql/postgresql-$VER-$NAME.log; fail "PostgreSQL не запустился (лог выше)"
  fi
  sleep 10
done
[ -n "$ok" ] || fail "PostgreSQL не стал доступен за 4 ч"
[ "$(runuser -u postgres -- psql -X -At -p "$PORT" -d postgres -c 'select pg_is_in_recovery()')" = f ] || fail "кластер в режиме recovery"
pg_lsclusters

st neutralize
runuser -u postgres -- psql -X -At -v ON_ERROR_STOP=1 -v stage_url="$URL" -p "$PORT" -d "$DB" -f "$W/neutralize.sql" \
  > "$W/neutralize.out" || { cat "$W/neutralize.out"; fail "обезвреживание не прошло"; }
cat "$W/neutralize.out"
SIZE=$(runuser -u postgres -- psql -X -At -p "$PORT" -d postgres -c "select pg_size_pretty(pg_database_size('$DB'))")
printf 'step=done\nresult=ok\nstarted=%s\nended=%s\nsize=%s\n' "$START" "$(date +%s)" "$SIZE" > "$ST"
grep = "$W/neutralize.out" | sed 's/^/n_/' >> "$ST"
echo "$(date '+%F %T') == done"
"""

REMOTE_CLONE_PLAN = r"""
D=__DATA__; C=__CONNF__; V=__VER__; N=__NAME__
echo "cluster=$(pg_lsclusters -h | awk -v v="$V" -v n="$N" '$1==v && $2==n {print $1"/"$2" port "$3" "$4}')"
echo "conninfo=$(sudo -n grep -c '^\s*primary_conninfo' "$C" 2>/dev/null || echo 0)"
echo "dbsize=$(__PSQL__ -X -At -d postgres -c "select pg_size_pretty(sum(pg_database_size(datname))) from pg_database" 2>/dev/null)"
echo "free=$(df -h --output=avail "$(dirname "$D")" | tail -1 | tr -d ' ')"
echo "unit=$(systemctl is-active __UNIT__ 2>/dev/null)"
echo "last=$(sudo -n cat __CDIR__/status 2>/dev/null | tr '\n' ' ')"
"""

REMOTE_CLONE_POLL = r"""
echo "unit=$(systemctl is-active __UNIT__ 2>/dev/null)"
sudo -n cat __CDIR__/status 2>/dev/null
echo "progress=$(sudo -n tail -c 4000 __CDIR__/log 2>/dev/null | tr '\r' '\n' | grep -E 'kB \(' | tail -1)"
echo ---
sudo -n tail -c 20000 __CDIR__/log 2>/dev/null | tr '\r' '\n' | grep -vE 'kB \(|^\s*$' | tail -n 8
"""


def _clone_sub(script, target):
    c = TARGETS[target]["clone"]
    ver, name = c["cluster"].split()
    return (script.replace("__CDIR__", CLONE_DIR).replace("__UNIT__", CLONE_UNIT).replace("__VER__", ver)
            .replace("__NAME__", name).replace("__DATA__", shlex.quote(c["datadir"]))
            .replace("__CONNF__", shlex.quote(c["conninfo_file"])).replace("__URL__", shlex.quote(c["stage_url"]))
            .replace("__DBNAME__", shlex.quote(TARGETS[target]["db"])))


def _clone_head(target):
    c = TARGETS[target]["clone"]
    return f"<b>/2stage {html.escape(label(target))}</b> · {html.escape(c.get('source', L('мастер', 'майстер')))} → {html.escape(c['cluster'])}\n"


def clone_plan(chat_id, target):
    c = TARGETS[target]["clone"]
    st = _kv(ssh(target, _clone_sub(REMOTE_CLONE_PLAN, target).replace("__PSQL__", TARGETS[target]["psql"]),
                 timeout=120, host_field="db_ssh"))
    if st.get("unit") in ("active", "activating"):
        send(chat_id, _clone_head(target) + "⏳ " + html.escape(L("копирование уже идёт — подключаюсь к прогрессу.",
                                                                  "копіювання вже йде — підключаюсь до прогресу.")))
        clone_watch(chat_id, target)
        return
    odoo = ""
    if TARGETS[target]["odoo_docker"]:
        odoo = ssh(target, f"docker inspect -f '{{{{.State.Status}}}}' {shlex.quote(TARGETS[target]['odoo_docker'])} 2>&1", timeout=60).strip()
    if st.get("conninfo", "0") == "0":
        send(chat_id, _clone_head(target) + "❌ " + html.escape(L(f"в {c['conninfo_file']} нет primary_conninfo — не знаю, откуда копировать.",
                                                                  f"у {c['conninfo_file']} немає primary_conninfo — не знаю, звідки копіювати.")))
        return
    info = L(f"Кластер stage: {st.get('cluster')}\nБазы сейчас: {st.get('dbsize') or '?'}, свободно на диске: {st.get('free')}\n"
             f"Odoo stage ({TARGETS[target]['odoo_docker'] or TARGETS[target]['service']}): {odoo or '?'}\n"
             f"Прошлый запуск: {st.get('last') or '—'}",
             f"Кластер stage: {st.get('cluster')}\nБази зараз: {st.get('dbsize') or '?'}, вільно на диску: {st.get('free')}\n"
             f"Odoo stage ({TARGETS[target]['odoo_docker'] or TARGETS[target]['service']}): {odoo or '?'}\n"
             f"Попередній запуск: {st.get('last') or '—'}")
    steps = L(
        "1. Остановить Odoo stage\n2. Остановить PostgreSQL stage и УДАЛИТЬ все его данные\n"
        "3. pg_basebackup -X stream -c fast с мастера (часы; на мастере — контрольная точка и чтение всего кластера)\n"
        "4. Очистить postgresql.auto.conf, запустить как самостоятельную базу (конфиги stage не меняются)\n"
        f"5. Обезвредить {TARGETS[target]['db']}: все cron и queue jobs выключить, почтовые серверы выключить, web.base.url = {c['stage_url']}\n"
        "6. Запустить Odoo stage и проверить сайт\n\nОтменить после шага 2 нельзя — старая база stage будет удалена.",
        "1. Зупинити Odoo stage\n2. Зупинити PostgreSQL stage і ВИДАЛИТИ всі його дані\n"
        "3. pg_basebackup -X stream -c fast з майстра (години; на майстрі — контрольна точка і читання всього кластера)\n"
        "4. Очистити postgresql.auto.conf, запустити як самостійну базу (конфіги stage не змінюються)\n"
        f"5. Знешкодити {TARGETS[target]['db']}: усі cron і queue jobs вимкнути, поштові сервери вимкнути, web.base.url = {c['stage_url']}\n"
        "6. Запустити Odoo stage і перевірити сайт\n\nСкасувати після кроку 2 не можна — стара база stage буде видалена.")
    tok = _dep_put(chat_id, target, "2stage")
    send(chat_id, _clone_head(target) + "\n" + html.escape(info) + "\n\n" + html.escape(steps),
         reply_markup=_yes_no(tok, L("🗑➡️ Удалить stage и скопировать с мастера", "🗑➡️ Видалити stage і скопіювати з майстра")))


def _odoo_ctl(target, action):
    t = TARGETS[target]
    if t["odoo_docker"]:
        return ssh(target, f"docker {action} {shlex.quote(t['odoo_docker'])} >/dev/null 2>&1 && echo ok=1 || echo ok=0", timeout=180)
    return ssh(target, f"sudo -n systemctl {action} {shlex.quote(t['service'])} && echo ok=1 || echo ok=0", timeout=180)


def clone_run(chat_id, p):
    target = p["target"]
    head = _clone_head(target)
    msg = send(chat_id, head + "⏳ " + L("останавливаю Odoo stage…", "зупиняю Odoo stage…"))
    if "ok=1" not in _odoo_ctl(target, "stop"):
        send(chat_id, head + "❌ " + L("не удалось остановить Odoo stage — ничего не трогаю.", "не вдалося зупинити Odoo stage — нічого не чіпаю."))
        return
    script = _clone_sub(CLONE_RUN_SH, target)
    launch = (f"set -e; sudo -n install -d -m 700 {CLONE_DIR}; sudo -n rm -f {CLONE_DIR}/status\n"
              f"sudo -n tee {CLONE_DIR}/run.sh >/dev/null <<'HK_RUN_EOF'\n{script}\nHK_RUN_EOF\n"
              f"sudo -n tee {CLONE_DIR}/neutralize.sql >/dev/null <<'HK_SQL_EOF'\n{CLONE_NEUTRALIZE_SQL}\nHK_SQL_EOF\n"
              f"echo \"=== $(date '+%F %T') /2stage\" | sudo -n tee -a {CLONE_DIR}/log >/dev/null\n"
              f"sudo -n systemd-run --unit={CLONE_UNIT} --collect -p Type=exec -p KillMode=process /bin/bash {CLONE_DIR}/run.sh 2>&1 && echo launched=1\n")
    try:
        out = ssh(target, launch, timeout=120, host_field="db_ssh")
    except Exception as e:  # noqa: BLE001
        out = str(e)
    if "launched=1" not in out:
        _odoo_ctl(target, "start")
        send(chat_id, head + "❌ " + html.escape(L("не удалось запустить копирование (Odoo stage запущен обратно): ",
                                                   "не вдалося запустити копіювання (Odoo stage запущено назад): ") + out[-500:]))
        return
    _hist_add(target, {"kind": "2stage", "chat": chat_id})
    clone_watch(chat_id, target, msg["result"]["message_id"])


CLONE_STEPS = {"check": ("проверка подключения к мастеру", "перевірка підключення до майстра"),
               "stop": ("остановка PostgreSQL stage", "зупинка PostgreSQL stage"),
               "clean": ("удаление старых данных", "видалення старих даних"),
               "basebackup": ("pg_basebackup с мастера", "pg_basebackup з майстра"),
               "fix": ("подготовка к запуску", "підготовка до запуску"),
               "start": ("запуск PostgreSQL, догон WAL", "запуск PostgreSQL, догін WAL"),
               "neutralize": ("обезвреживание", "знешкодження"),
               "done": ("готово", "готово")}


def clone_watch(chat_id, target, mid=None):
    """Следит за юнитом hk-2stage: прогресс в одном сообщении, в конце — запуск Odoo и итог."""
    head = _clone_head(target)
    if mid is None:
        mid = send(chat_id, head + "⏳ …")["result"]["message_id"]
    t0, last_edit, st, tail = time.time(), 0, {}, ""
    while time.time() - t0 < 16 * 3600:
        time.sleep(20)
        try:
            top, _, tail = ssh(target, _clone_sub(REMOTE_CLONE_POLL, target), timeout=60, host_field="db_ssh").partition("---\n")
        except Exception:  # noqa: BLE001
            continue
        st = _kv(top)
        done = st.get("result") in ("ok", "fail") or (st.get("unit") not in ("active", "activating") and time.time() - t0 > 60)
        if done or time.time() - last_edit > 120:
            step = st.get("step", "?")
            mins = (int(time.time()) - int(st.get("started") or time.time())) // 60
            prog = st.get("progress", "").strip()
            body = (head + f"⏳ {html.escape(L(*CLONE_STEPS.get(step, (step, step))))} · ⏱ {mins} " + L("мин", "хв")
                    + (f"\n<code>{html.escape(prog[:120])}</code>" if prog and step == "basebackup" else "")
                    + f"\n<pre>{html.escape(cut_lines(tail.strip(), 160)[-1500:]) or '…'}</pre>")
            _edit(chat_id, mid, body)
            last_edit = time.time()
        if done:
            break
    ok = st.get("result") == "ok"
    mins = (int(st.get("ended") or time.time()) - int(st.get("started") or t0)) // 60
    if not ok:
        send(chat_id, head + "❌ " + html.escape(L(
            f"Ошибка на шаге «{L(*CLONE_STEPS.get(st.get('step', '?'), ('?', '?')))}»: {st.get('msg') or 'юнит завершился без результата'}. "
            f"Odoo stage оставлен остановленным. Подробный лог на сервере БД: {CLONE_DIR}/log",
            f"Помилка на кроці «{L(*CLONE_STEPS.get(st.get('step', '?'), ('?', '?')))}»: {st.get('msg') or 'юніт завершився без результату'}. "
            f"Odoo stage залишено зупиненим. Детальний лог на сервері БД: {CLONE_DIR}/log"))
             + f"\n<pre>{html.escape(cut_lines(tail.strip(), 160)[-1500:])}</pre>")
        if st.get("step") in ("check", "stop"):
            _odoo_ctl(target, "start")
            send(chat_id, html.escape(L("Данные stage не тронуты — Odoo stage запущен обратно.", "Дані stage не зачеплено — Odoo stage запущено назад.")))
        return
    _odoo_ctl(target, "start")
    code = None
    for _ in range(36):
        time.sleep(10)
        code, _, _ = http_check(TARGETS[target]["url"])
        if code == 200:
            break
    neut = ", ".join(f"{k[2:]}={v}" for k, v in st.items() if k.startswith("n_"))
    _hist_add(target, {"kind": "2stage_done", "chat": chat_id, "size": st.get("size"), "http": code})
    send(chat_id, head + ("✅ " if code == 200 else "⚠️ ") + html.escape(L(
        f"Свежая копия за {mins} мин, база {TARGETS[target]['db']}: {st.get('size', '?')}. Обезврежено: {neut}. "
        f"Odoo stage запущен, сайт отвечает {code}.",
        f"Свіжа копія за {mins} хв, база {TARGETS[target]['db']}: {st.get('size', '?')}. Знешкоджено: {neut}. "
        f"Odoo stage запущено, сайт відповідає {code}.")))
    run_action(chat_id, "health", [target])


# ---- точки входа

DEPLOY_START = {"pull": pull_plan, "update": lambda c, t: choose_db(c, t, update_plan), "restore": restore_list, "2stage": clone_plan}
DEPLOY_RUN = {"pull": pull_run, "update": update_run, "restart": restart_run, "restore": restore_run, "2stage": clone_run}
if CLONE_TARGETS:
    DEPLOY_TITLE["2stage"] = {"ru": "stage: удалить базу и скопировать свежую с мастера",
                              "uk": "stage: видалити базу і скопіювати свіжу з майстра"}


def _deploy_guard(chat_id, target, fn, *args):
    lock = _deploy_locks[target]
    if not lock.acquire(blocking=False):
        send(chat_id, "⏳ " + html.escape(L(f"{label(target)}: уже идёт /pull, /update или /restore — дождитесь окончания.",
                                           f"{label(target)}: вже йде /pull, /update або /restore — дочекайтеся завершення.")))
        return
    try:
        fn(chat_id, target, *args)
    except Exception as e:  # noqa: BLE001
        print(f"deploy {fn.__name__} {target} failed:", repr(e), file=sys.stderr, flush=True)
        send(chat_id, f"❌ <b>{html.escape(label(target))}</b>: {html.escape(str(e))}")
    finally:
        lock.release()


def deploy_command(chat_id, cmd, args):
    if chat_id not in ADMIN_CHAT_IDS:
        send(chat_id, html.escape(L(f"Команда /{cmd} доступна только администратору в личном чате с ботом.",
                                    f"Команда /{cmd} доступна лише адміністратору в особистому чаті з ботом.")))
        return
    allowed = CLONE_TARGETS if cmd == "2stage" else DEPLOY_TARGETS
    tgt = [a.lower() for a in args if a.lower() in TARGETS]
    if not tgt and len(allowed) == 1:
        tgt = allowed[:]
    if not tgt:
        send(chat_id, html.escape(L(f"Для какого сервера /{cmd}?", f"Для якого сервера /{cmd}?")),
             reply_markup={"inline_keyboard": [[{"text": k, "callback_data": f"dep:go:{cmd}:{k}"} for k in allowed]]})
        return
    if tgt[0] not in allowed:
        send(chat_id, html.escape(L(f"Для {tgt[0]} /{cmd} не настроен (нет update_cmd в targets.json).",
                                    f"Для {tgt[0]} /{cmd} не налаштовано (немає update_cmd у targets.json).")))
        return
    _deploy_guard(chat_id, tgt[0], DEPLOY_START[cmd])


def deploy_callback(chat_id, message_id, args):
    if chat_id not in ADMIN_CHAT_IDS:
        return
    if args[0] == "go" and len(args) == 3:
        if args[1] in DEPLOY_START and args[2] in (CLONE_TARGETS if args[1] == "2stage" else DEPLOY_TARGETS):
            try:
                tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
            except Exception:  # noqa: BLE001
                pass
            _deploy_guard(chat_id, args[2], DEPLOY_START[args[1]])
        return
    if len(args) != 2:
        return
    tok, op = args
    p = _dep.pop(tok, None)
    try:
        tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
    except Exception:  # noqa: BLE001
        pass
    if not p or p["chat"] != chat_id or time.time() - p["ts"] > DEP_TTL:
        send(chat_id, L("Устарело — запустите команду заново.", "Застаріло — запустіть команду знову."))
        return
    if op == "cancel":
        send(chat_id, L("Отменено.", "Скасовано."))
        return
    target = p["target"]
    if p["kind"] == "db" and op.startswith("db") and op[2:].isdigit() and int(op[2:]) < len(p["dbs"]):
        _deploy_guard(chat_id, target, p["then"], p["dbs"][int(op[2:])])
    elif p["kind"] == "rpick" and op.startswith("r") and op[1:].isdigit() and int(op[1:]) < len(p["pulls"]):
        _deploy_guard(chat_id, target, restore_plan, p["pulls"][int(op[1:])])
    elif op == "run" and p["kind"] in DEPLOY_RUN:
        _deploy_guard(chat_id, target, lambda c, t: DEPLOY_RUN[p["kind"]](c, p))


HELP_DEPLOY = (
    "<b>/pull</b> — git pull во всех репозиториях из addons_path конфига Odoo: показывает новые коммиты и затронутые модули, "
    "ждёт подтверждения; коммиты «до» сохраняются для /restore\n\n"
    "<b>/update</b> — обновить модули, у которых изменился код (click-odoo-update по хэшам). Если баз несколько — выбор базы. "
    "Odoo не останавливается; в конце — кнопка перезапуска\n\n"
    "<b>/restore</b> — выбрать один из последних /pull и вернуть код к состоянию до него, затем план /update\n"
    "Только администратор, в личном чате.\n\n",
    "<b>/pull</b> — git pull у всіх репозиторіях з addons_path конфігу Odoo: показує нові коміти і зачеплені модулі, "
    "чекає підтвердження; коміти «до» зберігаються для /restore\n\n"
    "<b>/update</b> — оновити модулі, у яких змінився код (click-odoo-update за хешами). Якщо баз кілька — вибір бази. "
    "Odoo не зупиняється; наприкінці — кнопка перезапуску\n\n"
    "<b>/restore</b> — вибрати один з останніх /pull і повернути код до стану до нього, потім план /update\n"
    "Лише адміністратор, в особистому чаті.\n\n",
)


HELP_BODY_RU = (
    "<b>/health</b> — диск, RAM, load average 1/5/15 мин и загрузка CPU, сервисы Odoo, postgresql и nginx "
    "(конфиг, порты 80/443, 5xx и 502/504, ошибки error.log за час), порты Odoo, "
    "ответ сайта, размер лога и число ошибок\n\n"
    "<b>/log [N|all]</b> — последние строки лога Odoo\n"
    "• без числа — 20 строк; до 100 — текстом в чат (без даты/времени/pid, ❗ у ERROR/CRITICAL/FATAL)\n"
    "• больше 100 — файлом с полными строками (до 200000)\n"
    "• all — весь текущий лог файлом .gz (до 50 МБ; архивы после ротации не входят)\n\n"
    "<b>/errors</b> — 10 последних FATAL/CRITICAL/ERROR из последних строк лога (по умолчанию 10000); шум сканеров отсеивается; serialization-ошибки не показываются — только их число за сегодня\n\n"
    "<b>/jobs</b> — cron: последний и следующий запуск, ▶ выполняется сейчас, ⚠ просрочен; "
    "queue_job: что выполняется, очередь, последние выполненные и упавшие\n\n"
    "<b>/users [all]</b> — пользователи за 24 ч: кто был в Odoo и когда, кто онлайн сейчас, "
    "кто вносил изменения в документы; действия каждого за сегодня (топ-40, all — весь список)\n\n"
    "<b>/analyze</b> — разбор состояния через Claude (~30 с): health, ошибки с трейсбеками, задачи. "
    "Помнит прошлый анализ сервера и сравнивает: что исправилось, что осталось, что нового\n\n"
)
HELP_BODY_UK = (
    "<b>/health</b> — диск, RAM, load average 1/5/15 хв і завантаження CPU, сервіси Odoo, postgresql і nginx "
    "(конфіг, порти 80/443, 5xx і 502/504, помилки error.log за годину), порти Odoo, "
    "відповідь сайту, розмір лога і кількість помилок\n\n"
    "<b>/log [N|all]</b> — останні рядки лога Odoo\n"
    "• без числа — 20 рядків; до 100 — текстом у чат (без дати/часу/pid, ❗ у ERROR/CRITICAL/FATAL)\n"
    "• більше 100 — файлом з повними рядками (до 200000)\n"
    "• all — весь поточний лог файлом .gz (до 50 МБ; архіви після ротації не входять)\n\n"
    "<b>/errors</b> — 10 останніх FATAL/CRITICAL/ERROR з останніх рядків лога (за замовчуванням 10000); шум сканерів відсіюється; serialization-помилки не показуються — лише їх кількість за сьогодні\n\n"
    "<b>/jobs</b> — cron: останній і наступний запуск, ▶ виконується зараз, ⚠ прострочено; "
    "queue_job: що виконується, черга, останні виконані і з помилкою\n\n"
    "<b>/users [all]</b> — користувачі за 24 год: хто був в Odoo і коли, хто онлайн зараз, "
    "хто вносив зміни в документи; дії кожного за сьогодні (топ-40, all — увесь список)\n\n"
    "<b>/analyze</b> — розбір стану через Claude (~30 с): health, помилки з трейсбеками, задачі. "
    "Пам'ятає минулий аналіз сервера і порівнює: що виправлено, що залишилось, що нового\n\n"
)
HELP_BACKUP = {
    "prod2stage": (
        "<b>/backup [модули|all|-]</b> — копия базы {p2s}: новая база {prefix}ДД.ММ.ГГ, filestore, "
        "обезвреживание ({neutralize}), переключение {dst}, обновление модулей "
        "(по умолчанию из настроек), проверка. Сначала показывает план и ждёт подтверждения. Только админ, в личном чате\n\n",
        "<b>/backup [модулі|all|-]</b> — копія бази {p2s}: нова база {prefix}ДД.ММ.РР, filestore, "
        "знешкодження ({neutralize}), перемикання {dst}, оновлення модулів "
        "(за замовчуванням з налаштувань), перевірка. Спершу показує план і чекає підтвердження. Лише адмін, в особистому чаті\n\n"),
    "systemd": (
        "<b>/backup</b> — запустить бэкап сейчас (юнит {unit}): показывает последний запуск и следующий по расписанию, "
        "ждёт подтверждения, потом показывает ход и итог. {note}\n\n",
        "<b>/backup</b> — запустити бекап зараз (юніт {unit}): показує останній запуск і наступний за розкладом, "
        "чекає підтвердження, потім показує хід і підсумок. {note}\n\n"),
}


def help_text():
    multi = len(TARGETS) > 1
    names = " / ".join(TARGETS)
    first = next(iter(TARGETS))
    last = list(TARGETS)[-1]
    if multi:
        how = L(f"<b>Как указывать сервер</b>\nБез сервера бот покажет кнопки {names} / оба. Или сразу: "
                f"<code>/health {first}</code>, <code>/errors both</code>, <code>/log {last} 50</code>, <code>/log {first} all</code>\n\n",
                f"<b>Як вказувати сервер</b>\nБез сервера бот покаже кнопки {names} / обидва. Або одразу: "
                f"<code>/health {first}</code>, <code>/errors both</code>, <code>/log {last} 50</code>, <code>/log {first} all</code>\n\n")
    else:
        how = L("Примеры: <code>/log 50</code>, <code>/log all</code>\n\n", "Приклади: <code>/log 50</code>, <code>/log all</code>\n\n")
    servers = L("<b>Серверы</b>\n", "<b>Сервери</b>\n") + "".join(
        f"{html.escape(k)} — {html.escape(v['note'])}\n" for k, v in TARGETS.items()) + "\n"
    bk = HELP_BACKUP.get(BACKUP_MODE)
    backup = ""
    if bk:
        backup = L(bk[0], bk[1]).format(unit=html.escape(BACKUP_UNIT),
                                        note=html.escape(L(BACKUP_NOTE_RU, BACKUP_NOTE_UK)),
                                        p2s=html.escape(P2S), dst=html.escape(P2S_DST), prefix=html.escape(DB_PREFIX),
                                        neutralize=html.escape(L(NEUTRALIZE["ru"], NEUTRALIZE["uk"])))
    deploy = L(*HELP_DEPLOY) if DEPLOY_TARGETS else ""
    if CLONE_TARGETS:
        deploy += L(f"<b>/2stage</b> — {html.escape(', '.join(CLONE_TARGETS))}: удалить базу stage и скопировать свежую с мастера "
                    "(pg_basebackup, часы), обезвредить (cron, queue jobs, почта, web.base.url), запустить Odoo. План → подтверждение. "
                    "Только администратор\n\n",
                    f"<b>/2stage</b> — {html.escape(', '.join(CLONE_TARGETS))}: видалити базу stage і скопіювати свіжу з майстра "
                    "(pg_basebackup, години), знешкодити (cron, queue jobs, пошта, web.base.url), запустити Odoo. План → підтвердження. "
                    "Лише адміністратор\n\n")
    return (f"<b>{L('Мониторинг', 'Моніторинг')} {html.escape(BOT_TITLE)}</b>\n\n" + L(HELP_BODY_RU, HELP_BODY_UK) + backup + deploy
            + L("<b>/help</b> — эта справка\n\n", "<b>/help</b> — ця довідка\n\n") + how + servers
            + L(f"Время: в логах — UTC, в /jobs и /users — {SERVER_TZ}.", f"Час: у логах — UTC, у /jobs і /users — {SERVER_TZ}."))


_menu_done = set()


def set_menu(chat_id):
    """Меню команд: chat_id=None — по умолчанию (язык DEFAULT_LANG, без /backup); иначе — для конкретного чата."""
    if chat_id is None:
        lang, scope, with_backup = DEFAULT_LANG, None, False
    else:
        lang, scope, with_backup = chat_lang(chat_id), {"type": "chat", "chat_id": int(chat_id)}, chat_id in BACKUP_CHAT_IDS
    cmds = [{"command": k, "description": title(k, lang)[:256]} for k in ACTIONS]
    if with_backup and BACKUP_MODE in HELP_BACKUP:
        cmds.append({"command": "backup", "description": BACKUP_TITLE[lang]})
    if chat_id is not None and chat_id in ADMIN_CHAT_IDS and (DEPLOY_TARGETS or CLONE_TARGETS):
        cmds += [{"command": k, "description": v[lang]} for k, v in DEPLOY_TITLE.items()]
    cmds.append({"command": "help", "description": HELP_TITLE[lang]})
    try:
        tg("setMyCommands", commands=cmds, **({"scope": scope} if scope else {}))
        _menu_done.add(chat_id)
    except urllib.error.HTTPError as e:  # чат ещё не начинал диалог с ботом — повторим при первом сообщении
        print(f"setMyCommands {scope}: {e.read()[:200]!r}", file=sys.stderr, flush=True)


def handle_update(u):
    if "callback_query" in u:
        q = u["callback_query"]
        chat_id = str(q["message"]["chat"]["id"])
        try:
            tg("answerCallbackQuery", callback_query_id=q["id"])
        except Exception:  # noqa: BLE001
            pass
        if chat_id not in ALLOWED:
            return
        set_lang(chat_lang(chat_id))
        action, *args = q.get("data", "").split(":")
        if action == "backup" and len(args) == 2:
            if chat_id in BACKUP_CHAT_IDS and BACKUP_MODE in HELP_BACKUP:
                lang = chat_lang(chat_id)
                threading.Thread(target=lambda: (set_lang(lang), backup_callback(chat_id, q["message"]["message_id"], *args)),
                                 daemon=True).start()
            return
        if action == "dep" and args:
            lang = chat_lang(chat_id)
            threading.Thread(target=lambda: (set_lang(lang), deploy_callback(chat_id, q["message"]["message_id"], args)),
                             daemon=True).start()
            return
        if action in ACTIONS:
            targets, extra, err = parse_args(action, args)
            if targets and not err:
                threading.Thread(target=run_action, args=(chat_id, action, targets, extra), daemon=True).start()
        return

    msg = u.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not text or not chat_id:
        return
    if chat_id not in ALLOWED:
        print(f"denied chat_id={chat_id}", file=sys.stderr, flush=True)
        send(chat_id, "Нет доступа.")
        return
    set_lang(chat_lang(chat_id))
    if chat_id not in _menu_done:
        set_menu(chat_id)
    parts = text.split()
    cmd = parts[0].split("@")[0].lstrip("/").lower()
    if cmd == "backup" and BACKUP_MODE not in HELP_BACKUP:  # /backup выключен в этом инстансе (BACKUP_MODE=none)
        send(chat_id, help_text())
        return
    if cmd == "backup":
        if chat_id not in BACKUP_CHAT_IDS:
            send(chat_id, L("Команда /backup доступна только администратору в личном чате с ботом.",
                            "Команда /backup доступна лише адміністратору в особистому чаті з ботом."))
            return
        lang = chat_lang(chat_id)
        arg = parts[1] if len(parts) > 1 else ""
        threading.Thread(target=lambda: (set_lang(lang), backup_plan(chat_id, arg)), daemon=True).start()
        return
    if cmd in DEPLOY_TITLE and (DEPLOY_TARGETS or CLONE_TARGETS):
        lang = chat_lang(chat_id)
        threading.Thread(target=lambda: (set_lang(lang), deploy_command(chat_id, cmd, parts[1:])), daemon=True).start()
        return
    if cmd in ACTIONS:
        targets, extra, err = parse_args(cmd, parts[1:])
        if err:
            send(chat_id, html.escape(err))
        elif targets:
            threading.Thread(target=run_action, args=(chat_id, cmd, targets, extra), daemon=True).start()
        else:
            send(chat_id, L("Для какого сервера — ", "Для якого сервера — ") + f"{html.escape(title(cmd).split(' (')[0])}?",
                 reply_markup=target_keyboard(cmd, extra if cmd in ("log", "users") else None))
    else:
        send(chat_id, help_text())


def main_loop():
    if not TOKEN:
        sys.exit("MONITOR_BOT_TOKEN не задан (.env)")
    if not ALLOWED:
        sys.exit("ALLOWED_CHAT_IDS не задан (.env)")
    for attempt in range(10):  # сеть до Telegram иногда рвётся — не падаем на старте
        try:
            set_menu(None)
            for c in ALLOWED:
                set_menu(c)
            me = tg("getMe")["result"]
            break
        except Exception as e:  # noqa: BLE001
            print("startup error:", repr(e), file=sys.stderr, flush=True)
            time.sleep(5 * (attempt + 1))
    else:
        sys.exit("не удалось подключиться к Telegram")
    print(f"started as @{me['username']}, allowed: {sorted(ALLOWED)}, uk: {sorted(UK_CHAT_IDS)}", flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
            if offset:
                params["offset"] = offset
            for u in tg("getUpdates", retries=1, **params).get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception as e:  # noqa: BLE001
                    print("handler error:", repr(e), file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            print("poll error:", repr(e), file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--p2s-env"]:  # для ops/prod2stage.sh при ручном запуске: eval "$(python3 bot.py --p2s-env)"
        import shlex
        if BACKUP_MODE != "prod2stage":
            sys.exit("backup.mode в targets.json не prod2stage")
        for k, v in p2s_env().items():
            print(f"export {k}={shlex.quote(str(v))}")
    elif len(sys.argv) >= 4 and sys.argv[1] == "--cli":
        action, target = sys.argv[2], sys.argv[3]
        fn = ACTIONS[action][0]
        res = fn(target, parse_count(sys.argv[4])) if len(sys.argv) > 4 else fn(target)
        for kind, text in res:
            print(text if kind != "b" else f"== {text}")
            print()
    else:
        main_loop()
