"""把积木布局交给操作系统原语跑一遍，产出产量和收益。"""

from __future__ import annotations

import time
from multiprocessing import Process, Queue, Semaphore

from brickforge import persist, scheduler, shipping, warehouse, sync, banker, memory, monitor
from brickforge import ipc, signals
from brickforge.scheduler import Job
from brickforge.workers import drain, shop_worker


RAW_KIND = {
    "steel": {"name": "冷轧钢板", "unit": "千克", "consume": 1.2},
    "alu": {"name": "铝卷", "unit": "千克", "consume": 0.8},
    "plastic": {"name": "工程塑料粒", "unit": "千克", "consume": 0.6},
    "parts": {"name": "标准紧固件", "unit": "件", "consume": 1.0},
}
SHOP_COST = {"press": 360, "assemble": 340, "store": 160}
MACHINE_COST = {"station": 90, "arm": 80}
WORKER_GAIN = [0.12, 1.0, 1.55, 1.85]
WAGE = 90
SHIFT_SECONDS = 840.0
STD_PPM = 20.0
JAM_RATIO = 1.3


def machine_kind(machine: dict) -> float:
    return 1.1 if machine.get("type") == "arm" else 1.0


def machine_ppm(machine: dict) -> float:
    if machine.get("ppm") not in (None, ""):
        try:
            return min(max(float(machine.get("ppm")), 1.0), 120.0)
        except (TypeError, ValueError):
            return STD_PPM
    if machine.get("cycle") not in (None, ""):
        try:
            cycle = max(float(machine.get("cycle")), 0.5)
            return min(max(60.0 / cycle, 1.0), 120.0)
        except (TypeError, ValueError):
            return STD_PPM
    burst = max(float(machine.get("burst") or 3), 0.5)
    try:
        eff = float(machine.get("efficiency") or 0)
    except (TypeError, ValueError):
        eff = 0
    if eff > 0:
        cycle = burst / (min(max(eff, 10.0), 120.0) / 100.0)
        return min(max(60.0 / max(cycle, 0.5), 1.0), 120.0)
    return min(max(60.0 / burst, 1.0), 120.0)


def machine_cycle(machine: dict) -> float:
    return 60.0 / machine_ppm(machine)


def machine_capacity(machine: dict) -> float:
    if machine.get("broken"):
        return 0.0
    staff = min(max(int(machine.get("workers") or 0), 0), 3)
    return machine_ppm(machine) * (SHIFT_SECONDS / 60.0) * WORKER_GAIN[staff] * machine_kind(machine)


def machine_efficiency(machine: dict) -> float:
    staff = min(max(int(machine.get("workers") or 0), 0), 3)
    actual = machine_ppm(machine) * WORKER_GAIN[staff] * machine_kind(machine)
    ideal = STD_PPM * WORKER_GAIN[2]
    if actual <= 0:
        return 0.0
    return min(actual / ideal, 1.5)


INLET_ID = "__inlet__"
OUTLET_ID = "__outlet__"


def _pull_limit(produced: dict, succ: dict, pred: dict, sink_id: str, limit: float) -> dict:
    ids = set(succ) | set(pred) | {sink_id}
    wait = {i: len(succ.get(i) or []) for i in ids}
    actual = {i: 0.0 for i in ids}
    actual[sink_id] = min(produced.get(sink_id, 0.0), float(limit))
    queue = [i for i in ids if wait[i] == 0]
    seen: set[str] = set()
    while queue:
        v = queue.pop(0)
        if v in seen:
            continue
        seen.add(v)
        preds = pred.get(v) or []
        contrib = [produced.get(p, 0.0) / max(len(succ.get(p) or []), 1) for p in preds]
        total_in = sum(contrib)
        want = actual.get(v, 0.0)
        for i, p in enumerate(preds):
            actual[p] = actual.get(p, 0.0) + (want * contrib[i] / total_in if total_in > 0 else 0.0)
            wait[p] = wait.get(p, 1) - 1
            if wait[p] <= 0:
                queue.append(p)
    for key, val in produced.items():
        actual.setdefault(key, val)
    return actual


