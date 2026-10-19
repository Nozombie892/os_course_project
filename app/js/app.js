const GRID = 20;
const SHOP = { w: 120, h: 126 };
const MACHINE = { w: 136, h: 148 };
const GAIN = [0.12, 1, 1.55, 1.85];
const SHIFT_SECONDS = 840;
const STD_PPM = 20;
const WAGE = 90;
const JAM_RATIO = 1.3;
const STARVE_RATIO = 0.3;
const MATERIALS = {
  steel: { name: "冷轧钢板", unit: "千克", consume: 1.2 },
  alu: { name: "铝卷", unit: "千克", consume: 0.8 },
  plastic: { name: "工程塑料粒", unit: "千克", consume: 0.6 },
  parts: { name: "标准紧固件", unit: "件", consume: 1 }
};
const SHOPS = {
  press: { name: "加工车间", role: "加工类" },
  assemble: { name: "组装车间", role: "组装类" },
  store: { name: "仓储车间", role: "仓储类" }
};
const MACHINES = {
  station: { name: "工位机", role: "人工工位" },
  arm: { name: "机械臂", role: "自动工位" }
};
const INLET_ID = "__inlet__";
const OUTLET_ID = "__outlet__";
const BAY_CAP = 3000;
const RAW_COST = 2;

let layout = null;
let view = { mode: "factory" };
let drag = null;
let pending = null;
let wireDrag = null;
let selected = null;
let uid = 100;
const DRAG_START = 8;
const MAGNET = 32;
let lastResult = null;
let replayFocus = null;
let monitorFocus = null;
let flowCache = null;
let runState = null;
let runRaf = 0;

const stage = document.getElementById("stage");
const palette = document.getElementById("palette");
const backBtn = document.getElementById("back");
const crumb = document.getElementById("crumb");

function snap(v) { return Math.round(v / GRID) * GRID; }
function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

// 车间/机器/货物名字都是用户输入的，拼进 innerHTML 前必须转义，
// 否则名字里写个 <img onerror=...> 就会在页面上执行。
function escHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function currentShop() { return layout.shops.find((s) => s.id === view.id); }
function nid(prefix) { uid += 1; return prefix + uid + "-" + Date.now().toString(36); }

