#!/usr/bin/env bash
# Копия боевой базы prod → stage: новая база <префикс>ДД.ММ.ГГ (дата копии, P2S_TZ), filestore,
# обезвреживание, переключение stage на новую базу, обновление модулей, проверка.
# Запускается на машине бота (здесь есть SSH-доступ к обоим серверам). Вызывается командой /backup бота.
#
#   ops/prod2stage.sh plan [--modules m1,m2|all|-]
#   ops/prod2stage.sh run  [--modules m1,m2|all|-] [--db ИМЯ] [--drop db1,db2]
#
# Вывод для бота: строки "##PLAN key=value", "##STEP n/N текст", "##INFO текст", "##OK текст", "##FAIL текст".
# Всё остальное — подробный лог.
#
# Настройки — из targets.json (секция "backup" + серверы source/dest) через переменные P2S_*:
# бот передаёт их сам; при ручном запуске скрипт берёт их из `python3 bot.py --p2s-env`.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

if [ -z "${P2S_PROD_SSH:-}" ]; then
  P2S_ENV=$(python3 bot.py --p2s-env) || { echo "##FAIL нет настроек prod2stage в targets.json"; exit 2; }
  eval "$P2S_ENV"
fi
SSH_KEY=${SSH_KEY:-/root/.ssh/id_ed25519}
PROD=$P2S_PROD_SSH                 # SSH источника (prod)
STAGE=$P2S_STAGE_SSH               # SSH приёмника (stage)
PROD_DB=$P2S_PROD_DB               # база на prod
PREFIX=$P2S_DB_PREFIX              # префикс новой базы: <PREFIX>ДД.ММ.ГГ
STAGE_URL=$P2S_BASE_URL            # web.base.url на stage
CHECK_URL=$P2S_CHECK_URL           # внешняя проверка stage после запуска
CONF=$P2S_STAGE_CONF               # конфиг Odoo на stage
FS_PROD=$P2S_PROD_FILESTORE        # папка filestore на prod
FILESTORE=$P2S_STAGE_FILESTORE     # папка filestore на stage
BACKUPS=$P2S_DUMP_DIR              # куда класть дамп на stage
SVC=$P2S_SVC                       # systemd-юнит Odoo на stage
ODOO_BIN=$P2S_ODOO_BIN             # odoo-bin на stage
HTTP_PORT=$P2S_HTTP_PORT           # порт Odoo на stage (проверка 127.0.0.1)
DB_OWNER=$P2S_DB_OWNER             # владелец новой базы (роль Odoo в PostgreSQL)
TZN=$P2S_TZ                        # часовой пояс для даты в имени базы
NEUTRALIZE_SQL=$P2S_NEUTRALIZE_SQL # SQL обезвреживания (путь от папки бота)
[[ "$PREFIX" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "##FAIL некорректный P2S_DB_PREFIX: $PREFIX"; exit 2; }
[ -r "$NEUTRALIZE_SQL" ] || { echo "##FAIL нет файла $NEUTRALIZE_SQL"; exit 2; }
TOTAL=8

SSHO=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new -i "$SSH_KEY")
p() { ssh "${SSHO[@]}" "$PROD" "$@"; }
s() { ssh "${SSHO[@]}" "$STAGE" "$@"; }
step() { echo "##STEP $1/$TOTAL $2"; }
info() { echo "##INFO $*"; }
gbs() { awk -v b="$1" 'BEGIN { printf "%.1f ГБ", b / 1024 / 1024 / 1024 }'; }

MODE=${1:-plan}; shift || true
MODULES=${STAGE_UPDATE_MODULES:--}
NEW_DB=""
DROP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --modules) MODULES=$2; shift 2 ;;
    --db) NEW_DB=$2; shift 2 ;;
    --drop) DROP=$2; shift 2 ;;
    *) echo "##FAIL неизвестный параметр: $1"; exit 2 ;;
  esac
done
# имена модулей: только буквы/цифры/_ и запятые; "all" или "-" (не обновлять)
if ! [[ "$MODULES" =~ ^(-|all|[a-z0-9_]+(,[a-z0-9_]+)*)$ ]]; then echo "##FAIL некорректный список модулей: $MODULES"; exit 2; fi
COPY_RE="${PREFIX}[0-9]{2}\.[0-9]{2}\.[0-9]{2}(_[0-9]{4})?"   # имена копий: <PREFIX>ДД.ММ.ГГ[_ЧЧММ]
if [ -n "$DROP" ] && ! [[ "$DROP" =~ ^$COPY_RE(,$COPY_RE)*$ ]]; then
  echo "##FAIL некорректный список баз для удаления: $DROP"; exit 2
