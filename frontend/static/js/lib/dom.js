// Micro-biblioteca DOM (sem innerHTML → sem XSS por construção) + ícones + formatação pt-BR.

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props !== undefined && props !== null && (typeof props !== "object" || props instanceof Node || Array.isArray(props))) { children.unshift(props); props = null; }
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected" || k === "required" || k === "readOnly") el[k] = !!v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function mount(el, ...children) { clear(el); append(el, children); return el; }

const NS = "http://www.w3.org/2000/svg";
export function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, v);
  for (const c of children.flat()) if (c) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

// Ícones no estilo "feather/lucide" (traços de 24x24)
const ICONS = {
  dashboard: "M3 3h7v9H3z M14 3h7v5h-7z M14 12h7v9h-7z M3 16h7v5H3z",
  pipeline: "M3 4h5v16H3z M10 4h5v10h-5z M17 4h4v6h-4z",
  building: "M4 21V5a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v16 M14 9h5a1 1 0 0 1 1 1v11 M8 8h2 M8 12h2 M8 16h2 M3 21h18",
  users: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M23 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75",
  user: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8",
  box: "M21 8l-9-5-9 5v8l9 5 9-5z M3 8l9 5 9-5 M12 13v8",
  flask: "M9 3h6 M10 3v6L4.5 19a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9V3",
  file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z M14 3v5h5 M9 13h6 M9 17h6",
  chat: "M21 11.5a8.4 8.4 0 0 1-9 8.4 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.2A8.4 8.4 0 1 1 21 11.5z",
  eye: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12z M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6",
  template: "M3 3h18v6H3z M3 13h8v8H3z M15 13h6 M15 17h6 M15 21h6",
  chart: "M3 3v18h18 M7 14l4-4 3 3 5-6",
  webhook: "M18 16.98h-5.99c-1.1 0-1.95.94-2.48 1.9A4 4 0 0 1 2 17c.01-.7.2-1.4.57-2 M6 17.17l3.13-5.78c.53-.97.43-2.22-.26-3.07a4 4 0 1 1 6.52-.8 M12 6l3.13 5.73c.53.97 1.58 1.4 2.66 1.2A4 4 0 1 1 22 17",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M9 12l2 2 4-4",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6 M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z",
  plus: "M12 5v14 M5 12h14", x: "M18 6L6 18 M6 6l12 12", check: "M20 6L9 17l-5-5", search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16 M21 21l-4.35-4.35",
  send: "M22 2L11 13 M22 2l-7 20-4-9-9-4z", paperclip: "M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20 M12 6v6l4 2", alert: "M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z M12 9v4 M12 17h.01",
  trash: "M3 6h18 M8 6V4h8v2 M19 6l-1 14H6L5 6", edit: "M12 20h9 M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z", more: "M12 12h.01 M19 12h.01 M5 12h.01",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z", sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10 M12 1v2 M12 21v2 M4.2 4.2l1.4 1.4 M18.4 18.4l1.4 1.4 M1 12h2 M21 12h2 M4.2 19.8l1.4-1.4 M18.4 5.6l1.4-1.4",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4 M16 17l5-5-5-5 M21 12H9", menu: "M3 12h18 M3 6h18 M3 18h18", chevron: "M9 18l6-6-6-6",
  link: "M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7 M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7",
  dollar: "M12 1v22 M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6", target: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20 M12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12 M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4",
  repeat: "M17 1l4 4-4 4 M3 11V9a4 4 0 0 1 4-4h14 M7 23l-4-4 4-4 M21 13v2a4 4 0 0 1-4 4H3", download: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  star: "M12 2l3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z", zap: "M13 2L3 14h9l-1 8 10-12h-9z", lock: "M5 11h14v10H5z M8 11V7a4 4 0 0 1 8 0v4",
  note: "M4 4h16v12l-4 4H4z M16 20v-4h4 M8 9h8 M8 13h5", phone: "M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z",
  refresh: "M23 4v6h-6 M1 20v-6h6 M3.5 9a9 9 0 0 1 14.9-3.4L23 10 M1 14l4.6 4.4A9 9 0 0 0 20.5 15", key: "M21 2l-2 2 M15.5 7.5L19 4 M12 11a5 5 0 1 1-1-9 5 5 0 0 1 1 9z M12 11l-9 9v3h3l1-1v-2h2v-2h2l1-1",
  whisper: "M2 12a10 10 0 0 1 20 0 M6 12a6 6 0 0 1 12 0 M10 12a2 2 0 0 1 4 0 M12 14v8", bot: "M12 8V4H8 M4 8h16v12H4z M2 14h2 M20 14h2 M15 13v2 M9 13v2",
};
export function icon(name, cls = "") {
  const el = svg("svg", { viewBox: "0 0 24 24", class: ("i " + cls).trim(), "aria-hidden": "true", "data-icon": name });
  for (const d of (ICONS[name] || ICONS.file).split(" M").map((p, i) => (i ? "M" + p : p))) el.append(svg("path", { d }));
  return el;
}

