#!/usr/bin/env python3
"""Interactive local-only setup. Never send credentials to chat."""
import json
import os
from pathlib import Path
import re
import secrets
import sys

from .bridge import atomic_write, parse_ngrok_yml
from .paths import ROOT, CONFIG_FILE as CONFIG
from .system import OS_NAME, script, shell_fits

TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{20,}')
DOMAIN_RE = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?')


def masked(token):
    return f'{token[:4]}…{token[-4:]}' if len(token) > 12 else '…'


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        raise SystemExit('Ввод прерван; файлы не изменены.') from None


def parse_token(raw):
    """Accept the bare token or the whole `ngrok config add-authtoken <token>` command."""
    words = raw.replace('"', ' ').replace("'", ' ').split()
    token = words[-1] if words else ''
    return token if TOKEN_RE.fullmatch(token) else ''


def parse_domain(raw):
    """Accept `name.ngrok-free.app` as well as a pasted https://…/ URL."""
    domain = re.sub(r'^[a-z]+://', '', raw.strip(), flags=re.I).split('/', 1)[0].lower()
    return domain if '.' in domain and DOMAIN_RE.fullmatch(domain) else ''


def main():
    if sys.version_info < (3, 10):
        raise SystemExit('Требуется Python 3.10 или новее.')
    print(f'Настройка MCP-моста для Notion ({OS_NAME})')
    print(f'ПОЛНЫЙ ДОСТУП: все папки и терминал в пределах прав вашей учётной записи {OS_NAME}.')
    print('Запускайте от обычного пользователя, не от администратора/root. Для изоляции используйте виртуальную машину.\n')
    source = CONFIG if CONFIG.exists() else ROOT/'config/config.example.json'
    cfg = json.loads(source.read_text(encoding='utf-8-sig'))
    if not isinstance(cfg, dict) or not all(isinstance(cfg.get(k), dict) for k in ('ngrok', 'desktop_commander')):
        raise SystemExit('Неверная структура конфигурации; файлы не изменены.')
    dc, ng = cfg['desktop_commander'], cfg['ngrok']
    cfg['access_mode'] = 'full'
    dc['allowedDirectories'] = []
    dc['telemetryEnabled'] = False
    if not shell_fits(dc.get('defaultShell')):
        dc['defaultShell'] = ''   # resolved per OS at start
    cfg['require_api_key'] = True
    key = cfg.get('api_key', '')
    cfg['api_key'] = key if isinstance(key, str) and len(key) >= 32 and key.isascii() and not any(c.isspace() for c in key) else secrets.token_urlsafe(48)
    secret = cfg.get('secret_path', '')
    cfg['secret_path'] = secret if isinstance(secret, str) and re.fullmatch(r'[A-Za-z0-9_-]{24,128}', secret) else 'mcp-' + secrets.token_hex(24)
    cfg['stateless'] = True
    ng['public_url'] = ''

    # ── ngrok token: typed or pasted text stays visible so the user can check it.
    config_path = Path(ng.get('config_path') or 'config/ngrok.yml')
    if not config_path.is_absolute():
        config_path = ROOT/config_path
    file_token, file_domain = parse_ngrok_yml(config_path)
    current = ng.get('authtoken') or file_token or os.environ.get('NGROK_AUTHTOKEN', '')
    print('Токен ngrok: https://dashboard.ngrok.com/get-started/your-authtoken')
    hint = f'Enter — оставить текущий {masked(current)}' if current else 'обязательно'
    while True:
        raw = ask(f'Вставьте токен ngrok ({hint}): ')
        if not raw and current:
            token = ''
            break
        token = parse_token(raw)
        if token:
            print(f'  ✓ Токен принят: {masked(token)} ({len(token)} символов)')
            break
        print('  Не похоже на токен ngrok. Вставьте строку из ngrok Dashboard → Your Authtoken.')

    # ── optional static domain: Enter keeps the current one, «-» removes it.
    current_domain = ng.get('domain') or file_domain
    print('\nЗакреплённый домен ngrok (необязательно): https://dashboard.ngrok.com/domains')
    hint = f'Enter — оставить {current_domain}, «-» — убрать' if current_domain else 'Enter — назначить автоматически'
    while True:
        raw = ask(f'Домен, например name.ngrok-free.app ({hint}): ')
        if not raw:
            domain = current_domain
            break
        if raw == '-':
            domain = ''
            break
        domain = parse_domain(raw)
        if domain:
            break
        print('  Введите только имя домена, например name.ngrok-free.app.')
    ng['domain'] = domain

    # All inputs validated before the first write. Each individual write is atomic.
    if token:
        ng['config_path'] = 'config/ngrok.yml'
        ng['authtoken'] = ''
        atomic_write(ROOT/'config/ngrok.yml', 'version: "3"\nagent:\n  authtoken: '+token+'\n')
    else:
        ng.setdefault('config_path', 'config/ngrok.yml')
    atomic_write(CONFIG, json.dumps(cfg, ensure_ascii=False, indent=2)+'\n')
    print('\nСохранено: config/config.json' + (' и config/ngrok.yml' if token else '') + '. Не передавайте файлы из config/ другим.')
    print('URL и X-Api-Key для Notion появятся при запуске.')
    print(f"Далее: {script('start_notion')} — оставьте окно открытым.")


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Не удалось завершить настройку: {exc}') from None