fi

# ------------------------------------------------------------------ сбор данных (общий для plan и run)
collect() {
  OLD_DB=$(s "sed -n 's/^[[:space:]]*db_name[[:space:]]*=[[:space:]]*//p' $CONF | tail -1") || { echo "##FAIL нет SSH до stage"; exit 1; }
  STAGE_DBS=$(s "psql -X -At -d postgres -c \"select datname from pg_database where not datistemplate and datname<>'postgres' order by 1\"") || exit 1
  PROD_DB_BYTES=$(p "psql -X -At -d $PROD_DB -c 'select pg_database_size(current_database())'") || { echo "##FAIL нет SSH/psql до prod"; exit 1; }
  PROD_FS_BYTES=$(p "du -sb $FS_PROD/$PROD_DB | cut -f1") || exit 1
  STAGE_FREE=$(s "df -B1 --output=avail / | tail -1 | tr -d ' '") || exit 1
  if [ -z "$NEW_DB" ]; then
    NEW_DB="${PREFIX}$(TZ=$TZN date +%d.%m.%y)"
    if printf '%s\n' "$STAGE_DBS" | grep -qx "$NEW_DB"; then NEW_DB="${NEW_DB}_$(TZ=$TZN date +%H%M)"; fi
  fi
  # база после восстановления обычно меньше боевой (без раздувания), + дамп ~5%, + новые файлы filestore, + 2 ГБ запаса
  NEED=$(( PROD_DB_BYTES + PROD_DB_BYTES / 20 + 2 * 1024 * 1024 * 1024 ))
  # кандидаты на удаление: старые копии <PREFIX>ДД.ММ.ГГ, кроме текущей и новой
  CANDIDATES=$(printf '%s\n' "$STAGE_DBS" | grep -E "^$COPY_RE\$" | grep -vx "$OLD_DB" | grep -vx "$NEW_DB" | paste -sd, -)
}

if [ "$MODE" = plan ]; then
  collect
  echo "##PLAN new_db=$NEW_DB"
  echo "##PLAN old_db=$OLD_DB"
  echo "##PLAN stage_dbs=$(printf '%s\n' "$STAGE_DBS" | paste -sd, -)"
  echo "##PLAN drop_candidates=$CANDIDATES"
  echo "##PLAN modules=$MODULES"
  echo "##PLAN prod_db=$(gbs "$PROD_DB_BYTES")"
  echo "##PLAN prod_filestore=$(gbs "$PROD_FS_BYTES")"
  echo "##PLAN stage_free=$(gbs "$STAGE_FREE")"
  echo "##PLAN need=$(gbs "$NEED")"
  [ "$STAGE_FREE" -gt "$NEED" ] && echo "##PLAN space_ok=yes" || echo "##PLAN space_ok=no"
  exit 0
fi
[ "$MODE" = run ] || { echo "##FAIL режим: plan или run"; exit 2; }

TS=$(TZ=$TZN date +%Y%m%d-%H%M)
DUMP="$BACKUPS/prod2stage_${NEW_DB}.dump"
UPDLOG="$BACKUPS/prod2stage_${NEW_DB}_update.log"
CONF_BAK="$CONF.bak-prod2stage-$TS"
SWITCHED=0

cleanup_new() {  # откат до переключения: убрать новую базу, её filestore и дамп
  s "dropdb --if-exists '$NEW_DB'; rm -rf '$FILESTORE/$NEW_DB' '$DUMP'" || true
}
revert_switch() {  # откат после переключения: вернуть конфиг и старую базу
  s "sudo -n systemctl stop $SVC; cp -p '$CONF_BAK' '$CONF' && sudo -n systemctl start $SVC" || true
  info "конфиг возвращён, stage снова на базе $OLD_DB"
}
fail() {
  echo "##FAIL $*"
  if [ "$SWITCHED" = 1 ]; then revert_switch; info "новая база $NEW_DB оставлена для разбора"; else cleanup_new; info "новая база удалена, stage не менялся"; fi
  exit 1
}

