#!/usr/bin/env python3
"""Notion PC Bridge — expose local PC tools through an authenticated MCP endpoint.

Chain:
    Notion cloud agent
      -> https://<static>.ngrok-free.dev/<secret path>     (public, secret path + API key)
      -> ngrok agent                                        (outbound-only tunnel)
      -> mcp-proxy  127.0.0.1:<port>  (streamable HTTP MCP) (loopback only)
      -> @wonderwhy-er/desktop-commander (stdio)            (shell + files, on this PC)

Run from the project root:
    python scripts/manage.py start     start and verify the bridge
    python scripts/manage.py verify    check a running public endpoint
    python scripts/manage.py stop      request supervisor shutdown

Stdlib only. Windows-first, POSIX-tolerant.
"""
from __future__ import annotations

import queue
import threading
import socket
import tempfile
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .paths import ROOT, CONFIG_FILE, STATUS_FILE, RUNTIME_DIR
from .processes import ProcessJob, identity, same_process, recover_owned
DC_CONFIG = Path.home() / ".claude-server-commander" / "config.json"   # desktop-commander's REAL config
NGROK_API = "http://127.0.0.1:4040/api/tunnels"

# Keys bridge.py merges into DC_CONFIG. Only what is listed in config.json ->
# desktop_commander gets written; everything else in that shared file (clientId,
# usageStats, blockedCommands, limits) is preserved untouched.


