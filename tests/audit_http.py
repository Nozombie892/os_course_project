"""审计：HTTP 接口、畸形输入、静态文件安全、并发。

自己起一个真服务（python -m brickforge，占 8787），跑完收工。
运行：
    python -m tests.audit_http
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"
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


def call(path: str, method: str = "GET", body=None, raw: bytes | None = None, timeout: float = 60):
    data = raw if raw is not None else (
        json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    )
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            payload = res.read()
            ctype = res.headers.get("Content-Type", "")
            try:
                return res.status, payload.decode("utf-8"), ctype
            except UnicodeDecodeError:
                return res.status, payload, ctype
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers.get("Content-Type", "")
    except Exception as exc:  # 连接被掐断等
        return -1, repr(exc), ""


def jcall(path: str, method: str = "GET", body=None, **kw):
    status, text, ctype = call(path, method, body, **kw)
    try:
        return status, json.loads(text), ctype
    except (ValueError, TypeError):
        return status, text, ctype


def start_server() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-m", "brickforge"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.25)
        try:
            if jcall("/api/health", timeout=2)[0] == 200:
                return proc
        except Exception:
            continue
    proc.terminate()
    raise RuntimeError("服务没起来（8787 可能被占用）")


def stop_server(proc: subprocess.Popen) -> None:
    try:
        jcall("/api/run/stop", "POST", {}, timeout=10)
        jcall("/api/stop", "POST", {}, timeout=10)
    except Exception:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def audit_endpoints() -> None:
    print("\n=== 全部接口的基本契约 ===")
    for path, key in (("/api/layout", "shops"), ("/api/status", None),
                      ("/api/procs", "procs"), ("/api/board", "name"),
                      ("/api/run", "state"), ("/api/health", "ok")):
        status, body, ctype = jcall(path)
        ok = status == 200 and isinstance(body, dict) and "charset=utf-8" in ctype
        if key:
            ok = ok and key in body
        check(f"GET {path} -> 200 + JSON(utf-8)", ok, f"{status} {ctype} {str(body)[:80]}")

    for path in ("/api/nope", "/api/../README.md"):
        status, _b, _c = call(path)
        check(f"GET {path} -> 404", status == 404, str(status))

    status, _b, _c = call("/api/layout", "PUT", {})
    check("PUT /api/layout -> 405/501（不实现）", status in (405, 501), str(status))

    status, body, _c = jcall("/api/layout", "POST", {"scheduler": "rr"})
    check("POST /api/layout 接受对象并回存", status == 200 and body.get("scheduler") == "rr")

    status, body, _c = jcall("/api/reset", "POST", {})
    check("POST /api/reset 恢复默认工厂", status == 200 and len(body.get("shops") or []) == 2)

    status, body, _c = jcall("/api/simulate", "POST", body)
    check("POST /api/simulate 返回 ok", status == 200 and body.get("ok") is True)


def audit_static() -> None:
    print("\n=== 静态文件与路径穿越 ===")
    for path in ("/", "/index.html", "/css/app.css", "/js/app.js", "/js/bench.js", "/js/lab.js"):
        status, text, ctype = call(path)
        check(f"GET {path} -> 200", status == 200 and len(text) > 100, f"{status} {len(text)}B")

    for path in ("/../README.md", "/../../README.md", "/%2e%2e/%2e%2e/README.md",
                 "/..%2f..%2fREADME.md", "/../brickforge/kernel.py",
                 "/../data/factory.json", "/..%5c..%5cREADME.md"):
        status, text, _c = call(path)
        leaked = isinstance(text, str) and ("积木工坊" in text and "class=" not in text) or \
                 "import" in str(text)[:200] or "scheduler" in str(text)[:200]
        check(f"GET {path} 不泄露项目文件", status in (400, 403, 404) or not leaked,
              f"status={status} head={str(text)[:60]!r}")


def audit_bad_input() -> None:
    print("\n=== 畸形输入：不能 500、不能让服务挂掉 ===")
    bad_bodies = [
        ("数组", b"[1,2,3]"),
        ("字符串", b'"hello"'),
        ("数字", b"42"),
        ("坏 JSON", b"{oops"),
        ("空字节", b"\x00\x01\x02"),
        ("非 UTF-8", bytes([0xff, 0xfe, 0xfd])),
        ("深嵌套", b'{"a":' * 200 + b"1" + b"}" * 200),
    ]
    for label, raw in bad_bodies:
        for path in ("/api/simulate", "/api/layout", "/api/run"):
            status, text, _c = call(path, "POST", raw=raw, timeout=30)
            ok = status in (200, 400)
            check(f"POST {path} 传{label} -> {status}", ok, str(text)[:80])

    # 布局内部类型错乱（这些是 factory.json 手改最可能出的错）
    lay = jcall("/api/reset", "POST", {})[1]
    broken = {
        "shops 是字符串": {"shops": "abc"},
        "shops 里放数字": {"shops": [1, 2]},
        "shops 是对象": {"shops": {"a": 1}},
        "jobs 是对象": {"jobs": {"a": 1}},
        "jobs 里放字符串": {"jobs": ["x"]},
        "machines 是字符串": {"shops": [{"id": "s1", "machines": "abc", "wires": []}], "jobs": []},
        "wires 是字符串": {"shops": [{"id": "s1", "machines": [], "wires": "abc"}], "jobs": []},
        "inbound.items 是字符串": {"inbound": {"items": "abc"}},
        "outbound 缺 items": {"outbound": {}},
        "shelves 是字符串": {"shelves": "8,5"},
        "shelves 里是字符串": {"shelves": ["a"]},
        "quantum 是字符串": {"quantum": "abc"},
        "bank_available 是数字": {"bank_available": 5},
        "feeds 里缺字段": {"feeds": [{}]},
        "ships 里缺字段": {"ships": [{"from": None}]},
        "jobs.burst 是字符串": {"jobs": [{"burst": "abc"}]},
        "jobs.burst 是很大的数": {"jobs": [{"burst": 10 ** 7}]},
    }
    for label, patch in broken.items():
        body = dict(lay)
        body.update(patch)
        status, payload, _c = jcall("/api/simulate", "POST", body, timeout=60)
        healthy = jcall("/api/health")[0] == 200
        ok = status == 200 and healthy and isinstance(payload, dict) and payload.get("ok") is True
        check(f"simulate[{label}] 不 500 且服务存活", ok,
              f"status={status} alive={healthy} err={str(payload)[:100]}")
        jcall("/api/reset", "POST", {})


def audit_concurrency() -> None:
    print("\n=== 并发 ===")
    lay = jcall("/api/reset", "POST", {})[1]
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def hit(idx: int) -> None:
        status, text, _c = call("/api/simulate", "POST", lay, timeout=120)
        with lock:
            results.append((status, text[:80]))

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(5)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = all(status == 200 for status, _ in results)
    check(f"5 个并发 /api/simulate 全部 200（耗时 {time.time()-started:.1f}s）", ok, str(results))

    # 并发起班：只应有一个班次
    race: list[dict] = []

    def start_run() -> None:
        status, body, _c = jcall("/api/run", "POST", {"shop_seconds": 20}, timeout=60)
        with lock:
            race.append(body if isinstance(body, dict) else {"status": status})

    threads = [threading.Thread(target=start_run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    run_ids = {r.get("run_id") for r in race if r.get("ok") and not r.get("reused")}
    check("3 个并发 /api/run 只开出一个新班次", len(run_ids) <= 1, str([r.get("run_id") for r in race]))
    jcall("/api/run/stop", "POST", {})

    # 并发读写布局
    codes = []
    lock = threading.Lock()

    def mix() -> None:
        for _ in range(10):
            status = call("/api/layout")[0] if len(codes) % 2 else call("/api/reset", "POST", {})[0]
            with lock:
                codes.append(status)

    threads = [threading.Thread(target=mix) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("6 线程混读写布局全部 200", all(c == 200 for c in codes), str(set(codes)))


def audit_run_lifecycle() -> None:
    print("\n=== 长跑班次：起班 / 硬杀 / 拉起 / 收工 ===")
    jcall("/api/reset", "POST", {})
    jcall("/api/run/stop", "POST", {})
    status, started, _c = jcall("/api/run", "POST", {"shop_seconds": 25, "tick": 0.2})
    check("起班成功", status == 200 and started.get("ok") is True, str(started)[:120])
    time.sleep(2)
    view = jcall("/api/run")[1]
    guarded = [p for p in view.get("procs") or [] if p.get("state") == "running"]
    check("登记表里能看到运行中的车间进程", len(guarded) >= 1, str(view.get("procs")))

    old = int(guarded[0]["pid"])
    shop = guarded[0]["shop_id"]
    status, killed, _c = jcall("/api/kill", "POST", {"shop_id": shop})
    check("硬杀返回 ok", status == 200 and killed.get("ok") is True, str(killed)[:120])
    revived = None
    for _ in range(12):
        time.sleep(1)
        rows = [p for p in jcall("/api/run")[1].get("procs") or []
                if p.get("shop_id") == shop and p.get("pid") != old and p.get("state") == "running"]
        if rows:
            revived = rows[0]
            break
    check("守护进程在 12 秒内用新 pid 拉起", revived is not None, "没有拉起")
    if revived:
        check("拉起次数 +1", int(revived.get("revives") or 0) >= 1, str(revived))

    status, stopped, _c = jcall("/api/run/stop", "POST", {})
    time.sleep(1.5)
    procs = jcall("/api/procs")[1].get("procs") or []
    check("收工后登记表清空", procs == [], str(procs))
    check("收工后 /api/run 无运行中进程",
          not [p for p in jcall("/api/run")[1].get("procs") or [] if p.get("state") == "running"])

    status, body, _c = jcall("/api/kill", "POST", {})
    check("空班次下 /api/kill 返回 ok:false 而不是 500",
          status == 200 and body.get("ok") is False, str(body)[:100])


def audit_unicode() -> None:
    print("\n=== 中文 / 表情 / 特殊字符往返 ===")
    lay = jcall("/api/reset", "POST", {})[1]
    lay["shops"][0]["name"] = "加工车间·测试<script>alert(1)</script>"
    lay["jobs"][0]["name"] = "急单🚀 <b>加急</b>"
    status, body, _c = jcall("/api/simulate", "POST", lay)
    check("含标签与表情的布局能正常仿真", status == 200 and body.get("ok") is True,
          f"status={status}")
    status, saved, _c = jcall("/api/layout")
    same = saved["shops"][0]["name"] == lay["shops"][0]["name"]
    check("名称原样落盘/回读（不丢字节、不转义）", same,
          f"{saved['shops'][0]['name']!r}")
    jcall("/api/reset", "POST", {})


def main() -> int:
    if call("/api/health", timeout=1)[0] == 200:
        print("8787 上已经有服务在跑，先停掉它再跑本审计。")
        return 2
    proc = start_server()
    try:
        audit_endpoints()
        audit_static()
        audit_bad_input()
        audit_concurrency()
        audit_run_lifecycle()
        audit_unicode()
    finally:
        stop_server(proc)
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
    print("HTTP 审计全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
