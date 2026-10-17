// Component kit: toast, modal, drawer, forms, tables, tabs, menus.
import { h, icon, mount, clear } from "./dom.js";

// ---------- Toasts ----------
let toastBox;
export function toast(message, kind = "info", title) {
  toastBox ??= document.body.appendChild(h("div", { class: "toasts", role: "status", "aria-live": "polite" }));
  const t = h("div", { class: `toast ${kind}` }, h("div", { class: "grow" }, title ? h("b", title) : null, message));
  toastBox.append(t);
  setTimeout(() => t.remove(), kind === "error" ? 6500 : 4000);
}
export const toastError = (e) => toast(e?.message || String(e), "error");

// ---------- Modal ----------
export function openModal({ title, body, footer, size = "", onClose, dismissible = true }) {
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey); onClose?.(); };
  const onKey = (e) => { if (e.key === "Escape" && dismissible) close(); };
  const modal = h("div", { class: `modal ${size}`, role: "dialog", "aria-modal": "true" },
    h("div", { class: "modal-head" }, h("h2", title), dismissible ? h("button", { class: "btn ghost icon sm", onClick: close, "aria-label": "Fechar" }, icon("x")) : null),
    h("div", { class: "modal-body" }, body),
    footer ? h("div", { class: "modal-foot" }, footer) : null);
  const back = h("div", { class: "backdrop", onMousedown: (e) => { if (e.target === back && dismissible) close(); } }, modal);
  document.body.append(back);
  document.addEventListener("keydown", onKey);
  setTimeout(() => modal.querySelector("input,select,textarea")?.focus(), 30);
  return { close, el: modal };
}

export function openDrawer({ title, subtitle, body, actions, onClose }) {
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey); onClose?.(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const drawer = h("aside", { class: "drawer" },
    h("div", { class: "modal-head" }, h("div", h("h2", title), subtitle ? h("div", { class: "muted" }, subtitle) : null),
      h("div", { class: "row" }, actions, h("button", { class: "btn ghost icon sm", onClick: close, "aria-label": "Fechar" }, icon("x")))),
    h("div", { class: "drawer-body" }, body));
  const back = h("div", { class: "drawer-back", onMousedown: (e) => { if (e.target === back) close(); } }, drawer);
  document.body.append(back);
  document.addEventListener("keydown", onKey);
  return { close, el: drawer, body: drawer.querySelector(".drawer-body") };
}

export function confirmDialog({ title = "Confirmar", message, confirmLabel = "Confirmar", danger = false }) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; m.close(); resolve(v); } };
    const m = openModal({
      title, body: h("p", message), onClose: () => finish(false),
      footer: [h("button", { class: "btn", onClick: () => finish(false) }, "Cancelar"), h("button", { class: `btn ${danger ? "danger" : "primary"}`, onClick: () => finish(true) }, confirmLabel)],
    });
  });
}

export function promptDialog({ title, label, placeholder = "", multiline = true, confirmLabel = "Enviar", required = false }) {
  return new Promise((resolve) => {
    const input = multiline ? h("textarea", { class: "textarea", placeholder }) : h("input", { class: "input", placeholder });
    let done = false;
    const finish = (v) => { if (!done) { done = true; m.close(); resolve(v); } };
    const m = openModal({
      title, onClose: () => finish(null), body: h("div", { class: "field" }, h("label", label), input),
      footer: [h("button", { class: "btn", onClick: () => finish(null) }, "Cancelar"),
        h("button", { class: "btn primary", onClick: () => { if (required && !input.value.trim()) return input.focus(); finish(input.value.trim()); } }, confirmLabel)],
    });
  });
}

// ---------- Forms ----------
let fieldSeq = 0;
export function form(fields, values = {}) {
  const inputs = {};
  const el = h("div", { class: "form-grid" });
  for (const f of fields) {
    let input;
    const val = values[f.name] ?? f.value ?? (f.type === "checkbox" ? false : "");
    if (f.type === "select") {
      input = h("select", { class: "select", name: f.name }, (f.options || []).map((o) => h("option", { value: o.value, selected: String(o.value) === String(val) }, o.label)));
    } else if (f.type === "textarea") {
      input = h("textarea", { class: "textarea", name: f.name, placeholder: f.placeholder || "", rows: f.rows || 3 }, val || "");
    } else if (f.type === "checkbox") {
      input = h("input", { type: "checkbox", name: f.name, checked: !!val });
    } else {
      input = h("input", { class: "input", type: f.type || "text", name: f.name, value: val ?? "", placeholder: f.placeholder || "", min: f.min, max: f.max, step: f.step,
        required: f.required, autocomplete: f.autocomplete || "off", maxlength: f.maxlength });
    }
    inputs[f.name] = input;
    input.id = `f-${f.name}-${++fieldSeq}`;  // associa <label for> ao controle (leitores de tela)
    if (f.type === "checkbox") {
      el.append(h("div", { class: `field ${f.full ? "full" : ""}` }, h("label", { class: "check" }, input, f.label), f.hint ? h("div", { class: "hint" }, f.hint) : null));
    } else {
      el.append(h("div", { class: `field ${f.full ? "full" : ""}` }, h("label", { for: input.id }, f.label, f.required ? " *" : ""), input, f.hint ? h("div", { class: "hint" }, f.hint) : null));
    }
  }
  const get = () => {
    const out = {};
    for (const f of fields) {
      const i = inputs[f.name];
      let v = f.type === "checkbox" ? i.checked : i.value;
      if (f.type === "number") v = v === "" ? null : Number(v);
      else if (typeof v === "string") { v = v.trim(); if (v === "" && f.nullable !== false && f.type !== "select") v = null; if (f.type === "select" && v === "") v = null; }
      if (f.split && typeof v === "string") v = v.split(",").map((s) => s.trim()).filter(Boolean);
      else if (f.split && v === null) v = [];
      out[f.name] = v;
    }
    return out;
  };
  return { el, get, inputs };
}

