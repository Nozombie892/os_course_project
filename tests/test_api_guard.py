"""端到端验收：起真服务，用 HTTP 调新接口，验证守护进程真的能拉起车间进程。

跑法（项目根目录）：
    python tests/test_api_guard.py

为什么不放在 pytest 里：它要占 8787 端口、要等 2 秒巡视周期，更适合当
"答辩前跑一遍"的验收脚本。pytest 里那份（tests/test_run_guard.py）
用同样的代码路径但不占端口。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"


def call(path: str, method: str = "GET", body: dict | None = None, timeout: float = 60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_until(fn, timeout: float, interval: float = 0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = fn()
        except (urllib.error.URLError, ConnectionError):
            value = None
        if value:
            return value
        time.sleep(interval)
    return None


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "brickforge"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    failures: list[str] = []
    try:
        assert wait_until(lambda: call("/api/health").get("ok"), 30), "服务没起来"

        # 0) 先收工，避免上一轮残留
        call("/api/run/stop", "POST", {})

        # 1) 起长跑班次
        started = call("/api/run", "POST", {"shop_seconds": 120, "tick": 0.4})
        if not started.get("ok"):
            failures.append(f"起班失败：{started}")
        else:
            print(f"[1] 班次 {started['run_id']} 起，车间={[(s['shop_id'], s['pid']) for s in started['started']]}")

        # 2) 守护进程应当把这些车间纳入守护范围（这就是以前永远为 0 的 guarded）
        guarded = wait_until(lambda: (call("/api/run").get("guarded") or None), 20)
        if not guarded:
            failures.append("/api/run 里没有守护中的车间")
        else:
            print(f"[2] 守护中 = {[(g['shop_id'], g['pid']) for g in guarded]}")

        status = wait_until(lambda: (call("/api/status").get("guarded") or None), 20)
        if not status:
            failures.append("status.guarded 为空：守护进程没在巡视这些车间")
        else:
            print(f"[2b] status.guarded = {len(status)} 个，守护 pid={call('/api/status').get('daemon_pid')}")

        # 3) 杀掉一个车间
        killed = call("/api/kill", "POST", {})
        if not killed.get("ok"):
            failures.append(f"杀进程失败：{killed}")
            target_pid = None
        else:
            target_pid = killed["killed"]["pid"]
            shop = killed["killed"]["shop_id"]
            print(f"[3] 杀掉车间 {shop} pid={target_pid}（{killed['mode']}）")

        # 4) 守护进程应在两轮巡视内把它拉起来
        if target_pid:
            revived = wait_until(
                lambda: next(
                    (g for g in (call("/api/run").get("guarded") or [])
                     if g["pid"] != target_pid and g["shop_id"] == shop),
                    None,
                ),
                30,
            )
            if not revived:
                failures.append(f"守护进程没有拉起车间 {shop}（原 pid={target_pid}）")
            else:
                print(f"[4] 已拉起：{shop} 新 pid={revived['pid']}，累计拉起={revived['revives']}")

                # 5) 拉起计数与日志
                board = call("/api/board")
                if int(board.get("restarts") or 0) < 1:
                    failures.append("共享内存看板上的累计拉起没有涨")
                log = "\n".join(call("/api/status").get("log") or [])
                if str(target_pid) not in log or str(revived["pid"]) not in log:
                    failures.append("daemon.log 里没有这次拉起的记录")
                else:
                    print(f"[5] 看板累计拉起={board.get('restarts')}，日志已记录")

        # 6) 收工后不能再被拉起
        call("/api/run/stop", "POST", {})
        time.sleep(5)
        left = [p for p in (call("/api/run").get("procs") or []) if p["state"] == "running"]
        if left:
            failures.append(f"收工后仍有车间被拉起：{left}")
        else:
            print("[6] 收工后无残留进程")
    finally:
        try:
            proc.terminate()
            out, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        if out:
            print("--- 服务端输出 ---")
            print(out.strip()[-1500:])

    if failures:
        print("\n失败项：")
        for f in failures:
            print(" -", f)
        return 1
    print("\n端到端验收通过：长跑班次 + 守护进程保活 + 收工清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