async function api(path, method, body) {
  const opt = { method: method || "GET", headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  if (!res.ok) throw new Error("http " + res.status);
  return res.json();
}

function magnet(x, y, w, h, others) {
  let nx = snap(x);
  let ny = snap(y);
  others.forEach((o) => {
    if (Math.abs((x + w) - o.x) <= MAGNET || Math.abs((nx + w) - o.x) <= MAGNET) {
      nx = o.x - w;
      if (Math.abs(y - o.y) <= MAGNET) ny = o.y;
    }
    if (Math.abs(x - (o.x + o.w)) <= MAGNET || Math.abs(nx - (o.x + o.w)) <= MAGNET) {
      nx = o.x + o.w;
      if (Math.abs(y - o.y) <= MAGNET) ny = o.y;
    }
    if (Math.abs((y + h) - o.y) <= MAGNET || Math.abs((ny + h) - o.y) <= MAGNET) {
      ny = o.y - h;
      if (Math.abs(x - o.x) <= MAGNET) nx = o.x;
    }
    if (Math.abs(y - (o.y + o.h)) <= MAGNET || Math.abs(ny - (o.y + o.h)) <= MAGNET) {
      ny = o.y + o.h;
      if (Math.abs(x - o.x) <= MAGNET) nx = o.x;
    }
  });
  return { x: nx, y: ny };
}

function pairLinks(items, w, h) {
  const links = [];
  items.forEach((a, i) => {
    items.slice(i + 1).forEach((b) => {
      const ax2 = a.x + w, ay2 = a.y + h;
      const bx2 = b.x + w, by2 = b.y + h;
      const oy = Math.min(ay2, by2) - Math.max(a.y, b.y);
      const ox = Math.min(ax2, bx2) - Math.max(a.x, b.x);
      if (oy > 20 && (Math.abs(ax2 - b.x) <= 4 || Math.abs(bx2 - a.x) <= 4)) {
        const left = a.x < b.x ? a : b;
        const right = a.x < b.x ? b : a;
        const top = Math.max(left.y, right.y);
        const bot = Math.min(left.y + h, right.y + h);
        const cy = (top + bot) / 2;
        links.push({
          dir: "h",
          x: left.x + w - 4,
          y: cy - 4,
          w: Math.max(8, right.x - (left.x + w) + 8),
          h: 8
        });
      }
      if (ox > 20 && (Math.abs(ay2 - b.y) <= 4 || Math.abs(by2 - a.y) <= 4)) {
        const top = a.y < b.y ? a : b;
        const bot = a.y < b.y ? b : a;
        const left = Math.max(top.x, bot.x);
        const right = Math.min(top.x + w, bot.x + w);
        const cx = (left + right) / 2;
        links.push({
          dir: "v",
          x: cx - 4,
          y: top.y + h - 4,
          w: 8,
          h: Math.max(8, bot.y - (top.y + h) + 8)
        });
      }
    });
  });
  return links;
}

function countCrew() {
  return layout.shops.reduce((n, s) => n + (s.machines || []).reduce((m, x) => m + Math.min(x.workers || 0, 3), 0), 0);
}

function countBelts() {
  let n = validFeeds().length + validShips().length + shopLinks().length;
  layout.shops.forEach((shop) => {
    n += validWires(shop).length;
  });
  return n;
}

function machinePpm(m) {
  if (m.ppm != null && m.ppm !== "") {
    return clamp(Number(m.ppm) || STD_PPM, 1, 120);
  }
  if (m.cycle != null && m.cycle !== "") {
    return clamp(60 / Math.max(Number(m.cycle) || 3, 0.5), 1, 120);
  }
  const burst = Math.max(Number(m.burst) || 3, 0.5);
  const eff = Number(m.efficiency);
  if (eff > 0) {
    const cycle = burst / (Math.min(Math.max(eff, 10), 120) / 100);
    return clamp(60 / Math.max(cycle, 0.5), 1, 120);
  }
  return clamp(60 / burst, 1, 120);
}

function machineCycle(m) {
  return 60 / machinePpm(m);
}

function machineStaff(m) {
  return Math.min(Math.max(m.workers || 0, 0), 3);
}

function machineKind(m) {
  return m.type === "arm" ? 1.1 : 1;
}

function shopCrew(shop) {
  return (shop.machines || []).reduce((n, m) => n + Math.min(m.workers || 0, 3), 0);
}

function linePush(pred, succ, produced, id, skip) {
  let n = 0;
  (pred[id] || []).forEach((p) => {
    if (p === skip) return;
    n += (produced[p] || 0) / Math.max((succ[p] || []).length, 1);
  });
  return n;
}

function machineStatus(shop, m, graph) {
  if (m.broken) return { key: "down", text: "故障" };
  if (!machineStaff(m)) return { key: "idle", text: "空岗" };
  const wires = validWires(shop);
  const linked = wires.some((w) => w.from === m.id || w.to === m.id);
  if (!linked) return { key: "cut", text: "未接线" };
  if (!graph) return { key: "run", text: "生产" };
  const cap = graph.cap[m.id] || 0;
  const out = graph.produced[m.id] || 0;
  const preds = graph.pred[m.id] || [];
  const hasInlet = preds.includes(INLET_ID);
  const fromMachines = preds.some((p) => p !== INLET_ID);
  const push = linePush(graph.pred, graph.succ, graph.produced, m.id, INLET_ID);
  if (out <= 0) return { key: "wait", text: "没进料" };
  if (cap > 0 && push > cap * JAM_RATIO) return { key: "slow", text: "卡线" };
  if (!hasInlet && fromMachines && cap > 0 && push < cap * STARVE_RATIO) return { key: "wait", text: "等料" };
  return { key: "run", text: "生产" };
}

function machineRate(m, ignoreBroken) {
  if (m.broken && !ignoreBroken) return 0;
  return machinePpm(m) * (SHIFT_SECONDS / 60) * GAIN[machineStaff(m)] * machineKind(m);
}

function isMachineBroken(id) {
  if (!id || id === INLET_ID || id === OUTLET_ID) return false;
  for (const shop of (layout.shops || [])) {
    const machine = (shop.machines || []).find((m) => m.id === id);
    if (machine) return !!machine.broken;
  }
  return false;
}

function clearAllBroken() {
  (layout.shops || []).forEach((shop) => {
    (shop.machines || []).forEach((m) => { m.broken = false; });
  });
}

function pullLimit(produced, succ, pred, sinkId, limit) {
  const actual = {};
  const wait = {};
  const ids = new Set([sinkId, ...Object.keys(succ || {}), ...Object.keys(pred || {})]);
  ids.forEach((id) => {
    wait[id] = (succ[id] || []).length;
    actual[id] = 0;
  });
  const capTo = limit == null ? (produced[sinkId] || 0) : limit;
  actual[sinkId] = Math.min(produced[sinkId] || 0, capTo);
  const q = [];
  ids.forEach((id) => { if (!wait[id]) q.push(id); });
  const seen = new Set();
  while (q.length) {
    const v = q.shift();
    if (seen.has(v)) continue;
    seen.add(v);
    const preds = pred[v] || [];
    const contrib = preds.map((p) => (produced[p] || 0) / Math.max((succ[p] || []).length, 1));
    const totalIn = contrib.reduce((a, b) => a + b, 0);
    const want = actual[v] || 0;
    preds.forEach((p, i) => {
      actual[p] = (actual[p] || 0) + (totalIn > 0 ? want * contrib[i] / totalIn : 0);
      wait[p] -= 1;
      if (wait[p] <= 0) q.push(p);
    });
  }
  Object.keys(produced).forEach((id) => {
    if (actual[id] == null) actual[id] = produced[id];
  });
  return actual;
}

function shopGraph(shop, pullTo, ignoreBroken) {
  const machines = {};
  (shop.machines || []).forEach((m) => { machines[m.id] = m; });
  const succ = {};
  const pred = {};
  Object.keys(machines).forEach((id) => { succ[id] = []; pred[id] = []; });
  succ[INLET_ID] = [];
  pred[OUTLET_ID] = [];
  validWires(shop).forEach((w) => {
    if (w.from === INLET_ID && pred[w.to]) {
      succ[INLET_ID].push(w.to);
      pred[w.to].push(INLET_ID);
      return;
    }
    if (succ[w.from] && w.to === OUTLET_ID) {
      succ[w.from].push(OUTLET_ID);
      pred[OUTLET_ID].push(w.from);
      return;
    }
    if (succ[w.from] && pred[w.to] && w.to !== OUTLET_ID) {
      succ[w.from].push(w.to);
      pred[w.to].push(w.from);
    }
  });
  const cap = {};
  Object.keys(machines).forEach((id) => { cap[id] = machineRate(machines[id], ignoreBroken); });
  const produced = {};
  produced[INLET_ID] = 1e12;
  const indeg = {};
  Object.keys(machines).forEach((id) => { indeg[id] = pred[id].length; });
  indeg[OUTLET_ID] = (pred[OUTLET_ID] || []).length;
  const queue = [INLET_ID];
  Object.keys(machines).forEach((id) => {
    if (!indeg[id]) queue.push(id);
  });
  const seen = new Set();
  while (queue.length) {
    const u = queue.shift();
    if (seen.has(u)) continue;
    seen.add(u);
    if (u === INLET_ID) {
      produced[u] = 1e12;
    } else if (u === OUTLET_ID) {
      produced[u] = linePush(pred, succ, produced, u, null);
    } else if (!(pred[u] || []).length) {
      produced[u] = 0;
    } else {
      const incoming = linePush(pred, succ, produced, u, null);
      produced[u] = Math.min(incoming, cap[u] || 0);
    }
    (succ[u] || []).forEach((v) => {
      if (indeg[v] == null) return;
      indeg[v] -= 1;
      if (indeg[v] <= 0) queue.push(v);
    });
  }
  let isolated = 0;
  let slowCap = Infinity;
  Object.keys(machines).forEach((id) => {
    if (!(pred[id] || []).length && !(succ[id] || []).length) isolated += 1;
    if (produced[id] == null) produced[id] = 0;
    if ((produced[id] || 0) > 0 && (cap[id] || 0) > 0) slowCap = Math.min(slowCap, cap[id]);
  });
  let out = produced[OUTLET_ID] || 0;
  if (slowCap < Infinity) out = Math.min(out, slowCap);
  if (pullTo != null) out = Math.min(out, pullTo);
  if (ignoreBroken) {
    produced[OUTLET_ID] = out;
    Object.keys(machines).forEach((id) => {
      if (machines[id].broken) produced[id] = 0;
    });
  } else {
    const limited = pullLimit(produced, succ, pred, OUTLET_ID, out);
    Object.keys(limited).forEach((id) => { produced[id] = limited[id]; });
  }
  let bottleneck = "";
  let jamScore = 0;
  Object.keys(machines).forEach((id) => {
    const c = cap[id] || 0;
    const push = linePush(pred, succ, produced, id, INLET_ID);
    if (c > 0 && push / c > JAM_RATIO && push / c > jamScore) {
      jamScore = push / c;
      bottleneck = machines[id].name || id;
    }
  });
  return { machines, succ, pred, cap, produced, isolated, bottleneck, output: produced[OUTLET_ID] || 0 };
}

function shopGraphView(shop) {
  const fg = currentFactory();
  const budget = shop && shop.id && fg.produced ? fg.produced[shop.id] : null;
  return shopGraph(shop, budget, true);
}

function wireFlow(graph, from) {
  if (from === INLET_ID) {
    const outs = graph.succ[from] || [];
    if (!outs.length) return 0;
    return outs.reduce((n, id) => n + (graph.produced[id] || 0), 0) / outs.length;
  }
  return (graph.produced[from] || 0) / Math.max((graph.succ[from] || []).length, 1);
}

function minutes() {
  return SHIFT_SECONDS / 60;
}

function fmtPpm(n) {
  const x = Math.max(Number(n) || 0, 0);
  if (x < 0.05) return "0";
  if (x < 10) return String(Math.round(x * 10) / 10);
  return String(Math.round(x));
}

function edgePpm(graph, from, to) {
  const min = minutes();
  if (!graph || min <= 0) return 0;
  if (isMachineBroken(from) || isMachineBroken(to)) return 0;
  if (from === INLET_ID) return (graph.produced[to] || 0) / min;
  const split = Math.max((graph.succ[from] || []).length, 1);
  return (graph.produced[from] || 0) / split / min;
}

function factoryEdgePpm(graph, fromKind, from, toKind, to) {
  const min = minutes();
  if (!graph || min <= 0) return 0;
  if (fromKind === "crate") return (graph.produced[to] || 0) / min;
  const src = from;
  const split = Math.max((graph.succ[src] || []).length, 1);
  return (graph.produced[src] || 0) / split / min;
}

function bezierMid(x1, y1, x2, y2, t) {
  t = t == null ? 0.5 : t;
  const dx = Math.max(48, Math.abs(x2 - x1) * 0.5);
  const u = 1 - t;
  const c1x = x1 + dx;
  const c2x = x2 - dx;
  return {
    x: u * u * u * x1 + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t * t * t * x2,
    y: u * u * u * y1 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y2
  };
}

function placeTag(svg, x, y) {
  if (svg && svg.id === "floor-wires") {
    const floor = floorBox();
    const st = stage.getBoundingClientRect();
    x = clamp(x, st.left - floor.left + 30, st.right - floor.left - 30);
    y = clamp(y, st.top - floor.top + 14, st.bottom - floor.top - 14);
  }
  return { x, y };
}

function tagHost(svg) {
  if (svg && svg.id === "floor-wires") return document.querySelector(".floor") || stage;
  return stage;
}

function clearBeltLabels(host) {
  if (!host) return;
  host.querySelectorAll(":scope > .belt-label").forEach((el) => el.remove());
}

function addBeltTag(svg, x, y, ppm, fromId, toId) {
  const host = tagHost(svg);
  const el = document.createElement("div");
  el.className = "belt-label" + (ppm < 0.05 ? " idle" : "");
  el.textContent = fmtPpm(ppm) + " 件/分";
  el.style.left = Math.round(x) + "px";
  el.style.top = Math.round(y) + "px";
  if (fromId) el.dataset.from = fromId;
  if (toId) el.dataset.to = toId;
  host.appendChild(el);
}

function addBeltPkgs(svg, d, ppm) {
  if (ppm < 0.15) return;
  const tempo = beltTempo(ppm);
  const n = Math.max(1, Math.min(4, Math.round(ppm / 5)));
  for (let i = 0; i < n; i += 1) {
    const pkg = svgEl("circle");
    pkg.classList.add("belt-pkg");
    pkg.setAttribute("r", "4.5");
    const motion = svgEl("animateMotion");
    motion.setAttribute("dur", tempo.pkg + "s");
    motion.setAttribute("begin", ((-i * tempo.pkg) / n).toFixed(2) + "s");
    motion.setAttribute("repeatCount", "indefinite");
    motion.setAttribute("path", d);
    pkg.appendChild(motion);
    svg.appendChild(pkg);
  }
}

function currentFactory() {
  if (flowCache) return flowCache;
  const shopCap = {};
  (layout.shops || []).forEach((s) => { shopCap[s.id] = shopGraph(s, null, true).output || 0; });
  flowCache = factoryFlow(shopCap, true);
  return flowCache;
}

function factoryFlow(shopCap, loose) {
  const RAW = "__raw__";
  const PACK = "__pack__";
  const ids = (layout.shops || []).map((s) => s.id).filter(Boolean);
  const succ = {};
  const pred = {};
  ids.forEach((id) => { succ[id] = []; pred[id] = []; });
  succ[RAW] = [];
  pred[PACK] = [];
  validFeeds().forEach((f) => {
    if (!pred[f.to]) return;
    succ[RAW].push(f.to);
    pred[f.to].push(RAW);
  });
  shopLinks().forEach((l) => {
    if (!succ[l.from] || !pred[l.to]) return;
    succ[l.from].push(l.to);
    pred[l.to].push(l.from);
  });
  validShips().forEach((s) => {
    if (!succ[s.from]) return;
    succ[s.from].push(PACK);
    pred[PACK].push(s.from);
  });
  const produced = {};
  produced[RAW] = 1e12;
  const indeg = {};
  ids.forEach((id) => { indeg[id] = pred[id].length; });
  indeg[PACK] = pred[PACK].length;
  const queue = [RAW];
  ids.forEach((id) => {
    if (!indeg[id]) queue.push(id);
  });
  const seen = new Set();
  let jamId = "";
  let jamScore = 0;
  while (queue.length) {
    const u = queue.shift();
    if (seen.has(u)) continue;
    seen.add(u);
    if (u === RAW) {
      produced[u] = 1e12;
    } else if (u === PACK) {
      produced[u] = linePush(pred, succ, produced, u, null);
    } else if (!(pred[u] || []).length) {
      produced[u] = 0;
    } else {
      const incoming = linePush(pred, succ, produced, u, null);
      const cap = shopCap[u] || 0;
      produced[u] = Math.min(incoming, cap);
      const push = linePush(pred, succ, produced, u, RAW);
      if (cap > 0 && push / cap > JAM_RATIO && push / cap > jamScore) {
        jamScore = push / cap;
        jamId = u;
      }
    }
    (succ[u] || []).forEach((v) => {
      if (indeg[v] == null) return;
      indeg[v] -= 1;
      if (indeg[v] <= 0) queue.push(v);
    });
  }
  if (!loose) {
    const limited = pullLimit(produced, succ, pred, PACK, produced[PACK] || 0);
    Object.keys(limited).forEach((id) => { produced[id] = limited[id]; });
  }
  return { output: produced[PACK] || 0, jamId, produced, succ, pred };
}

function lineOpen() {
  const RAW = "__raw__";
  const PACK = "__pack__";
  const ids = new Set((layout.shops || []).map((s) => s.id).filter(Boolean));
  const succ = {};
  succ[RAW] = [];
  ids.forEach((id) => { succ[id] = []; });
  validFeeds().forEach((f) => {
    if (ids.has(f.to)) succ[RAW].push(f.to);
  });
  shopLinks().forEach((l) => {
    if (ids.has(l.from) && ids.has(l.to)) succ[l.from].push(l.to);
  });
  validShips().forEach((s) => {
    if (ids.has(s.from)) succ[s.from].push(PACK);
  });
  const seen = new Set();
  const queue = [RAW];
  while (queue.length) {
    const u = queue.shift();
    if (seen.has(u)) continue;
    seen.add(u);
    if (u === PACK) return true;
    (succ[u] || []).forEach((v) => queue.push(v));
  }
  return false;
}

function shopOpen(shop) {
  const succ = {};
  succ[INLET_ID] = [];
  (shop.machines || []).forEach((m) => { succ[m.id] = []; });
  validWires(shop).forEach((w) => {
    if (!succ[w.from]) return;
    if (w.to === OUTLET_ID || succ[w.to]) succ[w.from].push(w.to);
  });
  const seen = new Set();
  const queue = [INLET_ID];
  while (queue.length) {
    const u = queue.shift();
    if (seen.has(u)) continue;
    seen.add(u);
    if (u === OUTLET_ID) return true;
    (succ[u] || []).forEach((v) => queue.push(v));
  }
  return false;
}

function beltTempo(ppm) {
  const n = Math.max(Number(ppm) || 0, 0.4);
  return {
    slat: Math.min(2.8, Math.max(0.18, 6 / n)),
    pkg: Math.min(7, Math.max(0.45, 18 / n))
  };
}

function estimateLine() {
  let top = 0;
  let isolated = 0;
  let bottleneck = "";
  const shopCap = {};
  layout.shops.forEach((shop) => {
    const g = shopGraph(shop);
    shopCap[shop.id] = g.output || 0;
    isolated += g.isolated;
    if (g.bottleneck) bottleneck = g.bottleneck;
    Object.keys(g.machines).forEach((id) => {
      if ((g.produced[id] || 0) > 0) {
        top += SHIFT_SECONDS / machineCycle(g.machines[id]) * GAIN[2] * machineKind(g.machines[id]);
      }
    });
  });
  const flow = factoryFlow(shopCap);
  const throughput = flow.output || 0;
  if (!bottleneck && flow.jamId) {
    const jammed = layout.shops.find((s) => s.id === flow.jamId);
    bottleneck = (jammed && jammed.name) || flow.jamId;
  }
  const efficiency = top <= 0 ? 0 : Math.min(throughput / top, 1.2);
  const spec = MATERIALS[layout.raw_kind || "steel"] || MATERIALS.steel;
  const raw = Number(layout.raw || 0);
  const pieces = Math.min(Math.round(throughput), spec.consume ? Math.floor(raw / spec.consume) : 0);
  const used = pieces * spec.consume;
  const yieldRate = raw <= 0 ? 0 : used / raw;
  return {
    efficiency, isolated, bottleneck, throughput, pieces, yieldRate,
    rawLeft: Math.max(0, raw - used), rawUsed: used, rawIn: raw, spec
  };
}

function drawLinks(items, size) {
  pairLinks(items, size.w, size.h).forEach((lnk) => {
    const el = document.createElement("div");
    el.className = "link " + lnk.dir;
    el.style.left = lnk.x + "px";
    el.style.top = lnk.y + "px";
    el.style.width = lnk.w + "px";
    el.style.height = lnk.h + "px";
    stage.appendChild(el);
  });
}

function svgEl(tag) {
  return document.createElementNS("http://www.w3.org/2000/svg", tag);
}

function portPos(item, side, index, total) {
  const spread = total > 1 ? (index - (total - 1) / 2) * 16 : 0;
  return {
    x: side === "left" ? item.x : item.x + MACHINE.w,
    y: item.y + MACHINE.h / 2 + spread
  };
}

function bezier(x1, y1, x2, y2) {
  const dx = Math.max(48, Math.abs(x2 - x1) * 0.5);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

function validWires(shop) {
  const ids = new Set((shop.machines || []).map((m) => m.id));
  ids.add(INLET_ID);
  ids.add(OUTLET_ID);
  return (shop.wires || []).filter((w) => ids.has(w.from) && ids.has(w.to) && w.from !== w.to);
}

function addWire(shop, from, to) {
  if (from === OUTLET_ID || to === INLET_ID || from === to) return;
  if (from === INLET_ID && to === OUTLET_ID) return;
  shop.wires = validWires(shop);
  if (shop.wires.some((w) => w.from === from && w.to === to)) return;
  shop.wires.push({ from, to });
}

function hitPort(clientX, clientY) {
  const el = document.elementFromPoint(clientX, clientY);
  const port = el && el.closest ? el.closest(".port") : null;
  if (!port) return null;
  const side = port.classList.contains("left") ? "left" : "right";
  const crate = port.closest(".crate");
  if (crate) {
    const out = crate.closest(".bay.out");
    return { kind: out ? "outcrate" : "crate", id: crate.dataset.id, side };
  }
  const inlet = port.closest(".inlet");
  if (inlet) return { kind: "inlet", id: INLET_ID, side };
  const outlet = port.closest(".outlet");
  if (outlet) return { kind: "outlet", id: OUTLET_ID, side };
  const shop = port.closest(".piece.shop");
  if (shop) return { kind: "shop", id: shop.dataset.id, side };
  const piece = port.closest(".piece.machine");
  if (!piece) return null;
  return { kind: "machine", id: piece.dataset.id, side };
}

function ensureFeeds() {
  if (!layout.feeds || !Array.isArray(layout.feeds)) layout.feeds = [];
}

function validFeeds() {
  ensureBays();
  ensureFeeds();
  const crates = new Set((layout.inbound.items || []).map((it) => it.id));
  const shops = new Set((layout.shops || []).map((s) => s.id));
  layout.feeds = layout.feeds.filter((f) => crates.has(f.from) && shops.has(f.to) && f.from && f.to);
  return layout.feeds;
}

function addFeed(from, to) {
  layout.feeds = validFeeds().filter((f) => f.to !== to);
  if (!layout.feeds.some((f) => f.from === from && f.to === to)) {
    layout.feeds.push({ from, to });
  }
}

function ensureShips() {
  if (!layout.ships || !Array.isArray(layout.ships)) layout.ships = [];
}

function validShips() {
  ensureBays();
  ensureShips();
  const crates = new Set((layout.outbound.items || []).map((it) => it.id));
  const shops = new Set((layout.shops || []).map((s) => s.id));
  layout.ships = layout.ships.filter((s) => shops.has(s.from) && crates.has(s.to) && s.from && s.to);
  return layout.ships;
}

function addShip(from, to) {
  layout.ships = validShips().filter((s) => s.from !== from);
  if (!layout.ships.some((s) => s.from === from && s.to === to)) {
    layout.ships.push({ from, to });
  }
}

function ensureLinks() {
  if (!layout.links || !Array.isArray(layout.links)) layout.links = [];
}

function shopLinks() {
  ensureLinks();
  const ids = new Set((layout.shops || []).map((s) => s.id));
  layout.links = layout.links.filter((l) => ids.has(l.from) && ids.has(l.to) && l.from && l.to && l.from !== l.to);
  return layout.links;
}

function addLink(from, to) {
  if (from === to) return;
  layout.links = shopLinks();
  if (layout.links.some((l) => l.from === from && l.to === to)) return;
  layout.links.push({ from, to });
}

function shopTitle(id) {
  const shop = (layout.shops || []).find((s) => s.id === id);
  if (!shop) return "";
  return shop.name || (SHOPS[shop.type] && SHOPS[shop.type].name) || "车间";
}

function shopFeedName(shop) {
  const feed = validFeeds().find((f) => f.to === shop.id);
  if (feed) {
    const item = (layout.inbound.items || []).find((it) => it.id === feed.from);
    if (item && item.name) return item.name;
  }
  const link = shopLinks().find((l) => l.to === shop.id);
  return link ? "来自" + shopTitle(link.from) : "";
}

function shopShipName(shop) {
  const ship = validShips().find((s) => s.from === shop.id);
  if (ship) {
    const item = (layout.outbound.items || []).find((it) => it.id === ship.to);
    if (item && item.name) return item.name;
  }
  const link = shopLinks().find((l) => l.from === shop.id);
  return link ? "送往" + shopTitle(link.to) : "";
}

function floorBox() {
  return document.querySelector(".floor").getBoundingClientRect();
}

function floorSvg() {
  let svg = document.getElementById("floor-wires");
  if (!svg) {
    svg = svgEl("svg");
    svg.id = "floor-wires";
    svg.classList.add("wires", "floor-wires");
    document.querySelector(".floor").appendChild(svg);
  }
  return svg;
}

function floorPortXY(kind, id, side) {
  const floor = floorBox();
  let sel = `#stage .piece.shop[data-id="${id}"] .port.${side}`;
  if (kind === "crate") sel = `#bay-in .crate[data-id="${id}"] .port.${side}`;
  if (kind === "outcrate") sel = `#bay-out .crate[data-id="${id}"] .port.${side}`;
  const el = document.querySelector(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2 - floor.left, y: r.top + r.height / 2 - floor.top };
}

function drawBeltPath(svg, d, fromId, toId, onCut, ppm, p1, p2) {
  const tempo = beltTempo(ppm || 0);
  ["belt-bed", "belt-face", "belt-slat"].forEach((cls) => {
    const path = svgEl("path");
    path.classList.add(cls);
    path.setAttribute("d", d);
    path.setAttribute("data-from", fromId);
    path.setAttribute("data-to", toId);
    if (cls === "belt-slat") {
      path.style.animationDuration = tempo.slat + "s";
      if ((ppm || 0) < 0.15) path.classList.add("idle");
      path.setAttribute("title", "传送带 · 双击拆除");
      path.addEventListener("dblclick", (e) => {
        e.stopPropagation();
        onCut();
      });
    }
    svg.appendChild(path);
  });
  if (p1 && p2) {
    [[p1.x, p1.y], [p2.x, p2.y]].forEach(([x, y]) => {
      const roll = svgEl("circle");
      roll.classList.add("roller");
      roll.setAttribute("cx", x);
      roll.setAttribute("cy", y);
      roll.setAttribute("r", 5);
      svg.appendChild(roll);
    });
    addBeltPkgs(svg, d, ppm || 0);
    const raw = bezierMid(p1.x, p1.y, p2.x, p2.y, 0.58);
    const mid = placeTag(svg, raw.x, raw.y);
    addBeltTag(svg, mid.x, mid.y - 11, ppm || 0, fromId, toId);
  }
}

function drawFloorFeeds() {
  const svg = floorSvg();
  svg.innerHTML = "";
  svg.classList.remove("drawing");
  clearBeltLabels(document.querySelector(".floor"));
  if (view.mode !== "factory") return;
  const belts = [];
  validFeeds().forEach((f) => belts.push({ fromKind: "crate", from: f.from, fromSide: "right", toKind: "shop", to: f.to, toSide: "left", cut: () => {
    layout.feeds = validFeeds().filter((x) => !(x.from === f.from && x.to === f.to));
    renderStage();
  }}));
  validShips().forEach((s) => belts.push({ fromKind: "shop", from: s.from, fromSide: "right", toKind: "outcrate", to: s.to, toSide: "left", cut: () => {
    layout.ships = validShips().filter((x) => !(x.from === s.from && x.to === s.to));
    renderStage();
  }}));
  shopLinks().forEach((l) => belts.push({ fromKind: "shop", from: l.from, fromSide: "right", toKind: "shop", to: l.to, toSide: "left", cut: () => {
    layout.links = shopLinks().filter((x) => !(x.from === l.from && x.to === l.to));
    renderStage();
  }}));
  const fg = currentFactory();
  belts.forEach((b) => {
    const p1 = floorPortXY(b.fromKind, b.from, b.fromSide);
    const p2 = floorPortXY(b.toKind, b.to, b.toSide);
    if (!p1 || !p2) return;
    const d = bezier(p1.x, p1.y, p2.x, p2.y);
    const ppm = factoryEdgePpm(fg, b.fromKind, b.from, b.toKind, b.to);
    drawBeltPath(svg, d, b.from, b.to, b.cut, ppm, p1, p2);
  });
}

function dockPortXY(which, side) {
  const el = stage.querySelector("." + which + " .port." + side);
  if (!el) return null;
  const box = stage.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2 - box.left, y: r.top + r.height / 2 - box.top };
}

function wireAnchor(id, side, index, total, machines) {
  if (id === INLET_ID) return dockPortXY("inlet", "right");
  if (id === OUTLET_ID) return dockPortXY("outlet", "left");
  const m = (machines || []).find((x) => x.id === id);
  return m ? portPos(m, side, index, total) : null;
}

function drawDockPanels(shop) {
  const feed = shopFeedName(shop);
  const ship = shopShipName(shop);
  const graph = shopGraphView(shop);
  const inPpm = (graph.succ[INLET_ID] || []).reduce((n, id) => n + edgePpm(graph, INLET_ID, id), 0);
  const outPpm = (graph.output || 0) / minutes();
  const wrap = document.createElement("div");
  wrap.className = "dock-panels";
  wrap.innerHTML =
    `<div class="inlet dock${feed ? "" : " cut"}">
      <span class="label">原料</span>
      <strong>${escHtml(feed || "未进料")}</strong>
      <em>${feed ? fmtPpm(inPpm) + " 件/分" : "接到机器"}</em>
      <span class="port right" role="button" title="接到机器"></span>
    </div>
    <div class="outlet dock${ship ? "" : " cut"}">
      <span class="label">出货</span>
      <strong>${escHtml(ship || "未出货")}</strong>
      <em>${ship ? fmtPpm(outPpm) + " 件/分" : "接到这里"}</em>
      <span class="port left" role="button" title="接出货"></span>
    </div>`;
  stage.appendChild(wrap);
  const inPort = wrap.querySelector(".inlet .port");
  const outPort = wrap.querySelector(".outlet .port");
  if (inPort) {
    inPort.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      startWire({ kind: "inlet", id: INLET_ID }, "right", e);
    });
  }
  if (outPort) {
    outPort.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      startWire({ kind: "outlet", id: OUTLET_ID }, "left", e);
    });
  }
}