def shop_throughput(shop: dict) -> tuple[float, dict]:
    """车间产量：物料必须从原料台进、从出货台出，才算做出去。"""
    machines = {m["id"]: m for m in (shop.get("machines") or []) if m.get("id")}
    succ: dict[str, list[str]] = {mid: [] for mid in machines}
    pred: dict[str, list[str]] = {mid: [] for mid in machines}
    succ[INLET_ID] = []
    pred[OUTLET_ID] = []
    belts = 0
    workers = 0
    for mid, machine in machines.items():
        workers += min(max(int(machine.get("workers") or 0), 0), 3)
    for wire in shop.get("wires") or []:
        src, dst = wire.get("from"), wire.get("to")
        if not src or not dst or src == dst:
            continue
        if src in machines and dst in machines:
            succ[src].append(dst)
            pred[dst].append(src)
            belts += 1
        elif src == INLET_ID and dst in machines:
            succ[INLET_ID].append(dst)
            pred[dst].append(INLET_ID)
        elif src in machines and dst == OUTLET_ID:
            succ[src].append(OUTLET_ID)
            pred[OUTLET_ID].append(src)
    cap = {mid: machine_capacity(machine) for mid, machine in machines.items()}
    produced = {INLET_ID: 1e12}
    indeg = {mid: len(pred[mid]) for mid in machines}
    indeg[OUTLET_ID] = len(pred[OUTLET_ID])
    queue = [INLET_ID] + [mid for mid in machines if indeg[mid] == 0]
    seen: set[str] = set()
    while queue:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        if u == INLET_ID:
            produced[u] = 1e12
        elif u == OUTLET_ID:
            incoming = 0.0
            for p in pred[u]:
                incoming += produced.get(p, 0.0) / max(len(succ.get(p) or []), 1)
            produced[u] = incoming
        elif not pred.get(u):
            produced[u] = 0.0
        else:
            incoming = 0.0
            for p in pred[u]:
                incoming += produced.get(p, 0.0) / max(len(succ.get(p) or []), 1)
            produced[u] = min(incoming, cap.get(u, 0.0))
        for v in succ.get(u) or []:
            if v not in indeg:
                continue
            indeg[v] -= 1
            if indeg[v] <= 0:
                queue.append(v)
    isolated = 0
    jam_name = None
    jam_score = 0.0
    theoretical = 0.0
    for mid, machine in machines.items():
        if not pred.get(mid) and not succ.get(mid):
            isolated += 1
        rate = cap.get(mid, 0.0)
        push = 0.0
        for p in pred.get(mid) or []:
            if p == INLET_ID:
                continue
            push += produced.get(p, 0.0) / max(len(succ.get(p) or []), 1)
        if rate > 0 and push / rate > JAM_RATIO and push / rate > jam_score:
            jam_score = push / rate
            jam_name = machine.get("name") or mid
        if produced.get(mid, 0.0) > 0:
            theoretical += machine_ppm(machine) * (SHIFT_SECONDS / 60.0) * WORKER_GAIN[2] * machine_kind(machine)
    out = produced.get(OUTLET_ID, 0.0)
    path_caps = [cap[mid] for mid in machines if produced.get(mid, 0.0) > 0 and cap.get(mid, 0.0) > 0]
    if path_caps:
        out = min(out, min(path_caps))
    produced = _pull_limit(produced, succ, pred, OUTLET_ID, out)
    return produced.get(OUTLET_ID, 0.0), {
        "workers": workers,
        "belts": belts,
        "isolated": isolated,
        "slow_name": jam_name,
        "slow_rate": jam_score,
        "theoretical": theoretical,
        "machine_n": len(machines),
    }


