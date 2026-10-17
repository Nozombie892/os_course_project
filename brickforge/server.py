"""本地 HTTP 服务：静态画布 + JSON API。标准库即可运行。"""

from __future__ import annotations

import json
import signal
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from brickforge import ipc, persist, runtime, signals
from brickforge.daemon import start_daemon, stop_daemon
from brickforge.kernel import simulate

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
HOST = "127.0.0.1"
PORT = 8787
_DAEMON = None
_SERVER: ThreadingHTTPServer | None = None


def emergency_stop() -> dict:
    """急停：给每个在册的车间进程发 STOP 信号（POSIX 上是 SIGTERM）。

    注意顺序：先宣布班次收工，否则守护进程会把"被你急停掉的车间"
    当成崩溃，2 秒后又给你拉起来。
    """
    run_id = persist.load_run().get("run_id")
    if run_id:
        persist.save_run({"run_id": run_id, "state": "stopping", "stopped_at": time.time()})
    hub = signals.SignalHub("server")
    sent, failed = [], []
    for entry in persist.load_procs():
        pid = entry.get("pid")
        if not pid:
            continue
        row = {"pid": pid, "shop": entry.get("shop_id"), "name": entry.get("name")}
        (sent if hub.send(pid, signals.STOP) else failed).append(row)
    persist.save_run({"run_id": None, "state": "idle", "stopped_at": time.time()})
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
        if path == "/api/run":
            return self._json(runtime.status())
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
        if path == "/api/run":
            body = body or {}
            return self._json(
                runtime.start(
                    float(body.get("shop_seconds") or 90),
                    float(body.get("tick") or 0.35),
                    body.get("layout"),
                )
            )
        if path == "/api/run/stop":
            return self._json(runtime.stop())
        if path == "/api/kill":
            return self._json(runtime.kill((body or {}).get("shop_id")))
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
    global _DAEMON, _SERVER
    persist.DATA.mkdir(exist_ok=True)
    if not persist.LAYOUT.exists():
        persist.save_layout(persist.default_layout())

    # 先建服务再起守护进程：端口被占时端口错在守护进程之前暴露，
    # 不会留下一个"服务没起来、守护进程却在空转"的半成品状态。
    _SERVER = ThreadingHTTPServer((HOST, PORT), Handler)
    _install_sigterm()
    persist.write_watchdog()
    _start_owner_heartbeat()
    _DAEMON = start_daemon()
    try:
        print(f"积木工坊已启动 http://{HOST}:{PORT}")
        print("厂务守护进程 pid =", getattr(_DAEMON, "pid", None))
        _SERVER.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出")
    finally:
        try:
            runtime.stop()
        except Exception:
            pass
        stop_daemon(_DAEMON)
        persist.clear_watchdog()
        try:
            _SERVER.server_close()
        except Exception:
            pass


def _start_owner_heartbeat() -> None:
    """周期刷新 watchdog.json：告诉守护进程"我还活着"。

    没有这一步，服务被强杀（关控制台/任务管理器）后守护进程会一直空转，
    变成没人管、还在拉起车间的孤儿。
    """
    import threading

    def loop() -> None:
        while True:
            time.sleep(3.0)
            try:
                persist.write_watchdog()
            except Exception:
                pass

    threading.Thread(target=loop, name="owner-heartbeat", daemon=True).start()


def _install_sigterm() -> None:
    """收到 SIGTERM 也走优雅退出（走不到 KeyboardInterrupt 的话会留孤儿守护进程）。

    信号处理程序只做最轻的事：把 KeyboardInterrupt 抛出来，由 main 的 finally
    统一收尾。Windows 上 SIGTERM 不可注册，自动跳过。
    """
    def handler(signum, _frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, handler)
    except (OSError, ValueError, AttributeError):
        pass


if __name__ == "__main__":
    main()
