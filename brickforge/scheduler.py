"""乙：处理机调度。工单 = 进程，机器 = 处理机。别人不要改四种算法本体。"""

from __future__ import annotations

from dataclasses import dataclass, asdict


ALGOS = ("fcfs", "sjf", "priority", "rr", "srtf", "mlfq")
ALGO_NAME = {
    "fcfs": "FCFS 先到先服务",
    "sjf": "SJF 短作业优先",
    "priority": "优先级（静态）",
    "rr": "RR 时间片轮转",
    "srtf": "SRTF 最短剩余",
    "mlfq": "MLFQ 多级反馈",
}


@dataclass
class Job:
    id: str
    name: str
    burst: int
    priority: int
    dest: int = 0
    shop_id: str = ""


def fcfs(jobs: list[Job]) -> list[dict]:
    t = 0
    out = []
    for job in jobs:
        start = t
        end = t + job.burst
        out.append(_row(job, start, end, [[start, end]]))
        t = end
    return out


def sjf(jobs: list[Job]) -> list[dict]:
    return fcfs(sorted(jobs, key=lambda j: (j.burst, j.priority)))


def priority_sched(jobs: list[Job]) -> list[dict]:
    return fcfs(sorted(jobs, key=lambda j: (j.priority, j.burst)))


def rr(jobs: list[Job], quantum: int = 2) -> list[dict]:
    remain = {j.id: j.burst for j in jobs}
    first = {j.id: None for j in jobs}
    finish = {}
    slices = {j.id: [] for j in jobs}
    t = 0
    order = [j.id for j in jobs]
    catalog = {j.id: j for j in jobs}
    while remain:
        progressed = False
        for jid in order:
            if jid not in remain:
                continue
            if first[jid] is None:
                first[jid] = t
            slice_ = min(quantum, remain[jid])
            start = t
            t += slice_
            slices[jid].append([start, t])
            remain[jid] -= slice_
            progressed = True
            if remain[jid] == 0:
                finish[jid] = t
                del remain[jid]
        if not progressed:
            break
    return [_row(catalog[j.id], first[j.id] or 0, finish[j.id], slices[j.id]) for j in jobs]


def _add_slice(slices: dict, jid: str, start: int, end: int) -> None:
    if start >= end:
        return
    if slices[jid] and slices[jid][-1][1] == start:
        slices[jid][-1][1] = end
    else:
        slices[jid].append([start, end])


def srtf(jobs: list[Job]) -> list[dict]:
    remain = {j.id: j.burst for j in jobs}
    first = {j.id: None for j in jobs}
    finish = {}
    slices = {j.id: [] for j in jobs}
    catalog = {j.id: j for j in jobs}
    t = 0
    while remain:
        ready = [catalog[jid] for jid in remain]
        cur = min(ready, key=lambda j: (remain[j.id], j.priority, j.id))
        if first[cur.id] is None:
            first[cur.id] = t
        _add_slice(slices, cur.id, t, t + 1)
        remain[cur.id] -= 1
        t += 1
        if remain[cur.id] == 0:
            finish[cur.id] = t
            del remain[cur.id]
    return [_row(catalog[j.id], first[j.id] or 0, finish[j.id], slices[j.id]) for j in jobs]


def mlfq(jobs: list[Job], quanta: tuple[int, int, int] = (1, 2, 4)) -> list[dict]:
    from collections import deque

    queues = [deque(), deque(), deque()]
    for j in jobs:
        queues[0].append(j.id)
    remain = {j.id: j.burst for j in jobs}
    first = {j.id: None for j in jobs}
    finish = {}
    slices = {j.id: [] for j in jobs}
    catalog = {j.id: j for j in jobs}
    t = 0
    while any(queues):
        lvl = next(i for i in range(3) if queues[i])
        jid = queues[lvl].popleft()
        if first[jid] is None:
            first[jid] = t
        slice_ = min(quanta[lvl], remain[jid])
        _add_slice(slices, jid, t, t + slice_)
        t += slice_
        remain[jid] -= slice_
        if remain[jid] == 0:
            finish[jid] = t
        else:
            queues[min(lvl + 1, 2)].append(jid)
    return [_row(catalog[j.id], first[j.id] or 0, finish[j.id], slices[j.id]) for j in jobs]


def run(jobs: list[Job], algo: str, quantum: int = 2) -> list[dict]:
    table = {
        "fcfs": fcfs,
        "sjf": sjf,
        "priority": priority_sched,
        "rr": lambda js: rr(js, quantum),
        "srtf": srtf,
        "mlfq": mlfq,
    }
    fn = table.get(algo, priority_sched)
    return fn(jobs)


def metrics(timeline: list[dict]) -> dict:
    if not timeline:
        return {"avg_wait": 0.0, "makespan": 0, "finished": 0, "switches": 0}
    waits = [row["end"] - row["burst"] for row in timeline]
    events = []
    for row in timeline:
        for a, b in row.get("slices") or [[row["start"], row["end"]]]:
            events.append((a, b, row["id"]))
    events.sort()
    switches = 0
    prev = None
    for _a, _b, jid in events:
        if prev is not None and jid != prev:
            switches += 1
        prev = jid
    return {
        "avg_wait": round(sum(waits) / len(waits), 2),
        "makespan": max(row["end"] for row in timeline),
        "finished": len(timeline),
        "switches": switches,
    }


def compare(jobs: list[Job], quantum: int = 2) -> dict:
    out = {}
    for algo in ALGOS:
        timeline = run(jobs, algo, quantum)
        out[algo] = {
            "name": ALGO_NAME[algo],
            "timeline": timeline,
            "metrics": metrics(timeline),
        }
    return out


def _row(job: Job, start: int, end: int, slices: list[list[int]] | None = None) -> dict:
    data = asdict(job)
    data.update({"start": start, "end": end, "wait": start, "slices": slices or [[start, end]]})
    return data
