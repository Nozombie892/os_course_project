"""长跑（持续生产）班次：让车间进程活过守护进程的巡视间隔。

为什么需要这一层？
    原来的 /api/simulate 是"一锤子买卖"：车间进程只活几十毫秒，跑完工单就
    自己注销退出。守护进程每 2 秒才巡视一次，等它睁开眼睛，车间早就正常
    下班了 —— 于是 data/daemon.log 里永远是"守护=0"，守护进程看着像摆设。

    这个模块把车间进程的生命周期拉长：一个班次内车间反复消化工单，直到
    收工或急停。这样：
        1. procs.json 里的登记一直在，/api/procs 能看到"守护中：N 个车间"；
        2. 杀掉一个车间进程后，守护进程能在 2 秒内发现并按登记信息拉起；
        3. 演示"崩溃 → 重启"不再需要跑测试脚本，界面上点一下就行。

班次的"该不该继续"由 data/run.json 决定（persist.load_run / save_run）。
守护进程和车间进程都读同一份文件，所以"宣布收工"之后谁都不会再多拉一次。
"""

from __future__ import annotations

import os
import time
from multiprocessing import Process, Queue, Semaphore

from brickforge import ipc, persist, signals
from brickforge.workers import drain, shop_worker

# 本进程启动的车间进程（守护进程拉起的不在这里，它在 procs.json 里）
_PROCS: dict[str, Process] = {}
_BUNDLE: dict | None = None
_RUN_ID: str | None = None


def _run_id() -> str:
    return f"run-{os.getpid()}-{int(time.time() * 1000) % 1000000}"


def status() -> dict:
    """当前班次视图：给界面和 /api/run 用。"""
    run = persist.load_run()
    run_id = run.get("run_id") if run.get("state") == "running" else None
    procs = persist.load_procs()
    rows = []
    for entry in procs:
        pid = entry.get("pid")
        shop_id = entry.get("shop_id")
        alive = False
        try:
            from brickforge.daemon import _alive

            alive = bool(pid) and _alive(pid)
        except Exception:
            alive = False
        age = persist.heartbeat_age(shop_id) if shop_id else None
        rows.append(
            {
                "pid": pid,
                "shop_id": shop_id,
                "name": entry.get("name"),
                "state": "running" if alive else "dead",
                "revives": int(entry.get("revives") or 0),
                "heartbeat_age": round(age, 1) if age is not None else None,
                "started_at": entry.get("started_at"),
            }
        )
    rows.sort(key=lambda r: (str(r.get("shop_id")), r.get("pid") or 0))
    return {
        "run_id": run_id,
        "state": run.get("state") or "idle",
        "started_at": run.get("started_at"),
        "shop_seconds": run.get("shop_seconds"),
        "procs": rows,
        "guarded": [r for r in rows if r["state"] == "running"],
        "dead": [r for r in rows if r["state"] == "dead"],
        "local_pids": [p.pid for p in _PROCS.values() if p.is_alive()],
        "caps": signals.capabilities(),
        "log": persist.tail_log(8),
    }