function isJamWire(shop, w, graph) {
  if (replayFocus && (w.from === replayFocus || w.to === replayFocus)) return true;
  const name = graph && graph.bottleneck;
  const a = (shop.machines || []).find((m) => m.id === w.from);
  const b = (shop.machines || []).find((m) => m.id === w.to);
  return !!(name && ((a && a.name === name) || (b && b.name === name)));
}

function wireSvg() {
  let svg = stage.querySelector("svg.wires");
  if (!svg) {
    svg = svgEl("svg");
    svg.classList.add("wires");
    stage.prepend(svg);
  }
  return svg;
}

function drawWires(shop) {
  clearBeltLabels(stage);
  const svg = wireSvg();
  svg.innerHTML = "";
  const machines = shop.machines || [];
  const graph = shopGraphView(shop);
  const wires = validWires(shop);
  const outs = {};
  const ins = {};
  wires.forEach((w) => {
    outs[w.from] = outs[w.from] || [];
    ins[w.to] = ins[w.to] || [];
    outs[w.from].push(w);
    ins[w.to].push(w);
  });
  wires.forEach((w) => {
    const oi = (outs[w.from] || []).indexOf(w);
    const ii = (ins[w.to] || []).indexOf(w);
    const p1 = wireAnchor(w.from, "right", oi, (outs[w.from] || []).length, machines);
    const p2 = wireAnchor(w.to, "left", ii, (ins[w.to] || []).length, machines);
    if (!p1 || !p2) return;
    const d = bezier(p1.x, p1.y, p2.x, p2.y);
    const ppm = edgePpm(graph, w.from, w.to);
    const tempo = beltTempo(ppm);
    const cut = () => {
      shop.wires = validWires(shop).filter((x) => !(x.from === w.from && x.to === w.to));
      renderStage();
    };
    ["belt-bed", "belt-face", "belt-slat"].forEach((cls) => {
      const path = svgEl("path");
      path.classList.add(cls);
      path.setAttribute("d", d);
      path.setAttribute("data-from", w.from);
      path.setAttribute("data-to", w.to);
      if (cls === "belt-slat") {
        path.style.animationDuration = tempo.slat + "s";
        path.setAttribute("title", "传送带 · 双击拆除");
        if (isJamWire(shop, w, graph)) path.classList.add("jam");
        if (ppm < 0.15) path.classList.add("idle");
        path.addEventListener("dblclick", (e) => {
          e.stopPropagation();
          cut();
        });
      }
      svg.appendChild(path);
    });
    [[p1.x, p1.y], [p2.x, p2.y]].forEach(([x, y]) => {
      const roll = svgEl("circle");
      roll.classList.add("roller");
      roll.setAttribute("cx", x);
      roll.setAttribute("cy", y);
      roll.setAttribute("r", 5);
      svg.appendChild(roll);
    });
    addBeltPkgs(svg, d, ppm);
    const mid = bezierMid(p1.x, p1.y, p2.x, p2.y);
    addBeltTag(svg, mid.x, mid.y - 11, ppm, w.from, w.to);
  });
}