def factory_flow(layout: dict, shop_cap: dict[str, float]) -> tuple[float, str | None]:
    """厂区产量：进料区 → 车间（可串多间）→ 出货区。断线的车间不算成品。"""
    raw_id, pack_id = "__raw__", "__pack__"
    shops = layout.get("shops") or []
    ids = [s.get("id") for s in shops if s.get("id")]
    succ: dict[str, list[str]] = {sid: [] for sid in ids}
    pred: dict[str, list[str]] = {sid: [] for sid in ids}
    succ[raw_id] = []
    pred[pack_id] = []
    for feed in layout.get("feeds") or []:
        dst = feed.get("to")
        if dst in pred:
            succ[raw_id].append(dst)
            pred[dst].append(raw_id)
    for link in layout.get("links") or []:
        src, dst = link.get("from"), link.get("to")
        if src in succ and dst in pred:
            succ[src].append(dst)
            pred[dst].append(src)
    for ship in layout.get("ships") or []:
        src = ship.get("from")
        if src in succ:
            succ[src].append(pack_id)
            pred[pack_id].append(src)
    produced = {raw_id: 1e12}
    indeg = {sid: len(pred[sid]) for sid in ids}
    indeg[pack_id] = len(pred[pack_id])
    queue = [raw_id] + [sid for sid in ids if indeg[sid] == 0]
    seen: set[str] = set()
    jam_shop = None
    jam_score = 0.0
    while queue:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        if u == raw_id:
            produced[u] = 1e12
        elif u == pack_id:
            incoming = 0.0
            for p in pred[u]:
                incoming += produced.get(p, 0.0) / max(len(succ.get(p) or []), 1)
            produced[u] = incoming
        elif not pred.get(u):
            produced[u] = 0.0
        else:
            incoming = 0.0
            push = 0.0
            for p in pred[u]:
                share = produced.get(p, 0.0) / max(len(succ.get(p) or []), 1)
                incoming += share
                if p != raw_id:
                    push += share
            cap_u = float(shop_cap.get(u) or 0.0)
            produced[u] = min(incoming, cap_u)
            if cap_u > 0 and push / cap_u > JAM_RATIO and push / cap_u > jam_score:
                jam_score = push / cap_u
                jam_shop = u
        for v in succ.get(u) or []:
            if v not in indeg:
                continue
            indeg[v] -= 1
            if indeg[v] <= 0:
                queue.append(v)
    produced = _pull_limit(produced, succ, pred, pack_id, produced.get(pack_id, 0.0))
    return produced.get(pack_id, 0.0), jam_shop


def production_stats(shops: list[dict], layout: dict | None = None) -> dict:
    total_out = 0.0
    theoretical = 0.0
    workers = 0
    belts = 0
    isolated = 0
    bottleneck = None
    jam_score = 0.0
    machine_n = 0
    shop_cap: dict[str, float] = {}
    shop_map = {str(s.get("id")): s for s in shops if s.get("id")}

    for shop in shops:
        out, extra = shop_throughput(shop)
        if shop.get("id"):
            shop_cap[str(shop["id"])] = out
        total_out += out
        theoretical += extra["theoretical"]
        workers += extra["workers"]
        belts += extra["belts"]
        isolated += extra["isolated"]
        machine_n += extra["machine_n"]
        if extra["slow_name"] and extra["slow_rate"] > jam_score:
            jam_score = extra["slow_rate"]
            bottleneck = extra["slow_name"]

    if layout is not None:
        total_out, jam_shop = factory_flow(layout, shop_cap)
        if not bottleneck and jam_shop:
            shop = shop_map.get(str(jam_shop))
            bottleneck = (shop or {}).get("name") or jam_shop

    efficiency = 0.0 if theoretical <= 0 else min(total_out / theoretical, 1.2)
    return {
        "efficiency": round(efficiency * 100, 1),
        "throughput": round(total_out, 1),
        "workers": workers,
        "belts": belts,
        "isolated": isolated,
        "bottleneck": bottleneck,
        "machine_n": machine_n,
    }


def count_links(shops: list[dict]) -> int:
    n = _pair_count(shops, 120, 86)
    for shop in shops:
        ids = {m.get("id") for m in (shop.get("machines") or [])}
        for wire in shop.get("wires") or []:
            src, dst = wire.get("from"), wire.get("to")
            if src in ids and dst in ids and src != dst:
                n += 1
    return n