def start(shop_seconds: float = 90.0, tick: float = 0.35, layout: dict | None = None) -> dict:
    """开一个长跑班次：每个车间一个常驻进程。"""
    global _BUNDLE, _RUN_ID

    current = persist.load_run()
    if current.get("state") == "running":
        return {"ok": True, "reused": True, **status()}

    # 布局必须是对象：HTTP 层已拦一次，这里再兜一次，免得别的调用方传进来把服务打挂
    layout = layout if isinstance(layout, dict) else None
    layout = layout or persist.load_layout()
    persist.save_layout(layout)
    shops = layout.get("shops") or []
    if not shops:
        return {"ok": False, "error": "布局里没有车间，先画一个车间再开长跑"}

    run_id = _run_id()
    persist.save_run(
        {
            "run_id": run_id,
            "state": "running",
            "started_at": time.time(),
            "shop_seconds": float(shop_seconds),
            "tick": float(tick),
        }
    )
    # 上一班的残留登记先清掉，避免守护进程把旧 pid 当成本班的死亡进程
    persist.purge_procs(None)

    slots = max(1, len(layout.get("shelves") or [8, 5, 12, 3]))
    material = Semaphore(slots)
    report_q: Queue = Queue()
    links = layout.get("links") or []
    _BUNDLE = ipc.make_pipes(links)
    bundle = _BUNDLE
    index_of = {s.get("id"): i for i, s in enumerate(shops) if s.get("id")}

    # 工单按车间轮转分配（车间进程会在内部循环复用这批工单）
    jobs = layout.get("jobs") or []
    grouped: dict[str, list[dict]] = {str(s.get("id")): [] for s in shops}
    order = [str(s.get("id")) for s in shops]
    for i, job in enumerate(jobs):
        grouped[order[i % len(order)]].append(job)

    started = []
    for shop in shops:
        sid = str(shop.get("id"))
        ctx = {
            "index": index_of.get(sid, 0),
            "slots": slots,
            "senders": (bundle.get("senders") or {}).get(sid, []),
            "receivers": (bundle.get("receivers") or {}).get(sid, []),
            "run_id": run_id,
            "run_seconds": float(shop_seconds),
            "links": list(links),
        }
        proc = Process(
            target=shop_worker,
            args=(shop, grouped.get(sid) or jobs, material, report_q, float(tick), ctx),
            name=f"shop-{sid}",
        )
        proc.start()
        _PROCS[sid] = proc
        started.append({"shop_id": sid, "pid": proc.pid})
    _RUN_ID = run_id
    persist.append_log(
        f"{time.strftime('%H:%M:%S')} 长跑班次 {run_id} 开始：{len(started)} 个车间进程"
    )
    return {"ok": True, "run_id": run_id, "started": started, **status()}


def kill(shop_id: str | None = None) -> dict:
    """模拟"车间进程崩溃"：硬杀掉一个在册车间，验证守护进程会把它拉起来。

    不给 shop_id 就挑第一个活着的车间。

    必须用不可捕获的杀法（POSIX SIGKILL / Windows TerminateProcess）：
    如果发 SIGTERM，车间进程的处理程序会接住、优雅退出并注销登记，
    守护进程只会认为它正常下班了，**永远不会拉起**。这一点在 Linux 上
    尤其要小心 —— Windows 上 SIGTERM 恰好等于 TerminateProcess，所以
    在 Windows 上验证通过不代表 Linux 上成立。
    """
    rows = [r for r in status()["guarded"] if r.get("pid")]
    if not rows:
        return {"ok": False, "error": "当前没有正在运行的车间进程，先开一个长跑班次"}
    target = next((r for r in rows if shop_id and r.get("shop_id") == shop_id), rows[0])
    pid = int(target["pid"])
    ok, mode = signals.hard_kill(pid)
    persist.append_log(
        f"{time.strftime('%H:%M:%S')} 测谎：硬杀车间 {target.get('shop_id')} "
        f"pid={pid}（{mode}，模拟崩溃），等守护进程拉起"
    )
    return {"ok": bool(ok), "killed": target, "mode": mode}


def stop() -> dict:
    """宣布收工：先停班次，再收掉本进程起的车间。守护进程看到 idle 就不再拉起。"""
    global _BUNDLE, _RUN_ID

    run = persist.load_run()
    run_id = run.get("run_id")
    persist.save_run({"run_id": None, "state": "idle", "stopped_at": time.time()})
    # run.json 变成 idle 之后，车间线程会在下一轮 done() 里自己退出；
    # 还是不走就 terminate 兜底
    time.sleep(0.3)
    for sid, proc in list(_PROCS.items()):
        try:
            proc.join(timeout=1.5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1.5)
        except Exception:
            pass
        _PROCS.pop(sid, None)
    if _BUNDLE:
        ipc.close_pipes(_BUNDLE)
        _BUNDLE = None
    dropped = persist.purge_procs(None)
    _RUN_ID = None
    persist.append_log(
        f"{time.strftime('%H:%M:%S')} 长跑班次 {run_id or '—'} 收工，清理登记 {dropped} 条"
    )
    return {"ok": True, "cleaned": dropped, **status()}
