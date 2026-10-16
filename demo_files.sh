#!/usr/bin/env bash
# Показ и проверка Дня 34 одной командой: ассистент для работы с файлами проекта (DEMO-CHROME.md,
# «показ34»). Поднимает веб агента (web.py) на свободном порту 127.0.0.1 со СВОЕЙ временной памятью:
# там свой клон CRM на базовом коммите и ветка посеянных случаев — рабочая копия CRM владельца и
# память проекта не трогаются. Chrome — свой профиль, headless; запись — кадрами самого Chrome
# (CDP Page.startScreencast): в неё попадает только страница. После показа временная память
# удаляется. Ожидания модели — в ускоренной копии рядом с записью (×6, demo_speedup.py).
#
# Сцены (/chat, команда /files): найти все использования scan_file; обновить документацию по
# изменениям последнего коммита (ветка случаев) → «Применить»; проверить секреты и SQL → «Отклонить».
#
# Имена переменных латиницей: bash не понимает кириллицу в идентификаторах.
#
# Запуск:
#   bash demo_files.sh                 # показ с записью в demo/
#   bash demo_files.sh --no-record     # показ без записи
#   bash demo_files.sh --check         # проверка: режим «файлы», без подписей и пауз
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/day34-показ-XXXX")"
PAUSE=3000
RECORD=1
MODE="показ34"
free_port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }
DEBUG_PORT="$(free_port)"
APP_PORT="$(free_port)"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-record|--без-записи) RECORD=0 ;;
    --check|--проверка) MODE="файлы"; RECORD=0 ;;
    --pause|--пауза) PAUSE="$2"; shift ;;
    -h|--help|--помощь) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Не знаю ключа «$1»."; exit 1 ;;
  esac
  shift
done
command -v google-chrome >/dev/null 2>&1 || { echo "Не нашёл google-chrome"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "Нет node — им запускается browser.mjs"; exit 1; }

cleanup() {
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null && wait "$CHROME_PID" 2>/dev/null
  # web.py с перезагрузчиком Flask — два процесса: гасится вся группа.
  [ -n "${APP_PID:-}" ] && kill -- "-$APP_PID" 2>/dev/null && wait "$APP_PID" 2>/dev/null
  sleep 1      # Chrome дописывает профиль после сигнала — иначе rm оставляет каталог
  rm -rf "${SCRATCH:?}"
}
trap cleanup EXIT INT TERM

export MEMORY_DIR="$SCRATCH/memory"
mkdir -p "$MEMORY_DIR"
echo "1/4 Копия CRM и ветка посеянных случаев во временной памяти…"
(cd "$ROOT" && "$PY" cli.py --файлы-случаи) | tail -1
echo "    веб агента: http://127.0.0.1:$APP_PORT/chat"
(cd "$ROOT" && PORT="$APP_PORT" exec setsid "$PY" web.py) > "$SCRATCH/web.log" 2>&1 &
APP_PID=$!
for _ in $(seq 1 60); do curl -s -o /dev/null "http://127.0.0.1:$APP_PORT/chat" && break; sleep 0.5; done
curl -s -o /dev/null "http://127.0.0.1:$APP_PORT/chat" || { echo "Веб не поднялся:"; tail -5 "$SCRATCH/web.log"; exit 1; }

echo "2/4 Chrome со своим профилем, без окна на экране (ваш профиль и вкладки не трогаю)…"
mkdir -p "$SCRATCH/chrome/Default"
printf '%s' '{"credentials_enable_service": false, "profile": {"password_manager_enabled": false, "exit_type": "Normal", "exited_cleanly": true}}' \
  > "$SCRATCH/chrome/Default/Preferences"
google-chrome --headless=new --remote-debugging-port="$DEBUG_PORT" --user-data-dir="$SCRATCH/chrome" \
  --no-first-run --no-default-browser-check --hide-crash-restore-bubble --lang=ru \
  --window-size=1600,980 "about:blank" > "$SCRATCH/chrome.log" 2>&1 &
CHROME_PID=$!
for _ in $(seq 1 40); do curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" && break; sleep 0.5; done
curl -s -o /dev/null "http://127.0.0.1:$DEBUG_PORT/json/version" || { echo "Chrome не отдал порт отладки"; exit 1; }

FILE=""
if [ "$RECORD" = 1 ]; then
  mkdir -p "$ROOT/demo"
  FILE="$ROOT/demo/файлы-$(date +%Y%m%d-%H%M).mp4"
  echo "3/4 Запись страницы через CDP: $FILE"
else
  echo "3/4 Запись выключена"
fi
CDP="http://127.0.0.1:$DEBUG_PORT" PAUSE="$PAUSE" MARKS="$SCRATCH/marks.json" RECORD_CDP="$FILE" \
  node "$ROOT/browser.mjs" "http://127.0.0.1:$APP_PORT/chat" ds-flash "" "$MODE"
SHOW_CODE=$?

if [ "$RECORD" = 1 ] && [ -f "$FILE" ]; then
  REC_START_MS="$(cat "$FILE.start" 2>/dev/null || echo 0)"
  rm -f "$FILE.start"
  echo "4/4 Запись: $FILE ($(du -h "$FILE" | cut -f1))"
  # Ожидания модели (метки водителя) — ускоренная копия ×6 рядом с записью (demo_speedup.py).
  if [ -f "$SCRATCH/marks.json" ]; then
    "$PY" - "$SCRATCH/marks.json" "$REC_START_MS" "${FILE%.mp4}-метки.json" <<'PYCODE'
import json, sys
данные = json.load(open(sys.argv[1], encoding="utf-8"))
данные["старт_записи"] = int(sys.argv[2])
json.dump(данные, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
PYCODE
    "$PY" "$ROOT/demo_speedup.py" "$FILE" "${FILE%.mp4}-метки.json" --раз 6 --надпись "ассистент работает с файлами (облако ds-flash)" | tail -1
  fi
else
  echo "4/4 Готово"
fi
echo "Показ окончен (код водителя $SHOW_CODE)."
exit "$SHOW_CODE"