# ── config ──────────────────────────────────────────────────────────────────
def atomic_write(path: Path, text: str) -> None:
    """Replace a file on the same volume; never leave half-written JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise SystemExit("Нет config/config.json. Сначала запустите setup_notion.bat.")
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        if not isinstance(cfg, dict):
            raise ValueError("верхний уровень должен быть JSON-объектом")
        cfg.setdefault("port", 8888)
        if type(cfg["port"]) is not int or not 1 <= cfg["port"] <= 65535:
            raise ValueError("port должен быть целым числом от 1 до 65535")
        key = cfg.get("api_key", "")
        if not isinstance(key, str) or len(key) < 32 or not key.isascii() or any(c.isspace() for c in key):
            raise ValueError("требуется надёжный ASCII-ключ api_key; запустите setup_notion.bat")
        if cfg.get("require_api_key", True) is not True:
            raise ValueError("отключение авторизации по API-ключу не поддерживается")
        if cfg.get("stateless", True) is not True:
            raise ValueError("stateless должен быть true; режим с постоянными сессиями не поддерживается")
        cfg.setdefault("secret_path", "")
        if not isinstance(cfg["secret_path"], str) or (cfg["secret_path"] and not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", cfg["secret_path"])):
            raise ValueError("secret_path должен быть пустым или содержать 24–128 символов A-Z, a-z, 0-9, _ и -")
        for name in ("ngrok", "desktop_commander"):
            cfg.setdefault(name, {})
            if not isinstance(cfg[name], dict):
                raise ValueError(f"{name} должен быть JSON-объектом")
        dc = cfg["desktop_commander"]
        dc.setdefault("defaultShell", "cmd.exe")
        dc.setdefault("telemetryEnabled", False)
        # Explicitly selected full-PC mode. Empty list is DC's unrestricted mode.
        if cfg.get("access_mode", "full") != "full":
            raise ValueError("поддерживается только access_mode=full (полный доступ)")
        cfg["access_mode"] = "full"
        dc["allowedDirectories"] = []
        for name in ("ngrok_bin", "mcp_proxy_js", "desktop_commander_js"):
            if name in cfg and not isinstance(cfg[name], str):
                raise ValueError(f"{name} должен быть строкой с путём")
        if not isinstance(dc.get("defaultShell"), str) or not dc["defaultShell"].strip():
            raise ValueError("desktop_commander.defaultShell должен быть строкой с командой оболочки")
        if type(dc.get("telemetryEnabled")) is not bool:
            raise ValueError("desktop_commander.telemetryEnabled должен быть true или false")
        if "blockedCommands" in dc and (not isinstance(dc["blockedCommands"], list)
                or not all(isinstance(item, str) for item in dc["blockedCommands"])):
            raise ValueError("desktop_commander.blockedCommands должен быть списком строк")
        ng = cfg["ngrok"]
        for name in ("config_path", "domain", "authtoken", "public_url"):
            if name in ng and not isinstance(ng[name], str):
                raise ValueError(f"ngrok.{name} должен быть строкой")
        domain = ng.get("domain", "")
        if domain and (not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain)):
            raise ValueError("ngrok.domain должен содержать только имя домена")
        if ng.get("public_url"):
            raise ValueError("ngrok.public_url небезопасен; используйте ngrok.domain")
        for name in ("heartbeat_interval", "heartbeat_tolerance"):
            if name in ng and not re.fullmatch(r"[1-9][0-9]*(?:ms|s|m)", str(ng[name])):
                raise ValueError(f"ngrok.{name} должен быть длительностью, например 20s")
    except (OSError, ValueError, TypeError) as exc:
        detail = f"ошибка JSON в строке {exc.lineno}, столбце {exc.colno}" if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise SystemExit(f"Ошибка в config/config.json: {detail}") from None
    cfg["ngrok_bin"] = cfg.get("ngrok_bin") or ""
    return cfg


def resolve(cfg: dict, key: str, subpath: str) -> str:
    """Resolve the explicitly configured entrypoint or the local pinned runtime."""
    explicit = cfg.get(key)
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = ROOT / p
        if p.is_file():
            return str(p)
    local = ROOT / "node_modules" / subpath
    if local.exists():
        return str(local)
    raise SystemExit(f"Отсутствует локальная зависимость {subpath}. Запустите install_dependencies.bat.")


def find_ngrok(cfg: dict) -> str:
    explicit = cfg.get("ngrok_bin", "")
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        if candidate.is_file():
            return str(candidate)
        raise SystemExit("Путь ngrok_bin не указывает на файл. Исправьте config/config.json.")
    from .dependencies import ngrok_path
    found = ngrok_path()
    if found:
        return found
    raise SystemExit("ngrok не найден. Запустите install_dependencies.bat.")


# ── desktop-commander config ────────────────────────────────────────────────
def apply_dc_config(cfg: dict) -> dict:
    """Merge our keys into desktop-commander's real, SHARED config file.

    It lives at ~/.claude-server-commander/config.json — the package README's
    "config.json in the server's working directory" is wrong (dist/config.js pins
    CONFIG_FILE to the home dir). That file is shared with every other MCP client
    on this machine and carries clientId + usageStats, so it is merged, never
    overwritten, and backed up once before the first write. The server watches the
    directory, so the change hot-reloads.
    """
    DC_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    current: dict = {}
    if DC_CONFIG.exists():
        raw = DC_CONFIG.read_text(encoding="utf-8-sig")
        try:
            current = json.loads(raw)
            if not isinstance(current, dict):
                raise ValueError("ожидался JSON-объект")
        except ValueError as exc:
            detail = f"ошибка JSON в строке {exc.lineno}, столбце {exc.colno}" if isinstance(exc, json.JSONDecodeError) else str(exc)
            raise SystemExit(f"Общий конфиг Desktop Commander повреждён и не изменён: {detail}") from None
        if not list(DC_CONFIG.parent.glob("config.json.bak-*")):
            bak = DC_CONFIG.with_name(f"config.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            bak.write_text(raw, encoding="utf-8")
    changed = {}
    for key, value in cfg["desktop_commander"].items():
        if key.startswith("_"):
            continue
        if current.get(key) != value:
            changed[key] = (current.get(key), value)
        current[key] = value
    if changed:
        atomic_write(DC_CONFIG, json.dumps(current, indent=2, ensure_ascii=False) + "\n")
    return changed


# ── processes ───────────────────────────────────────────────────────────────
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def start_proxy(cfg: dict) -> subprocess.Popen:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    node = shutil.which("node") or "node"
    proxy_js = resolve(cfg, "mcp_proxy_js", "mcp-proxy/dist/bin/mcp-proxy.mjs")
    dc_js = resolve(cfg, "desktop_commander_js", "@wonderwhy-er/desktop-commander/dist/index.js")
    secret = cfg["secret_path"]
    args = [node, proxy_js,
            "--host", "127.0.0.1",            # loopback only: ngrok is the sole ingress
            "--port", str(cfg["port"]),
            "--server", "stream",              # no /sse endpoint to leave unauthenticated
            "--streamEndpoint", f"/{secret}",  # additional secret path; X-API-Key remains required
            "--corsAddAllowedHeader", "X-API-Key"]
    if cfg.get("stateless", True):
        # Stateless streamable HTTP: no Mcp-Session-Id, no persistent SSE stream per
        # client. Stateful mode kept one stream alive per session for 30 minutes
        # (--sessionIdleTimeout default) and every verify/agent run opened a new one;
        # they piled up until the free ngrok tier started dropping connections
        # (client-side RemoteDisconnected). Stateless removes the accumulation.
        args += ["--stateless"]
    if cfg.get("api_key"):
        args += ["--apiKey", cfg["api_key"]]
    args += ["--", node, dc_js]
    with (RUNTIME_DIR / "bridge.log").open("ab", buffering=0) as log:
        return subprocess.Popen(args, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW, start_new_session=os.name != "nt")


def parse_ngrok_yml(path: Path) -> tuple[str, str]:
    """Pull (authtoken, domain) out of an ngrok agent config of ANY version.

    The dashboard now emits version:"3" (agent.authtoken + endpoints[].url) while
    older agents reject it ("unknown version '3'. valid versions are: [1 2]").
    So the yml is read as DATA and ngrok is always driven by CLI flags — one
    invocation shape that works on both, no YAML dependency, no version pinning.
    """
    if not path.exists():
        return "", ""
    text = path.read_text(encoding="utf-8", errors="replace")
    token = ""
    m = re.search(r"^\s*authtoken:\s*[\"']?([A-Za-z0-9_\-]+)", text, re.M)
    if m:
        token = m.group(1)
    domain = ""
    m = re.search(r"^\s*url:\s*[\"']?https://([^/\"'\s]+)", text, re.M)
    if not m:
        m = re.search(r"^\s*(?:domain|hostname):\s*[\"']?([^/\"'\s]+)", text, re.M)
    if m:
        domain = m.group(1)
    return token, domain


def start_ngrok(cfg: dict) -> subprocess.Popen:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    ngrok = find_ngrok(cfg)
    ng = cfg["ngrok"]
    token = ng.get("authtoken") or ""
    domain = ng.get("domain") or ""
    cfg_path = ng.get("config_path") or ""
    if cfg_path and (not token or not domain):
        yml_token, yml_domain = parse_ngrok_yml(Path(cfg_path) if Path(cfg_path).is_absolute() else ROOT / cfg_path)
        token, domain = token or yml_token, domain or yml_domain
    token = token or os.environ.get("NGROK_AUTHTOKEN", "")
    if not token:
        sys.exit("Не задан токен ngrok. Запустите setup_notion.bat или задайте NGROK_AUTHTOKEN.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise SystemExit("Неверный формат токена ngrok.")
    # Explicit 127.0.0.1, NOT a bare port: a bare port makes ngrok dial
    # "localhost:<port>", which on Windows resolves to IPv6 [::1] first while
    # mcp-proxy listens on IPv4 only -> intermittent "failed to open private leg"
    # and RemoteDisconnected on the client side.
    # ngrok 3.39 has no --heartbeat-* CLI flags: they live in the agent config. With the
    # default tolerance a short outbound hiccup terminates the session ("heartbeat timeout,
    # terminating session") and every in-flight request dies with RemoteDisconnected while
    # it reconnects. Observed live; 60s tolerance absorbs it.
    ng = cfg["ngrok"]
    lines = ['version: "3"', "agent:"]
    if token:
        lines.append(f"  authtoken: {token}")
    lines += [f"  heartbeat_interval: {ng.get('heartbeat_interval', '20s')}",
              f"  heartbeat_tolerance: {ng.get('heartbeat_tolerance', '60s')}",
              "  log: stdout", "  log_format: json"]
    agent_cfg = RUNTIME_DIR / "ngrok-agent.yml"     # contains the authtoken -> gitignored (deny-all)
    atomic_write(agent_cfg, "\n".join(lines) + "\n")
    args = [ngrok, "http", f"127.0.0.1:{cfg['port']}", "--config", str(agent_cfg),
            "--host-header", f"127.0.0.1:{cfg['port']}"]
    if domain:
        args += ["--domain", domain]
    with (RUNTIME_DIR / "ngrok.log").open("wb", buffering=0) as log:
        return subprocess.Popen(args, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW, start_new_session=os.name != "nt")


def public_url(cfg: dict, deadline_s: int = 45) -> str:
    """Find this bridge's tunnel, never an unrelated tunnel on the same PC."""
    log_path = RUNTIME_DIR / "ngrok.log"
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if STOP_FILE.exists():
            return ""
        try:
            with urllib.request.urlopen(NGROK_API, timeout=5) as r:
                tunnels = json.loads(r.read().decode()).get("tunnels", [])
            for t in tunnels:
                if (t.get("proto") == "https" and t.get("public_url")
                        and str(t.get("config", {}).get("addr", "")).rstrip("/")
                        in (f"http://127.0.0.1:{cfg['port']}", f"127.0.0.1:{cfg['port']}")):
                    return t["public_url"].rstrip("/")
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
        if log_path.exists():
            found = re.findall(r'"url":"(https://[^"]+)"',
                               log_path.read_text(encoding="utf-8", errors="replace"))
            if found:
                return found[-1].rstrip("/")
        time.sleep(1)
    return ""


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ── MCP client (enough of the streamable-HTTP handshake to prove it works) ──
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a credential-bearing request to another origin.
        return None


