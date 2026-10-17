"""独立复现：退出/Ctrl+C 后，真守护进程是否变成停不掉的孤儿。

在仓库根目录运行：
    python check_daemon_orphan.py
"""
from __future__ import annotations

import time

from brickforge import daemon
from brickforge.persist import PID_FILE


def main() -> None:
    proc = daemon.start_daemon()

    deadline = time.time() + 8
    while time.time() < deadline and not PID_FILE.exists():
        time.sleep(0.2)

    real_pid = None
    if PID_FILE.exists():
        try:
            real_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            real_pid = None

    print(f"start_daemon() 返回的对象 pid = {proc.pid}")
    print(f"pidfile 里记录的真守护进程 pid = {real_pid}")
    print(f"返回对象的 is_alive() = {proc.is_alive()}   <-- False 就说明 stop_daemon() 抓不到真身")

    # 模拟"按了 Ctrl+C"：走正常退出路径
    daemon.stop_daemon(proc)
    print(f"stop_daemon() 之后 pidfile 还存在 = {PID_FILE.exists()}")

    time.sleep(1.0)
    alive = bool(real_pid) and daemon._alive(real_pid)
    print(f"真守护进程 pid={real_pid} 是否还活着 = {alive}")

    if alive:
        print("=> 残留守护进程：Ctrl+C 停不掉它，它还会继续拉起车间。")
        print(f"   清理（Windows）：taskkill /PID {real_pid} /F")
        print(f"   清理（POSIX）  ：kill -9 {real_pid}")
    else:
        print("=> 没有残留，这一条已修好。")


if __name__ == "__main__":
    main()
