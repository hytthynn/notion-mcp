"""Различия Windows, macOS и Linux собраны в одном месте."""
import os
import platform
import sys

from .paths import TOOLS_DIR

IS_WINDOWS = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')
OS_NAME = 'Windows' if IS_WINDOWS else 'macOS' if IS_MAC else 'Linux' if IS_LINUX else platform.system()


def script(name):
    """Имя сценария запуска для текущей ОС: setup_notion.bat или ./setup_notion.sh."""
    return f'{name}.bat' if IS_WINDOWS else f'./{name}.sh'


def default_shell():
    """Оболочка для команд агента: cmd.exe в Windows, zsh/bash в macOS и Linux."""
    if IS_WINDOWS:
        return 'cmd.exe'
    for candidate in (('/bin/zsh', '/bin/bash') if IS_MAC else ('/bin/bash',)):
        if os.access(candidate, os.X_OK):
            return candidate
    return '/bin/sh'


def shell_fits(value):
    """Отсекает пустое значение и оболочку другой ОС (например, cmd.exe в Linux)."""
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    name = value.replace('\\', '/').rsplit('/', 1)[-1].lower()
    if IS_WINDOWS:
        return not value.startswith('/')
    return not name.endswith('.exe') and name not in ('cmd', 'powershell')


def add_local_tools_to_path():
    """Node.js и ngrok, скачанные установщиком в tools/, становятся доступны этому процессу."""
    if IS_WINDOWS:
        return
    parts = [p for p in os.environ.get('PATH', '').split(os.pathsep) if p]
    for directory in (TOOLS_DIR, TOOLS_DIR/'node'/'bin'):
        if directory.is_dir() and str(directory) not in parts:
            parts.insert(0, str(directory))
    os.environ['PATH'] = os.pathsep.join(parts)


def machine():
    """Нормализованная архитектура: x64, arm64, arm или 386."""
    value = platform.machine().lower()
    if value in ('x86_64', 'amd64'):
        return 'x64'
    if value in ('arm64', 'aarch64'):
        return 'arm64'
    if value.startswith('armv7') or value.startswith('armv6'):
        return 'arm'
    if value in ('i386', 'i686', 'x86'):
        return '386'
    return value