def mcp_call(url: str, payload: dict, session: str | None = None,
             headers_extra: dict | None = None, timeout: int = 40):
    """Bounded JSON/SSE request; stop at our JSON-RPC response, not SSE EOF."""
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
           "ngrok-skip-browser-warning": "1", "User-Agent": "notion-pc-bridge/1.1", "MCP-Protocol-Version": "2025-06-18"}
    if session:
        hdr["Mcp-Session-Id"] = session
    hdr.update(headers_extra or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=hdr, method="POST")
    results = queue.Queue(maxsize=1)
    cancelled = threading.Event()
    deadline = time.monotonic() + timeout

    def request():
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
                status, sid = response.status, response.headers.get("Mcp-Session-Id")
                if status == 202 or response.headers.get("Content-Length") == "0":
                    results.put((status, {}, sid))
                    return
                sse = "text/event-stream" in response.headers.get("Content-Type", "").lower()
                buffer = b""
                total = 0
                while not cancelled.is_set() and time.monotonic() < deadline:
                    chunk = response.read1(4096)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 4 * 1024 * 1024:
                        raise ValueError("Ответ MCP превышает 4 МиБ")
                    buffer += chunk
                    if sse:
                        buffer = buffer.replace(b"\r\n", b"\n")
                        while b"\n\n" in buffer:
                            frame, buffer = buffer.split(b"\n\n", 1)
                            data = b"\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith(b"data:"))
                            if not data:
                                continue
                            body = json.loads(data.decode("utf-8"))
                            if isinstance(body, dict) and body.get("id") == payload.get("id") and ("result" in body or "error" in body):
                                results.put((status, body, sid))
                                return
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("Истекло время ожидания ответа MCP")
                if sse:
                    raise ValueError("Поток SSE завершился без соответствующего ответа JSON-RPC")
                results.put((status, json.loads(buffer.decode("utf-8")) if buffer else {}, sid))
        except urllib.error.HTTPError as exc:
            # Do not echo a remote response that might contain credentials.
            exc.close()
            results.put((exc.code, "HTTP-запрос отклонён", None))
        except Exception as exc:
            results.put((None, f"Ошибка транспорта MCP: {type(exc).__name__}", None))

    # A pathological peer must not leave a non-daemon executor keeping Python alive.
    threading.Thread(target=request, daemon=True, name="mcp-request").start()
    try:
        return results.get(timeout=timeout)
    except queue.Empty:
        cancelled.set()
        return None, f"Истекло время ожидания MCP ({timeout} с)", None


