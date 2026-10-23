"""Диагностика запуска без записи API-ключей и секретных URL."""
from datetime import datetime
import json
import re
import sys
import traceback

from .paths import CONFIG_FILE, RUNTIME_DIR


def redact(text):
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))
        for name in ('api_key', 'secret_path'):
            value = config.get(name)
            if isinstance(value, str) and value:
                text = text.replace(value, '[СКРЫТО]')
    except (OSError, ValueError, AttributeError):
        pass
    text = re.sub(r'(?i)(--apiKey(?:=|\s+)|X-Api-Key\s*:\s*)\S+', r'\1[СКРЫТО]', text)
    return text


def report_error(exc):
    message = redact(str(exc))
    print(f'Статус: ОШИБКА ({type(exc).__name__}) — {message}', file=sys.stderr)
    print('Подробности: runtime/launcher.log. Для проблем с компонентами запустите install_dependencies.bat.', file=sys.stderr)
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with (RUNTIME_DIR/'launcher.log').open('a', encoding='utf-8') as log:
            log.write(datetime.now().astimezone().isoformat()+'\n')
            log.write(redact(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))+'\n')
    except OSError:
        print('Не удалось записать runtime/launcher.log.', file=sys.stderr)