// ---------- Formatação pt-BR ----------
const BRL = new Intl.NumberFormat("pt-BR", { style: "currency", currency: "BRL" });
export const money = (v) => (v === null || v === undefined ? "—" : BRL.format(Number(v)));
export const moneyShort = (v) => { v = Number(v || 0); return Math.abs(v) >= 1e6 ? `R$ ${(v / 1e6).toFixed(1)} mi` : Math.abs(v) >= 1e3 ? `R$ ${(v / 1e3).toFixed(1)} mil` : BRL.format(v); };
export const num = (v, d = 0) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d }));
export const pct = (v, d = 1) => (v === null || v === undefined ? "—" : `${num(v, d)}%`);
export const date = (s) => (s ? new Date(s.length === 10 ? s + "T00:00:00" : s).toLocaleDateString("pt-BR") : "—");
export const dateTime = (s) => (s ? new Date(s).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : "—");
export const timeOnly = (s) => (s ? new Date(s).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" }) : "");
export function dur(sec) {
  if (sec === null || sec === undefined) return "—";
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}min ${sec % 60 ? (sec % 60) + "s" : ""}`.trim();
  const hh = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return hh >= 48 ? `${Math.floor(hh / 24)}d ${hh % 24}h` : `${hh}h ${m ? m + "min" : ""}`.trim();
}
export function ago(s) {
  if (!s) return "";
  const d = (Date.now() - new Date(s).getTime()) / 1000;
  if (d < 45) return "agora";
  if (d < 3600) return `${Math.floor(d / 60)} min`;
  if (d < 86400) return `${Math.floor(d / 3600)} h`;
  if (d < 7 * 86400) return `${Math.floor(d / 86400)} d`;
  return date(s);
}
export const initials = (n) => (n || "?").replace(/^(Dr\.?|Dra\.?|Prof\.?|Profa\.?)\s+/i, "").split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0]).join("").toUpperCase();
const PALETTE = ["#0284C7", "#0D9488", "#7C3AED", "#DB2777", "#EA580C", "#4F46E5", "#0891B2", "#65A30D"];
export function colorFor(s) { let x = 0; for (const c of String(s || "")) x = (x * 31 + c.charCodeAt(0)) >>> 0; return PALETTE[x % PALETTE.length]; }
export const avatar = (name, cls = "") => h("div", { class: ("avatar " + cls).trim(), style: { background: colorFor(name) }, title: name || "" }, initials(name));
/** Formatação estilo WhatsApp (*negrito*, _itálico_, ~tachado~, links https) construída via DOM — sem innerHTML. */
export function rich(text) {
  const out = [];
  const re = /(\*[^*\n]+\*)|(_[^_\n]+_)|(~[^~\n]+~)|(https:\/\/[^\s<>"']+)/g;
  let last = 0, m;
  const s = String(text ?? "");
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    const tok = m[0];
    if (m[1]) out.push(h("b", tok.slice(1, -1)));
    else if (m[2]) out.push(h("i", tok.slice(1, -1)));
    else if (m[3]) out.push(h("s", tok.slice(1, -1)));
    else out.push(h("a", { href: tok, target: "_blank", rel: "noopener noreferrer" }, tok));
    last = m.index + tok.length;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}
export const debounce = (fn, ms = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
