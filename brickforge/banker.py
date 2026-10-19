"""银行家算法：模具/工位/料口三类资源，避免死锁分配。"""

from __future__ import annotations


RESOURCES = ("模具", "工位", "料口")


def _normalize_available(available) -> list[int]:
    """把外部传进来的可用资源裁剪/补齐成正好 len(RESOURCES) 个非负整数。

    布局文件是用户可编辑的（也会经 HTTP 传进来），长度不对时如果直接
    `available[i]` 会 IndexError，把整个仿真打成 500。这里统一兜住。
    """
    m = len(RESOURCES)
    if not isinstance(available, (list, tuple)):
        return [4, 5, 3]
    out: list[int] = []
    for value in list(available)[:m]:
        try:
            out.append(max(int(value), 0))
        except (TypeError, ValueError):
            out.append(0)
    while len(out) < m:
        out.append(0)
    return out


def _get(job, key, default=None):
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


def _claim(job, i: int) -> int:
    burst = max(int(_get(job, "burst") or 3), 1)
    pri = max(int(_get(job, "priority") or 2), 1)
    if i == 0:
        return 1 + burst % 3
    if i == 1:
        return 1 + (4 - min(pri, 3))
    return 1 + burst % 2


def snapshot(jobs: list, available: list[int] | None = None) -> dict:
    init = _normalize_available(available if available is not None else [4, 5, 3])
    avail = list(init)
    n = len(jobs)
    m = len(RESOURCES)
    maximum = [[_claim(j, i) for i in range(m)] for j in jobs]
    alloc = []
    for j in range(n):
        row = []
        for i in range(m):
            take = min(maximum[j][i] // 2, avail[i])
            avail[i] -= take
            row.append(take)
        alloc.append(row)
    need = [[maximum[j][i] - alloc[j][i] for i in range(m)] for j in range(n)]
    names = [str(_get(j, "name") or _get(j, "id") or f"j{k}") for k, j in enumerate(jobs)]
    safe, seq = safety(avail, need, alloc)
    seq_names = [names[i] for i in seq]
    return {
        "resources": list(RESOURCES),
        "available_init": init,
        "available": avail,
        "maximum": maximum,
        "allocation": alloc,
        "need": need,
        "names": names,
        "safe": safe,
        "sequence": seq,
        "sequence_names": seq_names,
    }


def _pad_rows(rows: list[list[int]], width: int) -> list[list[int]]:
    """把每行裁/补到 width 个，避免调用方传进来的矩阵宽度和 work 不一致。"""
    out = []
    for row in rows:
        cells = list(row)[:width]
        cells += [0] * (width - len(cells))
        out.append([int(c) if isinstance(c, (int, float)) else 0 for c in cells])
    return out


def safety(work: list[int], need: list[list[int]], alloc: list[list[int]]) -> tuple[bool, list[int]]:
    w = [max(int(x), 0) for x in work]
    width = len(w)
    need = _pad_rows(need, width)
    alloc = _pad_rows(alloc, width)
    n = len(need)
    finish = [False] * n
    seq: list[int] = []
    progressed = True
    while progressed:
        progressed = False
        for i in range(n):
            if finish[i]:
                continue
            if all(need[i][k] <= w[k] for k in range(width)):
                w = [w[k] + alloc[i][k] for k in range(width)]
                finish[i] = True
                seq.append(i)
                progressed = True
    return all(finish), seq
