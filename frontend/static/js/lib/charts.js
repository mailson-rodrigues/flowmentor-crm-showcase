// Lightweight SVG charts (no dependencies, compatible with a strict CSP).
import { h, svg } from "./dom.js";

export const COLORS = ["#0EA5E9", "#14B8A6", "#6366F1", "#F59E0B", "#EC4899", "#84CC16", "#F97316", "#8B5CF6"];
const nice = (max) => { if (max <= 0) return 1; const p = 10 ** Math.floor(Math.log10(max)); const n = max / p; return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p; };

/** series: [{name,color,values:[]}], labels: [] — barras agrupadas ou empilhadas */
export function barChart({ labels, series, height = 220, stacked = false, fmt = (v) => v }) {
  const W = 640, H = height, pad = { l: 38, r: 10, t: 10, b: 26 };
  const el = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img" });
  const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const max = nice(stacked ? Math.max(...totals, 1) : Math.max(...series.flatMap((s) => s.values), 1));
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ih - (ih * i) / 4;
    el.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: "grid-line" }), svg("text", { x: pad.l - 6, y: y + 3, "text-anchor": "end" }, fmt((max * i) / 4)));
  }
  const gw = iw / Math.max(labels.length, 1), bw = stacked ? Math.min(gw * 0.6, 34) : Math.min((gw * 0.7) / series.length, 22);
  labels.forEach((lb, i) => {
    const x0 = pad.l + gw * i + gw / 2;
    if (!(labels.length > 14 && i % Math.ceil(labels.length / 12))) el.append(svg("text", { x: x0, y: H - 8, "text-anchor": "middle" }, lb));
    let acc = 0;
    series.forEach((s, k) => {
      const v = s.values[i] || 0, bh = (v / max) * ih;
      const x = stacked ? x0 - bw / 2 : x0 - (bw * series.length) / 2 + k * bw, y = stacked ? pad.t + ih - ((acc + v) / max) * ih : pad.t + ih - bh;
      acc += v;
      if (v > 0) el.append(svg("rect", { x, y, width: Math.max(bw - 2, 2), height: bh, rx: 3, fill: s.color || COLORS[k % COLORS.length] }, svg("title", {}, `${s.name}: ${fmt(v)} (${lb})`)));
    });
  });
  return wrap(el, series);
}

export function lineChart({ labels, series, height = 220, fmt = (v) => v, area = true }) {
  const W = 640, H = height, pad = { l: 38, r: 12, t: 12, b: 26 };
  const el = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img" });
  const max = nice(Math.max(...series.flatMap((s) => s.values.map((v) => v || 0)), 1));
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ih - (ih * i) / 4;
    el.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: "grid-line" }), svg("text", { x: pad.l - 6, y: y + 3, "text-anchor": "end" }, fmt((max * i) / 4)));
  }
  const px = (i) => pad.l + (labels.length > 1 ? (iw * i) / (labels.length - 1) : iw / 2), py = (v) => pad.t + ih - ((v || 0) / max) * ih;
  labels.forEach((lb, i) => { if (!(labels.length > 12 && i % Math.ceil(labels.length / 10))) el.append(svg("text", { x: px(i), y: H - 8, "text-anchor": "middle" }, lb)); });
  series.forEach((s, k) => {
    const c = s.color || COLORS[k % COLORS.length];
    const pts = s.values.map((v, i) => `${px(i)},${py(v)}`);
    if (area && pts.length > 1) el.append(svg("polygon", { points: `${px(0)},${pad.t + ih} ${pts.join(" ")} ${px(labels.length - 1)},${pad.t + ih}`, fill: c, opacity: ".12" }));
    el.append(svg("polyline", { points: pts.join(" "), fill: "none", stroke: c, "stroke-width": 2.2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    s.values.forEach((v, i) => el.append(svg("circle", { cx: px(i), cy: py(v), r: 3, fill: c }, svg("title", {}, `${s.name}: ${fmt(v)} (${labels[i]})`))));
  });
  return wrap(el, series);
}

export function donut({ items, size = 150, center }) {
  const total = items.reduce((a, i) => a + i.value, 0) || 1, r = 52, c = 2 * Math.PI * r;
  const el = svg("svg", { viewBox: "0 0 140 140", width: size, height: size, role: "img" });
  el.append(svg("circle", { cx: 70, cy: 70, r, fill: "none", stroke: "var(--surface-3)", "stroke-width": 16 }));
  let off = 0;
  items.forEach((it, k) => {
    const len = (it.value / total) * c;
    if (len > 0) el.append(svg("circle", { cx: 70, cy: 70, r, fill: "none", stroke: it.color || COLORS[k % COLORS.length], "stroke-width": 16, "stroke-dasharray": `${len} ${c - len}`, "stroke-dashoffset": -off, transform: "rotate(-90 70 70)" }, svg("title", {}, `${it.label}: ${it.value}`)));
    off += len;
  });
  if (center) el.append(svg("text", { x: 70, y: 75, "text-anchor": "middle", "font-size": 20, "font-weight": 700, class: "chart-strong" }, center));
  const legend = h("div", { class: "legend", style: { flexDirection: "column", gap: "6px" } }, items.map((it, k) => h("div", h("i", { style: { background: it.color || COLORS[k % COLORS.length] } }), `${it.label}: `, h("b", String(it.value)))));
  return h("div", { class: "row gap-16" }, el, legend);
}

export function hbars({ items, fmt = (v) => v, color = "var(--cyan-500)" }) {
  const max = Math.max(...items.map((i) => i.value), 1);
  return h("div", items.map((it) => h("div", { class: "funnel-row" }, h("div", { class: "truncate", title: it.label }, it.label),
    h("div", { class: "bar" }, h("i", { style: { width: `${(it.value / max) * 100}%`, background: it.color || color } })), h("div", { class: "right strong" }, fmt(it.value)))));
}

export function gauge(value, { min = 0, max = 100, label = "", color = "var(--mint-500)" } = {}) {
  const pct = Math.max(0, Math.min(1, (value - min) / (max - min || 1)));
  const el = svg("svg", { viewBox: "0 0 120 70", width: 140, role: "img" });
  const arc = (p) => { const a = Math.PI * (1 - p), x = 60 + 48 * Math.cos(a), y = 62 - 48 * Math.sin(a); return `M12 62 A48 48 0 ${p > 0.5 ? 1 : 0} 1 ${x} ${y}`; };
  el.append(svg("path", { d: "M12 62 A48 48 0 0 1 108 62", fill: "none", stroke: "var(--surface-3)", "stroke-width": 10, "stroke-linecap": "round" }),
    svg("path", { d: arc(pct), fill: "none", stroke: color, "stroke-width": 10, "stroke-linecap": "round" }),
    svg("text", { x: 60, y: 56, "text-anchor": "middle", "font-size": 17, "font-weight": 700, class: "chart-strong" }, label || String(value)));
  return el;
}

function wrap(el, series) {
  return h("div", el, h("div", { class: "legend mt-8" }, series.map((s, k) => h("span", h("i", { style: { background: s.color || COLORS[k % COLORS.length] } }), s.name))));
}