# ------------------------------------------------------------------ 1. проверки
step 1 "Проверки: доступ, место, имя базы"
collect
info "prod: база $PROD_DB $(gbs "$PROD_DB_BYTES"), filestore $(gbs "$PROD_FS_BYTES"); stage: свободно $(gbs "$STAGE_FREE"), нужно ~$(gbs "$NEED")"
info "stage сейчас на базе $OLD_DB, новая база: $NEW_DB, модули: $MODULES"
printf '%s\n' "$STAGE_DBS" | grep -qx "$NEW_DB" && { echo "##FAIL база $NEW_DB уже есть на stage"; exit 1; }
[ "$STAGE_FREE" -gt "$NEED" ] || { echo "##FAIL мало места на stage: свободно $(gbs "$STAGE_FREE"), нужно ~$(gbs "$NEED")"; exit 1; }
for d in ${DROP//,/ }; do
  [ "$d" = "$OLD_DB" ] && { echo "##FAIL нельзя удалять текущую базу stage $OLD_DB"; exit 1; }
done

# ------------------------------------------------------------------ 2. дамп prod → stage
step 2 "Дамп базы prod (pg_dump) и передача на stage"
t0=$(date +%s)
p "pg_dump -Fc -Z6 $PROD_DB" | s "cat > '$DUMP'"
rc=("${PIPESTATUS[@]}")
[ "${rc[0]}" = 0 ] && [ "${rc[1]}" = 0 ] || fail "pg_dump/передача: коды ${rc[*]}"
s "pg_restore -l '$DUMP' >/dev/null" || fail "дамп повреждён"
info "дамп $(s "du -h '$DUMP' | cut -f1") за $(( $(date +%s) - t0 )) с"

# ------------------------------------------------------------------ 3. восстановление
step 3 "Восстановление в новую базу $NEW_DB"
t0=$(date +%s)
s "createdb -O '$DB_OWNER' '$NEW_DB'" || fail "createdb $NEW_DB"
s "pg_restore -j 2 --no-owner --role='$DB_OWNER' -d '$NEW_DB' '$DUMP' 2> '$BACKUPS/prod2stage_${NEW_DB}_restore.err'; echo rc=\$?" | tail -1
N_MOD=$(s "psql -X -At -d '$NEW_DB' -c \"select count(*) from ir_module_module where state='installed'\"" 2>/dev/null || echo 0)
[ "${N_MOD:-0}" -gt 10 ] || fail "после восстановления в базе нет установленных модулей (ошибки: $BACKUPS/prod2stage_${NEW_DB}_restore.err)"
info "восстановлено за $(( $(date +%s) - t0 )) с, модулей установлено: $N_MOD, ошибок pg_restore: $(s "grep -ci 'error' '$BACKUPS/prod2stage_${NEW_DB}_restore.err' || true")"

# ------------------------------------------------------------------ 4. обезвреживание
neutralize() {
  s "cat > /tmp/stage-neutralize.sql && psql -X -q -v ON_ERROR_STOP=1 -v stage_url='$STAGE_URL' -d '$NEW_DB' -f /tmp/stage-neutralize.sql; rc=\$?; rm -f /tmp/stage-neutralize.sql; exit \$rc" < "$NEUTRALIZE_SQL"
}
step 4 "Обезвреживание: почта, интеграции, cron, web.base.url"
neutralize || fail "обезвреживание (stage-neutralize.sql)"

# ------------------------------------------------------------------ 5. filestore
step 5 "Filestore: копия с prod (только новые файлы)"
t0=$(date +%s)
s "if [ -d '$FILESTORE/$OLD_DB' ]; then cp -al '$FILESTORE/$OLD_DB' '$FILESTORE/$NEW_DB'; else mkdir -p '$FILESTORE/$NEW_DB'; fi" || fail "подготовка filestore"
mkdir -p tmp
p "cd $FS_PROD/$PROD_DB && find . -type f | LC_ALL=C sort" > "tmp/fs_prod.txt" || fail "список файлов prod"
s "cd '$FILESTORE/$NEW_DB' && find . -type f | LC_ALL=C sort" > "tmp/fs_stage.txt" || fail "список файлов stage"
LC_ALL=C comm -23 tmp/fs_prod.txt tmp/fs_stage.txt > tmp/fs_missing.txt
LC_ALL=C comm -13 tmp/fs_prod.txt tmp/fs_stage.txt > tmp/fs_extra.txt
info "файлов на prod: $(wc -l < tmp/fs_prod.txt), докопировать: $(wc -l < tmp/fs_missing.txt), лишних убрать: $(wc -l < tmp/fs_extra.txt)"
if [ -s tmp/fs_missing.txt ]; then
  p "cd $FS_PROD/$PROD_DB && tar -cf - -T -" < tmp/fs_missing.txt | s "cd '$FILESTORE/$NEW_DB' && tar -xf -"
  rc=("${PIPESTATUS[@]}"); [ "${rc[0]}" = 0 ] && [ "${rc[1]}" = 0 ] || fail "передача filestore: коды ${rc[*]}"
fi
if [ -s tmp/fs_extra.txt ]; then
  s "cd '$FILESTORE/$NEW_DB' && xargs -d '\n' rm -f" < tmp/fs_extra.txt || true
fi
rm -f tmp/fs_*.txt
info "filestore готов за $(( $(date +%s) - t0 )) с"

# ------------------------------------------------------------------ 6. переключение и обновление модулей
step 6 "Остановка Odoo, переключение на $NEW_DB, обновление модулей ($MODULES)"
ESC=${NEW_DB//./\\\\.}
s "cp -p '$CONF' '$CONF_BAK'" || fail "резервная копия конфига"
s "sudo -n systemctl stop $SVC" || fail "остановка $SVC"
SWITCHED=1
s "sed -i -E 's/^([[:space:]]*db_name[[:space:]]*=).*/\\1 $NEW_DB/; s/^([[:space:]]*dbfilter[[:space:]]*=).*/\\1 ^$ESC\$/' '$CONF' && grep -E '^[[:space:]]*(db_name|dbfilter)' '$CONF'" || fail "правка конфига"
if [ "$MODULES" != "-" ]; then
  t0=$(date +%s)
  s "'$ODOO_BIN' -c '$CONF' -d '$NEW_DB' -u '$MODULES' --stop-after-init --workers=0 --no-http --i18n-overwrite --without-demo=all --logfile='$UPDLOG'" \
    || fail "обновление модулей $MODULES (лог: $UPDLOG)"
  ERRS=$(s "grep -cE ' (ERROR|CRITICAL) ' '$UPDLOG'" || true)
  info "модули обновлены за $(( $(date +%s) - t0 )) с, ERROR/CRITICAL в логе обновления: ${ERRS:-0} ($UPDLOG)"
  neutralize || fail "повторное обезвреживание после обновления модулей"
fi

# ------------------------------------------------------------------ 7. запуск и проверка
step 7 "Запуск Odoo и проверка"
s "sudo -n systemctl start $SVC" || fail "запуск $SVC"
ok=0
for _ in $(seq 1 40); do
  sleep 3
  code=$(s "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$HTTP_PORT/web/login" 2>/dev/null || echo 000)
  [ "$code" = 200 ] && { ok=1; break; }
done
[ "$ok" = 1 ] || fail "Odoo не ответил 200 на /web/login за 2 минуты (последний код: $code)"
pub=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$CHECK_URL" || echo 000)
[ "$pub" = 200 ] || fail "снаружи $CHECK_URL отвечает $pub"
info "Odoo запущен на $NEW_DB, $CHECK_URL → 200"
SWITCHED=2  # дальше откатывать нечего

# ------------------------------------------------------------------ 8. уборка
step 8 "Уборка: дамп, старые базы"
s "rm -f '$DUMP'"
for d in ${DROP//,/ }; do
  [ "$d" = "$NEW_DB" ] || [ "$d" = "$OLD_DB" ] && continue
  if s "dropdb --if-exists '$d' && rm -rf '$FILESTORE/$d'"; then info "удалена база $d и её filestore"; else info "не удалось удалить $d"; fi
done
info "свободно на stage: $(gbs "$(s "df -B1 --output=avail / | tail -1 | tr -d ' '")")"
echo "##OK stage переключён на $NEW_DB (было: $OLD_DB). Резервная копия конфига: $CONF_BAK"
