"""长跑班次 + 守护进程保活：杀掉一个车间进程，看守护进程是否真的把它拉起来。

这是课件第 15 页"可运行：代码要跑通，守护线程有效"那一项的验收脚本：
不是断言"表单里 pid 变了"，而是断言**新的 pid 真的活着、能应答、拉起计数 +1、
日志里留下了这次拉起的记录**。

跑法（必须在项目根目录）：
    python -m tests.test_run_guard
"""
from __future__ import annotations

import time

from brickforge import persist, runtime
from brickforge.daemon import _alive, start_daemon, stop_daemon

# 端到端验证：start_daemon() 起的守护进程与 runtime.start() 起的车间都是真实进程，
# 与界面按钮走的是完全相同的代码路径（不开端口，免得和正在跑的界面抢 8787）。

REVIVE_DEADLINE = 30.0      # 守护进程每 2 秒巡视一次，留足余量
KILL_GRACE = 3.0


def _wait_for(fn, timeout: float, interval: float = 0.3):
    """轮询直到 fn() 返回真值。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def test_long_run_and_revive() -> None:
    # 清场：别让上一次留下的班次/登记影响判断
    try:
        runtime.stop()
    except Exception:
        pass
    persist.purge_procs(None)
    persist.save_run({"run_id": None, "state": "idle"})

    daemon_proc = start_daemon()
    try:
        started = runtime.start(shop_seconds=60, tick=0.35)
        assert started.get("ok"), f"长跑班次没起来：{started}"

        # 1) 车间进程真的活着、并且被登记（守护进程才有守护对象）
        guarded = _wait_for(
            lambda: [r for r in runtime.status()["guarded"] if r["state"] == "running"],
            timeout=15,
        )
        assert guarded, "长跑班次里没有活着的车间进程"
        target = guarded[0]
        old_pid = int(target["pid"])
        assert _alive(old_pid), f"登记里的 pid={old_pid} 其实不活着"

        # 2) 守护进程认领了它（status.json 里 guarded 非空）
        claim = _wait_for(
            lambda: (persist.load_status().get("guarded") or None), timeout=10
        )
        assert claim, "守护进程没有把车间进程纳入巡视范围（status.guarded 为空）"

        # 3) 主动杀掉它 —— 这就是"软件崩溃了"的场景
        killed = runtime.kill(target["shop_id"])
        assert killed.get("ok"), f"杀进程失败：{killed}"
        assert _wait_for(lambda: not _alive(old_pid), timeout=KILL_GRACE), \
            f"pid={old_pid} 没有被杀掉"

        # 4) 守护进程应当发现"登记还在、进程没了"，并按登记信息拉起新进程
        revived = _wait_for(
            lambda: next(
                (
                    r
                    for r in runtime.status()["guarded"]
                    if r["shop_id"] == target["shop_id"]
                    and r["pid"] != old_pid
                    and r["state"] == "running"
                    and _alive(int(r["pid"]))
                ),
                None,
            ),
            timeout=REVIVE_DEADLINE,
        )
        assert revived, (
            f"守护进程没有拉起车间 {target['shop_id']}（原 pid={old_pid}）；"
            f"当前登记={runtime.status()['procs']}"
        )
        new_pid = int(revived["pid"])

        # 5) 新的 pid 必须真的活着，而且拉起次数 +1
        assert _alive(new_pid), f"新的 pid={new_pid} 不活着（可能是僵尸）"
        assert int(revived["revives"]) >= 1, "拉起次数没有累加"
        assert new_pid != old_pid

        # 6) 日志里必须有这次拉起的记录（答辩时可以直接给人看）
        log = persist.tail_log(40)
        hit = [line for line in log if str(old_pid) in line and str(new_pid) in line]
        assert hit, f"daemon.log 里没有 pid={old_pid} → pid={new_pid} 的拉起记录；最近日志={log[-6:]}"

        # 7) 共享内存看板上的"累计拉起"也要涨
        board = persist.load_status().get("restarts")
        assert int(board or 0) >= 1, "状态里的累计拉起没有增加"
    finally:
        runtime.stop()
        stop_daemon(daemon_proc)


def test_stop_does_not_revive() -> None:
    """收工之后守护进程不能再拉起车间（否则 Ctrl+C 永远退不干净）。"""
    try:
        runtime.stop()
    except Exception:
        pass
    persist.purge_procs(None)
    persist.save_run({"run_id": None, "state": "idle"})

    daemon_proc = start_daemon()
    try:
        started = runtime.start(shop_seconds=30, tick=0.3)
        assert started.get("ok"), started
        assert _wait_for(lambda: runtime.status()["guarded"], timeout=15), "车间没起来"

        runtime.stop()
        time.sleep(5)      # 跨过至少两轮巡视
        leftover = [r for r in runtime.status()["procs"] if r["state"] == "running"]
        assert not leftover, f"收工后还有车间被拉起：{leftover}"
    finally:
        runtime.stop()
        stop_daemon(daemon_proc)


if __name__ == "__main__":
    test_long_run_and_revive()
    print("长跑 + 保活：通过")
    test_stop_does_not_revive()
    print("收工不复活：通过")