def authentication_headers(cfg: dict) -> dict:
    return {"X-API-Key": cfg["api_key"]} if cfg.get("api_key") else {}


def verify(url: str, cfg: dict) -> tuple[bool, str | None]:
    """Handshake + tools/list over the public URL. Returns (ok, session_id)."""
    extra = authentication_headers(cfg)
    print("[проверка] Проверяем подключение MCP с авторизацией (URL скрыт)")
    st, body, sid = mcp_call(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18",
                   "capabilities": {},
                   "clientInfo": {"name": "notion-pc-bridge-verify", "version": "1.0"}}},
        headers_extra=extra)
    if st != 200 or not isinstance(body, dict) or "result" not in body:
        print(f"[проверка] Ошибка initialize → HTTP {st}: {str(body)[:400]}")
        return False, None
    srv = body["result"].get("serverInfo", {})
    print(f"[проверка] initialize выполнен: {srv.get('name')} {srv.get('version')} (сессия: {sid or 'нет'})")
    notified, _, _ = mcp_call(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid, extra)
    if notified not in (200, 202, 204):
        print(f"[проверка] Ошибка уведомления initialized → HTTP {notified}")
        return False, sid
    st, body, _ = mcp_call(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                           sid, extra)
    if st != 200 or not isinstance(body, dict):
        print(f"[проверка] Ошибка tools/list → HTTP {st}: {str(body)[:400]}")
        return False, sid
    tools = [t.get("name") for t in body.get("result", {}).get("tools", [])]
    print(f"[проверка] tools/list выполнен: инструментов — {len(tools)}: {', '.join(tools)}")
    if not tools:
        print("[проверка] Ошибка: сервер ответил, но не предоставил инструменты")
        return False, sid
    return True, sid


