"""Dependency inspection and opt-in installation. Never starts MCP or a tunnel.

Windows installs through winget. macOS and Linux download the official Node.js
and ngrok builds into tools/ of this folder: no sudo, no system changes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

from .paths import ROOT, CONFIG_FILE, TOOLS_DIR
from .system import IS_WINDOWS, IS_MAC, add_local_tools_to_path, machine, script


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run(args, timeout=30):
    return subprocess.run([str(x) for x in args], cwd=str(ROOT), text=True,
                          encoding='utf-8', errors='replace', capture_output=True, timeout=timeout)


def version(text):
    match = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', text)
    return tuple(int(n or 0) for n in match.groups()) if match else (0, 0, 0)


def executable(name):
    return (shutil.which(name+'.cmd') if IS_WINDOWS else None) or shutil.which(name)


NGROK_MINIMUM = (3, 39, 0)
_VERIFIED_NGROK = ''
_LAST_NGROK = ''


def configured_ngrok():
    try:
        value = json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig')).get('ngrok_bin')
        return value if isinstance(value, str) else ''
    except (OSError, ValueError, AttributeError):
        return ''


def ngrok_candidates():
    """Enumerate known executable locations without executing anything."""
    explicit = configured_ngrok()
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            candidate = ROOT/candidate
        return [str(candidate)] if candidate.is_file() else []
    candidates = []
    def add(path):
        if isinstance(path, (str, os.PathLike)) and path:
            candidate = Path(path)
            if (candidate.is_file() and (IS_WINDOWS or os.access(candidate, os.X_OK))
                    and os.path.normcase(str(candidate)) not in seen):
                candidates.append(str(candidate))
                seen.add(os.path.normcase(str(candidate)))
    seen = set()
    receipt = read_receipt() or {}
    add(receipt.get('ngrok_binary'))
    add(executable('ngrok'))
    names = ('ngrok.exe', 'ngrok.cmd', 'ngrok.bat', 'ngrok') if IS_WINDOWS else ('ngrok',)
    for directory in os.environ.get('PATH', '').split(os.pathsep):
        if directory:
            for name in names:
                add(Path(os.path.expandvars(directory.strip('"')))/name)
    if not IS_WINDOWS:
        add(TOOLS_DIR/'ngrok')
        home = Path.home()
        for directory in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/snap/bin', home/'.local/bin', home/'bin'):
            add(Path(directory)/'ngrok')
        return candidates
    local = os.environ.get('LOCALAPPDATA')
    if local:
        add(Path(local)/'Microsoft/WindowsApps/ngrok.exe')
    for directory in filter(None, (local, os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'))):
        base = Path(directory)
        for winget in (base/'Microsoft/WinGet', base/'WinGet'):
            add(winget/'Links/ngrok.exe')
            for pattern in ('Packages/Ngrok.Ngrok_*/ngrok.exe', 'Packages/Ngrok.Ngrok_*/bin/ngrok.exe'):
                for candidate in winget.glob(pattern):
                    add(candidate)
        add(base/'ngrok/ngrok.exe')
    return candidates


def ngrok_path():
    """Use the installer's saved selection; never probe versions during startup."""
    if configured_ngrok():
        candidates = ngrok_candidates()
        return candidates[0] if candidates else ''
    if _VERIFIED_NGROK and Path(_VERIFIED_NGROK).is_file():
        return _VERIFIED_NGROK
    receipt = read_receipt() or {}
    selected = receipt.get('ngrok_binary')
    if isinstance(selected, str) and Path(selected).is_file():
        return selected
    candidates = ngrok_candidates()
    return candidates[0] if candidates else ''


def inspect_ngrok():
    """Installer-only version selection: old PATH entries must not hide newer copies."""
    global _VERIFIED_NGROK, _LAST_NGROK
    _VERIFIED_NGROK = _LAST_NGROK = ''
    candidates = ngrok_candidates()
    inspected = []
    for candidate in candidates:
        try:
            result = run([candidate, 'version'], timeout=15)
            parsed = version((result.stdout or result.stderr or '').strip())
            if result.returncode == 0 and parsed != (0, 0, 0):
                inspected.append((parsed, candidate))
        except (OSError, subprocess.SubprocessError):
            continue
    if not inspected:
        detail = ('не удалось определить версию; файл: '+candidates[0]) if candidates else 'не найдено; проверьте также ngrok_bin в конфигурации'
        return Check('ngrok >=3.39', False, detail)
    parsed, selected = max(inspected, key=lambda item: item[0])
    _LAST_NGROK = selected
    ok = parsed >= NGROK_MINIMUM
    if ok:
        _VERIFIED_NGROK = selected
    return Check('ngrok >=3.39', ok, 'версия '+'.'.join(map(str, parsed))+'; файл: '+selected)


