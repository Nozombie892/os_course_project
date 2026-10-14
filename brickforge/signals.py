"""信号处理：信号处理程序、可靠信号、急停与换班。

对应课件《操作系统原理课程设计》第 4 页"信号和同步处理"：
    - 信号处理
    - 可靠信号
    - 信号处理程序

平台差异（答辩时要说清楚）：
    POSIX  —— 使用真正的内核信号。RELOAD 优先取实时信号 SIGRTMIN，
              实时信号是"可靠信号"：会被排队、不会丢失；
              普通信号（SIGUSR1/SIGTERM）不排队，同一个信号连发两次只算一次。
              TICK 用 SIGALRM + setitimer 由内核定时触发。
    Windows —— 只支持 SIGINT / SIGTERM / SIGBREAK / SIGABRT，没有 SIGUSR1、
              SIGALRM、SIGRTMIN。此时这些语义降级为"软信号"：进程内的标志位，
              接口完全一致，但不再经过内核。

用法：
    hub = SignalHub()
    hub.on(STOP, lambda name, sig: print("收到急停"))
    hub.install()                 # 装处理程序（必须在主线程）
    hub.send(pid, STOP)           # 给别的进程发信号
    if hub.take(STOP): ...        # 取走并清零
"""

from __future__ import annotations

import os
import signal
import threading

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------- 语义信号 ---
STOP = "stop"      # 急停：停止当前班次
RELOAD = "reload"  # 换班：重新读取布局
TICK = "tick"      # 心跳：周期任务

_ALL = (STOP, RELOAD, TICK)


def resolve(name: str) -> int | None:
    """语义名 → 平台信号号；该平台没有就返回 None（走软信号）。"""
    if name == STOP:
        return getattr(signal, "SIGTERM", None)
    if name == RELOAD:
        # 实时信号优先：它是可靠信号，会排队
        for attr in ("SIGRTMIN", "SIGUSR1"):
            sig = getattr(signal, attr, None)
            if sig is not None:
                return sig
        return None
    if name == TICK:
        return getattr(signal, "SIGALRM", None)
    return None


def signal_name(sig: int) -> str:
    try:
        return signal.Signals(sig).name
    except (ValueError, AttributeError):
        return str(sig)


def capabilities() -> dict:
    """平台信号能力，用于面板展示与答辩说明。"""
    return {
        "platform": "windows" if IS_WINDOWS else "posix",
        "real_signals": not IS_WINDOWS,
        "reliable": bool(getattr(signal, "SIGRTMIN", None) is not None),
        "stop": signal_name(resolve(STOP)) if resolve(STOP) is not None else "软信号",
        "reload": signal_name(resolve(RELOAD)) if resolve(RELOAD) is not None else "软信号",
        "tick": signal_name(resolve(TICK)) if resolve(TICK) is not None else "软信号",
    }


