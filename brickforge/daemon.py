"""丁：厂务守护进程。必须是独立进程，不许用前端定时器冒充。

对应课件《操作系统原理课程设计》第 7–12 页"所实现系统必须有守护进程"：

    (1) fork 之后父进程退出，子进程继续        → daemonize_posix()
    (2) setsid() 开启新会话                    → daemonize_posix()
    (3) umask() 清除掩码                       → daemonize_posix()
    (4) 修改当前工作目录                        → daemonize_posix()
    (5) 关闭继承来的文件描述符                   → _redirect_std_to_devnull()
    (6) 把 0/1/2 重定向到 /dev/null             → _redirect_std_to_devnull()
    (7) 核心业务逻辑                           → daemon_loop()

核心职责（第 7、8 页强调的"确保被守护进程的运行 / 重启进程"）：
    车间进程启动时在 data/procs.json 登记自己，正常结束时注销自己。
    守护进程按登记表检查进程是否真的还活着：
        - 活着            → 记活性，不动
        - 不在了 + 已注销  → 正常下班，移出登记表
        - 不在了 + 未注销  → 判定被异常杀掉 → **真的用登记信息把它拉起来**
    因此日志里的"累计拉起"是真实的重启次数，而不是把正常退出也数进去。

平台差异：
    POSIX   —— 完整 7 步；SIGTERM 能触发优雅退出。
    Windows —— 无 fork/setsid，创建流程退化为"独立进程"；
               os.kill(pid, 0) 在 Windows 上会直接终止目标进程，因此存活
               检查必须走 OpenProcess/GetExitCodeProcess。
"""

from __future__ import annotations

import os
import time
from multiprocessing import Process, Queue, Semaphore

from brickforge import ipc, signals
from brickforge.persist import (
    HB_DIR,
    PID_FILE,
    append_log,
    heartbeat_age,
    load_procs,
    load_status,
    save_procs,
    save_status,
)

IS_WINDOWS = os.name == "nt"
MAX_REVIVES = 3          # 同一车间最多拉起 3 次，避免"崩溃—重启"死循环
HB_STALE_SECONDS = 6.0   # 心跳过期阈值（只用于展示活性，不用于判定崩溃）


# ------------------------------------------------------------ 进程存活检查 ---
def _alive(pid: int) -> bool:
    """进程是否还活着。

    Windows 上绝不能用 os.kill(pid, 0)：CPython 在该平台会把它变成
    TerminateProcess(handle, 0)，也就是"检查存活"会把进程杀掉。
    """
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # 存在，只是没权限发信号
    except OSError:
        return False


# -------------------------------------------------------- 守护进程创建流程 ---
def daemonize_posix() -> None:
    """课件第 9 页的守护进程创建流程。Windows 上没有 fork/setsid，直接返回。"""
    if IS_WINDOWS:
        return
    # (1) fork 之后父进程退出，子进程继续
    if os.fork() > 0:
        os._exit(0)
    # (2) 开启新会话，脱离控制终端
    os.setsid()
    # 再 fork 一次，确保不会重新获得控制终端（比课件更严格）
    if os.fork() > 0:
        os._exit(0)
    # (3) 清除 umask
    os.umask(0o22)
    # (4) 修改当前工作目录
    try:
        os.chdir("/")
    except OSError:
        pass
    # (5)(6) 关闭并重定向标准描述符
    _redirect_std_to_devnull()


def _redirect_std_to_devnull() -> None:
    """把标准输入/输出/错误重定向到 /dev/null，之后守护进程不再产生任何终端输出。"""
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
    except OSError:
        return
    try:
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                pass
    finally:
        if devnull > 2:
            try:
                os.close(devnull)
            except OSError:
                pass


