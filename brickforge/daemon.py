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
    drop_proc,
    heartbeat_age,
    load_procs,
    load_run,
    load_status,
    read_watchdog,
    replace_proc,
    save_status,
    write_watchdog,
)

IS_WINDOWS = os.name == "nt"
MAX_REVIVES = 3          # 同一车间最多拉起 3 次，避免"崩溃—重启"死循环
HB_STALE_SECONDS = 6.0   # 心跳过期阈值（只用于展示活性，不用于判定崩溃）
OWNER_STALE_SECONDS = 15.0  # 主人心跳停这么久，就认为服务端已经没了（≈7 轮巡视）


def can_fork_to_background() -> bool:
    """本进程能不能靠 fork 把自己转成后台守护进程。

    两个前提：POSIX（Windows 没有 fork/setsid），并且当前进程是"可丢弃的"
    —— 也就是 multiprocessing 派出来的子进程。主线程自己调 fork 会把整个
    解释器（含父进程的线程、套接字、HTTP 服务）复制一份，那是灾难。
    """
    if IS_WINDOWS:
        return False
    try:
        from multiprocessing import parent_process

        return parent_process() is not None
    except (ImportError, AttributeError):
        return False


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
def _rebuild_pipes(links: list[dict]) -> tuple[dict, dict[str, list]]:
    """按登记里的连线重建管道拓扑，返回 (管道包, 各车间写端)。

    原先拉起时 senders/receivers 传的是空列表，被拉起的车间就成了"孤岛"：
    既不往下游投递，也收不到上游的批次。这里按 data/factory.json 的 links
    重新建一套，把该车间的写端接回去。
    """
    bundle = ipc.make_pipes(links or [])
    return bundle, dict(bundle.get("senders") or {})