class SignalHub:
    """信号总线：装处理程序、发信号、记录到达、回调处理程序。"""

    def __init__(self, label: str = "brickforge") -> None:
        self.label = label
        self._lock = threading.RLock()
        self._handlers: dict[str, list] = {}
        self._pending: dict[str, int] = {}
        self._total: dict[str, int] = {}
        self._saved: dict[int, object] = {}
        self._installed: tuple[str, ...] = ()

    # ------------------------------------------------------------ 接收端 ---
    def install(self, names: tuple[str, ...] = (STOP, RELOAD)) -> list[str]:
        """安装信号处理程序。只有主线程能装，返回真正装上的语义名。"""
        if threading.current_thread() is not threading.main_thread():
            return []
        installed: list[str] = []
        with self._lock:
            for name in names:
                sig = resolve(name)
                if sig is None or sig in self._saved:
                    continue
                try:
                    self._saved[sig] = signal.getsignal(sig)
                    signal.signal(sig, self._make_handler(name))
                    installed.append(name)
                except (OSError, ValueError):
                    continue
            self._installed = tuple(installed)
        return installed

    def _make_handler(self, name: str):
        def handler(signum, _frame):
            # 信号处理程序里只做最轻的事：记账 + 转交回调
            self.deliver(name, signum)
        return handler

    def restore(self) -> None:
        """还原原有处理程序（退出前调用，避免影响宿主）。"""
        if threading.current_thread() is not threading.main_thread():
            return
        with self._lock:
            for sig, original in list(self._saved.items()):
                try:
                    signal.signal(sig, original)
                except (OSError, ValueError):
                    pass
            self._saved.clear()
            self._installed = ()

    # ------------------------------------------------------------ 处理程序 ---
    def on(self, name: str, fn) -> None:
        """注册信号处理程序：收到该信号时回调 fn(name, signum)。"""
        with self._lock:
            self._handlers.setdefault(name, []).append(fn)

    def off(self, name: str, fn) -> None:
        with self._lock:
            if fn in self._handlers.get(name, []):
                self._handlers[name].remove(fn)

    def deliver(self, name: str, signum: int = 0) -> None:
        """登记一次信号到达并触发处理程序。软信号路径也走这里。"""
        with self._lock:
            self._pending[name] = self._pending.get(name, 0) + 1
            self._total[name] = self._total.get(name, 0) + 1
            callbacks = list(self._handlers.get(name, ()))
        for fn in callbacks:
            try:
                fn(name, signum)
            except Exception:
                # 处理程序内部出错不能影响信号接收
                pass

    # ------------------------------------------------------------ 查询 ---
    def pending(self, name: str) -> int:
        with self._lock:
            return self._pending.get(name, 0)

    def take(self, name: str) -> int:
        """取走该信号的待处理次数并清零。"""
        with self._lock:
            n = self._pending.get(name, 0)
            self._pending[name] = 0
            return n

    def total(self, name: str) -> int:
        with self._lock:
            return self._total.get(name, 0)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "label": self.label,
                "installed": list(self._installed),
                "received": dict(self._total),
                "pending": {k: v for k, v in self._pending.items() if v},
            }

    # ------------------------------------------------------------ 发送端 ---
    def send(self, pid: int, name: str = STOP) -> bool:
        """给指定进程发信号。Windows 上 SIGTERM 会直接终止目标进程。"""
        sig = resolve(name)
        if sig is None or pid <= 0:
            return False
        try:
            os.kill(pid, sig)
            return True
        except (OSError, ProcessLookupError, PermissionError):
            return False

    def soft(self, name: str = STOP) -> None:
        """进程内触发（Windows 降级路径 / 自测用）。"""
        self.deliver(name, 0)


# ------------------------------------------------------------- 定时器 ---
class IntervalTicker:
    """周期性 tick。

    POSIX 走 SIGALRM + setitimer（内核定时，对应课件示例的写法）；
    其它平台退化为后台线程，接口一致。
    """

    def __init__(self, seconds: float, hub: SignalHub | None = None, on_tick=None) -> None:
        self.seconds = max(float(seconds), 0.1)
        self.hub = hub
        self.on_tick = on_tick
        self.mode = "thread"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> str:
        if not IS_WINDOWS and hasattr(signal, "setitimer"):
            try:
                signal.signal(signal.SIGALRM, self._alarm_handler)
                signal.setitimer(signal.ITIMER_REAL, self.seconds, self.seconds)
                self.mode = "itimer"
                return self.mode
            except (OSError, ValueError, AttributeError):
                pass
        self._thread = threading.Thread(target=self._loop, name="ticker", daemon=True)
        self._thread.start()
        self.mode = "thread"
        return self.mode

    def _alarm_handler(self, signum, _frame):
        if self.hub is not None:
            self.hub.deliver(TICK, signum)
        if self.on_tick is not None:
            self.on_tick()

    def _loop(self) -> None:
        while not self._stop.wait(self.seconds):
            if self.hub is not None:
                self.hub.deliver(TICK, 0)
            if self.on_tick is not None:
                self.on_tick()

    def stop(self) -> None:
        self._stop.set()
        if not IS_WINDOWS and hasattr(signal, "setitimer"):
            try:
                signal.setitimer(signal.ITIMER_REAL, 0, 0)
            except (OSError, ValueError):
                pass
