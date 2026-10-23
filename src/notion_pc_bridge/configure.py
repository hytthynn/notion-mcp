#!/usr/bin/env python3
"""Interactive local-only setup. Never send credentials to chat."""
import getpass
import json
import os
from pathlib import Path
import secrets
import sys
import re
from .bridge import atomic_write
from .paths import ROOT, CONFIG_FILE as CONFIG


def main():
    if sys.version_info < (3, 10):
        raise SystemExit('Требуется Python 3.10 или новее.')
    print('Настройка MCP-моста для Notion (Windows)')
    print('ПОЛНЫЙ ДОСТУП К ПК: все папки и терминал в пределах прав вашей учётной записи Windows.')
    print('Запускайте от обычного пользователя, не от администратора. Для изоляции используйте виртуальную машину.')
    cfg = json.loads(CONFIG.read_text(encoding='utf-8-sig')) if CONFIG.exists() else json.loads((ROOT/'config/config.example.json').read_text(encoding='utf-8'))
    if not isinstance(cfg, dict) or not all(isinstance(cfg.get(k), dict) for k in ('ngrok', 'desktop_commander')):
        raise SystemExit('Неверная структура конфигурации; файлы не изменены.')
    cfg['access_mode'] = 'full'
    cfg['desktop_commander']['allowedDirectories'] = []
    cfg['desktop_commander']['defaultShell'] = 'cmd.exe'
    cfg['desktop_commander']['telemetryEnabled'] = False
    cfg['require_api_key'] = True
    key = cfg.get('api_key', '')
    cfg['api_key'] = key if isinstance(key, str) and len(key) >= 32 and key.isascii() and not any(c.isspace() for c in key) else secrets.token_urlsafe(48)
    secret = cfg.get('secret_path', '')
    cfg['secret_path'] = secret if isinstance(secret, str) and re.fullmatch(r'[A-Za-z0-9_-]{24,128}', secret) else 'mcp-' + secrets.token_hex(24)
    cfg['stateless'] = True
    cfg['ngrok'].setdefault('config_path', 'config/ngrok.yml')
    cfg['ngrok']['public_url'] = ''
    token = getpass.getpass('Токен ngrok (ввод скрыт; Enter — использовать существующий YAML или NGROK_AUTHTOKEN): ').strip()
    if token:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', token):
            raise SystemExit('Неверный формат токена ngrok. Вставьте только токен, а не всю команду.')
        cfg['ngrok']['config_path'] = 'config/ngrok.yml'
        cfg['ngrok']['authtoken'] = ''
    else:
        from .bridge import parse_ngrok_yml
        config_path = Path(cfg['ngrok'].get('config_path') or 'config/ngrok.yml')
        if not config_path.is_absolute():
            config_path = ROOT / config_path
        file_token, _ = parse_ngrok_yml(config_path)
        if not file_token and not os.environ.get('NGROK_AUTHTOKEN') and not cfg['ngrok'].get('authtoken'):
            raise SystemExit('Токен ngrok не настроен. Повторите настройку или добавьте config/ngrok.yml.')
    domain = input('Закреплённый домен ngrok (необязательно, без https://; Enter — взять из YAML или назначить автоматически): ').strip()
    if domain and ('.' not in domain or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', domain)):
        raise SystemExit('Введите только имя домена, например example.ngrok-free.dev.')
    cfg['ngrok']['domain'] = domain
    # All inputs validated before the first write. Each individual write is atomic.
    if token:
        atomic_write(ROOT/'config/ngrok.yml', 'version: "3"\nagent:\n  authtoken: '+token+'\n')
    atomic_write(CONFIG, json.dumps(cfg, ensure_ascii=False, indent=2)+'\n')
    print('Сохранён config/config.json и, если введён токен, config/ngrok.yml. Не передавайте эти файлы другим.')
    print('Настроен полный доступ к ПК. URL и ключ будут показаны локально при запуске MCP.')
    print('Далее запустите start_notion.bat и оставьте окно открытым.')

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Не удалось завершить настройку: {exc}') from None