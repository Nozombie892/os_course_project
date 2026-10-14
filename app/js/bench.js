/** 实验台：把后端算出的操作系统算法结果画到界面上。
 *
 * 原项目里 lab.* 这些字段只存在于 API 返回的 JSON 中，界面从不展示。
 * 本文件补上这一环：点「运行仿真」调用 POST /api/simulate，
 * 后端真的跑一遍调度 / 银行家 / 页面置换 / 仓储 / 管道 / 共享内存，
 * 结果分五个标签页渲染出来。
 *
 * 不修改 app.js：只读取它维护的 window.layout。
 */

(function () {
  "use strict";

  const state = { result: null, tab: "sched", busy: false, status: null };

  const PARAMS = [
    { key: "scheduler", label: "处理机调度", type: "select",
      options: [["fcfs", "FCFS 先来先服务"], ["sjf", "SJF 短作业优先"], ["priority", "动态优先级"],
                ["rr", "RR 时间片轮转"], ["srtf", "SRTF 最短剩余"], ["mlfq", "MLFQ 多级反馈"]] },
    { key: "quantum", label: "时间片", type: "number", min: 1 },
    { key: "ship_algo", label: "出货路径（磁盘调度）", type: "select",
      options: [["sstf", "SSTF 最短寻道"], ["fcfs", "FCFS"], ["scan", "SCAN 电梯"], ["cscan", "C-SCAN"]] },
    { key: "warehouse_policy", label: "货架分配（内存分区）", type: "select",
      options: [["best", "最佳适应"], ["first", "首次适应"], ["worst", "最坏适应"]] },
    { key: "shelves", label: "货架容量（逗号分隔，个数=信号量槽）", type: "list" },
    { key: "buffer_cap", label: "传送带缓冲容量", type: "number", min: 1 },
    { key: "page_frames", label: "页框数", type: "number", min: 1 },
    { key: "page_algo", label: "页面置换", type: "select", options: [["lru", "LRU"], ["fifo", "FIFO"]] },
    { key: "daemon", label: "启用厂务守护进程", type: "check" },
  ];

  // ---------------------------------------------------------------- 工具 ---
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
    ));
  }

  function $(id) { return document.getElementById(id); }

  async function api(path, method, body) {
    const opt = { method: method || "GET", headers: {} };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    const res = await fetch(path, opt);
    if (!res.ok) throw new Error("http " + res.status);
    return res.json();
  }

  function table(headers, rows) {
    const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
    const body = rows.map((r) => "<tr>" + r.map((c) =>
      `<td${typeof c === "number" ? ' class="num"' : ""}>${esc(c)}</td>`).join("") + "</tr>").join("");
    return `<table class="bench-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function kv(rows) {
    return `<dl class="bench-kv">${rows.map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
  }

  // ------------------------------------------------------------ 五个页签 ---
  function tabSched(r) {
    const s = r.sched || {};
    const metrics = kv([
      ["当前算法", (r.lab && r.lab.compare && r.lab.compare[r.scheduler] || {}).name || r.scheduler],
      ["平均等待", s.avg_wait],
      ["完成时刻", s.makespan],
      ["上下文切换", s.switches],
      ["已完成工单", s.finished],
    ]);
    const cmp = (r.lab && r.lab.compare) || {};
    const rows = Object.keys(cmp).map((k) => [
      cmp[k].name, cmp[k].metrics.avg_wait, cmp[k].metrics.makespan, cmp[k].metrics.switches,
      k === r.scheduler ? "← 当前" : "",
    ]);
    const timeline = (r.lab && r.lab.gantt) || r.timeline || [];
    const span = Math.max(1, ...timeline.map((t) => t.end || 1));
    const bars = timeline.map((t) => {
      const slices = t.slices || [[t.start, t.end]];
      const parts = slices.map(([a, b]) =>
        `<i style="left:${(a / span * 100).toFixed(2)}%;width:${Math.max((b - a) / span * 100, 1).toFixed(2)}%"></i>`
      ).join("");
      return `<div class="gt-row"><span class="gt-name">${esc(t.name)}</span>
        <span class="gt-track">${parts}</span>
        <span class="gt-num">${t.start}~${t.end}</span></div>`;
    }).join("");
    return `<div class="bench-grid">
      <div><h4>调度指标</h4>${metrics}</div>
      <div><h4>六种算法对照（同一批工单）</h4>
        ${table(["算法", "平均等待", "完成时刻", "上下文切换", ""], rows)}
        <p class="muted">换算法时"平均等待"和"上下文切换"必须变化，否则说明调度没生效。</p></div>
    </div>
    <h4>甘特图（同色多段=被抢占/轮转）</h4><div class="gantt">${bars || "<p class='muted'>无</p>"}</div>`;
  }

  function tabSync(r) {
    const ipc = r.ipc || {};
    const sync = (r.lab && r.lab.sync) || {};
    const caps = (r.lab && r.lab.signals) || {};
    const shm = ipc.shm || {};
    const belts = (sync.belts || []).map((b) => [
      b.shop, b.from + " → " + b.to, b.capacity, b.fill, b.waiters ? "有" : "无",
    ]);
    return `<div class="bench-grid">
      <div><h4>同步原语</h4>
        ${kv([
          ["库位信号量槽（=货架数）", ipc.semaphore_slots],
          ["库位阻塞次数", ipc.blocked],
          ["共享工装等待（线程互斥）", ipc.tool_waits],
          ["车间进程 / 机器线程", `${(ipc.shop_processes || []).length} / ${ipc.machine_threads}`],
          ["消息队列工单数", ipc.queue_jobs],
        ])}
        <h4>信号</h4>
        ${kv([
          ["平台", caps.platform],
          ["真实内核信号", caps.real_signals ? "是" : "否（降级为软信号）"],
          ["可靠信号（实时信号可排队）", caps.reliable ? "支持" : "不支持"],
          ["急停信号", caps.stop],
          ["换班信号", caps.reload],
        ])}
      </div>
      <div><h4>进程间通信</h4>
        ${kv([
          ["消息队列（工单）", "multiprocessing.Queue"],
          ["信号量（库位）", ipc.semaphore_slots + " 个槽"],
          ["管道（车间串联）", (ipc.pipes && ipc.pipes.count) || 0],
          ["管道投递 / 接收", `${ipc.pipe_delivered} / ${ipc.pipe_received}`],
          ["管道明细", ((ipc.pipes || {}).pairs || []).join("、") || "无车间连线"],
          ["共享内存看板", shm.degraded ? "不可用（已降级）" : shm.name],
          ["看板·守护进程 pid", shm.daemon_pid],
          ["看板·守护心跳计数", shm.daemon_tick],
          ["看板·累计拉起", shm.restarts],
          ["看板·各车间完工槽", JSON.stringify(shm.shop_slots || {})],
        ])}
        <h4>传送带有界缓冲</h4>
        ${table(["车间", "方向", "容量", "水位", "有人等"], belts)}
      </div>
    </div>`;
  }

  function tabMem(r) {
    const wh = (r.lab && r.lab.warehouse) || {};
    const pg = (r.lab && r.lab.paging) || {};
    const bank = (r.lab && r.lab.banker) || {};
    const whRows = (wh.log || []).map((l, i) => [
      i + 1, l.size, l.ok ? "放入" : "拒绝", l.index < 0 ? "—" : "货架" + l.index, l.fragment,
    ]);
    const pgRows = (pg.log || []).map((l, i) => [
      i + 1, l.page, l.hit ? "命中" : "缺页", (l.frames || []).join(","),
    ]);
    const bankRows = (bank.names || []).map((n, i) => [
      n, (bank.maximum[i] || []).join("/"), (bank.allocation[i] || []).join("/"),
      (bank.need[i] || []).join("/"),
    ]);
    return `<div class="bench-grid">
      <div><h4>货架分配 · ${esc(wh.policy)}</h4>
        ${kv([
          ["初始货架", (wh.origin || []).join(", ")],
          ["分配后货架", (wh.shelves || []).join(", ")],
          ["放入 / 拒绝", `${wh.placed} / ${wh.rejected}`],
          ["碎片率", wh.fragment_rate],
        ])}
        ${table(["序", "大小", "结果", "货架", "剩余碎片"], whRows)}
      </div>
      <div><h4>页面置换 · ${esc(pg.algo)} · ${esc(pg.frames)} 页框</h4>
        ${kv([
          ["引用串", (pg.refs || []).join(", ")],
          ["缺页 / 命中", `${pg.faults} / ${pg.hits}`],
          ["缺页率", pg.fault_rate],
        ])}
        ${table(["步", "页号", "结果", "页框"], pgRows)}
        <h4>银行家算法 · ${bank.safe ? "安全" : "不安全"}</h4>
        <p class="muted">资源：${esc((bank.resources || []).join(" / "))}；
          安全序列：${esc((bank.sequence_names || []).join(" → ") || "无（存在死锁风险）")}</p>
        ${table(["工单", "最大需求", "已分配", "还需"], bankRows)}
      </div>
    </div>`;
  }

  function tabDev(r) {
    const sh = (r.lab && r.lab.shipping) || {};
    const prod = r.production || {};
    const path = (sh.path || []);
    const span = Math.max(1, ...path);
    const dots = path.map((p) => `<i style="left:${(p / span * 100).toFixed(2)}%" title="${p}"></i>`).join("");
    return `<div class="bench-grid">
      <div><h4>出货路径 · ${esc(sh.algo)}</h4>
        ${kv([
          ["路径", path.join(" → ")],
          ["总寻道长度", sh.distance],
          ["成品单价", r.price],
          ["本轮良品 / 次品", `${r.yield} / ${r.scrap}`],
          ["收入 / 成本 / 利润", `${r.yield * r.price} / ${r.cost} / ${r.revenue}`],
        ])}
        <div class="track">${dots}</div>
        <p class="muted">目的地当作磁道号，起点为 0。总寻道长度会按 1.2 元/格计入成本，
          所以换出货算法能改变利润。</p>
      </div>
      <div><h4>产能与瓶颈</h4>
        ${kv([
          ["拓扑产能（件/班）", prod.capacity],
          ["投料 / 已用 / 剩余", `${prod.raw_in} / ${prod.raw_used} / ${prod.raw_left}`],
          ["原料", `${prod.raw_name || ""} ${prod.raw_unit || ""}`],
          ["良品率", prod.yield_rate + "%"],
          ["设备效率", prod.efficiency + "%"],
          ["卡线工位", prod.bottleneck || "无"],
          ["工人 / 传送带 / 孤立机器", `${prod.workers} / ${prod.belts} / ${prod.isolated}`],
        ])}
      </div>
    </div>`;
  }

  function tabProc(r) {
    const ipc = r.ipc || {};
    const pcb = (r.lab && r.lab.pcb) || [];
    const procs = (state.status && state.status.guarded) || [];
    const syscalls = (r.lab && r.lab.syscalls) || [];
    const pcbRows = pcb.map((p) => [p.pid, p.name, p.kind, p.state, p.threads, p.cpu]);
    return `<div class="bench-grid">
      <div><h4>PCB 表（车间=进程，机器=线程，工单=作业）</h4>
        ${table(["pid", "名称", "类型", "状态", "线程数", "cpu"], pcbRows)}
        <h4>被守护的进程</h4>
        ${procs.length
          ? table(["pid", "车间", "状态", "心跳(秒)", "已拉起"],
              procs.map((p) => [p.pid, p.shop_id, p.state, p.age, p.revives]))
          : `<p class="muted">登记表为空：当前没有正在运行的车间进程。
              守护进程每 2 秒巡视一次，发现"登记还在、进程没了"就按登记信息重新拉起。</p>`}
      </div>
      <div><h4>系统调用轨迹</h4>
        <pre class="bench-sys">${esc(syscalls.join("\n"))}</pre>
      </div>
    </div>`;
  }

  const TABS = { sched: tabSched, sync: tabSync, mem: tabMem, dev: tabDev, proc: tabProc };

  // ---------------------------------------------------------------- 渲染 ---
  function render() {
    const box = $("bench-body");
    if (!box) return;
    if (!state.result) return;
    box.innerHTML = TABS[state.tab](state.result);
  }

  function setHint(text, tone) {
    const hint = $("bench-hint");
    if (!hint) return;
    hint.textContent = text;
    hint.className = "bench-hint" + (tone ? " " + tone : "");
  }

  async function refreshStatus() {
    try {
      state.status = await api("/api/status");
    } catch (err) {
      state.status = null;
    }
  }

  async function runSim() {
    if (state.busy) return;
    state.busy = true;
    setHint("正在跑后端算法…");
    try {
      const layout = window.layout;
      const result = await api("/api/simulate", "POST", layout || undefined);
      state.result = result;
      await refreshStatus();
      render();
      setHint(`良品 ${result.yield} · 利润 ${result.revenue} · 耗时 ${result.elapsed_sec}s`, "ok");
    } catch (err) {
      setHint("仿真失败：" + err.message, "bad");
    } finally {
      state.busy = false;
    }
  }

  // ------------------------------------------------------------ 参数面板 ---
  function paramHTML() {
    const layout = window.layout || {};
    return PARAMS.map((p) => {
      const value = layout[p.key];
      if (p.type === "select") {
        const opts = p.options.map(([v, t]) =>
          `<option value="${esc(v)}"${v === value ? " selected" : ""}>${esc(t)}</option>`).join("");
        return `<label class="bp"><span>${esc(p.label)}</span><select data-key="${esc(p.key)}">${opts}</select></label>`;
      }
      if (p.type === "check") {
        return `<label class="bp bp-check"><input type="checkbox" data-key="${esc(p.key)}"${value ? " checked" : ""} /><span>${esc(p.label)}</span></label>`;
      }
      if (p.type === "list") {
        return `<label class="bp"><span>${esc(p.label)}</span><input type="text" data-key="${esc(p.key)}" data-list="1" value="${esc((value || []).join(","))}" /></label>`;
      }
      return `<label class="bp"><span>${esc(p.label)}</span><input type="number" data-key="${esc(p.key)}" min="${p.min || 1}" value="${esc(value)}" /></label>`;
    }).join("") + `<div class="bp-actions"><button type="button" id="bench-apply">应用并重跑</button></div>`;
  }

  async function applyParams() {
    const layout = window.layout;
    if (!layout) { setHint("布局还没加载完", "bad"); return; }
    document.querySelectorAll("#bench-param-box [data-key]").forEach((input) => {
      const key = input.dataset.key;
      if (input.type === "checkbox") {
        layout[key] = input.checked;
      } else if (input.dataset.list) {
        layout[key] = input.value.split(/[,，\s]+/).map((x) => parseInt(x, 10)).filter((n) => n > 0);
      } else if (input.type === "number") {
        layout[key] = Math.max(parseInt(input.value, 10) || 1, 1);
      } else {
        layout[key] = input.value;
      }
    });
    try {
      await api("/api/layout", "POST", layout);
      setHint("参数已应用", "ok");
    } catch (err) {
      setHint("参数保存失败：" + err.message, "bad");
    }
    await runSim();
  }

  // ---------------------------------------------------------------- 绑定 ---
  function bind() {
    const tabs = $("bench-tabs");
    if (tabs) {
      tabs.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-tab]");
        if (!btn) return;
        state.tab = btn.dataset.tab;
        tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
        render();
      });
    }
    const run = $("bench-run");
    if (run) run.addEventListener("click", runSim);
    const params = $("bench-params");
    const box = $("bench-param-box");
    if (params && box) {
      params.addEventListener("click", async () => {
        box.hidden = !box.hidden;
        if (!box.hidden) {
          if (!window.layout) { try { window.layout = await api("/api/layout"); } catch (err) { /* 忽略 */ } }
          box.innerHTML = paramHTML();
          const apply = $("bench-apply");
          if (apply) apply.addEventListener("click", applyParams);
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
