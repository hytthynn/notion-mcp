#!/usr/bin/env python3
"""Re-verify an already-running bridge without restarting it.

Reads status.json, then walks the MCP handshake (initialize -> tools/list)
over the PUBLIC ngrok URL and prints the exposed tool names.

Usage: python verify_bridge.py
Exit:  0 = the cloud agent can reach and list tools, 1 = it cannot.
"""
from __future__ import annotations

import json
from .cli import RussianParser
import re
import time
from pathlib import Path

from . import bridge
from .paths import STATUS_FILE as STATUS, CONFIG_FILE as CONFIG



def prove_tools(url: str, cfg: dict, sid: str | None) -> bool:
    """Actually exercise tools over the public URL and inspect the full-access configuration.

    tools/list only proves the tunnel is wired. This proves the cloud agent can
    act on this PC, and reports honestly the running configuration.
    Reuses the session bridge.verify() already opened: a second initialize against
    the same proxy hangs on an unclosed stream, so there is one handshake per run.
    """
    extra = {"X-API-Key": cfg["api_key"]} if cfg.get("api_key") else None

    def call_tool(req_id: int, name: str, arguments: dict) -> tuple[bool | None, str]:
        """True = the tool ran, False = the server refused it, None = transport failed.

        Conflating None with False once reported a dropped request as "sandbox is
        gone" — a false security alarm. One retry absorbs transient tunnel drops.
        """
        st_, b_ = None, None
        for attempt in (1, 2):
            st_, b_, _ = bridge.mcp_call(url, {
                "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}, sid, extra, timeout=90)
            if st_ == 200 and isinstance(b_, dict):
                break
            if attempt == 1:
                time.sleep(2)
        if st_ != 200 or not isinstance(b_, dict):
            return None, f"HTTP {st_}: {str(b_)[:300]}"
        if "error" in b_:
            return None, str(b_["error"])[:300]
        res = b_.get("result") or {}
        text = "".join(p.get("text", "") for p in res.get("content", []) if isinstance(p, dict))
        return (not res.get("isError")), text

    allowed = cfg.get("desktop_commander", {}).get("allowedDirectories", [])
    ok = True
    if allowed:
        good, out = call_tool(2, "list_directory", {"path": allowed[0]})
        label = "ГОТОВО" if good else ("ОШИБКА ТРАНСПОРТА" if good is None else "ОТКАЗ")
        print(f"[диагностика] list_directory {allowed[0]} → {label} (символов в списке: {len(out)})")
        if not good:
            print(f"        {out[:300]}")
            ok = False
    good, out = call_tool(3, "get_config", {})
    if good:
        blob = out[out.find("{"):] if "{" in out else out
        try:
            server_cfg = json.loads(blob)
        except json.JSONDecodeError:
            print("[диагностика] Некорректный ответ get_config")
            ok = False
            server_cfg = {}
        server_dirs = server_cfg.get("allowedDirectories")
        print(f"[диагностика] get_config выполнен: разрешённые папки={server_dirs}; "
              f"заблокированных команд={len(server_cfg.get('blockedCommands', []))}; "
              f"телеметрия={'включена' if server_cfg.get('telemetryEnabled') else 'выключена'}")
        if not isinstance(server_dirs, list) or set(server_dirs) != set(allowed):
            ok = False
            print("[диагностика] ВНИМАНИЕ: сервер не применил настройку allowedDirectories — "
                  "проверьте ~/.claude-server-commander/config.json")
    else:
        print(f"[диагностика] Ошибка get_config → {out[:300]}")
        ok = False
    # Full-access mode: do not claim a folder sandbox exists or probe private files.
    print("[диагностика] Полный доступ к ПК: ограничений по папкам нет.")
    marker = "BRIDGE-SHELL-OK"
    good, out = call_tool(5, "start_process",
                          {"command": f"echo {marker} && hostname", "timeout_ms": 20000})
    ran = good is True and marker in out
    print(f"[диагностика] Команда оболочки → {'ГОТОВО, выполнена на этом ПК' if ran else 'ОШИБКА: ' + out[:200]}")
    if not ran:
        ok = False
    sess = re.search(r'"sessionId":\s*"([^"]+)"', out)
    if sess:                                     # best-effort cleanup of the spawned shell
        call_tool(6, "force_terminate", {"sessionId": sess.group(1)})
    return ok


def main() -> int:
    parser = RussianParser(description="Проверка уже запущенного MCP-моста без перезапуска.")
    parser.add_argument("--prove", action="store_true", help="также проверить конфигурацию файлового доступа и безопасную команду оболочки")
    args = parser.parse_args()
    if not STATUS.exists():
        print("Нет runtime/status.json — мост не запущен. Запустите start_notion.bat.")
        return 1
    status = json.loads(STATUS.read_text(encoding="utf-8"))
    cfg = bridge.load_config()
    url = status.get("mcp_url")
    if not url:
        print("В runtime/status.json отсутствует mcp_url")
        return 1
    print(f"Мост: состояние={'работает' if status.get('state') == 'up' else 'неизвестно'}; PID={status.get('pid')}; "
          f"PID прокси={status.get('proxy_pid')}; PID ngrok={status.get('ngrok_pid')}")
    print(f"Время запуска={status.get('started_at')}; порт={status.get('port')}")
    ok, sid = bridge.verify(url, cfg)
    if ok and args.prove:
        # sid is None in stateless mode (mcp-proxy --stateless) — tools/call works without it.
        ok = prove_tools(url, cfg, sid)
    print("РЕЗУЛЬТАТ:", "ГОТОВО — публичное подключение MCP работает" if ok
          else "ОШИБКА — публичное подключение MCP недоступно")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Проверка не пройдена: {exc}") from None