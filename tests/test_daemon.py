"""守护进程真保活：杀掉车间进程，看它是否被真正拉起。

对应课件第 7、8 页与第 15 页考核项"守护线程有效"。
关键在于区分两种"进程没了"：
    正常下班 —— 车间进程退出前注销自己，守护进程不动它
    异常被杀 —— 登记表里还留着记录但 pid 已不存在，守护进程按登记信息重新拉起

运行：
    python -m tests.test_daemon
"""

import os
import signal
import time
from multiprocessing import Process, Queue, Semaphore

from brickforge import persist
from brickforge.daemon import start_daemon, stop_daemon
from brickforge.workers import shop_worker


def kill_all(rows):
    for row in rows:
        try:
            os.kill(int(row.get("pid")), signal.SIGTERM)
        except Exception:
            pass


def main():
    daemon_proc = start_daemon()
    time.sleep(1.5)
    old_pid = None
    try:
        layout = persist.default_layout()
        shop = layout["shops"][0]
        # burst 调大，保证它活得够久，来得及被杀
        jobs = [{"id": "j1", "burst": 60, "machine": "m1", "shop": shop["id"]}]
        material = Semaphore(2)
        report_q: Queue = Queue()
        ctx = {"index": 0, "slots": 2, "senders": [], "receivers": []}
        victim = Process(target=shop_worker, args=(shop, jobs, material, report_q, 0.5, ctx))
        victim.start()
        old_pid = victim.pid
        time.sleep(1.5)

        rows = persist.load_procs()
        assert any(r.get("pid") == old_pid for r in rows), "车间进程启动后没有登记自己"

        victim.terminate()
        victim.join()

        revived = None
        for _ in range(8):
            time.sleep(2)
            hit = [r for r in persist.load_procs() if int(r.get("revives") or 0) > 0]
            if hit:
                revived = hit[0]
                break

        assert revived is not None, "守护进程没有拉起被杀的车间进程"
        assert revived["pid"] != old_pid, "登记表里的 pid 没有更新"
        assert revived.get("shop_id") == shop["id"]
        print(f"守护进程把 pid={old_pid} 拉起为 pid={revived['pid']}"
              f"（累计第 {revived['revives']} 次）")
        print("丁 · 守护进程真保活通过。")
    finally:
        # 只清理本次测试涉及的进程与登记项，不碰表里其它记录
        rows = persist.load_procs()
        mine = [r for r in rows
                if r.get("pid") == old_pid or int(r.get("revives") or 0) > 0]
        kill_all(mine)
        mine_pids = {r.get("pid") for r in mine}
        keep = [r for r in rows if r.get("pid") not in mine_pids]
        if len(keep) != len(rows):
            persist.save_procs(keep)
        time.sleep(0.3)
        stop_daemon(daemon_proc)


if __name__ == "__main__":
    main()
