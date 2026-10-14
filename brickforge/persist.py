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
        LAYOUT.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8")
    return layout


def save_status(payload: dict) -> None:
    with file_lock():
        STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_status() -> dict:
    if not STATUS.exists():
        return {}
    return json.loads(STATUS.read_text(encoding="utf-8"))


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
    lines = DAEMON_LOG.read_text(encoding="utf-8").splitlines()
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
    PROCS.write_text(
        json.dumps({"procs": items}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