# ------------------------------------------------------------------ 拉起 ---
def revive(entry: dict) -> Process | None:
    """按登记信息真正重启一个车间进程。"""
    from brickforge.workers import shop_worker

    shop = entry.get("shop") or {}
    if not shop.get("id"):
        return None
    slots = max(int(entry.get("slots") or 1), 1)
    try:
        material = Semaphore(slots)
        report_q: Queue = Queue()
        # 拉起时要把上下文一起复原，否则新进程会退化成"无看板槽位、计数归零"
        ctx = {
            "index": int(entry.get("index") or 0),
            "slots": slots,
            "senders": [],
            "receivers": [],
            "revives": int(entry.get("revives") or 0) + 1,
        }
        proc = Process(
            target=shop_worker,
            args=(shop, entry.get("jobs") or [], material, report_q,
                  float(entry.get("tick") or 0.03), ctx),
            name=f"revive-{shop.get('id')}",
        )
        proc.daemon = False
        proc.start()
        return proc
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------ 核心业务 ---
def daemon_loop(interval: float = 2.0) -> None:
    HB_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    # 信号：收到 SIGTERM 优雅退出，收到换班信号重新读登记表
    hub = signals.SignalHub("daemon")
    state = {"stop": False, "reload": 0}
    hub.on(signals.STOP, lambda *_: state.__setitem__("stop", True))
    hub.on(signals.RELOAD, lambda *_: state.__setitem__("reload", state["reload"] + 1))
    installed = hub.install((signals.STOP, signals.RELOAD))

    board = ipc.open_board(reset=False)
    board.set(ipc.SLOT_DAEMON_PID, os.getpid())
    board.set(ipc.SLOT_RESTARTS, 0)

    append_log(f"{time.strftime('%H:%M:%S')} 厂务精灵启动 pid={os.getpid()}"
               f" 信号={'/'.join(installed) or '软信号'} 共享内存={'开' if not board.degraded else '降级'}")

    restarts = 0
    tick = 0
    revived: list[Process] = []

    while not state["stop"]:
        tick += 1
        now = time.strftime("%H:%M:%S")
        try:
            alive_rows, stale_rows, deaths = _inspect()
            # 真正拉起被异常杀掉的车间
            for entry in deaths:
                shop_id = entry.get("shop_id") or "?"
                done = int(entry.get("revives") or 0)
                if done >= MAX_REVIVES:
                    append_log(f"{now} 车间 {shop_id} 连续崩溃 {done} 次，放弃拉起")
                    _drop(entry.get("pid"))
                    continue
                old_pid = entry.get("pid")
                proc = revive(entry)
                if proc is not None:
                    restarts += 1
                    _drop(old_pid)
                    _append(dict(entry, pid=proc.pid, revives=done + 1, started_at=time.time()))
                    revived.append(proc)
                    append_log(f"{now} 车间 {shop_id} 进程 pid={old_pid} 已失联，"
                               f"拉起新进程 pid={proc.pid}（第 {done + 1} 次）")
                else:
                    append_log(f"{now} 车间 {shop_id} 拉起失败")

            board.bump(ipc.SLOT_DAEMON_TICK)
            board.set(ipc.SLOT_RESTARTS, restarts)
            board.set(ipc.SLOT_DAEMON_PID, os.getpid())

            # 拉起之后再统计一次，让日志与状态反映本轮结束时的真实情况
            alive_rows, stale_rows, _ = _inspect()
            append_log(f"{now} 巡视 守护={len(alive_rows)} 异常退出={len(deaths)} "
                       f"心跳过期={stale_rows or '无'} 累计拉起={restarts}")

            status = load_status()
            status.update({
                "daemon_pid": os.getpid(),
                "daemon_alive": True,
                "restarts": restarts,
                "guarded": alive_rows,
                "stale": stale_rows,
                "checked_at": now,
                "signal_caps": signals.capabilities(),
                "board": board.snapshot(),
            })
            save_status(status)
        except Exception as exc:
            # 任何一轮巡视出错都不能让守护进程退出
            append_log(f"{now} 巡视异常 {type(exc).__name__}: {exc}")

        time.sleep(interval)

    # 优雅退出：收掉自己拉起的子进程
    for proc in revived:
        try:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2)
        except Exception:
            pass
    append_log(f"{time.strftime('%H:%M:%S')} 厂务精灵退出 pid={os.getpid()}")
    hub.restore()
    board.close()


def _drop(pid) -> None:
    if not pid:
        return
    items = [p for p in load_procs() if p.get("pid") != pid]
    save_procs(items)


def _append(entry: dict) -> None:
    items = [p for p in load_procs() if p.get("pid") != entry.get("pid")]
    items.append(entry)
    save_procs(items)


def _inspect() -> tuple[list[dict], list[str], list[dict]]:
    """检查登记表：谁活着、谁心跳过期、谁被异常杀掉了。"""
    entries = load_procs()
    alive: list[dict] = []
    stale: list[str] = []
    deaths: list[dict] = []
    for entry in entries:
        pid = entry.get("pid")
        shop_id = entry.get("shop_id")
        if _alive(pid):
            age = heartbeat_age(shop_id) if shop_id else None
            row = {
                "pid": pid,
                "shop_id": shop_id,
                "name": entry.get("name"),
                "age": round(age, 1) if age is not None else None,
                "state": "stale" if (age is not None and age > HB_STALE_SECONDS) else "running",
                "revives": int(entry.get("revives") or 0),
            }
            alive.append(row)
            if row["state"] == "stale":
                stale.append(shop_id)
        else:
            # 进程不在了：正常结束的会自己注销，还留在表里就是异常退出
            deaths.append(entry)
    return alive, stale, deaths


# ------------------------------------------------------------------ 生命周期 ---
def start_daemon() -> Process:
    proc = Process(target=_entry, name="factory-daemon", daemon=False)
    proc.start()
    return proc


def _entry() -> None:
    try:
        daemonize_posix()
    except OSError:
        pass
    try:
        daemon_loop()
    except Exception as exc:                       # pragma: no cover
        append_log(f"{time.strftime('%H:%M:%S')} 厂务精灵异常退出 {type(exc).__name__}: {exc}")
        raise


def stop_daemon(proc: Process | None) -> None:
    if proc is not None and proc.is_alive():
        # POSIX 上 terminate 发的是 SIGTERM，会被守护进程的处理程序接住并优雅退出
        proc.terminate()
        proc.join(timeout=3)
    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except OSError:
            pass
