"""审计：守护进程生命周期、进程管理、持久化与文件锁。

不占端口（直接调 runtime / daemon），跑完自己收工。
运行：
    python -m tests.audit_lifecycle
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import pathlib
import time

from brickforge import persist, runtime
from brickforge.daemon import MAX_REVIVES, start_daemon, stop_daemon

FAILS: list[str] = []
NOTES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f"   | {detail}" if detail else ""))
        FAILS.append(name)


def note(text: str) -> None:
    print(f"  NOTE  {text}")
    NOTES.append(text)


def wait_for(fn, timeout: float, interval: float = 0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


# ------------------------------------------------------- 多进程写文件的竞争 ---
def _writer(tag: str, times: int, out: "mp.Queue") -> None:
    from brickforge import persist as p

    errors = 0
    for i in range(times):
        layout = p.default_layout()
        layout["price"] = 10 + i
        try:
            p.save_layout(layout)
        except Exception as exc:          # noqa: BLE001 - 就是要抓住一切
            errors += 1
            out.put(f"{tag}: {type(exc).__name__}: {exc}")
    out.put(f"{tag}: done errors={errors}")


def audit_filelock() -> None:
    print("\n=== 文件锁：多进程同时写布局 ===")
    persist.save_layout(persist.default_layout())
    q: mp.Queue = mp.Queue()
    procs = [mp.Process(target=_writer, args=(f"w{i}", 40, q)) for i in range(4)]
    for proc in procs:
        proc.start()
    bad_json = 0
    for _ in range(80):
        data = persist.load_layout()
        if not isinstance(data, dict) or "shops" not in data:
            bad_json += 1
        time.sleep(0.01)
    for proc in procs:
        proc.join(timeout=30)
    messages = []
    while not q.empty():
        messages.append(q.get())
    errors = [m for m in messages if "done errors=" not in m]
    check("4 进程 × 40 次并发写：没有异常", not errors, str(errors[:3]))
    check("并发读写期间读到的永远是完整 JSON", bad_json == 0, f"{bad_json} 次坏读")
    left = list(persist.DATA.glob("*.tmp"))
    check("不留 .tmp 临时文件", not left, str(left))

    # 直接写坏文件，看自愈
    for content in ("{bad json", "[1,2,3]", '"string"', ""):
        persist.LAYOUT.write_text(content, encoding="utf-8")
        got = persist.load_layout()
        check(f"factory.json 被写成 {content[:12]!r} 时回落到默认布局",
              isinstance(got, dict) and "shops" in got)
    persist.save_layout(persist.default_layout())


def _registry_worker(pid: int, q: "mp.Queue") -> None:
    """模块级函数：Windows 的 spawn 只能 pickle 模块级对象。"""
    from brickforge.persist import register_proc, unregister_proc

    register_proc({"pid": pid, "kind": "shop", "shop_id": f"s{pid}", "name": "x"})
    unregister_proc(pid)
    q.put(pid)


def audit_registry() -> None:
    print("\n=== 进程登记表：并发登记/注销 ===")
    persist.purge_procs(None)
    q: mp.Queue = mp.Queue()
    procs = [mp.Process(target=_registry_worker, args=(os.getpid() * 1000 + i, q))
             for i in range(6)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=20)
    entries = persist.load_procs()
    pids = [p.get("pid") for p in entries]
    check("6 个进程各自登记再注销后表为空", entries == [], str(entries))
    check("登记表里没有重复 pid", len(pids) == len(set(pids)), str(pids))

    # 寄存器残留 + 无效内容
    persist.PROCS.write_text("{bad", encoding="utf-8")
    check("procs.json 损坏时返回空表", persist.load_procs() == [])
    persist.RUN.write_text("not json", encoding="utf-8")
    check("run.json 损坏时回落 idle", persist.load_run().get("state") == "idle")

    persist.append_log("审计：一行日志")
    check("日志能追加并读回", "审计：一行日志" in (persist.tail_log(3) or []))


def audit_cycles(daemon) -> None:
    print("\n=== 反复起班/收工：不泄漏进程和登记 ===")
    for i in range(3):
        started = runtime.start(shop_seconds=8, tick=0.3)
        ok = started.get("ok") is True
        time.sleep(1.2)
        status = runtime.status()
        running = [p for p in status["procs"] if p["state"] == "running"]
        check(f"第 {i+1} 轮起班成功且车间在跑", ok and len(running) >= 1, str(started)[:80])
        stopped = runtime.stop()
        time.sleep(0.6)
        check(f"第 {i+1} 轮收工后登记表清空", persist.load_procs() == [], str(persist.load_procs())[:80])
        check(f"第 {i+1} 轮收工后本地进程全部退出",
              all(not p.is_alive() for p in runtime._PROCS.values()),
              str([p.pid for p in runtime._PROCS.values()]))
    _ = daemon


def audit_revive_cap(daemon) -> None:
    print(f"\n=== 守护进程拉起上限（MAX_REVIVES={MAX_REVIVES}）===")
    # 守护进程盯着 watchdog.json：15 秒没人刷就认为"主人没了"并自退。
    # 真服务有后台线程每 3 秒刷一次；这里自己冒充，否则审计跑久了它先走了。
    persist.write_watchdog()
    note("守护进程在 watchdog.json 超过 15 秒没人刷新时会自退；"
         "直接调 start_daemon() 的脚本必须自己刷心跳，否则会被当成主人已死")
    runtime.start(shop_seconds=90, tick=0.3)
    time.sleep(2.5)
    rows = [p for p in runtime.status()["guarded"] if p["state"] == "running"]
    if not rows:
        check("起班后有可杀的车间进程", False, str(runtime.status()["procs"]))
        return
    shop = rows[0]["shop_id"]
    seen_pids = {int(rows[0]["pid"])}
    for attempt in range(1, MAX_REVIVES + 2):
        persist.write_watchdog()
        current = [p for p in runtime.status()["procs"] if p.get("shop_id") == shop]
        if not current:
            break
        pid = int(current[0]["pid"])
        kill = runtime.kill(shop)
        check(f"第 {attempt} 次硬杀投递成功", kill.get("ok") is True, str(kill)[:80])

        def poll():
            persist.write_watchdog()
            return next((p for p in runtime.status()["procs"]
                         if p.get("shop_id") == shop and int(p["pid"]) not in seen_pids), None)

        revived = wait_for(poll, timeout=8)
        if attempt <= MAX_REVIVES:
            check(f"第 {attempt} 次被拉起", revived is not None,
                  f"kill pid={pid} 状态={runtime.status()['procs']}")
            if revived:
                seen_pids.add(int(revived["pid"]))
        else:
            check(f"超过上限（第 {attempt} 次）不再拉起", revived is None,
                  str(runtime.status()["procs"])[:160])
    log = "\n".join(persist.tail_log(60))
    check("日志里出现「放弃拉起」", "放弃拉起" in log, log[-200:])
    runtime.stop()


def main() -> int:
    runtime.stop()
    persist.purge_procs(None)
    persist.save_run({"run_id": None, "state": "idle"})
    daemon = start_daemon()
    audit_filelock()
    audit_registry()
    try:
        audit_cycles(daemon)
        audit_revive_cap(daemon)
    finally:
        runtime.stop()
        stop_daemon(daemon if hasattr(daemon, "is_alive") else None)
    print()
    if NOTES:
        print(f"观察项 {len(NOTES)} 条：")
        for text in NOTES:
            print("  -", text)
        print()
    if FAILS:
        print(f"发现 {len(FAILS)} 处问题：")
        for name in FAILS:
            print("  -", name)
        return 1
    print("生命周期审计全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