def update_ngrok_after_winget():
    """A stale winget manifest is not proof that the selected binary is up to date."""
    status = inspect_ngrok()
    if status.ok:
        return
    print('[ngrok] После установки: '+status.detail, flush=True)
    if _LAST_NGROK and confirm('Обновить этот ngrok встроенной командой «ngrok update»? Файл: '+_LAST_NGROK):
        try:
            result = subprocess.run([_LAST_NGROK, 'update'], cwd=str(ROOT), timeout=180)
            if result.returncode:
                print('Встроенное обновление завершилось с ошибкой.', flush=True)
        except (OSError, subprocess.SubprocessError):
            print('Не удалось выполнить встроенное обновление ngrok.', flush=True)
        refresh_path()
    print('Если версия осталась старой, установите свежий ngrok с https://ngrok.com/download и укажите его путь в ngrok_bin. Старая копия из PATH не удаляется автоматически.', flush=True)


def inspect_runtime(name, path, minimum, args=('--version',)):
    if not path:
        return Check(name, False, 'не найдено')
    try:
        result = run([path, *args], timeout=15)
        text = (result.stdout or result.stderr).strip()
        ok = result.returncode == 0 and version(text) >= minimum
        return Check(name, ok, ('версия ' + '.'.join(map(str, version(text)))) if version(text) != (0, 0, 0) else 'не удалось определить версию')
    except (OSError, subprocess.SubprocessError):
        return Check(name, False, 'не удалось выполнить проверку версии')


def inspect_packages():
    npm, node = executable('npm'), executable('node')
    if not npm or not node:
        return Check('npm-пакеты', False, 'сначала установите Node.js и npm')
    try:
        package = json.loads((ROOT/'package.json').read_text(encoding='utf-8'))
        lock = json.loads((ROOT/'package-lock.json').read_text(encoding='utf-8'))
        locked = lock.get('packages', {})
        if locked.get('', {}).get('dependencies') != package.get('dependencies'):
            return Check('npm-пакеты', False, 'package.json и package-lock.json не совпадают; восстановите lock-файл')
        for entry in ('mcp-proxy/dist/bin/mcp-proxy.mjs', '@wonderwhy-er/desktop-commander/dist/index.js'):
            if not (ROOT/'node_modules'/entry).is_file():
                return Check('npm-пакеты', False, 'локальные зависимости отсутствуют; требуется npm ci')
        result = run([npm, 'ls', '--all', '--json'], timeout=90)
        if result.returncode != 0:
            return Check('npm-пакеты', False, 'npm ls обнаружил отсутствующие, некорректные или лишние пакеты')
        count = 0
        for location, expected in locked.items():
            if not location.startswith('node_modules/') or not expected.get('version'):
                continue
            installed = ROOT/location/'package.json'
            if installed.exists():
                actual = json.loads(installed.read_text(encoding='utf-8'))
                if actual.get('version') != expected['version']:
                    return Check('npm-пакеты', False, f'установленная версия не совпадает с lock-файлом: {location}')
                count += 1
        # Native and document modules can exist on disk yet fail to load.
        probe = run([node, '-e', "require('sharp'); if(!require('exceljs').Workbook)process.exit(1)"], timeout=30)
        if probe.returncode:
            return Check('npm-пакеты', False, 'не удалось загрузить sharp/exceljs; переустановите зависимости')
        return Check('npm-пакеты', True, f'проверено пакетов по lock-файлу: {count}; всё дерево и sharp/exceljs исправны')
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return Check('npm-пакеты', False, 'не удалось проверить файлы зависимостей или среду выполнения')


def inspect_all():
    checks = [Check('Python', sys.version_info >= (3, 10), sys.version.split()[0]+'; только стандартная библиотека, pip-пакеты не нужны'),
              inspect_runtime('Node.js >=22', executable('node'), (22, 0, 0)),
              inspect_runtime('npm >=9', executable('npm'), (9, 0, 0)),
              inspect_ngrok(),
              inspect_packages()]
    return checks


def display(checks):
    print('\n=== Проверка зависимостей ===', flush=True)
    for item in checks:
        print(f"[{'ГОТОВО' if item.ok else 'НЕТ/ОШИБКА'}] {item.name}: {item.detail}", flush=True)
    print(f"Учётная запись и токен ngrok настраиваются отдельно через {script('setup_notion')}.", flush=True)


