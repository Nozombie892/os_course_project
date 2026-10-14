"""新补的操作系统原语：信号 / 共享内存 / 管道 / 进程登记表。

对应课件《操作系统原理课程设计》第 4、5 页点名而原先缺失的部分。

运行：
    python -m tests.test_primitives
"""

import time

from brickforge import ipc, signals
from brickforge.persist import load_procs, register_proc, unregister_proc

TEST_BOARD = "brickforge_board_test"


def test_signals():
    """信号处理程序、软信号触发、可靠信号能力探测。"""
    hub = signals.SignalHub("test")
    got = []
    hub.on(signals.STOP, lambda name, sig: got.append(name))
    hub.install((signals.STOP,))

    hub.soft(signals.STOP)                     # Windows 上走软信号路径
    assert hub.total(signals.STOP) == 1, "处理程序没有被调用"
    assert got == [signals.STOP]
    assert hub.take(signals.STOP) == 1
    assert hub.pending(signals.STOP) == 0

    caps = signals.capabilities()
    assert caps["platform"] in ("windows", "posix")
    assert "reliable" in caps
    hub.restore()

    # 定时器两条路径都要能起来
    ticker = signals.IntervalTicker(0.05, hub=hub)
    mode = ticker.start()
    time.sleep(0.2)
    ticker.stop()
    assert mode in ("itimer", "thread")
    print(f"信号：处理程序可用；平台={caps['platform']} 可靠信号={caps['reliable']} "
          f"定时器={mode} 急停={caps['stop']}")


def test_shared_memory():
    """共享内存看板：一个 Board 写，另一个 Board 应看到同一块内存。"""
    board = ipc.Board(TEST_BOARD)
    board.open(reset=True)
    assert not board.degraded, f"共享内存不可用: {board.error}"

    board.set(ipc.SLOT_GOOD, 42)
    board.bump(ipc.SLOT_SHOP_BASE + 1)
    board.bump(ipc.SLOT_SHOP_BASE + 1)

    other = ipc.Board(TEST_BOARD)
    assert other.open(reset=False), "第二个 Board 无法 attach"
    assert other.get(ipc.SLOT_GOOD) == 42, "跨 Board 读不到写入的值"
    assert other.get(ipc.SLOT_SHOP_BASE + 1) == 2

    snap = other.snapshot()
    assert snap["good"] == 42
    assert snap["shop_slots"]["slot1"] == 2

    # reset 必须保留守护进程槽位
    other.set(ipc.SLOT_DAEMON_PID, 1234)
    board.open(reset=True)
    assert board.get(ipc.SLOT_DAEMON_PID) == 1234, "reset 把守护进程槽位清掉了"
    assert board.get(ipc.SLOT_GOOD) == 0

    other.close()
    board.close()
    print("共享内存：跨进程可见，车间槽位 =", snap["shop_slots"])


def test_pipes():
    """管道：上游投递、下游收取。"""
    bundle = ipc.make_pipes([{"from": "a", "to": "b"}, {"from": "b", "to": "c"}])
    assert bundle["pairs"] == [("a", "b"), ("b", "c")]

    assert ipc.deliver(bundle["senders"]["a"], {"from": "a", "count": 3}) == 1
    got = ipc.collect(bundle["receivers"]["b"], wait=0.3)
    assert got and got[0]["count"] == 3, "下游没收到上游投递的数据"

    # 没有上游数据的车间不该被卡住
    assert ipc.collect(bundle["receivers"]["c"], wait=0.05) == []
    ipc.close_pipes(bundle)
    print("管道：a→b 投递并收取", got[0]["count"], "件；无数据时非阻塞返回")


def test_proc_registry():
    """进程登记表：登记、注销、重复注销。"""
    pid = 999001
    try:
        assert register_proc({"pid": pid, "shop_id": "sX", "revives": 0})
        assert any(r.get("pid") == pid for r in load_procs())
        assert unregister_proc(pid) is True
        assert not any(r.get("pid") == pid for r in load_procs())
        assert unregister_proc(pid) is False, "重复注销应返回 False"
    finally:
        unregister_proc(pid)
    print("进程登记表：登记 / 注销 / 重复注销返回 False")


if __name__ == "__main__":
    test_signals()
    test_shared_memory()
    test_pipes()
    test_proc_registry()
    print("OS 原语测试通过。")
