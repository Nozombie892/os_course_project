/** 生产结算：工厂话。 */

// 瓶颈名来自用户给车间/机器起的名字，拼进 innerHTML 前必须转义
function escLab(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function renderLab(result) {
  const box = document.getElementById("shift-report");
  if (!box) return;
  const prod = result.production || {};
  const floor = result.floor || {};
  const bits = [];
  bits.push(`<p><b>良品 ${result.yield} 件</b> · 次品 ${floor.scrap || 0} · 利润 ${result.revenue}</p>`);
  bits.push(`<p>工资 ${floor.wages || result.wages || 0}（${prod.workers || 0} 人 × 90）。人多更快，也更贵。</p>`);
  if (prod.bottleneck) bits.push(`<p>卡线在「${escLab(prod.bottleneck)}」。</p>`);
  if ((floor.new_broken || []).length) bits.push("<p>有机器过热停机。点顶部红色故障条「去监控」跳到那台机器，修好才能继续生产。</p>");
  if (!result.yield) bits.push("<p>流水线没接通或工位空岗，这次没有成品出货。</p>");
  if (prod.isolated) bits.push(`<p>${prod.isolated} 台机器没接传送带。</p>`);
  const idle = floorAlerts(window.layout).filter((a) => a.kind === "idle").length;
  if (idle) bits.push(`<p>${idle} 台空岗。</p>`);
  box.innerHTML = bits.join("");
}

function clearLab() {
  const box = document.getElementById("shift-report");
  if (box) box.innerHTML = "<p class=\"muted\">点「停下」后出结算：良品、次品、卡线、工资。</p>";
}

function shopsReady(layout) {
  if (!layout || !layout.shops || !layout.shops.length) return false;
  return layout.shops.some((s) => (s.machines || []).length);
}

function floorAlerts(layout) {
  const alerts = [];
  if (!layout) return alerts;
  (layout.shops || []).forEach((shop) => {
    const fed = (layout.feeds || []).some((f) => f.to === shop.id) || (layout.links || []).some((l) => l.to === shop.id);
    const shipped = (layout.ships || []).some((s) => s.from === shop.id) || (layout.links || []).some((l) => l.from === shop.id);
    if (!fed) alerts.push({ kind: "cut", text: `「${shop.name || "车间"}」没接进料` });
    if (!shipped) alerts.push({ kind: "cut", text: `「${shop.name || "车间"}」没接出货` });
    (shop.machines || []).forEach((m) => {
      const staff = Math.min(m.workers || 0, 3);
      const wired = (shop.wires || []).some((w) => w.from === m.id || w.to === m.id);
      if (m.broken) alerts.push({
        kind: "down",
        text: `「${shop.name || "车间"}」的「${m.name || "机器"}」故障`,
        shopId: shop.id,
        machineId: m.id
      });
      else if (!staff) alerts.push({ kind: "idle", text: `「${m.name || "机器"}」空岗` });
      else if (!wired) alerts.push({ kind: "cut", text: `「${m.name || "机器"}」没接传送带` });
    });
  });
  return alerts;
}
