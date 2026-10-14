"""丙：车间进程与机器线程。每个车间一个 Process，每台机器一个 Thread。

对应课件《操作系统原理课程设计》：
    第 4 页 进程控制和管理（进程环境 / 进程控制 / 进程间关系）
    第 5 页 线程及线程编程（线程创建及管理 / 线程同步 / 线程间通信）

本模块把四件事落到实处：
    进程      每个车间一个 multiprocessing.Process，启动时登记、正常结束时注销
    线程      车间内每台机器一个 Thread
    线程同步  state_lock 保护共享状态；tools 信号量表示车间里有限的共享工装
    线程通信  线程把完工记录交给车间，车间再经管道投递给下游车间
"""

from __future__ import annotations

import os
import threading
import time
from queue import Empty
from threading import Lock, Thread

from brickforge import ipc, signals
from brickforge.persist import HB_DIR, register_proc, unregister_proc

TOOL_SLOTS = 2          # 一个车间里共享工装的数量（线程互斥资源）
TOOL_WAIT = 1.0         # 抢不到工装的等待上限（秒）


def beat(shop_id: str) -> None:
    """写一次心跳：把我的活跃时间戳留给守护进程看。"""
    HB_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (HB_DIR / f"{shop_id}.hb").write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def shop_worker(
    shop: dict,
    jobs: list[dict],
    material,
    report_q,
    tick: float = 0.03,
    ctx: dict | None = None,
) -> None:
    """一个车间进程的主体。

    material 是跨进程信号量（库位）；ctx 携带看板槽位与管道端点。
    """
    ctx = ctx or {}
    shop_id = shop.get("id") or "shop"
    index = int(ctx.get("index") or 0)
    senders = ctx.get("senders") or []
    receivers = ctx.get("receivers") or []

    # ---- 信号处理程序：急停 -------------------------------------------------
    hub = signals.SignalHub(f"shop-{shop_id}")
    stop = {"flag": False}
    hub.on(signals.STOP, lambda *_: stop.__setitem__("flag", True))
    hub.install((signals.STOP,))

    # ---- 共享内存看板：所有进程挂到同一块内存上 -----------------------------
    board = ipc.open_board(reset=False)

    # ---- 登记自己，让守护进程能发现我的异常退出 -----------------------------
    my_pid = os.getpid()
    register_proc(
        {
            "pid": my_pid,
            "kind": "shop",
            "shop_id": shop_id,
            "name": shop.get("name") or shop_id,
            "started_at": time.time(),
            "shop": shop,
            "jobs": jobs,
            "slots": int(ctx.get("slots") or 1),
            "index": index,
            "tick": float(tick),
            "revives": int(ctx.get("revives") or 0),
        }
    )

    beat(shop_id)
    # ---- 上游管道：开工前收下半成品批次（短暂等待，给上游投递的时间窗口）----
    upstream = ipc.collect(receivers, wait=float(ctx.get("pipe_wait") or 0.3))

    # ---- 线程共享状态 -------------------------------------------------------
    state_lock = Lock()
    tools = threading.Semaphore(TOOL_SLOTS)     # 车间里有限的共享工装
    finished: list[dict] = []
    blocked = 0
    tool_waits = 0
    delivered = 0
    machines = shop.get("machines") or [{"id": "idle", "burst": 1}]

    def machine_loop(machine: dict, assigned: list[dict]) -> None:
        nonlocal blocked, tool_waits, delivered
        for job in assigned:
            if stop["flag"]:
                return
            beat(shop_id)
            # 同步一：抢占库位（跨进程信号量）
            if not material.acquire(timeout=0.4):
                with state_lock:
                    blocked += 1
                continue
            try:
                # 同步二：抢占车间共享工装（线程信号量 → 互斥）
                if not tools.acquire(timeout=TOOL_WAIT):
                    with state_lock:
                        tool_waits += 1
                    continue
                try:
                    staff = max(int(machine.get("workers") or 0), 0)
                    pace = max(0.22, 0.12 + min(staff, 3) * 0.42)
                    time.sleep(max(job.get("burst", 1), 1) * tick / pace)
                finally:
                    tools.release()
            finally:
                material.release()

            # 线程同步：保护共享的完工列表
            with state_lock:
                finished.append(
                    {"job": job["id"], "machine": machine.get("id"), "shop": shop_id}
                )
                # 管道投递：完工一件就通知下游。等全部做完再发，下游往往已经收工了
                if senders:
                    delivered += ipc.deliver(
                        senders,
                        {"from": shop_id, "count": 1, "jobs": [job["id"]],
                         "at": round(time.time(), 3)},
                    )
            # 线程通信：把完工件数写进共享内存看板（每个车间独占一个槽位）
            board.bump(ipc.SLOT_SHOP_BASE + index)
            beat(shop_id)

    threads: list[Thread] = []
    if not jobs:
        beat(shop_id)
    else:
        chunks: list[list[dict]] = [[] for _ in machines]
        for i, job in enumerate(jobs):
            chunks[i % len(machines)].append(job)
        for machine, assigned in zip(machines, chunks):
            th = Thread(target=machine_loop, args=(machine, assigned), daemon=True)
            threads.append(th)
            th.start()
        for th in threads:
            th.join()

    board.close()
    report_q.put(
        {
            "shop_id": shop_id,
            "finished": finished,
            "blocked": blocked,
            "tool_waits": tool_waits,
            "received": upstream,
            "delivered": delivered,
            "stopped": stop["flag"],
            "signals": hub.snapshot()["received"],
            "pid": my_pid,
        }
    )
    # ---- 正常结束：注销登记，守护进程不会把我误判成崩溃 ---------------------
    unregister_proc(my_pid)
    hub.restore()


def drain(q, expected: int, timeout: float = 8.0) -> list:
    got = []
    deadline = time.time() + timeout
    while len(got) < expected and time.time() < deadline:
        try:
            got.append(q.get(timeout=0.2))
        except Empty:
            continue
    return got