function retuneBelts(shop) {
  const svg = stage.querySelector("svg.wires");
  if (!svg || !shop) return;
  const graph = shopGraphView(shop);
  svg.querySelectorAll("path.belt-slat").forEach((path) => {
    const ppm = edgePpm(graph, path.getAttribute("data-from"), path.getAttribute("data-to"));
    const tempo = beltTempo(ppm);
    path.style.animationDuration = tempo.slat + "s";
    const d = path.getAttribute("d");
    svg.querySelectorAll("circle.belt-pkg animateMotion").forEach((motion) => {
      if (motion.getAttribute("path") === d) motion.setAttribute("dur", tempo.pkg + "s");
    });
  });
  svg.querySelectorAll("path.belt-slat").forEach((path) => {
    const fromId = path.getAttribute("data-from");
    const toId = path.getAttribute("data-to");
    const ppm = edgePpm(graph, fromId, toId);
    const tag = stage.querySelector(`.belt-label[data-from="${CSS.escape(fromId)}"][data-to="${CSS.escape(toId)}"]`);
    if (!tag) return;
    tag.classList.toggle("idle", ppm < 0.05);
    tag.textContent = fmtPpm(ppm) + " 件/分";
  });
}

function startWire(source, side, e) {
  e.preventDefault();
  e.stopPropagation();
  pending = null;
  drag = null;
  const inside = source.kind === "machine" || source.kind === "inlet" || source.kind === "outlet";
  const box = inside ? stage.getBoundingClientRect() : floorBox();
  wireDrag = {
    scope: inside ? "machine" : "feed",
    fromKind: source.kind,
    fromId: source.id,
    fromSide: side,
    x: e.clientX - box.left,
    y: e.clientY - box.top
  };
  const svg = wireDrag.scope === "feed" ? floorSvg() : wireSvg();
  svg.classList.add("drawing");
  updateWirePreview(e);
}

function updateWirePreview(e) {
  if (!wireDrag) return;
  if (wireDrag.scope === "feed") {
    const floor = floorBox();
    wireDrag.x = e.clientX - floor.left;
    wireDrag.y = e.clientY - floor.top;
    const kind = wireDrag.fromKind === "shop" ? "shop" : wireDrag.fromKind;
    const start = floorPortXY(kind, wireDrag.fromId, wireDrag.fromSide);
    if (!start) return;
    const svg = floorSvg();
    let preview = svg.querySelector(".preview");
    if (!preview) {
      preview = svgEl("path");
      preview.classList.add("preview");
      svg.appendChild(preview);
    }
    const x1 = wireDrag.fromSide === "right" ? start.x : wireDrag.x;
    const y1 = wireDrag.fromSide === "right" ? start.y : wireDrag.y;
    const x2 = wireDrag.fromSide === "right" ? wireDrag.x : start.x;
    const y2 = wireDrag.fromSide === "right" ? wireDrag.y : start.y;
    preview.setAttribute("d", bezier(x1, y1, x2, y2));
    let wantKind = [];
    let want = "left";
    if (wireDrag.fromKind === "crate") { wantKind = ["shop"]; want = "left"; }
    else if (wireDrag.fromKind === "outcrate") { wantKind = ["shop"]; want = "right"; }
    else if (wireDrag.fromKind === "shop" && wireDrag.fromSide === "left") { wantKind = ["crate", "shop"]; want = "right"; }
    else if (wireDrag.fromKind === "shop" && wireDrag.fromSide === "right") { wantKind = ["outcrate", "shop"]; want = "left"; }
    document.querySelectorAll(".floor .port").forEach((port) => {
      const crate = port.closest(".crate");
      const shopEl = port.closest(".piece.shop");
      let kind = "";
      let id = "";
      if (crate) {
        kind = crate.closest(".bay.out") ? "outcrate" : "crate";
        id = crate.dataset.id;
      } else if (shopEl) {
        kind = "shop";
        id = shopEl.dataset.id;
      }
      const ok = wantKind.includes(kind) && id && id !== wireDrag.fromId && port.classList.contains(want);
      port.classList.toggle("lit", !!ok);
    });
    return;
  }
  const shop = currentShop();
  if (!shop) return;
  const box = stage.getBoundingClientRect();
  wireDrag.x = e.clientX - box.left;
  wireDrag.y = e.clientY - box.top;
  const start = wireAnchor(wireDrag.fromId, wireDrag.fromSide, 0, 1, shop.machines || []);
  if (!start) return;
  const svg = wireSvg();
  let preview = svg.querySelector(".preview");
  if (!preview) {
    preview = svgEl("path");
    preview.classList.add("preview");
    svg.appendChild(preview);
  }
  const x1 = wireDrag.fromSide === "right" ? start.x : wireDrag.x;
  const y1 = wireDrag.fromSide === "right" ? start.y : wireDrag.y;
  const x2 = wireDrag.fromSide === "right" ? wireDrag.x : start.x;
  const y2 = wireDrag.fromSide === "right" ? wireDrag.y : start.y;
  preview.setAttribute("d", bezier(x1, y1, x2, y2));
  stage.querySelectorAll(".port").forEach((port) => {
    const piece = port.closest(".piece.machine");
    const inlet = port.closest(".inlet");
    const outlet = port.closest(".outlet");
    let ok = false;
    if (wireDrag.fromKind === "inlet" && piece && port.classList.contains("left")) ok = true;
    else if (wireDrag.fromKind === "outlet" && piece && port.classList.contains("right")) ok = true;
    else if (wireDrag.fromKind === "machine" && wireDrag.fromSide === "right") {
      if (piece && piece.dataset.id !== wireDrag.fromId && port.classList.contains("left")) ok = true;
      if (outlet && port.classList.contains("left")) ok = true;
    } else if (wireDrag.fromKind === "machine" && wireDrag.fromSide === "left") {
      if (piece && piece.dataset.id !== wireDrag.fromId && port.classList.contains("right")) ok = true;
      if (inlet && port.classList.contains("right")) ok = true;
    }
    port.classList.toggle("lit", ok);
  });
}

function finishWire(e) {
  const fromId = wireDrag.fromId;
  const fromSide = wireDrag.fromSide;
  const fromKind = wireDrag.fromKind;
  const scope = wireDrag.scope;
  const hit = hitPort(e.clientX, e.clientY);
  wireDrag = null;
  document.querySelectorAll(".port.lit").forEach((p) => p.classList.remove("lit"));
  const floorEl = document.getElementById("floor-wires");
  if (floorEl) floorEl.classList.remove("drawing");
  const stageEl = stage.querySelector("svg.wires");
  if (stageEl) stageEl.classList.remove("drawing");
  if (scope === "feed") {
    if (hit && hit.id !== fromId) {
      if (fromKind === "crate" && fromSide === "right" && hit.kind === "shop" && hit.side === "left") {
        addFeed(fromId, hit.id);
      } else if (fromKind === "shop" && fromSide === "left" && hit.kind === "crate" && hit.side === "right") {
        addFeed(hit.id, fromId);
      } else if (fromKind === "shop" && fromSide === "right" && hit.kind === "outcrate" && hit.side === "left") {
        addShip(fromId, hit.id);
      } else if (fromKind === "outcrate" && fromSide === "left" && hit.kind === "shop" && hit.side === "right") {
        addShip(hit.id, fromId);
      } else if (fromKind === "shop" && fromSide === "right" && hit.kind === "shop" && hit.side === "left") {
        addLink(fromId, hit.id);
      } else if (fromKind === "shop" && fromSide === "left" && hit.kind === "shop" && hit.side === "right") {
        addLink(hit.id, fromId);
      }
    }
    renderStage();
    return;
  }
  const shop = currentShop();
  if (shop && hit && hit.id !== fromId) {
    if (fromKind === "inlet" && fromSide === "right" && hit.kind === "machine" && hit.side === "left") {
      addWire(shop, INLET_ID, hit.id);
    } else if (fromKind === "outlet" && fromSide === "left" && hit.kind === "machine" && hit.side === "right") {
      addWire(shop, hit.id, OUTLET_ID);
    } else if (fromKind === "machine" && fromSide === "right" && hit.kind === "outlet" && hit.side === "left") {
      addWire(shop, fromId, OUTLET_ID);
    } else if (fromKind === "machine" && fromSide === "left" && hit.kind === "inlet" && hit.side === "right") {
      addWire(shop, INLET_ID, fromId);
    } else if (fromKind === "machine" && hit.kind === "machine") {
      if (fromSide === "right" && hit.side === "left") addWire(shop, fromId, hit.id);
      else if (fromSide === "left" && hit.side === "right") addWire(shop, hit.id, fromId);
    }
  }
  renderStage();
}

function renderPalette() {
  const spec = view.mode === "factory" ? SHOPS : MACHINES;
  palette.innerHTML = "";
  document.getElementById("palette-label").textContent =
    view.mode === "factory" ? "厂区积木 · 拖到平面" : "车间积木 · 机器和工人";
  Object.entries(spec).forEach(([type, meta]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = type;
    btn.innerHTML = `<strong>${meta.name}</strong><small>${meta.role}</small>`;
    btn.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      startDrag({ mode: "new", type, level: view.mode === "factory" ? "shop" : "machine" }, e);
    });
    palette.appendChild(btn);
  });
  if (view.mode === "inside") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "worker";
    btn.innerHTML = "<strong>工人</strong><small>拖到机器上岗</small>";
    btn.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      startDrag({ mode: "new", type: "worker", level: "worker" }, e);
    });
    palette.appendChild(btn);
  }
}

function pieceHTML(title, role, extra) {
  return `<div class="studs"><i></i><i></i><i></i></div>
    <div class="body"><strong>${escHtml(title)}</strong><em>${role}</em>${extra || ""}</div>`;
}