def revive(entry: dict) -> Process | None:
    """按登记信息真正重启一个车间进程。"""
    from brickforge.workers import shop_worker

    shop = entry.get("shop") or {}
    if not shop.get("id"):
        return None
    slots = max(int(entry.get("slots") or 1), 1)
    shop_id = str(shop.get("id"))
    bundle = None
    try:
        material = Semaphore(slots)
        report_q: Queue = Queue()
        # 上下文要按登记信息复原：槽位、连线、班次、寿命模型
        links = list(entry.get("links") or [])
        sender_map: dict = {}
        if links:
            bundle, sender_map = _rebuild_pipes(links)
        ctx = {
            "index": int(entry.get("index") or 0),
            "slots": slots,
            "senders": sender_map.get(shop_id, []),
            "receivers": (bundle.get("receivers") or {}).get(shop_id, []) if bundle else [],
            "revives": int(entry.get("revives") or 0) + 1,
            "run_id": entry.get("run_id"),
            "run_seconds": float(entry.get("run_seconds") or 0.0),
            "links": links,
        }
        proc = Process(
            target=shop_worker,
            args=(shop, entry.get("jobs") or [], material, report_q,
                  float(entry.get("tick") or 0.03), ctx),
            name=f"revive-{shop_id}",
        )
        proc.daemon = False
        proc.start()
        if bundle:
            # 子进程已经拿到自己的端点副本，父进程这份要关掉，否则句柄泄漏
            ipc.close_pipes(bundle)
        return proc
    except (OSError, ValueError):
        if bundle:
            ipc.close_pipes(bundle)
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
    # 守护进程也要能被"反向守护"：主人（HTTP 服务）被强杀时，自己得退出去，
    # 否则关掉控制台/任务管理器结束进程之后会留一个没人管的孤儿守护进程。
    owner_pid = None
    owner_beat = None
    wd = read_watchdog()
    if wd:
        owner_pid = int(wd.get("pid") or 0) or None
        owner_beat = wd

    while not state["stop"]:
        tick += 1
        now = time.strftime("%H:%M:%S")
        try:
            # 换班信号：真的重新读一遍登记表（以前只加计数、没人消费）
            if state["reload"]:
                loaded = len(load_procs())
                append_log(f"{now} 收到换班信号（第 {state['reload']} 次），"
                           f"重新读取登记表 {loaded} 条")
                board.bump(ipc.SLOT_RELOAD)

            # 主人还在吗？只对"有主"的守护进程生效（独立脚本起的守护不算）
            if owner_pid:
                fresh = read_watchdog()
                if fresh:
                    owner_beat = fresh
                age = time.time() - float((owner_beat or {}).get("at") or 0)
                if not _alive(owner_pid) or age > OWNER_STALE_SECONDS:
                    append_log(
                        f"{now} 主人 pid={owner_pid} 已退出（心跳停在 {age:.0f}s 前），"
                        f"守护进程随之退出"
                    )
                    break

            alive_rows, stale_rows, deaths = _inspect()
            run = load_run()
            run_id = run.get("run_id") if run.get("state") == "running" else None
            # 真正拉起被异常杀掉的车间
            for entry in deaths:
                shop_id = entry.get("shop_id") or "?"
                # 班次已经收工（或这条记录属于上一班）：不是崩溃，直接清掉，别拉起
                if entry.get("run_id") and entry.get("run_id") != run_id:
                    _drop(entry.get("pid"))
                    append_log(f"{now} 车间 {shop_id} 属于已收工的班次，登记已清理（不拉起）")
                    continue
                done = int(entry.get("revives") or 0)
                if done >= MAX_REVIVES:
                    append_log(f"{now} 车间 {shop_id} 连续崩溃 {done} 次，放弃拉起")
                    _drop(entry.get("pid"))
                    continue
                old_pid = entry.get("pid")
                proc = revive(entry)
                if proc is not None:
                    restarts += 1
                    revived.append(proc)
                    # 一次加锁内完成"删旧 + 写新"，避免和车间进程的登记互相覆盖
                    replace_proc(old_pid, dict(
                        entry, pid=proc.pid, revives=done + 1, started_at=time.time()
                    ))
                    append_log(f"{now} 车间 {shop_id} 进程 pid={old_pid} 已失联，"
                               f"拉起新进程 pid={proc.pid}（第 {done + 1} 次）")
                else:
                    append_log(f"{now} 车间 {shop_id} 拉起失败")

            # 回收自己拉起的子进程，别留僵尸（僵尸会被 _alive() 误判成"活着"）
            revived = _reap(revived)

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
                "run_id": run_id,
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
    revived.clear()
    append_log(f"{time.strftime('%H:%M:%S')} 厂务精灵退出 pid={os.getpid()}")
    # 自己是自愿退出的，pidfile 自己收拾干净，别留一个指向死进程的文件
    _clear_pidfile(os.getpid())
    hub.restore()
    board.close()


def _reap(children: list[Process]) -> list[Process]:
    """回收已经结束的子进程（显式 waitpid），返回还活着的那些。

    不回收的话，被拉起的车间退出后会变成僵尸；而 Windows/POSIX 上
    "僵尸进程"在 _alive() 眼里仍是"活着"，守护进程就不会再拉起它。
    """
    alive: list[Process] = []
    for proc in children:
        try:
            if proc.is_alive():
                alive.append(proc)
                continue
            proc.join(timeout=0)
            if proc.exitcode is not None:
                try:
                    os.waitpid(proc.pid, os.WNOHANG)
                except (OSError, ChildProcessError):
                    pass
        except Exception:
            continue
    return alive


def _drop(pid) -> None:
    if not pid:
        return
    drop_proc(int(pid))


