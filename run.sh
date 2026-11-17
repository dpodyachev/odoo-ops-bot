#!/usr/bin/env bash
# Запуск бота в фоне с автоперезапуском при падении. Лог: bot.log
# ./run.sh start | stop | status | restart
cd "$(dirname "$0")" || exit 1
PIDF=bot.pid
case "${1:-start}" in
  start)
    if [ -f $PIDF ] && kill -0 "$(cat $PIDF)" 2>/dev/null; then echo "уже запущен (pid $(cat $PIDF))"; exit 0; fi
    nohup bash -c 'while true; do python3 bot.py; echo "$(date -Is) bot exited ($?), restart in 10s"; sleep 10; done' >> bot.log 2>&1 &
    echo $! > $PIDF; sleep 3; tail -n 3 bot.log ;;
  stop)
    # без pkill/ps: убиваем цикл-обёртку и все python3 bot.py этой папки через /proc
    [ -f $PIDF ] && kill "$(cat $PIDF)" 2>/dev/null
    for p in /proc/[0-9]*; do
      [ "$(readlink $p/cwd 2>/dev/null)" = "$PWD" ] || continue
      case "$(tr '\0' ' ' < $p/cmdline 2>/dev/null)" in "python3 bot.py"*) kill "${p#/proc/}" 2>/dev/null;; esac
    done
    rm -f $PIDF; echo остановлен ;;
  status)
    [ -f $PIDF ] && kill -0 "$(cat $PIDF)" 2>/dev/null && echo "работает (pid $(cat $PIDF))" || echo "не запущен"; tail -n 5 bot.log 2>/dev/null ;;
  restart) "$0" stop; "$0" start ;;
esac