function crewHTML(item) {
  const n = Math.min(item.workers || 0, 3);
  let html = `<div class="crew">`;
  for (let i = 0; i < 3; i += 1) {
    html += i < n ? `<i class="person"></i>` : `<i class="slot" title="空位 · 点一下上岗"></i>`;
  }
  return html + `</div>`;
}

function renderStage() {
  flowCache = null;
  stage.classList.toggle("inside", view.mode === "inside");
  stage.innerHTML = "";
  if (view.mode === "factory") {
    backBtn.hidden = true;
    crumb.textContent = "厂区平面";
    drawLinks(layout.shops, SHOP);
    layout.shops.forEach((shop) => drawPiece(shop, "shop"));
    if (!layout.shops.length) {
      stage.insertAdjacentHTML("beforeend", `<div class="stage-hint">从左侧拖一个车间到这张平面上</div>`);
    }
  } else {
    const shop = currentShop();
    backBtn.hidden = false;
    crumb.textContent = (shop.name || SHOPS[shop.type].name) + (monitorFocus ? " · 故障监控" : " · 接传送带、上工人");
    drawDockPanels(shop);
    drawWires(shop);
    const graph = shopGraphView(shop);
    (shop.machines || []).forEach((m) => drawPiece(m, "machine", graph));
    if (!(shop.machines || []).length) {
      stage.insertAdjacentHTML("beforeend", `<div class="stage-hint">车间是空的。把工位机或机械臂拖进来</div>`);
    }
  }
  drawMonitorHud(currentShop());
  refreshBoard();
  paintAlerts();
  drawBays();
}

function refreshBoard(guess) {
  ensureBays();
  syncRawFromInbound();
  const machines = layout.shops.reduce((n, s) => n + (s.machines || []).length, 0);
  guess = guess || estimateLine();
  document.getElementById("struct").textContent =
    `车间 ${layout.shops.length} · 机器 ${machines} · 传送带 ${countBelts()} · 工人 ${countCrew()} · 进料 ${inboundStock()} · 出货堆积 ${outboundPile()}`;
  if (!runState && !document.getElementById("m-eff").dataset.locked) {
    document.getElementById("m-eff").textContent = Math.round(guess.efficiency * 100) + "%";
    document.getElementById("m-yield").textContent = guess.pieces + " 件";
    const price = cratePrice((layout.outbound.items || [])[0]) || Number(layout.price) || 18;
    const wages = countCrew() * WAGE;
    const rawBill = (layout.inbound.items || []).reduce((n, it) => n + Math.max(Number(it.qty) || 0, 0) * crateCost(it), 0);
    document.getElementById("m-wage").textContent = wages;
    document.getElementById("m-rev").textContent = "约 " + Math.round(guess.pieces * price - wages - rawBill * (guess.rawUsed || 0) / Math.max(inboundStock(), 1));
  }
  drawBays();
}

function ensureBays() {
  if (!layout.inbound || !Array.isArray(layout.inbound.items) || !layout.inbound.items.length) {
    layout.inbound = { items: [{ id: "in1", name: "冷轧钢板", qty: Number(layout.raw) || 120, cap: BAY_CAP }] };
  }
  if (!layout.outbound || !Array.isArray(layout.outbound.items) || !layout.outbound.items.length) {
    layout.outbound = { items: [{ id: "out1", name: "积木成品", qty: 0, cap: BAY_CAP }] };
  }
  layout.inbound.items.forEach((it) => {
    it.cap = BAY_CAP;
    if (!(Number(it.cost) >= 0)) it.cost = RAW_COST;
  });
  layout.outbound.items.forEach((it) => {
    it.cap = BAY_CAP;
    if (!(Number(it.price) >= 0)) it.price = Number(layout.price) || 18;
  });
}

function inboundStock() {
  ensureBays();
  return layout.inbound.items.reduce((n, it) => n + Math.max(Number(it.qty) || 0, 0), 0);
}

function outboundPile() {
  ensureBays();
  return layout.outbound.items.reduce((n, it) => n + Math.max(Number(it.qty) || 0, 0), 0);
}

function stockTone(qty, cap) {
  if (qty <= 0) return { key: "empty", text: "空仓" };
  const r = qty / Math.max(cap, 1);
  if (r < 0.2) return { key: "low", text: "告急" };
  if (r >= 1) return { key: "ok", text: "满仓" };
  return { key: "ok", text: "正常" };
}

function pileTone(qty, cap) {
  if (qty <= 0) return { key: "empty", text: "空台" };
  const r = qty / Math.max(cap, 1);
  if (r >= 1) return { key: "jam", text: "堆积" };
  if (r >= 0.7) return { key: "warn", text: "将满" };
  return { key: "ok", text: "畅通" };
}

function syncRawFromInbound() {
  layout.raw = inboundStock();
}

function crateCost(item) {
  const n = Number(item && item.cost);
  return n >= 0 ? n : RAW_COST;
}

function cratePrice(item) {
  const n = Number(item && item.price);
  if (n >= 0) return n;
  return Math.max(Number(layout && layout.price) || 18, 0);
}

function moneyHTML(item, kind) {
  if (kind === "in") {
    const cost = crateCost(item);
    const qty = Math.max(Number(item.qty) || 0, 0);
    return `<label class="money"><span>进价</span><input class="bay-cost" type="number" min="0" step="0.1" value="${cost}" title="原料进价"></label>
      <b class="est">约 ${Math.round(qty * cost)}</b>`;
  }
  const price = cratePrice(item);
  const qty = Math.max(Number(item.qty) || 0, 0);
  return `<label class="money"><span>预估售价</span><input class="bay-price" type="number" min="0" step="1" value="${price}" title="成品单价"></label>
    <b class="est">约 ${Math.round(qty * price)}</b>`;
}

function crateHTML(item, kind) {
  const cap = Math.max(Number(item.cap) || BAY_CAP, 1);
  const qty = Math.max(Number(item.qty) || 0, 0);
  const tone = kind === "in" ? stockTone(qty, cap) : pileTone(qty, cap);
  const pct = Math.round(Math.min(qty / cap, 1) * 100);
  return `<div class="crate" data-id="${escHtml(item.id)}">
    ${kind === "in"
      ? `<span class="port right" role="button" title="拉到车间"></span>`
      : `<span class="port left" role="button" title="接到出货"></span>`}
    <input class="bay-name" value="${escHtml(item.name || "")}" title="货物名称" />
    <div class="row">
      <input class="bay-qty" type="number" min="0" value="${qty}" title="${kind === "in" ? "库存" : "堆积"}" />
      <button type="button" class="odel" title="去掉">×</button>
    </div>
    ${moneyHTML(item, kind)}
    <div class="fill"><i style="width:${pct}%"></i></div>
    <span class="tone ${tone.key}">${tone.text} ${qty}/${cap}</span>
  </div>`;
}

function fillInbound() {
  ensureBays();
  layout.inbound.items.forEach((it) => {
    it.qty = Math.max(Number(it.cap) || BAY_CAP, 1);
  });
  syncRawFromInbound();
  if (!runState) {
    const eff = document.getElementById("m-eff");
    if (eff) delete eff.dataset.locked;
  }
  if (runState && runState.starved) runState.starved = false;
  drawBays();
  refreshBoard();
  paintAlerts();
}

function bindBay(box, key) {
  box.querySelectorAll(".crate").forEach((el) => {
    const id = el.dataset.id;
    const item = layout[key].items.find((it) => it.id === id);
    if (!item) return;
    el.querySelector(".bay-name").addEventListener("change", (e) => {
      item.name = e.target.value.trim() || (key === "inbound" ? "原料" : "成品");
      renderStage();
    });
    el.querySelector(".bay-qty").addEventListener("change", (e) => {
      item.qty = Math.max(Number(e.target.value) || 0, 0);
      syncRawFromInbound();
      delete document.getElementById("m-eff").dataset.locked;
      refreshBoard();
    });
    const costEl = el.querySelector(".bay-cost");
    if (costEl) {
      costEl.addEventListener("pointerdown", (e) => e.stopPropagation());
      costEl.addEventListener("input", () => {
        item.cost = Math.max(Number(costEl.value) || 0, 0);
        const est = el.querySelector(".est");
        if (est) est.textContent = "约 " + Math.round(Math.max(Number(item.qty) || 0, 0) * item.cost);
        delete document.getElementById("m-eff").dataset.locked;
        refreshBoard();
      });
    }
    const priceEl = el.querySelector(".bay-price");
    if (priceEl) {
      priceEl.addEventListener("pointerdown", (e) => e.stopPropagation());
      priceEl.addEventListener("input", () => {
        item.price = Math.max(Number(priceEl.value) || 0, 0);
        layout.price = item.price || 18;
        const side = document.getElementById("price");
        if (side && document.activeElement !== side) side.value = layout.price;
        const est = el.querySelector(".est");
        if (est) est.textContent = "约 " + Math.round(Math.max(Number(item.qty) || 0, 0) * item.price);
        delete document.getElementById("m-eff").dataset.locked;
        refreshBoard();
      });
    }
    el.querySelector(".odel").addEventListener("click", () => {
      if (layout[key].items.length <= 1) {
        item.qty = 0;
      } else {
        layout[key].items = layout[key].items.filter((it) => it.id !== id);
        if (key === "inbound") {
          layout.feeds = (layout.feeds || []).filter((f) => f.from !== id);
        } else {
          layout.ships = (layout.ships || []).filter((s) => s.to !== id);
        }
      }
      syncRawFromInbound();
      refreshBoard();
    });
    const port = el.querySelector(".port");
    if (port && key === "inbound") {
      port.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        startWire({ kind: "crate", id }, "right", e);
      });
    }
    if (port && key === "outbound") {
      port.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        startWire({ kind: "outcrate", id }, "left", e);
      });
    }
  });
  const add = box.querySelector(".add-bay");
  if (add) {
    add.onclick = () => {
      if (layout[key].items.length >= 4) return;
      const isIn = key === "inbound";
      layout[key].items.push({
        id: nid(isIn ? "in" : "out"),
        name: isIn ? "新原料" : "新成品",
        qty: isIn ? 40 : 0,
        cap: BAY_CAP,
        cost: isIn ? RAW_COST : undefined,
        price: isIn ? undefined : (Number(layout.price) || 18)
      });
      syncRawFromInbound();
      drawBays();
    };
  }
  const fill = box.querySelector(".fill-bay");
  if (fill) fill.onclick = fillInbound;
}

function drawBays() {
  const inn = document.getElementById("bay-in");
  const out = document.getElementById("bay-out");
  if (!inn || !out || !layout) return;
  ensureBays();
  const inQty = inboundStock();
  const inCap = layout.inbound.items.reduce((n, it) => n + Math.max(Number(it.cap) || BAY_CAP, 1), 0);
  const outQty = outboundPile();
  const outCap = layout.outbound.items.reduce((n, it) => n + Math.max(Number(it.cap) || BAY_CAP, 1), 0);
  const inTone = stockTone(inQty, inCap);
  const outTone = pileTone(outQty, outCap);
  inn.classList.toggle("empty", inTone.key === "empty" || inTone.key === "low");
  out.classList.toggle("jam", outTone.key === "jam" || outTone.key === "warn");
  inn.classList.toggle("linkable", view.mode === "factory");
  out.classList.toggle("linkable", view.mode === "factory");
  inn.innerHTML = `<h3>进料区</h3><span class="tone ${inTone.key}">仓储 ${inTone.text}</span>` +
    layout.inbound.items.map((it) => crateHTML(it, "in")).join("") +
    `<button type="button" class="fill-bay">一键补满</button>` +
    `<button type="button" class="ghost-btn add-bay">+ 原料</button>`;
  out.innerHTML = `<h3>出货区</h3><span class="tone ${outTone.key}">堆积 ${outTone.text}</span>` +
    layout.outbound.items.map((it) => crateHTML(it, "out")).join("") +
    `<button type="button" class="ghost-btn add-bay">+ 成品</button>`;
  bindBay(inn, "inbound");
  bindBay(out, "outbound");
  drawFloorFeeds();
}