def _pair_count(items: list[dict], w: int, h: int) -> int:
    n = 0
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            ax2, ay2 = a["x"] + w, a["y"] + h
            bx2, by2 = b["x"] + w, b["y"] + h
            overlap_y = min(ay2, by2) - max(a["y"], b["y"])
            overlap_x = min(ax2, bx2) - max(a["x"], b["x"])
            if overlap_y > 20 and (abs(ax2 - b["x"]) <= 4 or abs(bx2 - a["x"]) <= 4):
                n += 1
            if overlap_x > 20 and (abs(ay2 - b["y"]) <= 4 or abs(by2 - a["y"]) <= 4):
                n += 1
    return n


def pick_break(shops: list[dict], shift_no: int) -> tuple[list[str], list[dict]]:
    if shift_no % 2 == 0:
        return [], []
    cands = []
    for shop in shops:
        for machine in shop.get("machines") or []:
            staff = min(max(int(machine.get("workers") or 0), 0), 3)
            if staff > 0 and not machine.get("broken") and machine.get("id"):
                cands.append((staff, machine_ppm(machine), machine, shop))
    if not cands:
        return [], []
    cands.sort(key=lambda row: (row[0], row[1]))
    machine = cands[0][2]
    shop = cands[0][3]
    name = machine.get("name") or machine["id"]
    shop_name = shop.get("name") or shop.get("id") or "车间"
    return [machine["id"]], [{
        "t": 340,
        "kind": "break",
        "id": machine["id"],
        "name": name,
        "shop_id": shop.get("id"),
        "shop": shop_name,
        "text": f"「{shop_name}」的「{name}」过热停机",
    }]


def quality_rate(workers: int, manned: int) -> float:
    if manned <= 0:
        return 0.16
    avg = workers / manned
    return round(max(0.03, 0.14 - 0.035 * min(avg, 3.0)), 3)


def bay_items(layout: dict, key: str, fallback: dict) -> list[dict]:
    bay = layout.get(key)
    if not isinstance(bay, dict):
        bay = {}
        layout[key] = bay
    items = bay.get("items")
    if not isinstance(items, list) or not items:
        items = [dict(fallback)]
        bay["items"] = items
    cleaned = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        cap = max(float(item.get("cap") or fallback.get("cap") or 3000), 1)
        qty = max(float(item.get("qty") or 0), 0)
        cost_raw = item.get("cost", fallback.get("cost", 2))
        price_raw = item.get("price", fallback.get("price", 18))
        try:
            cost = max(float(cost_raw), 0)
        except (TypeError, ValueError):
            cost = 2.0
        try:
            price = max(float(price_raw), 0)
        except (TypeError, ValueError):
            price = 18.0
        cleaned.append({
            "id": str(item.get("id") or f"{key[0]}{i+1}"),
            "name": str(item.get("name") or fallback.get("name") or "货物").strip() or fallback.get("name") or "货物",
            "qty": round(qty, 1),
            "cap": cap,
            "cost": cost,
            "price": price,
        })
    if not cleaned:
        cleaned = [dict(fallback)]
    bay["items"] = cleaned
    return cleaned


def fed_items(layout: dict, inbound: list[dict]) -> list[dict]:
    feeds = layout.get("feeds") or []
    ids = {str(f.get("from")) for f in feeds if isinstance(f, dict) and f.get("from") and f.get("to")}
    if not ids:
        return inbound
    picked = [item for item in inbound if str(item.get("id")) in ids]
    return picked or inbound


def shipped_items(layout: dict, outbound: list[dict]) -> list[dict]:
    ships = layout.get("ships") or []
    ids = {str(s.get("to")) for s in ships if isinstance(s, dict) and s.get("from") and s.get("to")}
    if not ids:
        return outbound
    picked = [item for item in outbound if str(item.get("id")) in ids]
    return picked or outbound


def stock_tone(qty: float, cap: float) -> dict:
    if qty <= 0:
        return {"key": "empty", "text": "空仓"}
    ratio = qty / max(cap, 1)
    if ratio < 0.2:
        return {"key": "low", "text": "告急"}
    if ratio >= 1:
        return {"key": "ok", "text": "满仓"}
    return {"key": "ok", "text": "正常"}


def pile_tone(qty: float, cap: float) -> dict:
    if qty <= 0:
        return {"key": "empty", "text": "空台"}
    ratio = qty / max(cap, 1)
    if ratio >= 1:
        return {"key": "jam", "text": "堆积"}
    if ratio >= 0.7:
        return {"key": "warn", "text": "将满"}
    return {"key": "ok", "text": "畅通"}