# ── lifecycle ───────────────────────────────────────────────────────────────
def write_status(cfg: dict, url: str, proxy=None, tunnel=None, state="up") -> None:
    atomic_write(STATUS_FILE, json.dumps({
        "state": state, "project_root": str(ROOT.resolve()),
        "access_mode": cfg.get("access_mode", "full"),
        "pid": os.getpid(), "supervisor": identity(os.getpid()),
        "proxy_pid": proxy.pid if proxy else None,
        "ngrok_pid": tunnel.pid if tunnel else None,
        "proxy": identity(proxy.pid) if proxy else None,
        "tunnel": identity(tunnel.pid) if tunnel else None,
        "port": cfg["port"], "mcp_url": url,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "allowed_directories": cfg["desktop_commander"]["allowedDirectories"],
    }, indent=2) + "\n")


def terminate_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


STOP_FILE = RUNTIME_DIR / "stop.request"
LOCK_FILE = RUNTIME_DIR / ".bridge.lock"


class AlreadyRunning(SystemExit):
    pass


def _configured_port():
    try:
        port = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig")).get("port", 8888)
        return port if type(port) is int and 1 <= port <= 65535 else 8888
    except (OSError, ValueError, AttributeError):
        return 8888


def _recover():
    try:
        count = recover_owned(ROOT)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    if count:
        print(f"[восстановление] Завершено оставшихся процессов этой папки: {count}.", flush=True)
    return count


