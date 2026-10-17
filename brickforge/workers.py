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


def run_alive(run_id: str | None) -> bool:
    """长跑班次是否还该继续。

    守护进程和运行时都靠 data/run.json 判断。班次一旦被宣布收工，
    车间进程自己就会退出，不会等守护进程把"正常收工"错当崩溃再拉起来。
    """
    if not run_id:
        return True
    try:
        from brickforge import persist

        return persist.active_run_id() == run_id
    except Exception:
        return False


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

    两种寿命模型（ctx 决定）：
        一锤子  ctx 里没有 run_seconds、run_id —— 做完这批工单就正常注销退出，
                这是 /api/simulate 的用法，一次仿真 = 一个班次。
        长跑    ctx 里有 run_seconds、run_id —— 车间反复消化工单，直到班次收工
                或收到急停信号才退出。**持续生产时车间进程才活得过守护进程的
                巡视间隔，守护进程才有活可干（见 README 的守护进程演示）。**
    """
    ctx = ctx or {}
    shop_id = shop.get("id") or "shop"
    index = int(ctx.get("index") or 0)
    senders = ctx.get("senders") or []
    receivers = ctx.get("receivers") or []
    run_id = ctx.get("run_id")
    run_seconds = max(float(ctx.get("run_seconds") or 0.0), 0.0)
    long_run = bool(run_id and run_seconds > 0)

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
            "run_id": run_id,
            "run_seconds": run_seconds,
            "links": list(ctx.get("links") or []),
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
    cursor = 0
    machines = shop.get("machines") or [{"id": "idle", "burst": 1}]

    def take_job():
        """从工单池里取一件。长跑时取完一轮就复用，持续生产。

        取活时就把游标推进（在锁里），否则两个线程会领到同一件工单。
        一锤子模式下取完就返回 None —— 不能偷偷多产，否则产量对不上。
        """
        nonlocal cursor
        with state_lock:
            if not jobs:
                return None
            if cursor >= len(jobs):
                if not long_run:
                    return None
                cursor = 0
            job = dict(jobs[cursor])
            cursor += 1
            return job

    def done() -> bool:
        if stop["flag"]:
            return True
        if long_run:
            return not run_alive(run_id)
        return False

    def machine_loop(machine: dict, assigned: list[dict] | None) -> None:
        nonlocal blocked, tool_waits, delivered
        pool = list(assigned) if assigned is not None else None
        while not done():
            # 一锤子模式：只吃自己分到的那批，吃完就退（不能多产）
            # 长跑模式：从车间共享工单池里领活，领完一轮再复用（持续生产）
            job = pool.pop(0) if pool else (take_job() if long_run else None)
            if job is None:
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
                    {"job": job.get("id"), "machine": machine.get("id"), "shop": shop_id}
                )
                # 管道投递：完工一件就通知下游。等全部做完再发，下游往往已经收工了
                if senders:
                    delivered += ipc.deliver(
                        senders,
                        {"from": shop_id, "count": 1, "jobs": [job.get("id")],
                         "at": round(time.time(), 3)},
                    )
            # 线程通信：把完工件数写进共享内存看板（每个车间独占一个槽位）
            board.bump(ipc.SLOT_SHOP_BASE + index)
            beat(shop_id)

    threads: list[Thread] = []
    if long_run:
        # 持续生产：每台机器各跑一个线程，从同一个工单池里循环取活
        for machine in machines:
            th = Thread(target=machine_loop, args=(machine, None), daemon=True)
            threads.append(th)
            th.start()
        deadline = time.time() + run_seconds
        while threads and time.time() < deadline and not done():
            beat(shop_id)
            time.sleep(0.1)
            threads = [t for t in threads if t.is_alive()]
        # 班次到点或收工：让线程在下一个循环里自己看到 done()
        stop["flag"] = True
        for th in threads:
            th.join(timeout=2.0)
    elif not jobs:
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
            "run_id": run_id,
            "long_run": long_run,
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