def jobs_from(layout: dict) -> list[Job]:
    raw = layout.get("jobs") or []
    shops = layout.get("shops") or []
    shop_ids = [s["id"] for s in shops] or [""]
    out = []
    for i, item in enumerate(raw):
        out.append(
            Job(
                id=str(item.get("id") or f"j{i}"),
                name=item.get("name") or f"工单{i+1}",
                burst=int(item.get("burst") or 3),
                priority=int(item.get("priority") or 2),
                dest=int(item.get("dest") or 0),
                shop_id=shop_ids[i % len(shop_ids)],
            )
        )
    return out


def simulate(layout: dict) -> dict:
    persist.save_layout(layout)
    shops = layout.get("shops") or []
    jobs = jobs_from(layout)
    algo = layout.get("scheduler") or "priority"
    quantum = max(int(layout.get("quantum") or 2), 1)
    timeline = scheduler.run(jobs, algo, quantum)
    sched_m = scheduler.metrics(timeline)
    compare = scheduler.compare(jobs, quantum)
    buffers = sync.line_buffers(shops, layout.get("buffer_cap") or 6)
    bank = banker.snapshot(jobs)
    refs = [int(job.dest or i + 1) for i, job in enumerate(jobs)]
    if refs:
        refs = refs + [refs[0]] + list(reversed(refs))
    paging = memory.replace(
        refs,
        int(layout.get("page_frames") or 4),
        layout.get("page_algo") or "lru",
    )

    slots = max(1, len(layout.get("shelves") or [8, 5, 12, 3]))
    material = Semaphore(slots)
    report_q: Queue = Queue()
    procs: list[Process] = []
    grouped: dict[str, list[dict]] = {s["id"]: [] for s in shops}
    for row in timeline:
        grouped.setdefault(row["shop_id"], []).append(row)

    # 共享内存看板：主进程 / 车间进程 / 守护进程读写同一块物理内存
    board = ipc.open_board(reset=True)
    # 管道：每条"车间 → 车间"连线一条单向管道，上游投递批次、下游开工前收取
    pipes = ipc.make_pipes(layout.get("links") or [])
    index_of = {s.get("id"): i for i, s in enumerate(shops) if s.get("id")}

    t0 = time.time()
    for shop in shops:
        sid = shop.get("id")
        ctx = {
            "index": index_of.get(sid, 0),
            "slots": slots,
            "senders": pipes["senders"].get(sid, []),
            "receivers": pipes["receivers"].get(sid, []),
        }
        proc = Process(
            target=shop_worker,
            args=(shop, grouped.get(sid, []), material, report_q, 0.03, ctx),
            name=f"shop-{sid}",
        )
        procs.append(proc)
        proc.start()
    ipc.close_pipes(pipes)          # 父进程关掉自己那份端点，避免句柄泄漏
    reports = drain(report_q, expected=len(procs), timeout=10)
    for proc in procs:
        proc.join(timeout=2)
        if proc.is_alive():
            proc.terminate()
    elapsed = round(time.time() - t0, 3)
    board_state = board.snapshot()
    board.close()

    finished = sum(len(r.get("finished") or []) for r in reports)
    blocked = sum(int(r.get("blocked") or 0) for r in reports)
    tool_waits = sum(int(r.get("tool_waits") or 0) for r in reports)
    delivered = sum(int(r.get("delivered") or 0) for r in reports)
    piped_in = sum(len(r.get("received") or []) for r in reports)
    live_pids = [r.get("pid") for r in reports]

    pack = warehouse.pack_jobs(
        [job.burst for job in jobs],
        list(layout.get("shelves") or [8, 5, 12, 3]),
        layout.get("warehouse_policy") or "best",
    )
    route = shipping.run(
        [job.dest for job in jobs],
        layout.get("ship_algo") or "sstf",
    )

    machine_n = sum(len(s.get("machines") or []) for s in shops)
    links = count_links(shops)
    stats = production_stats(shops, layout)
    shift_no = int(layout.get("shift_no") or 0) + 1
    layout["shift_no"] = shift_no
    new_broken, ev_break = pick_break(shops, shift_no)
    manned = sum(1 for s in shops for m in (s.get("machines") or []) if int(m.get("workers") or 0) > 0)
    scrap_pct = quality_rate(int(stats["workers"]), manned)
    break_pen = 1 - min(len(new_broken) * 0.14, 0.28)
    crash = 0.03 if layout.get("daemon", True) else 0.18
    wait_penalty = 1 - min(sched_m["avg_wait"] / 30.0, 0.35)
    block_penalty = 1 - min(blocked * 0.05, 0.25)
    reject_penalty = 1 - min(pack["rejected"] * 0.04, 0.2)
    ship_penalty = 1 - min(route["distance"] / 80.0, 0.15)
    page_penalty = 1 - min(paging["faults"] * 0.02, 0.2)
    bank_penalty = 1.0 if bank.get("safe") else 0.72
    flow = stats["throughput"]
    capacity = int(max(flow, 0))
    spec = RAW_KIND.get(layout.get("raw_kind") or "steel") or RAW_KIND["steel"]
    consume = float(spec["consume"])
    inbound = bay_items(layout, "inbound", {"id": "in1", "name": spec["name"], "qty": float(layout.get("raw") or 0), "cap": 3000, "cost": 2})
    outbound = bay_items(layout, "outbound", {"id": "out1", "name": "积木成品", "qty": 0, "cap": 3000, "price": float(layout.get("price") or 18)})
    fed = fed_items(layout, inbound)
    raw_in = sum(float(item["qty"]) for item in fed)
    layout["raw"] = sum(float(item["qty"]) for item in inbound)
    max_from_raw = int(raw_in / consume) if consume else 0
    attempted = min(capacity, max_from_raw)
    scrap = int(attempted * scrap_pct)
    yield_day = max(attempted - scrap, 0)
    used = yield_day * consume
    remain = used
    raw_bill = 0.0
    for item in fed:
        take = min(float(item["qty"]), remain)
        raw_bill += take * float(item.get("cost") or 2)
        item["qty"] = round(float(item["qty"]) - take, 1)
        remain -= take
    leftover = sum(float(item["qty"]) for item in inbound)
    overflow = float(yield_day)
    docks = shipped_items(layout, outbound) or outbound
    if docks:
        docks[0]["qty"] = round(float(docks[0].get("qty") or 0) + overflow, 1)
        overflow = 0.0
    piled = any(
        float(i.get("cap") or 1) > 0 and float(i.get("qty") or 0) / float(i.get("cap") or 1) >= 1
        for i in outbound
    )
    yield_rate = 0.0 if raw_in <= 0 else round(used / raw_in * 100, 1)
    stats["raw_in"] = raw_in
    stats["raw_used"] = round(used, 1)
    stats["raw_left"] = round(leftover, 1)
    stats["capacity"] = capacity
    stats["yield_rate"] = yield_rate
    stats["raw_name"] = fed[0]["name"] if fed else spec["name"]
    stats["raw_unit"] = spec["unit"]
    stats["pieces"] = yield_day
    stats["scrap"] = scrap
    stats["attempted"] = attempted
    wages = int(stats["workers"]) * WAGE
    price = int(layout.get("price") or 18)
    if docks:
        price = int(float(docks[0].get("price") or price))
    elif outbound:
        price = int(float(outbound[0].get("price") or price))
    layout["price"] = price
    cost = sum(SHOP_COST.get(s.get("type"), 900) for s in shops)
    cost += sum(MACHINE_COST.get(m.get("type"), 300) for s in shops for m in (s.get("machines") or []))
    cost += wages
    cost += int(round(raw_bill))
    if layout.get("daemon", True):
        cost += 80
    cost += pack["rejected"] * 40 + int(route["distance"] * 1.2) + scrap * max(price // 3, 2)
    if new_broken:
        cost += 60
    revenue = yield_day * price - cost
    shelves = list(layout.get("shelves") or [8, 5, 12, 3])
    fill = min(1.0, yield_day / max(sum(shelves) * 8, 1))
    held = False
    in_fill = leftover / max(sum(float(i["cap"]) for i in inbound), 1)
    out_fill = sum(float(i["qty"]) for i in outbound) / max(sum(float(i["cap"]) for i in outbound), 1)
    events = list(ev_break)
    events.sort(key=lambda e: int(e.get("t") or 0))
    for shop in shops:
        for machine in shop.get("machines") or []:
            if machine.get("id") in new_broken:
                machine["broken"] = True
    persist.save_layout(layout)

    already = [m.get("id") for s in shops for m in (s.get("machines") or []) if m.get("broken")]
    floor = {
        "events": events,
        "broken": already,
        "new_broken": new_broken,
        "scrap": scrap,
        "scrap_rate": scrap_pct,
        "wages": wages,
        "inbound": leftover,
        "outbound": yield_day,
        "warehouse_fill": round(out_fill, 2),
        "held": held,
        "inbound_items": inbound,
        "outbound_items": outbound,
        "in_status": stock_tone(leftover, sum(float(i["cap"]) for i in inbound)),
        "out_status": pile_tone(sum(float(i["qty"]) for i in outbound), sum(float(i["cap"]) for i in outbound)),
        "overflow": round(overflow, 1),
        "bottleneck": stats.get("bottleneck"),
        "shift_no": shift_no,
    }

    result = {
        "ok": True,
        "yield": max(yield_day, 0),
        "revenue": revenue,
        "cost": cost,
        "wages": wages,
        "scrap": scrap,
        "price": price,
        "elapsed_sec": elapsed,
        "scheduler": algo,
        "timeline": timeline,
        "sched": sched_m,
        "warehouse": pack,
        "shipping": route,
        "floor": floor,
        "ipc": {
            "shop_processes": live_pids,
            "machine_threads": machine_n,
            "queue_jobs": len(jobs),
            "semaphore_slots": slots,
            "blocked": blocked,
            "links": links,
            "pipes": ipc.describe_pipes(pipes),
            "pipe_delivered": delivered,
            "pipe_received": piped_in,
            "thread_jobs_done": finished,
            "tool_waits": tool_waits,
            "shm": board_state,
        },
        "production": stats,
        "lab": {
            "gantt": timeline,
            "compare": compare,
            "sync": {
                **buffers,
                "blocked": blocked,
                "tool_waits": tool_waits,
                "semaphore_slots": slots,
                "shop_pids": live_pids,
                "threads": machine_n,
                "pipes": ipc.describe_pipes(pipes),
                "pipe_delivered": delivered,
                "pipe_received": piped_in,
                "shm": board_state,
                "note": sync.describe(blocked, slots, buffers),
            },
            "signals": signals.capabilities(),
            "warehouse": pack,
            "shipping": route,
            "banker": bank,
            "paging": paging,
            "pcb": monitor.pcb(shops, reports, timeline),
            "syscalls": monitor.syscalls(
                shops, algo, slots, blocked, bank, paging, sched_m.get("switches") or 0,
                pipes=ipc.describe_pipes(pipes),
                board=board_state,
                sigcaps=signals.capabilities(),
                tool_waits=tool_waits,
            ),
        },
        "notes": _notes(pack, blocked, shops, machine_n, stats, floor),
    }
    persist.save_status({**persist.load_status(), "last_sim": result})
    return result


def _notes(pack, blocked, shops, machine_n, stats, floor) -> str:
    bits = [f"本轮良品 {stats.get('pieces', 0)} 件，次品 {stats.get('scrap', 0)}。"]
    bits.append(f"工资 {floor.get('wages', 0)}。")
    if (stats.get("throughput") or 0) <= 0:
        bits.append("流水线没接通，物料到不了出货区。")
    if stats.get("bottleneck"):
        bits.append(f"卡线在「{stats['bottleneck']}」。")
    if floor.get("new_broken"):
        bits.append("有机器过热停机，点顶部故障条「去监控」可跳到那台机器。")
    if (stats.get("raw_left") or 0) <= 0:
        bits.append("进料区空了。")
    if not shops or machine_n == 0:
        bits.append("先拼车间、放机器、接传送带、上工人。")
    return "".join(bits)