def stop_previous(timeout=30) -> bool:
    """Stop only this project's supervisor; recover verified legacy orphans if needed."""
    try:
        with InstanceLock():
            count = _recover()
            STATUS_FILE.unlink(missing_ok=True)
            STOP_FILE.unlink(missing_ok=True)
            print("[остановка] Мост этой папки остановлен." if count else
                  "[остановка] В этой папке нет запущенного моста.", flush=True)
            if port_busy(_configured_port()):
                print("[порт] Предпочитаемый порт занят другой программой или копией. Она не изменена; при запуске будет выбран свободный порт.", flush=True)
            return True
    except AlreadyRunning:
        atomic_write(STOP_FILE, "stop\n")
        print("[остановка] Отправлен запрос. Ожидаем освобождения процессов…", flush=True)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with InstanceLock():
                _recover()
                STATUS_FILE.unlink(missing_ok=True)
                STOP_FILE.unlink(missing_ok=True)
                print("[остановка] Мост этой папки остановлен; его процессы освобождены.", flush=True)
                return True
        except AlreadyRunning:
            time.sleep(0.2)
    print("[остановка] Не удалось дождаться завершения за отведённое время. Чужие процессы не завершались.", flush=True)
    return False


def start_or_show(args):
    try:
        with InstanceLock():
            return run(args)
    except AlreadyRunning:
        try:
            status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        print("[запуск] Мост этой папки уже работает или запускается. Второй экземпляр не создан.", flush=True)
        if (status.get("state") == "up" and status.get("project_root") == str(ROOT.resolve())
                and same_process(status.get("supervisor")) and same_process(status.get("proxy"))
                and same_process(status.get("tunnel")) and status.get("mcp_url")):
            print(f"Локальный порт: {status.get('port')}", flush=True)
            banner(status["mcp_url"], load_config())
        else:
            print("Дождитесь готовности в первом окне. Для остановки используйте stop_notion.bat из этой же папки.", flush=True)
        return 0


class InstanceLock:
    """OS-owned lock released even after a crash (file itself is intentionally retained)."""
    def __enter__(self):
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.stream = None
        try:
            self.stream = LOCK_FILE.open("a+b")
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
        except OSError:
            if self.stream is not None:
                self.stream.close()
            raise SystemExit("Нет доступа к файлу блокировки runtime/.bridge.lock.") from None
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise AlreadyRunning("Мост этой папки уже запущен.") from None
        return self

    def __exit__(self, *exc):
        self.stream.close()


def cleanup_processes(*processes):
    for proc in processes:
        if proc is not None:
            terminate_tree(proc)
    for proc in processes:
        if proc is not None:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=8)


def banner(url: str, cfg: dict) -> None:
    print("\n  ┌──────────────────────────────────────────────────────────────────────┐", flush=True)
    print("  │                     ПОДКЛЮЧЕНИЕ MCP                                  │", flush=True)
    print("  └──────────────────────────────────────────────────────────────────────┘", flush=True)
    print("Статус: РАБОТАЕТ", flush=True)
    print(f"URL: {url}", flush=True)
    print(f"X-Api-Key: {cfg['api_key']}", flush=True)
    print("Транспорт: Streamable HTTP", flush=True)
    print("Доступ: ВЕСЬ ПК (в пределах прав вашей учётной записи Windows)", flush=True)
    print("ВНИМАНИЕ: обладатель URL и ключа получит доступ к вашим файлам и терминалу.", flush=True)
    print("Не показывайте эту консоль другим. Для остановки: Ctrl+C или stop_notion.bat.\n", flush=True)


