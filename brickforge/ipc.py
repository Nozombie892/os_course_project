"""IPC 原语：管道（车间串联）与共享内存（全厂看板）。

对应课件《操作系统原理课程设计》第 5 页"进程间通信"：
    管道 ✓  消息队列 ✓（multiprocessing.Queue）  信号量 ✓  共享内存 ✓

本模块补齐课件点名、而原实现缺失的两种：

管道（pipe）
    相邻车间之间的半成品流。`multiprocessing.Pipe(duplex=False)` 在 POSIX 上
    是匿名管道、在 Windows 上是命名管道，语义一致：一端写、一端读、有内核缓冲。
    下游车间开工前用 poll() 非阻塞收走上游投递的批次记录。

共享内存（shared memory）
    全厂看板：一块命名共享内存段，主进程、车间进程、守护进程都能 attach 到
    同一块物理内存上读写，不经过内核拷贝、也不需要消息传递。
    看板按"每个写入者独占一个槽位"划分，避免并发读改写互相覆盖。
"""

from __future__ import annotations

import struct
import time
from multiprocessing import Pipe

# ---------------------------------------------------------------- 共享内存 ---
BOARD_NAME = "brickforge_board"

# 看板槽位布局（int64 数组）。前 6 槽全局，之后每车间一槽，各写各的，无竞争。
SLOT_DAEMON_PID = 0
SLOT_DAEMON_TICK = 1
SLOT_RESTARTS = 2
SLOT_GOOD = 3
SLOT_SCRAP = 4
SLOT_BLOCKED = 5
SLOT_SHOP_BASE = 6
SHOP_SLOTS = 16                      # 最多登记 16 个车间
BOARD_SLOTS = SLOT_SHOP_BASE + SHOP_SLOTS
BOARD_FMT = f"<{BOARD_SLOTS}q"
BOARD_SIZE = BOARD_SLOTS * 8


class Board:
    """全厂看板：一块命名共享内存段。

    任何进程都能按名字 attach 到同一块内存。若平台不支持共享内存，
    自动降级为进程内列表（degraded=True），不影响仿真继续跑。
    """

    def __init__(self, name: str = BOARD_NAME) -> None:
        self.name = name
        self.size = BOARD_SIZE
        self.shm = None
        self.created = False
        self.degraded = False
        self.error = ""
        self._local = [0] * BOARD_SLOTS

    # ------------------------------------------------------------ 生命周期 ---
    def open(self, reset: bool = False) -> bool:
        """连接共享内存；段不存在就创建。返回是否真的用上了共享内存。

        可重复调用：已经打开时不再重新 attach，只按需重置统计槽位。
        （Windows 上"先 close 再 attach"并不可靠，会让调用方误判成新建。）
        """
        if self.shm is not None:
            if reset:
                self._reset_keep_daemon()
            return True

        try:
            from multiprocessing import shared_memory
        except ImportError:                      # pragma: no cover
            self.degraded, self.error = True, "shared_memory 不可用"
            return False

        self.created = False
        try:
            self.shm = shared_memory.SharedMemory(name=self.name)
        except FileNotFoundError:
            try:
                self.shm = shared_memory.SharedMemory(name=self.name, create=True, size=self.size)
                self.created = True
            except OSError as exc:
                self.degraded, self.error = True, f"创建失败: {exc}"
                return False
        except OSError as exc:
            self.degraded, self.error = True, f"连接失败: {exc}"
            return False

        if reset:
            self._reset_keep_daemon()
        return True

    def _reset_keep_daemon(self) -> None:
        """清空统计与车间槽位，但保留守护进程写入的三个槽。"""
        keep = (self.get(SLOT_DAEMON_PID), self.get(SLOT_DAEMON_TICK), self.get(SLOT_RESTARTS))
        self.write([0] * BOARD_SLOTS)
        self.set(SLOT_DAEMON_PID, keep[0])
        self.set(SLOT_DAEMON_TICK, keep[1])
        self.set(SLOT_RESTARTS, keep[2])

    def close(self) -> None:
        if self.shm is not None:
            try:
                self.shm.close()
            except Exception:
                pass

    def unlink(self) -> None:
        """销毁共享内存段（最后一个使用者负责调用）。"""
        if self.shm is not None:
            try:
                self.shm.unlink()
            except Exception:
                pass

    # ------------------------------------------------------------ 读写 ---
    def read(self) -> list[int]:
        if self.shm is None:
            return list(self._local)
        try:
            return list(struct.unpack(BOARD_FMT, bytes(self.shm.buf[: self.size])))
        except Exception:
            return list(self._local)

    def write(self, values) -> None:
        values = list(values)[:BOARD_SLOTS]
        values += [0] * (BOARD_SLOTS - len(values))
        if self.shm is None:
            self._local = values
            return
        try:
            self.shm.buf[: self.size] = struct.pack(BOARD_FMT, *values)
        except Exception:
            self._local = values

    def set(self, index: int, value: int) -> None:
        if not 0 <= index < BOARD_SLOTS:
            return
        if self.shm is None:
            self._local[index] = int(value)
            return
        try:
            # 必须用单元素格式：BOARD_FMT 是整段长度，从 offset 起会越界
            struct.pack_into("<q", self.shm.buf, index * 8, int(value))
        except Exception:
            self._local[index] = int(value)

    def get(self, index: int) -> int:
        if not 0 <= index < BOARD_SLOTS:
            return 0
        if self.shm is None:
            return int(self._local[index])
        try:
            return int(struct.unpack_from("<q", self.shm.buf, index * 8)[0])
        except Exception:
            return int(self._local[index])

    def bump(self, index: int, delta: int = 1) -> None:
        """累加。同一槽位只由同一类写入者使用，避免读改写互相覆盖。"""
        self.set(index, self.get(index) + delta)

    def snapshot(self) -> dict:
        """人类可读的看板快照。"""
        raw = self.read()
        shops = {f"slot{i}": raw[SLOT_SHOP_BASE + i] for i in range(SHOP_SLOTS) if raw[SLOT_SHOP_BASE + i]}
        return {
            "name": self.name,
            "degraded": self.degraded,
            "error": self.error,
            "daemon_pid": raw[SLOT_DAEMON_PID],
            "daemon_tick": raw[SLOT_DAEMON_TICK],
            "restarts": raw[SLOT_RESTARTS],
            "good": raw[SLOT_GOOD],
            "scrap": raw[SLOT_SCRAP],
            "blocked": raw[SLOT_BLOCKED],
            "shop_slots": shops,
            "raw": raw,
        }


