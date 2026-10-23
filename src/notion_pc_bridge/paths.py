"""Project-relative locations, independent of the shell's working directory."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / 'config'
RUNTIME_DIR = ROOT / 'runtime'
CONFIG_FILE = CONFIG_DIR / 'config.json'
STATUS_FILE = RUNTIME_DIR / 'status.json'
