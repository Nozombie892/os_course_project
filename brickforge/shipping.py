"""丁：出货路径。目的地像磁道号，SSTF / FCFS。和仓储、守护进程同一人。"""

from __future__ import annotations


def fcfs_route(dests: list[int], start: int = 0) -> dict:
    path = [start, *dests]
    return _score(path)


def sstf_route(dests: list[int], start: int = 0) -> dict:
    left = list(dests)
    cur = start
    path = [start]
    while left:
        nxt = min(left, key=lambda d: abs(d - cur))
        path.append(nxt)
        left.remove(nxt)
        cur = nxt
    return _score(path)


def scan_route(dests: list[int], start: int = 0) -> dict:
    left = sorted(dests)
    path = [start]
    right = [d for d in left if d >= start]
    lefts = [d for d in left if d < start]
    path.extend(right)
    path.extend(reversed(lefts))
    return _score(path)


def cscan_route(dests: list[int], start: int = 0) -> dict:
    left = sorted(dests)
    path = [start]
    right = [d for d in left if d >= start]
    lefts = [d for d in left if d < start]
    path.extend(right)
    path.extend(lefts)
    result = _score(path)
    if lefts:
        # C-SCAN 扫到最右端后要跳回 0 再继续向上扫。这一段寻道是真实发生的，
        # 但 0 不是目的地，所以不进 path；距离上补回来（多走 2×最小左向目标）。
        # 起点为 0 时 lefts 必为空，走不到这里，SCAN 与 C-SCAN 因此完全等价。
        result["jump_to_zero"] = 2 * lefts[0]
        result["distance"] += result["jump_to_zero"]
    return result


def run(dests: list[int], algo: str = "sstf", start: int = 0) -> dict:
    if not dests:
        return {"path": [start], "distance": 0, "algo": algo}
    table = {
        "sstf": sstf_route,
        "fcfs": fcfs_route,
        "scan": scan_route,
        "cscan": cscan_route,
    }
    fn = table.get(algo, sstf_route)
    result = fn(dests, start)
    result["algo"] = algo
    return result


def _score(path: list[int]) -> dict:
    dist = sum(abs(path[i] - path[i - 1]) for i in range(1, len(path)))
    return {"path": path, "distance": dist}