def confirm(message):
    try:
        return input(message+' [да/НЕТ]: ').strip().lower() in ('y', 'yes', 'д', 'да')
    except (EOFError, KeyboardInterrupt):
        return False


def refresh_path():
    """Installers update registry PATH, not this Python process's environment."""
    if not IS_WINDOWS:
        add_local_tools_to_path()
        return
    import winreg
    values = []
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
                      (winreg.HKEY_CURRENT_USER, r'Environment')):
        try:
            with winreg.OpenKey(hive, key) as handle:
                values.append(os.path.expandvars(winreg.QueryValueEx(handle, 'Path')[0]))
        except OSError:
            pass
    os.environ['PATH'] = os.pathsep.join(values+[os.environ.get('PATH', '')])


def install_winget(package_id):
    winget = executable('winget')
    if not winget:
        print('winget недоступен. Установите «Установщик приложений» или используйте официальный установщик и повторите проверку.', flush=True)
        return False
    # No silent install and no acceptance of source/package agreements on the user's behalf.
    result = subprocess.run([winget, 'install', '--id', package_id, '--exact', '--source', 'winget'], cwd=str(ROOT))
    refresh_path()
    return result.returncode == 0


# ── macOS/Linux: official builds into tools/ ─────────────────────────────────
NODE_DIST = 'https://nodejs.org/dist'
NGROK_DIST = 'https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-{system}-{arch}.{ext}'


def download(url, target):
    """urllib first; curl as a fallback for Python builds without CA certificates."""
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'notion-pc-bridge'})
        with urllib.request.urlopen(request, timeout=60) as response, open(target, 'wb') as stream:
            shutil.copyfileobj(response, stream)
        return
    except OSError:
        curl = shutil.which('curl')
        if not curl:
            raise
    subprocess.run([curl, '-fsSL', '--retry', '2', '-o', str(target), url], check=True, timeout=900)


def install_node_local():
    """Newest Node.js LTS from nodejs.org, SHA-256 verified, into tools/node."""
    arch = {'x64': 'x64', 'arm64': 'arm64', 'arm': 'armv7l'}.get(machine())
    system = 'darwin' if IS_MAC else 'linux'
    if not arch or (IS_MAC and arch == 'armv7l'):
        raise OSError(f'нет официальной сборки Node.js для архитектуры {machine()}')
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TOOLS_DIR) as temp:
        temp = Path(temp)
        download(f'{NODE_DIST}/index.json', temp/'index.json')
        key = f'osx-{arch}-tar' if IS_MAC else f'linux-{arch}'
        releases = json.loads((temp/'index.json').read_text(encoding='utf-8'))
        release = next(r for r in releases if r.get('lts') and key in r.get('files', ()))
        name = f"node-{release['version']}-{system}-{arch}"
        print(f"[Node.js] Загрузка {release['version']} (LTS «{release['lts']}»)…", flush=True)
        download(f"{NODE_DIST}/{release['version']}/SHASUMS256.txt", temp/'sums.txt')
        download(f"{NODE_DIST}/{release['version']}/{name}.tar.gz", temp/'node.tar.gz')
        expected = re.search(r'^([0-9a-f]{64})\s+' + re.escape(name + '.tar.gz') + '$',
                             (temp/'sums.txt').read_text(encoding='utf-8'), re.M)
        digest = hashlib.sha256((temp/'node.tar.gz').read_bytes()).hexdigest()
        if not expected or expected.group(1) != digest:
            raise OSError('контрольная сумма Node.js не совпала; файл отброшен')
        with tarfile.open(temp/'node.tar.gz') as archive:
            if hasattr(tarfile, 'data_filter'):
                archive.extractall(temp, filter='data')
            else:
                archive.extractall(temp)
        shutil.rmtree(TOOLS_DIR/'node', ignore_errors=True)
        (temp/name).rename(TOOLS_DIR/'node')
    add_local_tools_to_path()
    print(f"[Node.js] Установлен в {TOOLS_DIR/'node'}", flush=True)