class _DaemonHandle:
    """单例命中时的轻量句柄：指向那个已经在跑的守护进程。"""

    def __init__(self, pid: int, reused: bool = True) -> None:
        self.pid = pid
        self.reused = reused

    def is_alive(self) -> bool:
        return _alive(self.pid)

    def join(self, timeout: float | None = None) -> None:
        return None

    def __repr__(self) -> str:                    # pragma: no cover
        return f"<daemon pid={self.pid} reused={self.reused}>"


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
def daemon_pid_running() -> int | None:
    """已有守护进程在跑的话返回它的 pid（单例检查）。"""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 and _alive(pid) else None


def start_daemon() -> Process | _DaemonHandle:
    """启动厂务精灵。

    平台差异（答辩时要讲清楚）：
        POSIX  —— 子进程再走一遍课件第 9 页的 7 步（fork/setsid/umask/chdir/
                  关 fd/dup2/业务），彻底脱离终端与父进程。
        Windows —— 没有 fork/setsid，只能"独立进程"：multiprocessing 起的子进程
                  本身就是独立进程，但不脱离控制台（控制台关了它也会退）。
    单例：pidfile 里的 pid 还活着就直接复用它，避免调试时叠出一堆守护进程。
    """
    running = daemon_pid_running()
    if running is not None:
        return _DaemonHandle(running)
    # 写下"主人是谁"：守护进程据此在主人被强杀后自己退出，不留孤儿
    write_watchdog()
    proc = Process(target=_entry, name="factory-daemon", daemon=False)
    proc.start()
    return proc


def _entry() -> None:
    try:
        if can_fork_to_background():
            daemonize_posix()
    except OSError:
        pass
    try:
        daemon_loop()
    except Exception as exc:                       # pragma: no cover
        append_log(f"{time.strftime('%H:%M:%S')} 厂务精灵异常退出 {type(exc).__name__}: {exc}")
        raise


def stop_daemon(proc: Process | None) -> None:
    """停掉厂务精灵（含 POSIX 下已 fork 到后台、不再是 proc 子进程的那种）。

    以前只对 `proc` 发 terminate：POSIX 上 proc 是 fork 前的中间子进程，
    早已 os._exit(0)，于是真守护进程永远收不到信号、成了孤儿。
    现在改成按 **pidfile 里的真实 pid** 发 SIGTERM，并确认它真的退出了
    才删 pidfile；删不掉 pidfile 反而会让下次启动误以为还有守护进程在跑。
    """
    targets: list[int] = []
    if proc is not None:
        try:
            if proc.is_alive():
                targets.append(proc.pid)
        except (AssertionError, ValueError):
            pass
    real = None
    if PID_FILE.exists():
        try:
            real = int(PID_FILE.read_text(encoding="utf-8").strip() or 0) or None
        except (OSError, ValueError):
            real = None
    if real and real not in targets and _alive(real):
        targets.append(real)

    for pid in targets:
        _terminate_pid(pid)

    if proc is not None:
        try:
            proc.join(timeout=3)
        except (AssertionError, ValueError):
            pass
    if real and _alive(real):
        return          # 没停掉就别删 pidfile，留给下一次尝试
    # 停掉了（或本来就已退出）：顺手清掉可能残留的 pidfile
    _clear_pidfile(real)


def _clear_pidfile(expect: int | None = None) -> None:
    """删掉 pidfile；给了 expect 就只在该 pid 确实是它时删（防误删别人的）。"""
    if not PID_FILE.exists():
        return
    try:
        current = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        current = None
    if expect is not None and current not in (None, expect):
        return
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def _terminate_pid(pid: int) -> None:
    """尽力终止一个进程：POSIX 先 SIGTERM 让处理程序优雅收尾，再 SIGKILL 兜底。"""
    if not _alive(pid):
        return
    if IS_WINDOWS:
        try:
            os.kill(pid, 15)      # Windows 上这等价于 TerminateProcess
        except OSError:
            pass
        return
    try:
        os.kill(pid, 15)          # SIGTERM：守护进程的处理程序会接到
    except OSError:
        pass
    for _ in range(30):
        if not _alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, 9)           # SIGKILL 兜底
    except OSError:
        pass
