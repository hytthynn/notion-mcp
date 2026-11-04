#!/usr/bin/env bash
# Единая точка входа для macOS и Linux (аналог bootstrap.ps1 для Windows).
# Использование: scripts/bootstrap.sh install|setup|start|stop
set -u
ACTION="${1:-install}"
case "$ACTION" in
  install) LABEL='ПРОВЕРКА И УСТАНОВКА' ;;
  setup)   LABEL='НАСТРОЙКА ПОДКЛЮЧЕНИЯ' ;;
  start)   LABEL='ЗАПУСК MCP-СЕРВЕРА' ;;
  stop)    LABEL='ОСТАНОВКА MCP-СЕРВЕРА' ;;
  *) echo "Неизвестное действие: $ACTION (install|setup|start|stop)" >&2; exit 2 ;;
esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

if [ -t 1 ]; then
  CYAN=$'\033[36m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  CYAN=''; DIM=''; GREEN=''; YELLOW=''; RED=''; BOLD=''; RESET=''
fi
LINE='  ────────────────────────────────────────────────────────────────────────'

header() {
  printf '\n%s  ╔══════════════════════════════════════════════════════════════════════╗%s\n' "$CYAN" "$RESET"
  printf '%s  ║                         NOTION · MCP МОСТ                            ║%s\n' "$CYAN" "$RESET"
  printf '%s  ╚══════════════════════════════════════════════════════════════════════╝%s\n' "$CYAN" "$RESET"
  printf '    %s%s%s\n    %sПапка: %s%s\n%s%s%s\n\n' "$BOLD" "$LABEL" "$RESET" "$DIM" "$ROOT" "$RESET" "$CYAN" "$LINE" "$RESET"
}

footer() {
  printf '\n%s%s%s\n' "$CYAN" "$LINE" "$RESET"
  case "$1" in
    0)   printf '    %s✓ ГОТОВО  ·  Операция завершена без ошибок%s\n' "$GREEN" "$RESET" ;;
    130) printf '    %s■ ОТМЕНЕНО  ·  Выполнение прервано пользователем%s\n' "$YELLOW" "$RESET" ;;
    *)   printf '    %s! ОШИБКА  ·  Код завершения: %s%s\n' "$RED" "$1" "$RESET"
         printf '    %sПодробности — в сообщениях выше и в папке runtime.%s\n' "$DIM" "$RESET" ;;
  esac
  printf '%s%s%s\n\n' "$CYAN" "$LINE" "$RESET"
}

finish() { footer "$1"; exit "$1"; }

ask() {
  printf '%s [да/НЕТ]: ' "$1"
  read -r answer || return 1
  case "$answer" in y|Y|yes|Yes|YES|д|Д|да|Да|ДА) return 0 ;; *) return 1 ;; esac
}

python_ok() {
  # Без Xcode Command Line Tools /usr/bin/python3 в macOS открывает окно установки — пропускаем.
  if [ "$1" = /usr/bin/python3 ] && [ "$(uname -s)" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
    return 1
  fi
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_python() {
  local name path
  for name in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python; do
    path="$(command -v "$name" 2>/dev/null)" || continue
    if python_ok "$path"; then printf '%s\n' "$path"; return 0; fi
  done
  for path in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$path" ] && python_ok "$path"; then printf '%s\n' "$path"; return 0; fi
  done
  return 1
}

receipt_value() {
  sed -n "s/^ *\"$1\": *\"\\([^\"]*\\)\".*/\\1/p" "$ROOT/config/dependencies.checked.json" 2>/dev/null | head -n 1
}

install_python() {
  printf '%s[НЕ НАЙДЕНО] Требуется Python 3.10 или новее.%s\n' "$YELLOW" "$RESET"
  if [ "$(uname -s)" = Darwin ]; then
    local brew
    brew="$(command -v brew || ls /opt/homebrew/bin/brew /usr/local/bin/brew 2>/dev/null | head -n 1)"
    if [ -n "$brew" ] && ask 'Установить Python 3.13 через Homebrew?'; then
      "$brew" install python@3.13 && return 0
    fi
    echo 'Установите Python: https://www.python.org/downloads/macos/ и повторите запуск.'
  elif command -v apt-get >/dev/null 2>&1; then echo 'Установите Python: sudo apt-get install python3'
  elif command -v dnf >/dev/null 2>&1;     then echo 'Установите Python: sudo dnf install python3'
  elif command -v pacman >/dev/null 2>&1;  then echo 'Установите Python: sudo pacman -S python'
  elif command -v zypper >/dev/null 2>&1;  then echo 'Установите Python: sudo zypper install python3'
  else echo 'Установите Python 3.10+: https://www.python.org/downloads/'
  fi
  return 1
}

header
PY=''
if [ "$ACTION" = install ]; then
  PY="$(find_python)" || { install_python && PY="$(find_python)"; } || finish 1
else
  # Только читаем результат установщика; проверки версий выполняет install.
  PY="$(receipt_value python_binary)"
  if [ -n "$PY" ] && [ -x "$PY" ]; then
    printf '    %s✓ Все зависимости установлены · проверены %s%s\n\n' "$GREEN" "$(receipt_value checked_at)" "$RESET"
  elif [ "$ACTION" = stop ]; then
    printf '    %sНет сохранённой проверки; остановка всё равно доступна.%s\n' "$YELLOW" "$RESET"
    PY="$(find_python)" || { printf '%sPython не найден.%s\n' "$RED" "$RESET"; finish 1; }
  else
    printf '    %sСначала запустите ./install_dependencies.sh%s\n' "$RED" "$RESET"
    finish 1
  fi
fi

# Ctrl+C получает Python (он корректно останавливает мост); bash лишь дожидается его.
trap ':' INT
"$PY" "$ROOT/scripts/manage.py" "$ACTION"
finish $?
