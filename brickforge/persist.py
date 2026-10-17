"""布局与日志落盘，带跨平台文件锁。"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
LAYOUT = DATA / "factory.json"
STATUS = DATA / "status.json"
DAEMON_LOG = DATA / "daemon.log"
LOCK = DATA / "factory.lock"
PROCS = DATA / "procs.json"      # 被守护进程的登记表
HB_DIR = DATA / "heartbeats"     # 车间心跳文件
PID_FILE = DATA / "daemon.pid"
RUN = DATA / "run.json"          # 长跑（持续生产）班次状态
WATCHDOG = DATA / "watchdog.json"  # 守护进程反过来盯"主人"（HTTP 服务）是否还活着


def write_watchdog(pid: int | None = None) -> None:
    """服务端周期性写一次"我还活着"，守护进程读它判断主人是否已经走了。

    只能靠 pid+心跳时间：服务器被强杀（关控制台、任务管理器结束进程）时
    不会走任何退出路径，守护进程只凭 os.kill(0) 也分不清"pid 还在"是不是
    复用给了别的进程，加上心跳时间就不会误判。
    """
    payload = {"pid": pid or os.getpid(), "at": time.time()}
    try:
        with file_lock():
            _atomic_write_text(WATCHDOG, json.dumps(payload), sync=True)
    except OSError:
        pass


def read_watchdog() -> dict | None:
    if not WATCHDOG.exists():
        return None
    try:
        with file_lock():
            data = json.loads(WATCHDOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_watchdog() -> None:
    try:
        with file_lock():
            if WATCHDOG.exists():
                WATCHDOG.unlink()
    except OSError:
        pass


def _atomic_write_text(path: Path, text: str, sync: bool = False) -> None:
    """原子写：临时文件 → flush/fsync → os.replace。

    落盘过程中任何一刻断电/崩溃，读到的要么是旧的完整文件，要么是新的
    完整文件，不会是"写了一半的 JSON"。临时文件名带 pid，避免并发进程
    抢同一个 .tmp 互相覆盖（锁已经串行化了，这里再兜一层）。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            if sync:
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@contextmanager
def file_lock():
    LOCK.parent.mkdir(exist_ok=True)
    handle = open(LOCK, "a+b")
    try:
        _lock(handle)
        yield
    finally:
        _unlock(handle)
        handle.close()


def _lock(handle):
    handle.seek(0, 2)
    if handle.tell() < 1:
        handle.write(b"1")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_layout() -> dict:
    with file_lock():
        if not LAYOUT.exists():
            return default_layout()
        return json.loads(LAYOUT.read_text(encoding="utf-8"))


def save_layout(layout: dict) -> dict:
    with file_lock():
        _atomic_write_text(LAYOUT, json.dumps(layout, ensure_ascii=False, indent=2))
    return layout


def save_status(payload: dict) -> None:
    with file_lock():
        _atomic_write_text(STATUS, json.dumps(payload, ensure_ascii=False, indent=2))


