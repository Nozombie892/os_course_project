"""无界面跑一遍内核，确认分流产线和原料产率可启动。"""

from brickforge.kernel import production_stats, shop_throughput, simulate
from brickforge.persist import default_layout


def test_fanout_and_raw():
    layout = default_layout()
    stats = production_stats(layout["shops"], layout)
    assert stats["belts"] == 2
    assert stats["throughput"] > 0
    result = simulate(layout, persist_state=False)
    assert result["ok"]
    prod = result["production"]
    assert prod["raw_name"] == "冷轧钢板"
    assert prod["raw_in"] == 120
    assert prod["yield_rate"] >= 0
    assert result["yield"] > 0
    lab = result["lab"]
    assert set(lab["compare"]) >= {"fcfs", "sjf", "priority", "rr", "srtf", "mlfq"}
    assert lab["sync"]["belts"]
    assert lab["warehouse"]["log"]
    assert lab["shipping"]["path"]
    assert lab["pcb"]
    assert lab["syscalls"]
    assert lab["banker"]
    assert lab["paging"]["faults"] >= 0
    floor = result["floor"]
    broken = floor.get("new_broken") or []
    if broken:
        ev = next(e for e in floor["events"] if e.get("kind") == "break")
        assert ev.get("id") in broken
        assert ev.get("shop_id")
    return result


def test_open_line_required():
    layout = default_layout()
    layout["feeds"] = []
    layout["ships"] = []
    layout["links"] = []
    stats = production_stats(layout["shops"], layout)
    assert stats["throughput"] == 0
    cut = {
        "id": "s9",
        "machines": [{"id": "m9", "type": "station", "name": "孤岛", "workers": 1, "ppm": 20}],
        "wires": [],
    }
    out, extra = shop_throughput(cut)
    assert out == 0
    assert extra["isolated"] == 1


if __name__ == "__main__":
    test_open_line_required()
    result = test_fanout_and_raw()
    print("yield", result["yield"])
    print("revenue", result["revenue"])
    print("ipc", result["ipc"])
    print(result["notes"])
