// Graphique linéaire SVG (sans dépendance). Les valeurs null créent des ruptures de ligne (donnée indisponible).
const NS = "http://www.w3.org/2000/svg";
const W = 640, H = 170, PL = 42, PR = 10, PT = 10, PB = 22;

function node(tag, attrs = {}, text) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}
export function lineChart(container, { points, color = "#3b82ff", unit = "", min = 0, max = null, integer = false, label = "" }) {
  container.replaceChildren();
  const valid = points.filter((p) => p.v !== null && p.v !== undefined);
  if (points.length < 2 || !valid.length) {
    const d = document.createElement("div"); d.className = "chart-empty";
    d.textContent = points.length < 2 ? "Pas encore assez de données" : "Serveur arrêté : données indisponibles";
    container.append(d); return;
  }
  const vmax0 = Math.max(...valid.map((p) => p.v));
  let ymax = max ?? Math.max(integer ? 2 : 1, vmax0 * 1.15);
  if (integer) ymax = Math.max(2, Math.ceil(ymax));
  const t0 = points[0].t, t1 = points[points.length - 1].t, span = Math.max(1, t1 - t0);
  const x = (t) => PL + ((t - t0) / span) * (W - PL - PR);
  const y = (v) => PT + (1 - (v - min) / (ymax - min)) * (H - PT - PB);
  const svg = node("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": label });
  const gid = "g" + Math.random().toString(36).slice(2, 8);
  const defs = node("defs"); const grad = node("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(node("stop", { offset: "0%", "stop-color": color }), node("stop", { offset: "100%", "stop-color": color, "stop-opacity": 0 }));
  defs.append(grad); svg.append(defs);
  for (let i = 0; i <= 3; i++) {
    const v = min + ((ymax - min) * i) / 3, yy = y(v);
    svg.append(node("line", { x1: PL, x2: W - PR, y1: yy, y2: yy, class: "grid-l" }));
    svg.append(node("text", { x: PL - 6, y: yy + 3, "text-anchor": "end", class: "axis" }, (integer ? Math.round(v) : v >= 100 ? Math.round(v) : v.toFixed(1)) + (i === 3 ? unit : "")));
  }
  const fmt = (t) => new Date(t).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  svg.append(node("text", { x: PL, y: H - 5, class: "axis" }, fmt(t0)), node("text", { x: W - PR, y: H - 5, "text-anchor": "end", class: "axis" }, fmt(t1)));
  // segments continus
  let seg = [];
  const flush = () => {
    if (seg.length > 1) {
      const d = seg.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
      svg.append(node("path", { d: `${d}L${x(seg[seg.length - 1].t).toFixed(1)},${y(min)}L${x(seg[0].t).toFixed(1)},${y(min)}Z`, fill: `url(#${gid})`, class: "area" }));
      svg.append(node("path", { d, stroke: color, class: "ln" }));
    } else if (seg.length === 1) svg.append(node("circle", { cx: x(seg[0].t), cy: y(seg[0].v), r: 2.5, fill: color }));
    seg = [];
  };
  for (const p of points) { if (p.v === null || p.v === undefined) flush(); else seg.push(p); }
  flush();
  // survol
  const cursor = node("line", { y1: PT, y2: H - PB, stroke: color, "stroke-dasharray": "3 3", opacity: 0 });
  const tip = node("text", { class: "axis", y: PT + 10, fill: "currentColor", opacity: 0 });
  tip.style.fill = "var(--text)";
  const hit = node("rect", { x: PL, y: PT, width: W - PL - PR, height: H - PT - PB, fill: "transparent" });
  hit.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect(); const px = ((e.clientX - r.left) / r.width) * W;
    const t = t0 + ((px - PL) / (W - PL - PR)) * span;
    let best = null; for (const p of points) if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    if (!best) return;
    cursor.setAttribute("x1", x(best.t)); cursor.setAttribute("x2", x(best.t)); cursor.setAttribute("opacity", 1);
    tip.textContent = `${fmt(best.t)}  ${best.v === null || best.v === undefined ? "indisponible" : (integer ? best.v : best.v.toFixed(1)) + unit}`;
    const right = x(best.t) > W * 0.6; tip.setAttribute("x", right ? x(best.t) - 6 : x(best.t) + 6); tip.setAttribute("text-anchor", right ? "end" : "start"); tip.setAttribute("opacity", 1);
  });
  hit.addEventListener("mouseleave", () => { cursor.setAttribute("opacity", 0); tip.setAttribute("opacity", 0); });
  svg.append(cursor, tip, hit);
  container.append(svg);
}
