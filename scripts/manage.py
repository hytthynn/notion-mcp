#!/usr/bin/env python3
"""Single project entrypoint. It can be called from any working directory."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))


def main():
    from notion_pc_bridge.cli import RussianParser
    parser = RussianParser(description='Управление MCP-мостом для Notion')
    parser.add_argument('action', metavar='действие', help='install — зависимости; check — проверка; setup — настройка; start — запуск; stop — остановка; verify — диагностика', choices=('install', 'check', 'setup', 'start', 'stop', 'verify'))
    parser.add_argument('--prove', action='store_true', help='проверить настройки файлового доступа и выполнить безопасную команду оболочки')
    args = parser.parse_args()
    from notion_pc_bridge import bridge, configure, dependencies, verify
    if args.action == 'install':
        dependencies.clear_receipt()
        if not dependencies.ensure_all(interactive=True):
            return 1
        dependencies.save_receipt()
        return 0
    if args.action == 'check':
        dependencies.require_receipt()
        print('Все зависимости установлены. Сохранённая проверка: '+dependencies.read_receipt()['checked_at'])
        return 0
    if args.action == 'stop':
        return 0 if bridge.stop_previous() else 1
    if args.action in ('setup', 'start'):
        dependencies.require_receipt()
    if args.action == 'setup' or (args.action == 'start' and not bridge.CONFIG_FILE.exists()):
        configure.main()
        if args.action == 'setup':
            return 0
    if args.action == 'start':
        return bridge.start_or_show(argparse.Namespace(verify=True, print_url=False))
    # verify's own CLI only accepts its optional --prove switch.
    sys.argv = [sys.argv[0]] + (['--prove'] if args.prove else [])
    return verify.main()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print('Статус: ОШИБКА — '+exc.code, file=sys.stderr)
            raise SystemExit(1) from None
        raise
    except (OSError, ValueError) as exc:
        from notion_pc_bridge.diagnostics import report_error
        report_error(exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print('\nОтменено.')
        raise SystemExit(130) from None