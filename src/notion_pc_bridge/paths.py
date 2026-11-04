"""Пути проекта, не зависящие от текущей папки консоли."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / 'config'
RUNTIME_DIR = ROOT / 'runtime'
TOOLS_DIR = ROOT / 'tools'
CONFIG_FILE = CONFIG_DIR / 'config.json'
STATUS_FILE = RUNTIME_DIR / 'status.json'