def open_board(reset: bool = False) -> Board:
    board = Board()
    board.open(reset=reset)
    return board


# ------------------------------------------------------------------ 管道 ---
def make_pipes(links: list[dict]) -> dict:
    """为每条"车间 → 车间"连线创建一条单向管道。

    返回 {"senders": {车间: [写端…]}, "receivers": {车间: [读端…]}, "pairs": [(上游, 下游)]}
    调用方必须在子进程启动后关闭自己持有的端点（close_pipes）。
    """
    senders: dict[str, list] = {}
    receivers: dict[str, list] = {}
    pairs: list[tuple[str, str]] = []
    for link in links or []:
        src, dst = link.get("from"), link.get("to")
        if not src or not dst or src == dst:
            continue
        try:
            recv_conn, send_conn = Pipe(duplex=False)
        except OSError:
            continue
        senders.setdefault(src, []).append(send_conn)
        receivers.setdefault(dst, []).append(recv_conn)
        pairs.append((src, dst))
    return {"senders": senders, "receivers": receivers, "pairs": pairs}


def deliver(senders: list, payload: dict) -> int:
    """上游投递一批半成品记录；对端已关闭就跳过，不阻塞生产。"""
    sent = 0
    for conn in senders or []:
        try:
            conn.send(payload)
            sent += 1
        except (BrokenPipeError, OSError, ValueError):
            continue
    return sent


def collect(receivers: list, wait: float = 0.0) -> list:
    """下游收走上游投递的批次记录。

    wait > 0 时，没数据就短暂等待，给上游留出投递的时间窗口——
    否则下游车间先做完就会直接退出，永远收不到上游的批次。
    """
    got: list = []
    deadline = time.time() + max(float(wait), 0.0)
    for conn in receivers or []:
        while True:
            try:
                if conn.poll(0):
                    got.append(conn.recv())
                    continue
            except (EOFError, OSError, ValueError):
                break
            if time.time() < deadline:
                time.sleep(0.02)
                continue
            break
    return got


def close_pipes(bundle: dict) -> None:
    """关闭父进程持有的全部端点。"""
    for group in ("senders", "receivers"):
        for conns in (bundle.get(group) or {}).values():
            for conn in conns:
                try:
                    conn.close()
                except Exception:
                    pass


def describe_pipes(bundle: dict) -> dict:
    return {
        "pairs": [f"{a}→{b}" for a, b in bundle.get("pairs") or []],
        "count": len(bundle.get("pairs") or []),
    }