def select_port(preferred, attempts=32):
    """Probe by binding, not by killing a listener. Never modify the saved preference."""
    for port in range(preferred, min(65536, preferred + attempts)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run(args) -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    if not args.print_url:
        print("Статус: ЗАПУСК", flush=True)
    STOP_FILE.unlink(missing_ok=True)
    _recover()
    preferred = cfg["port"]
    cfg["port"] = select_port(preferred)
    if not args.print_url:
        if cfg["port"] != preferred:
            print(f"[порт] {preferred} занят. Используем {cfg['port']}; существующие процессы не изменены.", flush=True)
        print(f"Локальный порт: {cfg['port']}", flush=True)
    if not cfg["secret_path"]:
        cfg["secret_path"] = "mcp-" + secrets.token_hex(24)
        # Persist only the intended key; preserve explicit binary paths and user options.
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        saved["secret_path"] = cfg["secret_path"]
        atomic_write(CONFIG_FILE, json.dumps(saved, indent=2, ensure_ascii=False) + "\n")
    changed = apply_dc_config(cfg)
    if changed and not args.print_url:
        print(f"[настройки] Обновлено параметров: {len(changed)}. Остальные значения сохранены.")
    proxy = tunnel = None
    jobs = []
    stage = "создание группы процессов прокси"
    try:
        proxy_job = ProcessJob()
        jobs.append(proxy_job)
        write_status(cfg, "", state="starting")
        stage = "запуск локального MCP-прокси"
        proxy = start_proxy(cfg)
        proxy_job.add(proxy)
        write_status(cfg, "", proxy, state="starting")
        end = time.monotonic() + 20
        while not port_busy(cfg["port"]):
            if proxy.poll() is not None or time.monotonic() >= end or STOP_FILE.exists():
                raise SystemExit("Прокси не запустился. Проверьте runtime/bridge.log.")
            time.sleep(0.2)
        if not args.print_url:
            print("Статус: ПРОКСИ ГОТОВ; открываем туннель…", flush=True)
        # Store/MSIX ngrok may already belong to a different Windows job tree.
        # A separate, initially empty job can nest there; a shared Node job cannot.
        stage = "создание отдельной группы процессов ngrok"
        tunnel_job = ProcessJob()
        jobs.append(tunnel_job)
        stage = "запуск ngrok"
        tunnel = start_ngrok(cfg)
        stage = "привязка ngrok к отдельной группе процессов"
        tunnel_job.add(tunnel)
        write_status(cfg, "", proxy, tunnel, state="starting")
        stage = "получение публичного URL ngrok"
        url = public_url(cfg)
        if STOP_FILE.exists():
            return 0
        if not url or tunnel.poll() is not None:
            raise SystemExit("Не удалось получить URL туннеля. Проверьте runtime/ngrok.log.")
        full = f"{url}/{cfg['secret_path']}"
        ready = False
        for _ in range(5):
            if STOP_FILE.exists():
                return 0
            if proxy.poll() is not None or tunnel.poll() is not None:
                raise SystemExit("Дочерний процесс завершился во время запуска. Проверьте логи в runtime/.")
            st, body, _ = mcp_call(full, {
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "bridge-readiness", "version": "1.1"}}},
                headers_extra=authentication_headers(cfg), timeout=10)
            if st == 200 and isinstance(body, dict) and "result" in body:
                ready = True
                break
            time.sleep(1)
        if not ready:
            raise SystemExit("Проверка готовности MCP с авторизацией не пройдена. Проверьте логи в runtime/.")
        if args.print_url:
            print(full)
            return 0
        if args.verify and not args.print_url:
            ok, _ = verify(full, cfg)
            if not ok:
                print("Статус: ОШИБКА (проверка MCP не пройдена)", flush=True)
                return 1
        write_status(cfg, full, proxy, tunnel)
        banner(full, cfg)
        while not STOP_FILE.exists():
            if proxy.poll() is not None or tunnel.poll() is not None:
                print("[мост] Дочерний процесс неожиданно завершился. Проверьте логи в runtime/.")
                return 1
            time.sleep(0.5)
        return 0
    except OSError as exc:
        code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        detail = f"; код Windows/ОС: {code}" if code is not None else ""
        raise OSError(f"Этап: {stage}{detail}. {exc}") from exc
    except KeyboardInterrupt:
        print("\n[мост] Останавливаем…")
        return 0
    finally:
        # Closing the Job also removes descendants when a root already exited.
        try:
            cleanup_processes(tunnel, proxy)
        finally:
            for owned_job in reversed(jobs):
                owned_job.close()
            STATUS_FILE.unlink(missing_ok=True)
            STOP_FILE.unlink(missing_ok=True)
        if not args.print_url:
            print("Статус: ОСТАНОВЛЕН", flush=True)