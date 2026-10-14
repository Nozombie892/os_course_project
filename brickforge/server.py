"""本地 HTTP 服务：静态画布 + JSON API。标准库即可运行。"""

from __future__ import annotations

import json
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from brickforge import ipc, persist, signals
from brickforge.daemon import start_daemon, stop_daemon
from brickforge.kernel import simulate

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
HOST = "127.0.0.1"
PORT = 8787
_DAEMON = None


def emergency_stop() -> dict:
    """急停：给每个在册的车间进程发 STOP 信号（POSIX 上是 SIGTERM）。"""
    hub = signals.SignalHub("server")
    sent, failed = [], []
    for entry in persist.load_procs():
        pid = entry.get("pid")
        if not pid:
            continue
        row = {"pid": pid, "shop": entry.get("shop_id"), "name": entry.get("name")}
        (sent if hub.send(pid, signals.STOP) else failed).append(row)
    persist.append_log(
        f"{time.strftime('%H:%M:%S')} 急停信号已投递 {len(sent)} 个车间进程"
        f"（失败 {len(failed)}）"
    )
    return {"ok": True, "sent": sent, "failed": failed, "count": len(sent),
            "caps": signals.capabilities()}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP), **kwargs)

    def log_message(self, fmt, *args):
        print("[http]", args[0] if args else fmt)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/layout":
            return self._json(persist.load_layout())
        if path == "/api/status":
            payload = persist.load_status()
            payload["log"] = persist.tail_log()
            return self._json(payload)
        if path == "/api/procs":
            return self._json({"procs": persist.load_procs(),
                               "caps": signals.capabilities()})
        if path == "/api/board":
            board = ipc.open_board(reset=False)
            snap = board.snapshot()
            board.close()
            return self._json(snap)
        if path == "/api/health":
            return self._json({"ok": True})
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json()
        if path == "/api/layout":
            return self._json(persist.save_layout(body or persist.load_layout()))
        if path == "/api/simulate":
            layout = body or persist.load_layout()
            persist.save_layout(layout)
            return self._json(simulate(layout))
        if path == "/api/stop":
            return self._json(emergency_stop())
        if path == "/api/reset":
            return self._json(persist.save_layout(persist.default_layout()))
        self.send_error(404)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def _json(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    global _DAEMON
    persist.DATA.mkdir(exist_ok=True)
    if not persist.LAYOUT.exists():
        persist.save_layout(persist.default_layout())
    _DAEMON = start_daemon()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        print(f"积木工坊已启动 http://{HOST}:{PORT}")
        print("厂务守护进程 pid =", getattr(_DAEMON, "pid", None))
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出")
    finally:
        stop_daemon(_DAEMON)


if __name__ == "__main__":
    main()
