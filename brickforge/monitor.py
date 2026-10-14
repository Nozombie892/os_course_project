"""系统监视器：PCB、系统调用轨迹。甲只负责画这些 JSON。"""

from __future__ import annotations


def pcb(shops: list[dict], reports: list[dict], timeline: list[dict]) -> list[dict]:
    by_shop = {r.get("shop_id"): r for r in reports}
    rows = []
    for shop in shops:
        rep = by_shop.get(shop.get("id"), {})
        njob = len([t for t in timeline if t.get("shop_id") == shop.get("id")])
        if shop.get("suspended"):
            state = "stopped"
        elif rep.get("blocked"):
            state = "blocked"
        elif njob:
            state = "running"
        else:
            state = "ready"
        rows.append(
            {
                "pid": rep.get("pid") or "—",
                "name": shop.get("name") or shop.get("id"),
                "kind": "进程",
                "state": state,
                "threads": len(shop.get("machines") or []),
                "cpu": njob,
            }
        )
        for machine in shop.get("machines") or []:
            staff = int(machine.get("workers") or 0)
            rows.append(
                {
                    "pid": machine.get("id"),
                    "name": machine.get("name") or machine.get("id"),
                    "kind": "线程",
                    "state": "running" if staff else "sleep",
                    "threads": 0,
                    "cpu": staff,
                }
            )
    for row in timeline:
        rows.append(
            {
                "pid": row.get("id"),
                "name": row.get("name"),
                "kind": "工单",
                "state": "zombie",
                "threads": 0,
                "cpu": row.get("burst"),
            }
        )
    return rows


def syscalls(
    shops: list[dict],
    algo: str,
    slots: int,
    blocked: int,
    bank: dict,
    paging: dict,
    switches: int,
    pipes: dict | None = None,
    board: dict | None = None,
    sigcaps: dict | None = None,
    tool_waits: int = 0,
) -> list[str]:
    """把一轮仿真走过的系统调用按真实顺序列出来。"""
    pipes = pipes or {}
    board = board or {}
    sigcaps = sigcaps or {}
    lines = [
        "open(\"data/factory.json\", O_RDWR)",
        "fcntl(fd, F_SETLKW, F_WRLCK)",
    ]

    # 进程控制与管理
    for shop in shops:
        lines.append(f"fork() → 车间进程 {shop.get('name') or shop.get('id')}")
        machine_n = len(shop.get("machines") or [])
        if machine_n:
            lines.append(f"  └ 登记 pid 到 data/procs.json（守护进程据此判活）")
    # 线程创建与管理
    for shop in shops:
        for machine in shop.get("machines") or []:
            lines.append(f"clone(CLONE_THREAD) → {machine.get('name') or machine.get('id')}")

    # 线程同步：互斥
    lines.append("pthread_mutex_lock(&state_lock) 保护车间共享的完工表")
    lines.append(f"pthread_mutex_unlock(&state_lock)")

    # 信号量与信号
    lines.append(f"sem_init(库位, {slots})")
    if blocked:
        lines.append(f"sem_wait() 阻塞 {blocked} 次 EAGAIN")
    else:
        lines.append("sem_wait()/sem_post() 库位未饿死")
    lines.append(f"sem_init(共享工装, 2) 线程互斥资源")
    if tool_waits:
        lines.append(f"sem_wait(共享工装) 等待超时 {tool_waits} 次")
    else:
        lines.append("sem_wait/sem_post(共享工装) 线程间未争抢")

    # IPC：管道 + 共享内存
    pairs = pipes.get("pairs") or []
    lines.append(f"pipe2(pipefd, O_NONBLOCK) × {len(pairs)} → " + ("、".join(pairs) or "无车间连线"))
    if board and not board.get("degraded"):
        lines.append(f"shm_open(\"{board.get('name')}\", O_RDWR|O_CREAT) → mmap(MAP_SHARED)")
        lines.append(f"  看板槽位：守护 pid={board.get('daemon_pid')} 心跳={board.get('daemon_tick')} "
                     f"拉起={board.get('restarts')}")
    else:
        lines.append("shm_open 不可用，已降级为进程内状态")

    # 信号处理程序
    if sigcaps.get("real_signals"):
        lines.append(f"sigaction(SIGTERM, handler) 急停 · 可靠信号 SIGRTMIN 可用于换班")
        lines.append(f"setitimer(ITIMER_REAL, 2s) 守护进程周期任务")
    else:
        lines.append(f"sigaction(SIGTERM, handler) 注册成功；本平台无 SIGUSR1/SIGALRM，"
                     f"降级为软信号")

    lines.append(f"sched_setscheduler({algo}) 上下文切换 {switches}")
    if bank.get("safe"):
        seq = " → ".join(bank.get("sequence_names") or [])
        lines.append(f"银行家安全序列 {seq or '空'}")
    else:
        lines.append("银行家判定不安全，拒绝超额申请")
    lines.append(
        f"缺页 {paging.get('faults', 0)}/{len(paging.get('refs') or [])} "
        f"置换 {paging.get('algo')} 页框 {paging.get('frames')}"
    )
    lines.append("write(\"data/daemon.log\")")
    lines.append("shm_unlink 由最后一个使用者负责；子进程先注销登记再退出")
    lines.append("fcntl(fd, F_SETLK, F_UNLCK); close(fd)")
    return lines