export function formModal({ title, fields, values, submitLabel = "Salvar", size = "", onSubmit, extra }) {
  const f = form(fields, values);
  let saving = false;
  const btn = h("button", { class: "btn primary", onClick: async () => {
    if (saving) return;
    for (const fl of fields) if (fl.required && (f.get()[fl.name] === null || f.get()[fl.name] === "")) { toast(`Preencha: ${fl.label}`, "warn"); f.inputs[fl.name].focus(); return; }
    saving = true; btn.disabled = true;
    try { const r = await onSubmit(f.get()); m.close(); return r; }
    catch (e) { toastError(e); } finally { saving = false; btn.disabled = false; }
  } }, submitLabel);
  const m = openModal({ title, size, body: [f.el, extra], footer: [h("button", { class: "btn", onClick: () => m.close() }, "Cancelar"), btn] });
  return m;
}

// ---------- Tabelas / cards / badges ----------
export function dataTable({ columns, rows, onRowClick, empty = "Nenhum registro encontrado", dense = false }) {
  if (!rows?.length) return emptyState(empty);
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", h("tr", columns.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)))),
    h("tbody", rows.map((r) => h("tr", { class: onRowClick ? "clickable" : "", onClick: onRowClick ? () => onRowClick(r) : null },
      columns.map((c) => h("td", { class: `${c.num ? "num" : ""} ${c.cls || ""}` }, c.render ? c.render(r) : r[c.key] ?? "—")))))));
}

export const badge = (text, kind = "") => h("span", { class: `badge ${kind}` }, text);
export const emptyState = (msg, ic = "box") => h("div", { class: "empty" }, icon(ic), h("div", msg));
export const spinner = () => h("div", { class: "spinner", role: "progressbar" });
export const card = (title, body, { actions, tight } = {}) => h("div", { class: "card" }, title ? h("div", { class: "card-head" }, h("h3", title), h("div", { class: "row" }, actions)) : null, h("div", { class: `card-body ${tight ? "tight" : ""}` }, body));
export function kpi(label, value, sub, ic = "chart", kind = "") {
  return h("div", { class: `card kpi ${kind}` }, h("div", { class: "icon-bg" }, icon(ic)), h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null);
}
export const progress = (pct, kind = "") => h("div", { class: `progress ${kind}` }, h("i", { style: { width: `${Math.max(0, Math.min(100, pct || 0))}%` } }));

export function tabs(items, active, onChange) {
  const el = h("div", { class: "tabs", role: "tablist" });
  const draw = (a) => mount(el, items.map((t) => h("button", { class: `tab ${t.id === a ? "active" : ""}`, role: "tab", onClick: () => { draw(t.id); onChange(t.id); } }, t.label)));
  draw(active);
  return el;
}
export function segmented(items, active, onChange) {
  const el = h("div", { class: "seg" });
  const draw = (a) => mount(el, items.map((t) => h("button", { class: t.id === a ? "active" : "", onClick: () => { draw(t.id); onChange(t.id); } }, t.label)));
  draw(active);
  return el;
}

export function popMenu(anchor, items) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  const r = anchor.getBoundingClientRect();
  const m = h("div", { class: "menu", style: { top: `${r.bottom + 4}px`, left: `${Math.max(8, Math.min(r.left, innerWidth - 220))}px` } },
    items.filter(Boolean).map((i) => h("button", { onClick: () => { m.remove(); i.onClick(); } }, i.icon ? icon(i.icon) : null, i.label)));
  document.body.append(m);
  const off = (e) => { if (!m.contains(e.target)) { m.remove(); document.removeEventListener("mousedown", off); } };
  setTimeout(() => document.addEventListener("mousedown", off), 0);
}

export function pageHead(title, subtitle, ...actions) {
  return h("div", { class: "page-head" }, h("div", h("h1", title), subtitle ? h("p", subtitle) : null), h("div", { class: "row wrap" }, actions));
}

export const searchBox = (placeholder, onInput, value = "") => {
  const i = h("input", { class: "input search", type: "search", placeholder, value });
  let t; i.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => onInput(i.value.trim()), 280); });
  return h("div", { class: "search-wrap" }, icon("search"), i);
};

export async function withLoading(container, fn) {
  mount(container, spinner());
  try { return await fn(); }
  catch (e) { mount(container, h("div", { class: "callout warn" }, e.message || "Falha ao carregar")); throw e; }
}
export { clear };
