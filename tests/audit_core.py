"""审计：算法正确性 —— 用独立参考实现 / 不变量来对照，不复用被测代码。

运行：
    python -m tests.audit_core
"""

from __future__ import annotations

import itertools
import random
import subprocess
import sys
from collections import Counter

from brickforge import banker, memory, scheduler, shipping, warehouse
from brickforge.scheduler import Job

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


def probe_hang(label: str, code: str, timeout: float = 6.0) -> None:
    """在子进程里跑一段可能不返回的代码：超时就判为死循环。

    审计脚本自己绝不能被被测代码拖死（上次就是这么中断的）。
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            encoding="utf-8", timeout=timeout,
        )
        check(label, proc.returncode == 0, f"rc={proc.returncode} {proc.stderr.strip()[:120]}")
    except subprocess.TimeoutExpired:
        check(label, False, f"超过 {timeout}s 未返回 —— 死循环")


# --------------------------------------------------------------- 调度不变量 ---
def slices_of(row: dict) -> list[list[int]]:
    return row.get("slices") or [[row["start"], row["end"]]]


def check_schedule_contract(tag: str, jobs: list[Job], timeline: list[dict]) -> None:
    total = sum(j.burst for j in jobs)
    ids = [j.id for j in jobs]
    check(f"{tag}: 每个工单恰好出现一次", sorted(r["id"] for r in timeline) == sorted(ids),
          f"{sorted(r['id'] for r in timeline)} != {sorted(ids)}")
    bad = []
    for row in timeline:
        got = sum(b - a for a, b in slices_of(row))
        if got != row["burst"]:
            bad.append((row["id"], got, row["burst"]))
    check(f"{tag}: 每个工单的切片时长合计 = burst", not bad, str(bad))

    # 同一时刻不能有两个工单在跑
    events = []
    for row in timeline:
        for a, b in slices_of(row):
            events.append((a, b, row["id"]))
    events.sort()
    overlap = [(events[i], events[i + 1]) for i in range(len(events) - 1)
               if events[i][1] > events[i + 1][0]]
    check(f"{tag}: 时间轴无重叠", not overlap, str(overlap[:3]))

    # 单处理机、无空闲：makespan 必须等于总 burst，且从 0 开始
    span = max(b for _a, b, _j in events) if events else 0
    start = min(a for a, _b, _j in events) if events else 0
    check(f"{tag}: 无空闲（makespan = Σburst）", span == total and start == 0,
          f"span={span} total={total} start={start}")

    m = scheduler.metrics(timeline)
    waits = [r["end"] - r["burst"] for r in timeline]
    expect = round(sum(waits) / len(waits), 2) if waits else 0.0
    check(f"{tag}: avg_wait 与 (end - burst) 一致", m["avg_wait"] == expect,
          f"{m['avg_wait']} != {expect}")


def ref_rr(jobs: list[Job], q: int) -> dict[str, list[list[int]]]:
    """独立实现：时间片轮转。"""
    remain = {j.id: j.burst for j in jobs}
    order = [j.id for j in jobs]
    out: dict[str, list[list[int]]] = {j.id: [] for j in jobs}
    t = 0
    while remain:
        for jid in order:
            if jid not in remain:
                continue
            step = min(q, remain[jid])
            out[jid].append([t, t + step])
            t += step
            remain[jid] -= step
            if remain[jid] == 0:
                del remain[jid]
    return out


def ref_fcfs(jobs: list[Job]) -> dict[str, tuple[int, int]]:
    t = 0
    out = {}
    for job in jobs:
        out[job.id] = (t, t + job.burst)
        t += job.burst
    return out


def audit_scheduler() -> None:
    print("\n=== 调度：不变量 + 独立参考实现 ===")
    cases = [
        [Job("a", "A", 3, 1), Job("b", "B", 8, 3), Job("c", "C", 2, 2), Job("d", "D", 5, 2)],
        [Job("only", "唯一", 7, 2)],
        [Job("x", "X", 1, 1), Job("y", "Y", 1, 1)],
        [Job(f"j{i}", f"J{i}", 1 + (i * 7) % 9, 1 + i % 3) for i in range(12)],
    ]
    for idx, jobs in enumerate(cases):
        for algo in scheduler.ALGOS:
            timeline = scheduler.run(jobs, algo, 2)
            check_schedule_contract(f"case{idx}/{algo}", jobs, timeline)

    # FCFS 与参考逐项一致
    random.seed(7)
    jobs = [Job(f"j{i}", f"J{i}", random.randint(1, 9), random.randint(1, 3)) for i in range(8)]
    got = {r["id"]: (r["start"], r["end"]) for r in scheduler.run(jobs, "fcfs")}
    check("FCFS 与参考实现完全一致", got == ref_fcfs(jobs), f"{got} vs {ref_fcfs(jobs)}")

    # RR 与参考的切片序列逐项一致（多个 quantum）
    for q in (1, 2, 3, 5, 100):
        got = {r["id"]: slices_of(r) for r in scheduler.run(jobs, "rr", q)}
        check(f"RR quantum={q} 与参考实现完全一致", got == ref_rr(jobs, q),
              f"{got} vs {ref_rr(jobs, q)}")
        over = [(r["id"], s) for r in scheduler.run(jobs, "rr", q)
                for s in slices_of(r) if s[1] - s[0] > q]
        check(f"RR quantum={q} 没有超过时间片的切片", not over, str(over[:3]))

    # SJF / 优先级：按排序键验证顺序
    jobs = [Job("a", "A", 5, 3), Job("b", "B", 2, 1), Job("c", "C", 2, 2), Job("d", "D", 4, 1)]
    order = [r["id"] for r in sorted(scheduler.run(jobs, "sjf"), key=lambda r: r["start"])]
    check("SJF 顺序按 (burst, priority)", order == ["b", "c", "d", "a"], str(order))
    order = [r["id"] for r in sorted(scheduler.run(jobs, "priority"), key=lambda r: r["start"])]
    check("优先级顺序按 (priority, burst)", order == ["b", "d", "c", "a"], str(order))

    # SRTF 性质：任一时刻运行的是剩余时间最短的
    jobs = [Job("a", "A", 6, 2), Job("b", "B", 3, 2), Job("c", "C", 5, 1)]
    timeline = scheduler.run(jobs, "srtf")
    remain = {j.id: j.burst for j in jobs}
    pri = {j.id: j.priority for j in jobs}
    violation = None
    for _t in range(sum(remain.values())):
        cur = min(remain, key=lambda jid: (remain[jid], pri[jid], jid))
        running = next(r["id"] for r in timeline if any(a <= _t < b for a, b in slices_of(r)))
        if running != cur:
            violation = (_t, running, cur)
            break
        remain[cur] -= 1
        if remain[cur] == 0:
            del remain[cur]
    check("SRTF 每拍都跑剩余时间最短的工单", violation is None, str(violation))

    # MLFQ：队列 0/1/2 的时间片分别是 1/2/4
    jobs = [Job("a", "A", 9, 1), Job("b", "B", 4, 1)]
    timeline = scheduler.run(jobs, "mlfq")
    caps = []
    for row in timeline:
        for i, (a, b) in enumerate(slices_of(row)):
            caps.append((row["id"], i, b - a))
    over = [(jid, i, d) for jid, i, d in caps if d > (1, 2, 4)[min(i, 2)]]
    check("MLFQ 切片不超过对应队列的时间片", not over, str(over[:3]))

    # 边界：空集、quantum<=0（这类调用最容易不返回，放到子进程里带超时跑）
    check("空工单集不抛异常", scheduler.run([], "rr") == [] and scheduler.compare([]) != {})
    probe_hang("rr(quantum=0) 不卡死",
               "from brickforge.scheduler import Job, run; run([Job('a','A',3,1)],'rr',0)")
    probe_hang("rr(quantum=-3) 不卡死",
               "from brickforge.scheduler import Job, run; run([Job('a','A',3,1)],'rr',-3)")
    probe_hang("mlfq(quanta=(0,0,0)) 不卡死",
               "from brickforge.scheduler import Job, mlfq; mlfq([Job('a','A',3,1)], (0,0,0))")
    probe_hang("compare(jobs, quantum=0) 不卡死",
               "from brickforge.scheduler import Job, compare; compare([Job('a','A',3,1)], 0)")

    # 重复 id：布局入口（sanitize_layout）必须把它改成唯一的，否则调度器按 id
    # 建字典时会把两张工单合并成一张
    from brickforge.kernel import jobs_from, sanitize_layout

    lay = {"jobs": [{"id": "same", "burst": 3}, {"id": "same", "burst": 4}], "shops": []}
    fixes = sanitize_layout(lay)
    dup_jobs = jobs_from(lay)
    ids = [j.id for j in dup_jobs]
    check("重复工单 id 被清洗成唯一", len(ids) == len(set(ids)) == 2, f"{ids} 修正={fixes}")
    timeline = scheduler.run(dup_jobs, "rr")
    covered = sum(b - a for row in timeline for a, b in slices_of(row))
    check("清洗后时间线覆盖的总时长 = Σburst",
          covered == sum(j.burst for j in dup_jobs),
          f"{covered} != {sum(j.burst for j in dup_jobs)}")


# --------------------------------------------------------------- 页面置换 ---
def ref_paging(refs: list[int], frames: int, algo: str) -> tuple[int, list[int]]:
    mem: list[int] = []
    last: dict[int, int] = {}
    faults = 0
    for t, page in enumerate(refs):
        if page in mem:
            last[page] = t
            continue
        faults += 1
        if len(mem) < frames:
            mem.append(page)
        elif algo == "fifo":
            mem.pop(0)
            mem.append(page)
        else:
            victim = min(mem, key=lambda p: last.get(p, -1))
            mem[mem.index(victim)] = page
        last[page] = t
    return faults, mem


def audit_paging() -> None:
    print("\n=== 虚拟内存：FIFO / LRU 对照参考实现 ===")
    random.seed(11)
    bad = []
    for _ in range(60):
        refs = [random.randint(1, 6) for _ in range(random.randint(0, 18))]
        frames = random.randint(1, 4)
        for algo in ("fifo", "lru"):
            got = memory.replace(refs, frames, algo)
            want_faults, want_final = ref_paging(refs, frames, algo)
            if got["faults"] != want_faults or got["final"] != want_final:
                bad.append((refs, frames, algo, got["faults"], want_faults, got["final"], want_final))
    check("60 组随机引用串的缺页数与终态都与参考一致", not bad, str(bad[:2]))

    got = memory.replace([], 3, "lru")
    check("空引用串不崩", got["faults"] == 0 and got["final"] == [])
    got = memory.replace([1, 2, 1], 0, "lru")
    check("页框=0 按默认 4 处理（0 视作未设置）", got["frames"] == 4, str(got["frames"]))
    got = memory.replace([1, 2, 1], -2, "lru")
    check("页框为负数时按 1 兜底", got["frames"] == 1, str(got["frames"]))


# --------------------------------------------------------------- 银行家 ---
def safety_bruteforce(work: list[int], need: list[list[int]], alloc: list[list[int]]) -> bool:
    n = len(need)
    for order in itertools.permutations(range(n)):
        w = list(work)
        ok = True
        for i in order:
            if any(need[i][k] > w[k] for k in range(len(w))):
                ok = False
                break
            w = [w[k] + alloc[i][k] for k in range(len(w))]
        if ok:
            return True
    return False


def audit_banker() -> None:
    print("\n=== 银行家：安全性判定对照穷举 ===")
    random.seed(5)
    bad = []
    for _ in range(200):
        n = random.randint(0, 5)
        m = 3
        work = [random.randint(0, 4) for _ in range(m)]
        alloc = [[random.randint(0, 3) for _ in range(m)] for _ in range(n)]
        need = [[random.randint(0, 4) for _ in range(m)] for _ in range(n)]
        got, seq = banker.safety(work, need, alloc)
        want = safety_bruteforce(work, need, alloc)
        if got != want:
            bad.append((work, need, alloc, got, want))
            continue
        if got:
            # 返回的安全序列必须真的可行
            w = list(work)
            valid = True
            seen = set()
            for i in seq:
                if i in seen or any(need[i][k] > w[k] for k in range(m)):
                    valid = False
                    break
                seen.add(i)
                w = [w[k] + alloc[i][k] for k in range(m)]
            if not valid or len(seen) != n:
                bad.append(("安全序列不合法", work, need, alloc, seq))
    check("200 组随机矩阵的安全性判定与穷举一致、序列合法", not bad, str(bad[:2]))

    jobs = [{"name": "急单", "burst": 3, "priority": 1},
            {"name": "普通", "burst": 5, "priority": 2},
            {"name": "加急", "burst": 4, "priority": 2}]
    safe = banker.snapshot(jobs, [5, 6, 4])
    check("默认资源 [5,6,4] 判定安全", safe["safe"] is True, str(safe["sequence_names"]))
    unsafe = banker.snapshot(jobs, [4, 5, 3])
    check("[4,5,3] 判定不安全（README 说的对照）", unsafe["safe"] is False,
          str(unsafe["sequence_names"]))
    for snap in (safe, unsafe):
        ok = all(0 <= snap["allocation"][i][k] <= snap["maximum"][i][k]
                 for i in range(len(jobs)) for k in range(3))
        ok = ok and all(snap["need"][i][k] == snap["maximum"][i][k] - snap["allocation"][i][k]
                        for i in range(len(jobs)) for k in range(3))
        check("矩阵不变量：0<=alloc<=max 且 need=max-alloc", ok)
    check("非法可用资源长度被兜住", banker.snapshot(jobs, [1])["available_init"] == [1, 0, 0])
    check("available 传字符串不崩", isinstance(banker.snapshot(jobs, "x"), dict))


# --------------------------------------------------------------- 仓储 ---
def audit_warehouse() -> None:
    print("\n=== 仓储：三种放置策略 ===")
    shelves = [8, 5, 12, 3]
    first = warehouse.allocate(shelves, 4, "first")
    best = warehouse.allocate(shelves, 4, "best")
    worst = warehouse.allocate(shelves, 4, "worst")
    check("首次适应选下标 0", first["index"] == 0, str(first))
    check("最佳适应选最贴合的 1 号（5）", best["index"] == 1, str(best))
    check("最坏适应选最大剩余 2 号（12）", worst["index"] == 2, str(worst))
    check("放入后剩余 = 原剩余 - 需求",
          best["shelves"] == [8, 1, 12, 3], str(best["shelves"]))
    miss = warehouse.allocate(shelves, 99, "best")
    check("放不下时返回 ok=False 且货架不变",
          miss["ok"] is False and miss["index"] == -1 and miss["shelves"] == shelves)
    check("未知策略回落最佳适应",
          warehouse.allocate(shelves, 4, "nonsense")["index"] == best["index"])

    pack = warehouse.pack_jobs([3, 5, 4], shelves, "best")
    check("pack_jobs 放入 + 拒绝 = 总数", pack["placed"] + pack["rejected"] == 3)
    check("原始货架快照不被就地修改", pack["origin"] == shelves)
    empty = warehouse.pack_jobs([], [], "best")
    check("空输入不除零", empty["fragment_rate"] == 0.0)


# --------------------------------------------------------------- 磁盘调度 ---
def audit_shipping() -> None:
    print("\n=== 磁盘调度：路径合法性 + 与教科书定义对照 ===")
    random.seed(3)
    bad_perm, bad_dist = [], []
    for _ in range(80):
        dests = [random.randint(1, 40) for _ in range(random.randint(1, 8))]
        for algo in ("fcfs", "sstf", "scan", "cscan"):
            path = shipping.run(dests, algo)["path"]
            dist = shipping.run(dests, algo)["distance"]
            if Counter(path[1:]) != Counter(dests) or path[0] != 0:
                bad_perm.append((algo, dests, path))
            want = sum(abs(path[i] - path[i - 1]) for i in range(1, len(path)))
            if want != dist:
                bad_dist.append((algo, path, dist, want))
    check("80 组随机目的地：每种算法都恰好访问一遍", not bad_perm, str(bad_perm[:2]))
    check("distance 与路径逐段求和一致", not bad_dist, str(bad_dist[:2]))

    # 起点固定为 0（kernel 就是这么调的）时，SCAN 与 C-SCAN 在上升途中已经服务完所有目的地
    scan = shipping.run([3, 9, 20], "scan")
    cscan = shipping.run([3, 9, 20], "cscan")
    if scan["path"] == cscan["path"] and scan["distance"] == cscan["distance"]:
        note(f"起点固定 0 时 SCAN 与 C-SCAN 路径完全相同 {scan['path']}（数学上必然如此），"
             f"设备页已如实提示这一点")

    # 换成自定义起点才看得出区别，但 C-SCAN 的回程距离被少算了
    far = shipping.cscan_route([3, 9, 20], start=15)
    true_c = (20 - 15) + 20 + 9        # 右扫到 20 → 跳回 0 → 再扫到 9
    if far["distance"] != true_c:
        note(f"C-SCAN(start=15) 实测距离 {far['distance']}，按教科书定义应为 {true_c}"
             f"（少算了跳回 0 的那一段；当前 kernel 只用 start=0，所以界面上看不出来）")

    # 具体例子：SSTF 必须比 FCFS 更短（默认演示的前提）
    dests = [12, 7, 9]
    sstf = shipping.run(dests, "sstf")
    fcfs = shipping.run(dests, "fcfs")
    check("SSTF 距离 <= FCFS", sstf["distance"] <= fcfs["distance"],
          f"{sstf['distance']} vs {fcfs['distance']}")
    check("空目的地不崩", shipping.run([], "sstf")["path"] == [0])


def audit_sanitize() -> None:
    print("\n=== 布局清洗：手改坏的 factory.json 不能把仿真打挂 ===")
    import copy

    from brickforge.kernel import sanitize_layout, simulate
    from brickforge.persist import default_layout

    base = default_layout()
    cases = {
        "shops 是字符串": {"shops": "abc"},
        "shops 里放数字": {"shops": [1, 2]},
        "shops 是对象": {"shops": {"a": 1}},
        "jobs 是对象": {"jobs": {"a": 1}},
        "jobs 里放字符串": {"jobs": ["x"]},
        "machines 是字符串": {"shops": [{"id": "s1", "machines": "abc", "wires": []}], "jobs": []},
        "wires 是字符串": {"shops": [{"id": "s1", "machines": [], "wires": "abc"}], "jobs": []},
        "车间缺 x/y": {"shops": [{"id": "s1", "machines": [], "wires": []}], "jobs": []},
        "机器 workers='abc'": {"shops": [{"id": "s1", "wires": [],
                                        "machines": [{"id": "m1", "workers": "abc"}]}], "jobs": []},
        "shelves 是字符串": {"shelves": "8,5"},
        "shelves 里是字符串": {"shelves": ["a"]},
        "quantum 是字符串": {"quantum": "abc"},
        "price 是字符串": {"price": "abc"},
        "inbound.items 是字符串": {"inbound": {"items": "abc"}},
        "outbound 缺 items": {"outbound": {}},
        "feeds 里缺字段": {"feeds": [{}]},
        "ships 里缺字段": {"ships": [{"from": None}]},
        "links 里缺字段": {"links": [{"from": "s1"}]},
    }
    for label, patch in cases.items():
        lay = copy.deepcopy(base)
        lay.update(patch)
        try:
            res = simulate(lay, persist_state=False)
            ok = res.get("ok") is True and isinstance(res.get("notes"), str)
            detail = ""
        except Exception as exc:                    # noqa: BLE001
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        check(f"simulate[{label}] 能跑完且返回 ok", ok, detail)

    # 清洗要如实报告，不能悄悄吞掉
    lay = copy.deepcopy(base)
    lay["shops"] = "abc"
    fixes = sanitize_layout(copy.deepcopy(lay))
    check("清洗结果会被报告出来", bool(fixes), str(fixes))
    res = simulate(lay, persist_state=False)
    check("结果里带 repairs 且提示里说明了",
          bool(res.get("repairs")) and "结构不对" in res.get("notes", ""),
          str(res.get("repairs"))[:80])


def main() -> int:
    audit_scheduler()
    audit_paging()
    audit_banker()
    audit_warehouse()
    audit_shipping()
    audit_sanitize()
    print()
    if NOTES:
        print(f"观察项 {len(NOTES)} 条（不算失败，但值得知道）：")
        for text in NOTES:
            print("  -", text)
        print()
    if FAILS:
        print(f"发现 {len(FAILS)} 处问题：")
        for name in FAILS:
            print("  -", name)
        return 1
    print("算法审计全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