def load_status() -> dict:
    """读状态快照。

    也拿文件锁：写入方是原子替换的，但读取方仍需与"读取期间的替换"串行化，
    否则 Windows 上偶发 replace 失败（文件正被读句柄占着）。
    """
    with file_lock():
        if not STATUS.exists():
            return {}
        try:
            return json.loads(STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


def append_log(line: str, retries: int = 3) -> bool:
    """追加一行日志。

    文件锁在 Windows 上是"重试 10 次后抛异常"的语义，而守护进程每 2 秒就要
    写一次日志、还要和仿真进程抢同一把锁。这里做有限重试并吞掉异常，
    避免一次抢锁失败把守护进程的主循环打穿。
    """
    for attempt in range(max(retries, 1)):
        try:
            with file_lock():
                with DAEMON_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(line.rstrip() + "\n")
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    return False


def tail_log(n: int = 12) -> list[str]:
    if not DAEMON_LOG.exists():
        return []
    try:
        lines = DAEMON_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return lines[-n:]


# ------------------------------------------------------------ 进程登记表 ---
# 守护进程靠这张表判断"谁应该活着"。车间进程启动时登记、正常结束时注销；
# 如果表里还留着记录、而对应 pid 已经不在了，就说明它是被异常杀掉的。


def _read_procs_raw() -> list[dict]:
    if not PROCS.exists():
        return []
    try:
        data = json.loads(PROCS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("procs") if isinstance(data, dict) else data
    return [p for p in (items or []) if isinstance(p, dict)]


def _write_procs_raw(items: list[dict]) -> None:
    _atomic_write_text(
        PROCS, json.dumps({"procs": items}, ensure_ascii=False, indent=2), sync=True
    )


def _mutate_procs(fn, retries: int = 5) -> tuple[bool, object]:
    """在**同一把锁**里完成读-改-写，返回 (是否落盘, fn 的返回值)。

    以前守护进程是 load_procs() 拿一次锁、save_procs() 再拿一次锁，
    两次加锁之间别的车间进程 register_proc() 的结果会被覆盖回去。
    """
    for attempt in range(max(retries, 1)):
        try:
            with file_lock():
                items = _read_procs_raw()
                changed, result = fn(items)
                if changed:
                    _write_procs_raw(items)
                return bool(changed), result
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    return False, None


def load_procs() -> list[dict]:
    with file_lock():
        return _read_procs_raw()


def save_procs(items: list[dict]) -> None:
    with file_lock():
        _write_procs_raw(items)


def register_proc(entry: dict, retries: int = 5) -> bool:
    """登记一个被守护进程（同 pid 覆盖旧记录）。

    多个车间进程会同时登记，文件锁在 Windows 上是"重试若干次后抛异常"的语义，
    因此这里自带重试：抢不到锁只是登记失败，绝不能让车间进程崩掉。
    """
    for attempt in range(max(retries, 1)):
        try:
            with file_lock():
                items = [p for p in _read_procs_raw() if p.get("pid") != entry.get("pid")]
                items.append(entry)
                _write_procs_raw(items)
            return True
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    return False


def unregister_proc(pid: int, retries: int = 5) -> bool:
    """注销登记，返回是否真的删掉了记录。

    正常退出的车间会调用它，因此守护进程不会把"干完活下班"误判成崩溃。
    """
    for attempt in range(max(retries, 1)):
        try:
            with file_lock():
                items = _read_procs_raw()
                keep = [p for p in items if p.get("pid") != pid]
                if len(keep) == len(items):
                    return False
                _write_procs_raw(keep)
                return True
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    return False


def update_proc(pid: int, **fields) -> bool:
    """就地更新登记项（例如重启后改写 pid）。"""
    with file_lock():
        items = _read_procs_raw()
        hit = False
        for item in items:
            if item.get("pid") == pid:
                item.update(fields)
                hit = True
        if hit:
            _write_procs_raw(items)
        return hit


def drop_proc(pid: int) -> bool:
    """删掉一条登记（守护进程丢掉已放弃拉起的记录时用）。"""
    if not pid:
        return False

    def _drop(items: list[dict]) -> tuple[bool, bool]:
        keep = [p for p in items if p.get("pid") != pid]
        items[:] = keep
        return len(keep) != len(items), True

    changed, _ = _mutate_procs(_drop)
    return changed


def replace_proc(old_pid: int, entry: dict) -> bool:
    """把 old_pid 那条记录换成 entry（拉起成功后改写登记，一次加锁完成）。"""
    def _replace(items: list[dict]) -> tuple[bool, bool]:
        keep = [p for p in items if p.get("pid") != old_pid and p.get("pid") != entry.get("pid")]
        keep.append(entry)
        items[:] = keep
        return True, True

    changed, _ = _mutate_procs(_replace)
    return changed


def purge_procs(keep_run: str | None = None) -> int:
    """清空登记表；给了 keep_run 就只保留该班次的记录。"""
    def _purge(items: list[dict]) -> tuple[bool, int]:
        if keep_run is None:
            dropped = len(items)
            items.clear()
            return dropped > 0, dropped
        keep = [p for p in items if p.get("run_id") == keep_run]
        dropped = len(items) - len(keep)
        items[:] = keep
        return dropped > 0, dropped

    _, dropped = _mutate_procs(_purge)
    return int(dropped or 0)


# ------------------------------------------------------------ 长跑班次状态 ---
# 守护进程靠它区分"这个班次还在跑，死了要拉起"和"班长已经宣布收工，别再拉"。
# 没有这一层，运行时主动停掉车间后，守护进程会把它们当成崩溃重新拉起来。

def load_run() -> dict:
    with file_lock():
        if not RUN.exists():
            return {"run_id": None, "state": "idle"}
        try:
            return json.loads(RUN.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"run_id": None, "state": "idle"}


def save_run(payload: dict) -> dict:
    with file_lock():
        _atomic_write_text(RUN, json.dumps(payload, ensure_ascii=False, indent=2), sync=True)
    return payload


def active_run_id() -> str | None:
    run = load_run()
    return run.get("run_id") if run.get("state") == "running" else None


def heartbeat_age(shop_id: str) -> float | None:
    """该车间心跳文件距今多少秒；没有心跳文件返回 None。"""
    path = HB_DIR / f"{shop_id}.hb"
    if not path.exists():
        return None
    try:
        stamp = float(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return None
    return max(time.time() - stamp, 0.0)

def default_layout() -> dict:
    return {
        "scheduler": "priority",
        "ship_algo": "sstf",
        "price": 18,
        "raw": 120,
        "raw_kind": "steel",
        "daemon": True,
        "warehouse_policy": "best",
        "shelves": [8, 5, 12, 3],
        "buffer_cap": 6,
        "quantum": 2,
        "page_frames": 4,
        "page_algo": "lru",
        "inbound": {
            "items": [{"id": "in1", "name": "冷轧钢板", "qty": 120, "cap": 3000, "cost": 2}]
        },
        "outbound": {
            "items": [{"id": "out1", "name": "积木成品", "qty": 0, "cap": 3000, "price": 18}]
        },
        "feeds": [{"from": "in1", "to": "s1"}],
        "ships": [{"from": "s2", "to": "out1"}],
        "links": [{"from": "s1", "to": "s2"}],
        "jobs": [
            {"id": "j1", "name": "急单", "burst": 3, "priority": 1, "dest": 12, "sku": "A"},
            {"id": "j2", "name": "普通", "burst": 5, "priority": 2, "dest": 7, "sku": "A"},
            {"id": "j3", "name": "加急", "burst": 4, "priority": 2, "dest": 9, "sku": "A"},
        ],
        "shops": [
            {
                "id": "s1",
                "type": "press",
                "name": "加工车间",
                "x": 40,
                "y": 80,
                "machines": [
                    {"id": "m1", "type": "station", "name": "工位机", "x": 200, "y": 80, "burst": 3, "priority": 1, "workers": 1, "ppm": 20},
                    {"id": "m3", "type": "arm", "name": "机械臂", "x": 400, "y": 20, "burst": 3, "priority": 2, "workers": 1, "ppm": 19},
                    {"id": "m4", "type": "station", "name": "折弯机", "x": 400, "y": 190, "burst": 4, "priority": 2, "workers": 1, "ppm": 16},
                ],
                "wires": [
                    {"from": "__inlet__", "to": "m1"},
                    {"from": "m1", "to": "m3"},
                    {"from": "m1", "to": "m4"},
                    {"from": "m3", "to": "__outlet__"},
                    {"from": "m4", "to": "__outlet__"},
                ],
            },
            {
                "id": "s2",
                "type": "assemble",
                "name": "组装车间",
                "x": 280,
                "y": 80,
                "machines": [
                    {"id": "m2", "type": "arm", "name": "机械臂", "x": 200, "y": 80, "burst": 4, "priority": 2, "workers": 1, "ppm": 15}
                ],
                "wires": [{"from": "__inlet__", "to": "m2"}, {"from": "m2", "to": "__outlet__"}],
            },
        ],
    }