def install_ngrok_local():
    """Official ngrok agent into tools/ngrok."""
    arch = {'x64': 'amd64', 'arm64': 'arm64', 'arm': 'arm', '386': '386'}.get(machine())
    if not arch or (IS_MAC and arch not in ('amd64', 'arm64')):
        raise OSError(f'нет официальной сборки ngrok для архитектуры {machine()}')
    url = NGROK_DIST.format(system='darwin' if IS_MAC else 'linux', arch=arch, ext='zip' if IS_MAC else 'tgz')
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    print('[ngrok] Загрузка официальной сборки…', flush=True)
    with tempfile.TemporaryDirectory(dir=TOOLS_DIR) as temp:
        archive_path, binary = Path(temp)/'ngrok.archive', Path(temp)/'ngrok'
        download(url, archive_path)
        if IS_MAC:
            with zipfile.ZipFile(archive_path) as archive:
                binary.write_bytes(archive.read('ngrok'))
        else:
            with tarfile.open(archive_path) as archive:
                member = archive.extractfile('ngrok')
                if member is None:
                    raise OSError('в архиве ngrok нет исполняемого файла')
                binary.write_bytes(member.read())
        binary.chmod(0o755)
        os.replace(binary, TOOLS_DIR/'ngrok')
    add_local_tools_to_path()
    print(f"[ngrok] Установлен в {TOOLS_DIR/'ngrok'}", flush=True)


def offer(question, installer, manual):
    if not confirm(question):
        return
    try:
        installer()
    except (OSError, ValueError, StopIteration, tarfile.TarError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        print(f'Не удалось установить автоматически: {exc}. Установите вручную: {manual}', flush=True)


def ensure_all(interactive=False):
    checks = inspect_all()
    display(checks)
    if all(item.ok for item in checks):
        return True
    if not interactive:
        print(f"Запустите {script('install_dependencies')}, чтобы установить недостающие компоненты.", flush=True)
        return False
    # Node includes npm; offer one installer, not two conflicting installations.
    if not checks[1].ok or not checks[2].ok:
        if IS_WINDOWS:
            if confirm('Установить или обновить Node.js LTS вместе с npm через winget?'):
                install_winget('OpenJS.NodeJS.LTS')
        else:
            offer('Скачать официальный Node.js LTS вместе с npm в папку tools/ (без sudo)?',
                  install_node_local, 'https://nodejs.org/en/download')
    if not checks[3].ok:
        if IS_WINDOWS:
            if confirm('Установить или обновить ngrok через winget?'):
                install_winget('Ngrok.Ngrok')
                update_ngrok_after_winget()
        else:
            offer('Скачать официальный ngrok в папку tools/ (без sudo)?',
                  install_ngrok_local, 'https://ngrok.com/download')
    node = inspect_runtime('Node', executable('node'), (22, 0, 0))
    npm = inspect_runtime('npm', executable('npm'), (9, 0, 0))
    if node.ok and npm.ok and not inspect_packages().ok:
        if confirm('Установить или переустановить локальные npm-зависимости по lock-файлу (npm ci --ignore-scripts)?'):
            subprocess.run([executable('npm'), 'ci', '--ignore-scripts', '--no-fund', '--no-audit'], cwd=str(ROOT))
    checks = inspect_all()
    display(checks)
    if not all(item.ok for item in checks):
        print('Часть зависимостей по-прежнему недоступна. Проверьте сообщения выше; после установки откройте консоль заново.', flush=True)
        return False
    print('Все зависимости проверены.', flush=True)
    return True

RECEIPT_FILE = CONFIG_FILE.parent/'dependencies.checked.json'


def read_receipt():
    """Read the last installer result; no runtime probes, version checks or npm ls."""
    try:
        receipt = json.loads(RECEIPT_FILE.read_text(encoding='utf-8-sig'))
        if (isinstance(receipt, dict) and receipt.get('schema') == 1
                and receipt.get('ok') is True and receipt.get('python_binary')
                and receipt.get('checked_at')):
            return receipt
    except (OSError, ValueError):
        pass
    return None


def save_receipt():
    """Called only after successful verification by the install action."""
    from datetime import datetime
    from .bridge import atomic_write
    atomic_write(RECEIPT_FILE, json.dumps({
        'schema': 1, 'ok': True,
        'checked_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'python_binary': sys.executable,
        'python_version': sys.version.split()[0],
        'ngrok_binary': _VERIFIED_NGROK,
    }, ensure_ascii=False, indent=2)+'\n')


def require_receipt():
    if not read_receipt():
        raise SystemExit(f"Нет сохранённой успешной проверки зависимостей. Один раз запустите {script('install_dependencies')}.")


def clear_receipt():
    """Invalidate the previous result before a new installer check starts."""
    RECEIPT_FILE.unlink(missing_ok=True)