function setTicker(text) {
  const el = document.getElementById("ticker");
  if (el) el.textContent = text || "";
}

function listFaults() {
  const rows = [];
  (layout.shops || []).forEach((shop) => {
    (shop.machines || []).forEach((machine) => {
      if (machine.broken) rows.push({ shop, machine });
    });
  });
  return rows;
}

function findMachine(id) {
  for (const shop of (layout.shops || [])) {
    const machine = (shop.machines || []).find((m) => m.id === id);
    if (machine) return { shop, machine };
  }
  return null;
}

function gotoMachine(shopId, machineId) {
  const hit = findMachine(machineId);
  const shop = (layout.shops || []).find((s) => s.id === shopId) || (hit && hit.shop);
  if (!shop) return;
  view = { mode: "inside", id: shop.id };
  selected = { id: machineId, level: "machine" };
  monitorFocus = machineId;
  replayFocus = machineId;
  renderPalette();
  renderStage();
  requestAnimationFrame(() => {
    const el = stage.querySelector(`.piece.machine[data-id="${machineId}"]`);
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
  });
}

function repairMachine(machine) {
  if (!machine) return;
  machine.broken = false;
  if (monitorFocus === machine.id) monitorFocus = null;
  if (replayFocus === machine.id) replayFocus = null;
  renderStage();
}

function paintFaultBar() {
  const bar = document.getElementById("fault-bar");
  if (!bar) return;
  const faults = listFaults();
  document.body.classList.toggle("has-fault", faults.length > 0);
  if (!faults.length) {
    bar.hidden = true;
    bar.innerHTML = "";
    if (monitorFocus) {
      const still = findMachine(monitorFocus);
      if (!still || !still.machine.broken) monitorFocus = null;
    }
    return;
  }
  bar.hidden = false;
  bar.innerHTML = "";
  const title = document.createElement("b");
  title.textContent = faults.length > 1 ? "故障 " + faults.length + " 台" : "机器故障";
  bar.appendChild(title);
  faults.forEach(({ shop, machine }) => {
    const row = document.createElement("div");
    row.className = "fault-item";
    const msg = document.createElement("span");
    msg.textContent = "「" + (shop.name || "车间") + "」的「" + (machine.name || "机器") + "」过热停机";
    const go = document.createElement("button");
    go.type = "button";
    go.textContent = "去监控";
    go.addEventListener("click", () => gotoMachine(shop.id, machine.id));
    const fix = document.createElement("button");
    fix.type = "button";
    fix.className = "fix-now";
    fix.textContent = "修好";
    fix.addEventListener("click", () => repairMachine(machine));
    row.appendChild(msg);
    row.appendChild(go);
    row.appendChild(fix);
    bar.appendChild(row);
  });
}

function drawMonitorHud(shop) {
  const hud = document.getElementById("monitor-hud");
  if (!hud) return;
  hud.innerHTML = "";
  if (!monitorFocus) {
    hud.hidden = true;
    hud.className = "monitor-hud";
    return;
  }
  const hit = shop ? { shop, machine: (shop.machines || []).find((m) => m.id === monitorFocus) } : findMachine(monitorFocus);
  const machine = hit && hit.machine;
  const here = hit && hit.shop;
  if (!machine || !here) {
    hud.hidden = true;
    return;
  }
  const graph = shopGraphView(here);
  const st = machineStatus(here, machine, graph);
  hud.hidden = false;
  hud.className = "monitor-hud" + (machine.broken ? "" : " ok");
  const cam = document.createElement("div");
  cam.className = "cam";
  cam.setAttribute("aria-hidden", "true");
  const copy = document.createElement("div");
  copy.className = "copy";
  const label = document.createElement("b");
  label.textContent = "正在监控";
  const name = document.createElement("strong");
  name.textContent = machine.name || "机器";
  const meta = document.createElement("span");
  meta.textContent = (here.name || "车间") + " · " + st.text + " · " + fmtPpm(machinePpm(machine)) + " 件/分 · " + machineStaff(machine) + " 人在岗";
  copy.appendChild(label);
  copy.appendChild(name);
  copy.appendChild(meta);
  hud.appendChild(cam);
  hud.appendChild(copy);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "quiet";
  close.textContent = "关闭监控";
  close.addEventListener("click", (e) => {
    e.stopPropagation();
    monitorFocus = null;
    renderStage();
  });
  if (machine.broken) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "修好这台";
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      repairMachine(machine);
    });
    hud.appendChild(btn);
  }
  hud.appendChild(close);
}

function paintAlerts() {
  paintFaultBar();
  const box = document.getElementById("alerts");
  if (!box) return;
  const items = typeof floorAlerts === "function" ? floorAlerts(layout) : [];
  const guess = estimateLine();
  if (layout.shops && layout.shops.length && !lineOpen()) {
    items.unshift({ kind: "cut", text: "流水线没接通，物料到不了出货区" });
  }
  (layout.shops || []).forEach((shop) => {
    if ((shop.machines || []).length && !shopOpen(shop)) {
      items.push({ kind: "cut", text: `「${shop.name || "车间"}」里原料没走到出货` });
    }
  });
  if (guess.bottleneck && guess.pieces) items.push({ kind: "slow", text: "卡线在「" + guess.bottleneck + "」" });
  ensureBays();
  const inQty = inboundStock();
  const inCap = layout.inbound.items.reduce((n, it) => n + Math.max(Number(it.cap) || BAY_CAP, 1), 0);
  const outQty = outboundPile();
  const outCap = layout.outbound.items.reduce((n, it) => n + Math.max(Number(it.cap) || BAY_CAP, 1), 0);
  const inn = stockTone(inQty, inCap);
  const outt = pileTone(outQty, outCap);
  if (inn.key === "empty" || inn.key === "low") items.unshift({ kind: "cut", text: "进料区" + inn.text });
  if (outt.key === "jam" || outt.key === "warn") items.unshift({ kind: "slow", text: "出货区" + outt.text });
  const mild = items.filter((a) => a.kind !== "down");
  if (!mild.length) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent = mild.slice(0, 3).map((a) => a.text).join(" · ");
}

function drawRawChart(guess) {
  const box = document.getElementById("raw-chart");
  const unitEl = document.getElementById("raw-unit");
  if (!box) return;
  const spec = (guess && guess.spec) || MATERIALS[layout.raw_kind || "steel"];
  const raw = Number((guess && guess.rawIn) != null ? guess.rawIn : layout.raw || 0);
  const used = Number((guess && guess.rawUsed) != null ? guess.rawUsed : 0);
  const left = Number((guess && guess.rawLeft) != null ? guess.rawLeft : raw);
  const max = Math.max(raw, used, left, 1);
  if (unitEl) unitEl.textContent = `约 ${spec.consume} ${spec.unit}出 1 件`;
  const row = (cls, label, val) =>
    `<div class="raw-bar ${cls}"><span>${label}</span><b><i style="width:${Math.round(val / max * 100)}%"></i></b><span>${val}${spec.unit}</span></div>`;
  box.innerHTML = row("in", "投入", raw) + row("out", "转化", Math.round(used * 10) / 10) + row("left", "余料", Math.round(left * 10) / 10);
}

function deleteSelected() {
  if (!selected) return;
  if (selected.level === "shop") {
    layout.shops = layout.shops.filter((s) => s.id !== selected.id);
    layout.feeds = (layout.feeds || []).filter((f) => f.to !== selected.id);
    layout.ships = (layout.ships || []).filter((s) => s.from !== selected.id);
    layout.links = (layout.links || []).filter((l) => l.from !== selected.id && l.to !== selected.id);
  } else if (currentShop()) {
    const shop = currentShop();
    const id = selected.id;
    shop.machines = (shop.machines || []).filter((m) => m.id !== id);
    shop.wires = (shop.wires || []).filter((w) => w.from !== id && w.to !== id);
  }
  selected = null;
  renderStage();
}

function drawPiece(item, level, graph) {
  const spec = level === "shop" ? SHOPS[item.type] : MACHINES[item.type];
  const el = document.createElement("div");
  el.className = "piece " + level + " " + item.type + (selected && selected.id === item.id ? " selected" : "");
  el.dataset.id = item.id;
  el.style.left = item.x + "px";
  el.style.top = item.y + "px";
  let role = spec.role;
  if (level === "shop") {
    const n = (item.machines || []).length;
    const ppm = (currentFactory().produced[item.id] || 0) / minutes();
    role = n ? `${n}台 ${shopCrew(item)}人 · ${fmtPpm(ppm)}件/分` : "空车间";
  } else {
    const st = machineStatus(currentShop(), item, graph);
    role = `<span class="badge ${st.key}">${st.text}</span>`;
    if (machineStaff(item)) el.classList.add("has-crew");
    if (item.broken) el.classList.add("down");
    if (monitorFocus === item.id) el.classList.add("watch");
  }
  let extra = "";
  if (level === "shop") {
    const feed = shopFeedName(item);
    const ship = shopShipName(item);
    const downs = (item.machines || []).filter((m) => m.broken);
    if (downs.length) el.classList.add("down");
    extra = `<span class="port left" role="button" title="接进料" aria-label="接进料"></span>
      <span class="port right" role="button" title="接出货" aria-label="接出货"></span>
      <div class="feed-tag ${feed ? "ok" : "cut"}">${feed ? "原料 " + escHtml(feed) : "未进料"}</div>
      <div class="feed-tag ship ${ship ? "ok" : "cut"}">${ship ? "出货 " + escHtml(ship) : "未出货"}</div>
      ${downs.length ? `<div class="fault-flag">${downs.length}台故障</div>` : ""}
      <button class="enter" type="button">${downs.length ? "去监控" : "进入"}</button>`;
  } else {
    extra = `<span class="port left" role="button" title="输入" aria-label="输入"></span><span class="port right" role="button" title="输出" aria-label="输出"></span>
      <div class="eff-line" title="每分钟能做多少件">
        <label><input class="ppm" type="number" min="1" max="120" step="1" value="${Math.round(machinePpm(item))}"> 件/分</label>
      </div>
      ${item.broken ? `<button class="fix" type="button">修好</button>` : crewHTML(item)}`;
  }
  if (selected && selected.id === item.id) {
    extra += `<button class="kill" type="button" title="删除">×</button>`;
  }
  el.innerHTML = pieceHTML(item.name || spec.name, role, extra);
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest(".enter") || e.target.closest(".rename-box") || e.target.closest(".port") || e.target.closest(".crew") || e.target.closest(".kill") || e.target.closest(".ppm") || e.target.closest(".eff-line") || e.target.closest(".fix")) return;
    selected = { id: item.id, level };
    pending = { info: { mode: "move", id: item.id, level }, x: e.clientX, y: e.clientY };
  });
  el.addEventListener("dblclick", (e) => {
    if (e.target.closest(".enter") || e.target.closest(".port") || e.target.closest(".crew") || e.target.closest(".ppm") || e.target.closest(".eff-line") || e.target.closest(".fix")) return;
    e.preventDefault();
    e.stopPropagation();
    pending = null;
    beginRename(item, el);
  });
  el.querySelectorAll(".port").forEach((port) => {
    const side = port.classList.contains("left") ? "left" : "right";
    port.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      startWire({ kind: level === "shop" ? "shop" : "machine", id: item.id }, side, e);
    });
  });
  const ppm = el.querySelector(".ppm");
  if (ppm) {
    ppm.addEventListener("pointerdown", (e) => e.stopPropagation());
    ppm.addEventListener("click", (e) => e.stopPropagation());
    const apply = (redraw) => {
      item.ppm = clamp(Number(ppm.value) || STD_PPM, 1, 120);
      delete item.cycle;
      delete document.getElementById("m-eff").dataset.locked;
      refreshBoard();
      if (view.mode === "inside") retuneBelts(currentShop());
      if (redraw) renderStage();
    };
    ppm.addEventListener("input", () => apply(false));
    ppm.addEventListener("change", () => apply(true));
  }
  const crew = el.querySelector(".crew");
  if (crew) {
    crew.addEventListener("pointerdown", (e) => e.stopPropagation());
    crew.querySelectorAll(".slot").forEach((slot) => {
      slot.addEventListener("click", (e) => {
        e.stopPropagation();
        item.workers = Math.min(3, (item.workers || 0) + 1);
        renderStage();
      });
    });
    crew.querySelectorAll(".person").forEach((person) => {
      person.title = "点一下下岗";
      person.addEventListener("click", (e) => {
        e.stopPropagation();
        item.workers = Math.max(0, (item.workers || 0) - 1);
        renderStage();
      });
    });
  }
  const fix = el.querySelector(".fix");
  if (fix) {
    fix.addEventListener("pointerdown", (e) => e.stopPropagation());
    fix.addEventListener("click", (e) => {
      e.stopPropagation();
      item.broken = false;
      if (monitorFocus === item.id) monitorFocus = null;
      if (replayFocus === item.id) replayFocus = null;
      renderStage();
    });
  }
  const enter = el.querySelector(".enter");
  if (enter) {
    enter.addEventListener("click", (e) => {
      e.stopPropagation();
      const firstDown = (item.machines || []).find((m) => m.broken);
      if (firstDown) gotoMachine(item.id, firstDown.id);
      else {
        view = { mode: "inside", id: item.id };
        selected = null;
        monitorFocus = null;
        renderPalette();
        renderStage();
      }
    });
  }
  const kill = el.querySelector(".kill");
  if (kill) {
    kill.addEventListener("pointerdown", (e) => e.stopPropagation());
    kill.addEventListener("click", (e) => {
      e.stopPropagation();
      selected = { id: item.id, level };
      deleteSelected();
    });
  }
  stage.appendChild(el);
}

function beginRename(item, el) {
  const title = el.querySelector("strong");
  if (!title || el.querySelector(".rename-box")) return;
  const input = document.createElement("input");
  input.className = "rename-box";
  input.value = item.name || "";
  title.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const commit = () => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    if (name) item.name = name;
    renderStage();
  };
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    }
    if (e.key === "Escape") {
      e.preventDefault();
      done = true;
      renderStage();
    }
  });
  input.addEventListener("blur", commit);
  input.addEventListener("pointerdown", (e) => e.stopPropagation());
}

function startDrag(info, e) {
  if (info.level === "worker") {
    drag = { ...info };
    const ghost = document.createElement("div");
    ghost.className = "person ghost-person";
    ghost.style.left = (e.clientX - 8) + "px";
    ghost.style.top = (e.clientY - 12) + "px";
    document.body.appendChild(ghost);
    drag.ghost = ghost;
    e.preventDefault();
    return;
  }
  const size = info.level === "shop" ? SHOP : MACHINE;
  const type = info.type || findType(info);
  drag = { ...info, type };
  const spec = (info.level === "shop" ? SHOPS : MACHINES)[type];
  const ghost = document.createElement("div");
  ghost.className = "piece ghost " + info.level + " " + type;
  ghost.style.width = size.w + "px";
  ghost.innerHTML = pieceHTML(spec.name, spec.role);
  ghost.style.left = e.clientX - size.w / 2 + "px";
  ghost.style.top = e.clientY - 16 + "px";
  document.body.appendChild(ghost);
  drag.ghost = ghost;
  e.preventDefault();
}

function findType(info) {
  const list = info.level === "shop" ? layout.shops : currentShop().machines;
  return list.find((x) => x.id === info.id).type;
}

function onMove(e) {
  if (wireDrag) {
    updateWirePreview(e);
    return;
  }
  if (pending) {
    const dx = e.clientX - pending.x;
    const dy = e.clientY - pending.y;
    if (Math.hypot(dx, dy) >= DRAG_START) {
      startDrag(pending.info, e);
      pending = null;
    }
  }
  if (!drag) return;
  if (drag.level === "worker") {
    drag.ghost.style.left = (e.clientX - 8) + "px";
    drag.ghost.style.top = (e.clientY - 12) + "px";
    return;
  }
  const size = drag.level === "shop" ? SHOP : MACHINE;
  drag.ghost.style.left = e.clientX - size.w / 2 + "px";
  drag.ghost.style.top = e.clientY - 16 + "px";
}

function onUp(e) {
  if (wireDrag) {
    finishWire(e);
    return;
  }
  const wasPending = pending;
  pending = null;
  if (!drag) {
    if (wasPending) renderStage();
    return;
  }
  if (drag.level === "worker") {
    const box = stage.getBoundingClientRect();
    const inside = e.clientX >= box.left && e.clientX <= box.right && e.clientY >= box.top && e.clientY <= box.bottom;
    if (inside && view.mode === "inside") {
      const hit = document.elementFromPoint(e.clientX, e.clientY);
      const piece = hit && hit.closest ? hit.closest(".piece.machine") : null;
      if (piece) {
        const hit = findMachine(piece.dataset.id);
        if (hit && hit.machine) hit.machine.workers = Math.min(3, (hit.machine.workers || 0) + 1);
      }
    }
    drag.ghost.remove();
    drag = null;
    renderStage();
    return;
  }
  const box = stage.getBoundingClientRect();
  const inside = e.clientX >= box.left && e.clientX <= box.right && e.clientY >= box.top && e.clientY <= box.bottom;
  const size = drag.level === "shop" ? SHOP : MACHINE;
  if (inside) {
    let x = e.clientX - box.left - size.w / 2;
    let y = e.clientY - box.top - 16;
    if (drag.level === "shop") {
      const others = layout.shops
        .filter((p) => p.id !== drag.id)
        .map((p) => ({ x: p.x, y: p.y, w: size.w, h: size.h }));
      const m = magnet(x, y, size.w, size.h, others);
      x = clamp(m.x, 0, box.width - size.w);
      y = clamp(m.y, 8, box.height - size.h);
    } else {
      x = clamp(snap(x), 0, box.width - size.w);
      y = clamp(snap(y), 8, box.height - size.h);
    }
    if (drag.mode === "new") {
      if (drag.level === "shop") {
        layout.shops.push({
          id: nid("s"),
          type: drag.type,
          name: SHOPS[drag.type].name,
          x, y,
          machines: [],
          wires: []
        });
      } else {
        currentShop().machines = currentShop().machines || [];
        currentShop().machines.push({
          id: nid("m"),
          type: drag.type,
          name: MACHINES[drag.type].name,
          x, y,
          burst: 3,
          priority: 2,
          workers: 0,
          ppm: 20
        });
      }
    } else {
      const list = drag.level === "shop" ? layout.shops : currentShop().machines;
      const item = list.find((p) => p.id === drag.id);
      item.x = x;
      item.y = y;
    }
  }
  drag.ghost.remove();
  drag = null;
  renderStage();
}

function setProducing(on) {
  document.body.classList.toggle("producing", !!on);
  stage.classList.toggle("shift-on", !!on);
  if (on) document.body.classList.add("line-live");
}

function haltLine() {
  document.body.classList.remove("producing", "line-live");
  stage.classList.remove("shift-on");
}

function readControls() {
  layout.price = Number(document.getElementById("price").value) || 18;
  ensureBays();
  const firstOut = (layout.outbound.items || [])[0];
  if (firstOut && Number(firstOut.price) >= 0) layout.price = cratePrice(firstOut);
  const side = document.getElementById("price");
  if (side) side.value = layout.price;
  ensureFeeds();
  ensureShips();
  ensureLinks();
  syncRawFromInbound();
  layout.scheduler = layout.scheduler || "priority";
  layout.ship_algo = layout.ship_algo || "sstf";
  layout.warehouse_policy = layout.warehouse_policy || "best";
  layout.daemon = layout.daemon !== false;
  layout.quantum = layout.quantum || 2;
  layout.page_frames = layout.page_frames || 4;
  layout.page_algo = layout.page_algo || "lru";
  layout.buffer_cap = layout.buffer_cap || 6;
}

function writeControls() {
  document.getElementById("price").value = layout.price || 18;
  ensureBays();
  ensureShips();
  ensureLinks();
  drawBays();
}

function cloneItems(items) {
  return (items || []).map((it) => ({
    id: it.id,
    name: it.name,
    qty: Math.max(Number(it.qty) || 0, 0),
    cap: Math.max(Number(it.cap) || 1, 1),
    cost: crateCost(it),
    price: cratePrice(it)
  }));
}

function lerpItems(from, to, p) {
  const end = {};
  (to || []).forEach((it) => { end[it.id] = it; });
  return (from || []).map((it) => {
    const b = end[it.id] || it;
    return { ...it, qty: it.qty + ((Number(b.qty) || 0) - it.qty) * p };
  });
}

function stepItems(from, to, n, total) {
  const end = {};
  (to || []).forEach((it) => { end[it.id] = it; });
  return (from || []).map((it) => {
    const b = end[it.id] || it;
    const delta = (Number(b.qty) || 0) - it.qty;
    const qty = total > 0 ? it.qty + delta * (n / total) : it.qty;
    return { ...it, qty: round1(qty) };
  });
}

function round1(n) {
  return Math.round((Number(n) || 0) * 10) / 10;
}

function paintCrates(boxId, items, kind) {
  const box = document.getElementById(boxId);
  if (!box) return;
  (items || []).forEach((it) => {
    const el = box.querySelector(`.crate[data-id="${it.id}"]`);
    if (!el) return;
    const cap = Math.max(Number(it.cap) || BAY_CAP, 1);
    const qty = round1(it.qty);
    const input = el.querySelector(".bay-qty");
    if (input && document.activeElement !== input) input.value = qty;
    const fill = el.querySelector(".fill i");
    if (fill) fill.style.width = Math.round(Math.min(qty / cap, 1) * 100) + "%";
    const toneEl = el.querySelector("span.tone");
    const tone = kind === "in" ? stockTone(qty, cap) : pileTone(qty, cap);
    if (toneEl) {
      toneEl.className = "tone " + tone.key;
      toneEl.textContent = tone.text + " " + qty + "/" + cap;
    }
    const est = el.querySelector(".est");
    if (est) {
      const unit = kind === "in" ? crateCost(it) : cratePrice(it);
      est.textContent = "约 " + Math.round(qty * unit);
    }
  });
  const total = (items || []).reduce((n, it) => n + Math.max(Number(it.qty) || 0, 0), 0);
  const cap = (items || []).reduce((n, it) => n + Math.max(Number(it.cap) || 1, 1), 0);
  const tone = kind === "in" ? stockTone(total, cap) : pileTone(total, cap);
  const head = box.querySelector(":scope > .tone");
  if (head) {
    head.className = "tone " + tone.key;
    head.textContent = (kind === "in" ? "仓储 " : "堆积 ") + tone.text;
  }
  if (kind === "in") box.classList.toggle("empty", tone.key === "empty" || tone.key === "low");
  else box.classList.toggle("jam", tone.key === "jam" || tone.key === "warn");
}

function liveOutputPpm() {
  if (!layout || !lineOpen()) return 0;
  const shopCap = {};
  (layout.shops || []).forEach((s) => { shopCap[s.id] = shopGraph(s, null, true).output || 0; });
  return (factoryFlow(shopCap, true).output || 0) / minutes();
}

function scrapRate() {
  let manned = 0;
  let workers = 0;
  (layout.shops || []).forEach((shop) => {
    (shop.machines || []).forEach((m) => {
      const staff = machineStaff(m);
      if (staff && !m.broken) {
        manned += 1;
        workers += staff;
      }
    });
  });
  if (manned <= 0) return 0.16;
  const avg = workers / manned;
  return Math.max(0.03, 0.14 - 0.035 * Math.min(avg, 3));
}

function takeInbound(amount) {
  ensureBays();
  let left = Math.max(Number(amount) || 0, 0);
  let bill = 0;
  let used = 0;
  (layout.inbound.items || []).forEach((it) => {
    if (left <= 0) return;
    const have = Math.max(Number(it.qty) || 0, 0);
    const take = Math.min(have, left);
    it.qty = have - take;
    bill += take * crateCost(it);
    used += take;
    left -= take;
  });
  syncRawFromInbound();
  return { used, bill };
}

function addOutbound(pieces) {
  ensureBays();
  const docks = layout.outbound.items || [];
  if (!docks.length) return;
  docks[0].qty = (Number(docks[0].qty) || 0) + Math.max(Number(pieces) || 0, 0);
}

function pickLiveBreak() {
  const cands = [];
  (layout.shops || []).forEach((shop) => {
    (shop.machines || []).forEach((m) => {
      const staff = machineStaff(m);
      if (staff > 0 && !m.broken && m.id) {
        cands.push({ staff, ppm: machinePpm(m), machine: m, shop });
      }
    });
  });
  if (!cands.length) return null;
  cands.sort((a, b) => a.staff - b.staff || a.ppm - b.ppm);
  const hit = cands[0];
  const shopName = hit.shop.name || "车间";
  const name = hit.machine.name || "机器";
  return {
    id: hit.machine.id,
    text: "「" + shopName + "」的「" + name + "」过热停机"
  };
}

function liveWages(elapsedSec) {
  return Math.round(countCrew() * WAGE * (Math.max(elapsedSec, 0) / minutes()));
}

function paintLiveBays() {
  ensureBays();
  paintCrates("bay-in", layout.inbound.items, "in");
  paintCrates("bay-out", layout.outbound.items, "out");
  const struct = document.getElementById("struct");
  if (!struct) return;
  const machines = layout.shops.reduce((n, s) => n + (s.machines || []).length, 0);
  struct.textContent =
    `车间 ${layout.shops.length} · 机器 ${machines} · 传送带 ${countBelts()} · 工人 ${countCrew()} · 进料 ${round1(inboundStock())} · 出货堆积 ${round1(outboundPile())}`;
}

function setRunButton(running) {
  const btn = document.getElementById("run");
  if (!btn) return;
  btn.disabled = false;
  btn.textContent = running ? "停下" : "开始生产";
  btn.classList.toggle("halt", !!running);
}

function startRun() {
  if (runState) return;
  readControls();
  replayFocus = null;
  monitorFocus = null;
  flowCache = null;
  lastResult = null;
  clearAllBroken();
  setProducing(true);
  setRunButton(true);
  setTicker("产线运行中，点「停下」结束");
  document.getElementById("m-note").textContent = "产线运行中，点「停下」结束。";
  document.getElementById("m-eff").dataset.locked = "1";
  renderStage();
  const spec = MATERIALS[layout.raw_kind || "steel"] || MATERIALS.steel;
  runState = {
    t0: performance.now(),
    last: performance.now(),
    yield: 0,
    scrap: 0,
    rawBill: 0,
    consume: spec.consume,
    faultAt: 8000,
    faultDone: false,
    carry: 0,
    starved: false
  };
  runRaf = requestAnimationFrame(tickRun);
}

function tickRun(now) {
  if (!runState) return;
  const dt = Math.min(0.2, (now - runState.last) / 1000);
  runState.last = now;
  const elapsed = now - runState.t0;
  if (!runState.faultDone && elapsed >= runState.faultAt) {
    runState.faultDone = true;
    if (!listFaults().length) {
      const ev = pickLiveBreak();
      if (ev) {
        replayFocus = ev.id;
        monitorFocus = ev.id;
        markBroken(ev.id, true);
        paintFaultBar();
        paintMachineHit(ev.id);
        setTicker(ev.text);
      }
    }
  }
  const ppm = liveOutputPpm();
  let want = ppm * dt + runState.carry;
  const raw = inboundStock();
  const maxFromRaw = runState.consume > 0 ? raw / runState.consume : want;
  let attempted = Math.min(Math.max(want, 0), Math.max(maxFromRaw, 0));
  if (!lineOpen()) attempted = 0;
  runState.carry = Math.max(0, want - attempted);
  if (ppm > 0 && raw <= 0) {
    runState.carry = 0;
    if (!runState.starved && !listFaults().length) {
      runState.starved = true;
      setTicker("进料区空了，补料后继续");
    }
  } else if (runState.starved && raw > 0) {
    runState.starved = false;
    if (!listFaults().length) setTicker("产线运行中，点「停下」结束");
  }
  if (attempted > 0) {
    const rate = scrapRate();
    const scrap = attempted * rate;
    const good = attempted - scrap;
    const took = takeInbound(attempted * runState.consume);
    runState.rawBill += took.bill;
    addOutbound(good);
    runState.yield += good;
    runState.scrap += scrap;
  }
  const wages = liveWages(elapsed / 1000);
  const price = cratePrice((layout.outbound.items || [])[0]) || Number(layout.price) || 18;
  const yieldEl = document.getElementById("m-yield");
  if (yieldEl) yieldEl.textContent = Math.round(runState.yield) + " 件";
  document.getElementById("m-wage").textContent = wages;
  document.getElementById("m-rev").textContent = Math.round(runState.yield * price - wages - runState.rawBill);
  const guess = estimateLine();
  document.getElementById("m-eff").textContent = Math.round((guess.efficiency || 0) * 100) + "%";
  paintLiveBays();
  runRaf = requestAnimationFrame(tickRun);
}

function abortRun() {
  if (runRaf) cancelAnimationFrame(runRaf);
  runRaf = 0;
  runState = null;
  haltLine();
  setRunButton(false);
}

async function stopRun() {
  if (!runState) {
    abortRun();
    return;
  }
  const elapsed = (performance.now() - runState.t0) / 1000;
  const st = runState;
  if (runRaf) cancelAnimationFrame(runRaf);
  runRaf = 0;
  runState = null;
  haltLine();
  setRunButton(false);
  setTicker("");
  const price = cratePrice((layout.outbound.items || [])[0]) || Number(layout.price) || 18;
  const wages = liveWages(elapsed);
  const yieldN = Math.round(st.yield);
  const scrapN = Math.round(st.scrap);
  const revenue = Math.round(yieldN * price - wages - st.rawBill);
  const guess = estimateLine();
  const result = {
    yield: yieldN,
    revenue,
    wages,
    production: {
      workers: countCrew(),
      bottleneck: guess.bottleneck,
      isolated: guess.isolated,
      efficiency: Math.round((guess.efficiency || 0) * 100)
    },
    floor: {
      scrap: scrapN,
      wages,
      new_broken: listFaults().map((row) => row.machine.id)
    }
  };
  lastResult = result;
  document.getElementById("m-yield").textContent = yieldN + " 件";
  document.getElementById("m-rev").textContent = revenue;
  document.getElementById("m-wage").textContent = wages;
  document.getElementById("m-eff").textContent = result.production.efficiency + "%";
  document.getElementById("m-eff").dataset.locked = "1";
  document.getElementById("m-note").textContent = yieldN ? ("这次做出 " + yieldN + " 件。") : "没出货。把线接上或补料后再开。";
  if (typeof renderLab === "function") renderLab(result);
  paintLiveBays();
  paintFaultBar();
  try {
    readControls();
    await api("/api/layout", "POST", layout);
  } catch (err) {}
}

function toggleRun() {
  if (runState) stopRun();
  else startRun();
}

function markBroken(id, flag) {
  (layout.shops || []).forEach((s) => {
    (s.machines || []).forEach((m) => {
      if (m.id === id) m.broken = flag;
    });
  });
}

function idleBeltsOf(id) {
  if (!id) return;
  document.querySelectorAll("path.belt-slat").forEach((path) => {
    if (path.getAttribute("data-from") !== id && path.getAttribute("data-to") !== id) return;
    path.classList.add("idle");
    const d = path.getAttribute("d");
    const svg = path.closest("svg");
    if (!svg || !d) return;
    svg.querySelectorAll("circle.belt-pkg").forEach((pkg) => {
      const motion = pkg.querySelector("animateMotion");
      if (motion && motion.getAttribute("path") === d) pkg.remove();
    });
  });
  document.querySelectorAll(".belt-label").forEach((el) => {
    if (el.dataset.from !== id && el.dataset.to !== id) return;
    el.classList.add("idle");
    el.textContent = "0 件/分";
  });
}

function paintMachineHit(id) {
  const hit = findMachine(id);
  if (!hit || !hit.machine) return;
  const el = stage.querySelector(`.piece.machine[data-id="${id}"]`);
  if (el) {
    el.classList.add("down");
    if (monitorFocus === id) el.classList.add("watch");
    const badge = el.querySelector(".badge");
    if (badge) {
      badge.className = "badge down";
      badge.textContent = "故障";
    }
    if (!el.querySelector(".fix")) {
      const btn = document.createElement("button");
      btn.className = "fix";
      btn.type = "button";
      btn.textContent = "修好";
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        repairMachine(hit.machine);
      });
      const crew = el.querySelector(".crew");
      if (crew) crew.replaceWith(btn);
      else el.appendChild(btn);
    }
  }
  if (view.mode === "factory") {
    const shopEl = stage.querySelector(`.piece.shop[data-id="${hit.shop.id}"]`);
    if (shopEl) {
      shopEl.classList.add("down");
      const enter = shopEl.querySelector(".enter");
      if (enter) enter.textContent = "去监控";
      if (!shopEl.querySelector(".fault-flag")) {
        const flag = document.createElement("div");
        flag.className = "fault-flag";
        flag.textContent = "1台故障";
        if (enter) enter.before(flag);
        else shopEl.appendChild(flag);
      }
    }
  }
  idleBeltsOf(id);
  drawMonitorHud(currentShop());
}

function bind() {
  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
  backBtn.onclick = () => {
    view = { mode: "factory" };
    selected = null;
    renderPalette();
    renderStage();
  };
  document.getElementById("run").onclick = toggleRun;
  document.getElementById("save").onclick = async () => {
    readControls();
    await api("/api/layout", "POST", layout);
    document.getElementById("m-note").textContent = "布局已保存。";
  };
  document.getElementById("reset").onclick = async () => {
    abortRun();
    layout = await api("/api/reset", "POST", {});
    window.layout = layout;
    lastResult = null;
    replayFocus = null;
    monitorFocus = null;
    writeControls();
    view = { mode: "factory" };
    selected = null;
    delete document.getElementById("m-eff").dataset.locked;
    setTicker("");
    haltLine();
    setRunButton(false);
    renderPalette();
    renderStage();
    if (typeof clearLab === "function") clearLab();
    document.getElementById("m-note").textContent = "已恢复示例工厂。";
  };
  document.getElementById("price").addEventListener("input", () => {
    layout.price = Number(document.getElementById("price").value) || 18;
    ensureBays();
    (layout.outbound.items || []).forEach((it) => { it.price = layout.price; });
    delete document.getElementById("m-eff").dataset.locked;
    drawBays();
    refreshBoard();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Delete" || e.key === "Backspace") {
      if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || e.target.tagName === "TEXTAREA")) return;
      deleteSelected();
    }
  });
}

async function boot() {
  try {
    layout = await api("/api/layout");
  } catch (err) {
    // 服务没起来时以前是未捕获的 Promise 拒绝：页面一片空白，控制台才有线索
    const note = document.getElementById("m-note");
    if (note) {
      note.textContent = "连不上后端（" + err.message + "）。请确认 `python -m brickforge` 还在运行，然后刷新页面。";
    }
    setTicker("后端未连接");
    return;
  }
  window.layout = layout;
  writeControls();
  renderPalette();
  renderStage();
  bind();
}

boot();
