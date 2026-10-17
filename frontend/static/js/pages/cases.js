import { api } from "../lib/api.js";
import { h, icon, mount, num, date, dateTime, pct, ago } from "../lib/dom.js";
import { can, CASE_TYPE_LABEL, CASE_STATUS_LABEL } from "../lib/store.js";
import { badge, card, dataTable, formModal, openDrawer, pageHead, progress, searchBox, tabs, toast, toastError } from "../lib/ui.js";

const PRIO = { low: ["Baixa", ""], normal: ["Normal", "info"], high: ["Alta", "warn"], urgent: ["Urgente", "danger"] };
const REPORT_STATUS = { received: "Recebido", processing: "Em processamento", in_review: "Em revisão", issued: "Emitido", rejected: "Rejeitado" };

export default async function cases(ctx) {
  let tab = "cases", q = "", status = "", ctype = "";
  const body = h("div");
  let accounts = [];
  try { accounts = (await api.get("/api/v1/accounts", { limit: 200 })).items; } catch { /* noop */ }

  async function load() {
    if (tab === "reports") {
      const r = await api.get("/api/v1/reports", { pending: true });
      return mount(body, card(null, dataTable({ rows: r.items, empty: "Nenhum laudo pendente. 🎉", onRowClick: (x) => openCase(x.case_id, load), columns: [
        { label: "Exame", render: (x) => h("div", h("div", { class: "strong mono" }, x.exam_code), h("small", x.panel || "")) }, { label: "Caso", render: (x) => `${x.case_number} — ${x.case_title}` },
        { label: "Urgência", render: (x) => badge(x.urgency === "urgent" ? "Urgente" : "Rotina", x.urgency === "urgent" ? "danger" : "") }, { label: "Status", render: (x) => badge(REPORT_STATUS[x.status], "info") },
        { label: "Prazo", render: (x) => h("span", { style: { color: x.due_at && new Date(x.due_at) < new Date() ? "var(--danger)" : "" } }, dateTime(x.due_at)) }] }), { tight: true }));
    }
    const r = await api.get("/api/v1/cases", { q, status, case_type: ctype, limit: 150 });
    mount(body, card(null, [h("div", { class: "row wrap mb-12" }, searchBox("Buscar caso…", (v) => { q = v; load(); }),
      sel([["", "Todos os status"], ...Object.entries(CASE_STATUS_LABEL)], (v) => (status = v)), sel([["", "Todos os eixos"], ...Object.entries(CASE_TYPE_LABEL)], (v) => (ctype = v))),
    dataTable({ rows: r.items, onRowClick: (c) => openCase(c.id, load), empty: "Nenhum caso encontrado.", columns: [
      { label: "Caso", render: (c) => h("div", h("div", { class: "strong" }, c.title), h("small", `${c.number} · ${c.account_name}`)) }, { label: "Eixo", render: (c) => CASE_TYPE_LABEL[c.case_type] },
      { label: "Status", render: (c) => badge(CASE_STATUS_LABEL[c.status], c.status === "done" ? "ok" : "info") }, { label: "Prioridade", render: (c) => badge(...PRIO[c.priority]) },
      { label: "Horas", render: (c) => (c.hours_contracted > 0 ? h("div", { style: { minWidth: "120px" } }, h("div", { class: "muted", style: { fontSize: "11.5px" } }, `${num(c.hours_consumed, 1)} / ${num(c.hours_contracted, 1)} h`), progress(c.hours_pct, c.hours_over ? "danger" : c.hours_pct > 80 ? "warn" : "ok")) : "—") },
      { label: "SLA", render: (c) => (c.sla_breached_at ? badge("Estourado", "danger") : c.sla_due_at ? h("span", { class: "muted" }, date(c.sla_due_at)) : "—") }] })]));
  }
  const sel = (opts, fn) => h("select", { class: "select", style: { width: "180px" }, onChange: (e) => { fn(e.target.value); load(); } }, opts.map(([v, l]) => h("option", { value: v }, l)));

  const root = h("div", pageHead("Casos & laudos", "Entregas técnicas, horas de consultoria consumidas vs. contratadas e laudos pendentes.",
    can("cases:write") ? h("button", { class: "btn primary", onClick: () => formModal({ title: "Novo caso", size: "wide", fields: [
      { name: "account_id", label: "Conta", type: "select", required: true, full: true, options: accounts.map((a) => ({ value: a.id, label: a.trade_name || a.corporate_name })) }, { name: "title", label: "Título", required: true, full: true },
      { name: "case_type", label: "Eixo", type: "select", options: Object.entries(CASE_TYPE_LABEL).map(([value, label]) => ({ value, label })) }, { name: "priority", label: "Prioridade", type: "select", value: "normal", options: Object.entries(PRIO).map(([value, [label]]) => ({ value, label })) },
      { name: "hours_contracted", label: "Horas contratadas", type: "number", step: "0.5", value: 0 }, { name: "sla_hours", label: "SLA (horas)", type: "number" }, { name: "description", label: "Descrição", type: "textarea", full: true }],
    onSubmit: async (v) => { await api.post("/api/v1/cases", { ...v, hours_contracted: v.hours_contracted || 0 }); toast("Caso criado", "ok"); load(); } }) }, icon("plus"), "Novo caso") : null),
  tabs([{ id: "cases", label: "Casos" }, { id: "reports", label: "Laudos pendentes" }], tab, (t) => { tab = t; load(); }), body);
  await load();
  ctx.on("crm_event", (e) => { if (e.type.startsWith("case.") || e.type.startsWith("report.")) load(); });
  return root;
}

export async function openCase(id, reload) {
  const holder = h("div");
  const drawer = openDrawer({ title: "Caso", body: holder, onClose: () => reload?.() });
  async function load() {
    const c = await api.get(`/api/v1/cases/${id}`);
    mount(holder, [
      h("div", { class: "row between mb-12" }, h("div", h("h2", c.title), h("div", { class: "muted" }, `${c.number} · `, h("a", { href: `#/accounts/${c.account_id}`, onClick: () => drawer.close() }, c.account_name))), h("div", { class: "row" }, badge(...PRIO[c.priority]), badge(CASE_STATUS_LABEL[c.status], "info"))),
      c.description ? h("p", c.description) : null,
      can("cases:write") ? h("div", { class: "row wrap mb-16" }, h("label", { class: "muted" }, "Status:"), h("select", { class: "select", style: { width: "200px" }, onChange: async (e) => { try { await api.patch(`/api/v1/cases/${id}`, { status: e.target.value }); toast("Status atualizado", "ok"); load(); } catch (x) { toastError(x); } } },
        Object.entries(CASE_STATUS_LABEL).map(([k, v]) => h("option", { value: k, selected: k === c.status }, v)))) : null,
      card("Horas técnicas", [h("div", { class: "row between mb-8" }, h("span", `${num(c.hours_consumed, 2)} h consumidas`), h("b", c.hours_contracted > 0 ? `${num(c.hours_contracted, 1)} h contratadas (${pct(c.hours_pct)})` : "sem franquia definida")),
        c.hours_contracted > 0 ? progress(c.hours_pct, c.hours_over ? "danger" : c.hours_pct > 80 ? "warn" : "ok") : null,
        c.hours_over ? h("div", { class: "callout warn mt-12" }, "Franquia excedida — as horas excedentes serão faturadas à hora técnica do contrato.") : null,
        dataTable({ rows: c.time_entries, empty: "Nenhum lançamento.", columns: [{ label: "Data", render: (e) => date(e.work_date) }, { label: "Horas", num: true, render: (e) => num(e.hours, 2) }, { label: "Descrição", render: (e) => e.description || "—" }, { label: "Faturável", render: (e) => (e.billable ? "Sim" : "Não") }] }),
        can("cases:write") ? h("div", { class: "mt-12" }, h("button", { class: "btn sm primary", onClick: () => formModal({ title: "Lançar horas", fields: [{ name: "hours", label: "Horas", type: "number", step: "0.25", min: "0.25", max: "24", required: true }, { name: "work_date", label: "Data", type: "date", value: new Date().toISOString().slice(0, 10) },
          { name: "description", label: "Descrição", full: true }, { name: "billable", label: "Faturável", type: "checkbox", value: true }], values: { billable: true }, onSubmit: async (v) => { await api.post(`/api/v1/cases/${id}/time-entries`, v); toast("Horas lançadas", "ok"); load(); } }) }, icon("clock"), "Lançar horas")) : null]),
      h("div", { class: "mt-16" }, card("Laudos / entregas técnicas", [dataTable({ rows: c.reports, empty: "Nenhum laudo vinculado.", columns: [
        { label: "Exame", render: (r) => h("div", h("b", { class: "mono" }, r.exam_code), h("small", r.panel || "")) }, { label: "Urgência", render: (r) => badge(r.urgency === "urgent" ? "Urgente" : "Rotina", r.urgency === "urgent" ? "danger" : "") },
        { label: "Status", render: (r) => (can("cases:write") && r.status !== "issued" ? h("select", { class: "select", style: { width: "150px" }, onChange: (e) => updateReport(r, e.target.value, load) }, Object.entries(REPORT_STATUS).map(([k, v]) => h("option", { value: k, selected: k === r.status }, v))) : badge(REPORT_STATUS[r.status], r.status === "issued" ? "ok" : "info")) },
        { label: "Prazo", render: (r) => (r.issued_at ? `Emitido ${dateTime(r.issued_at)}` : dateTime(r.due_at)) }] }),
      can("cases:write") ? h("div", { class: "mt-12" }, h("button", { class: "btn sm", onClick: () => formModal({ title: "Registrar laudo / exame", fields: [{ name: "exam_code", label: "Código do exame (LIS)", required: true }, { name: "panel", label: "Painel imunofenotípico" },
        { name: "urgency", label: "Urgência", type: "select", options: [{ value: "routine", label: "Rotina (24h)" }, { value: "urgent", label: "Urgente (4h)" }] }], onSubmit: async (v) => { await api.post(`/api/v1/cases/${id}/reports`, v); toast("Laudo registrado", "ok"); load(); } }) }, icon("plus"), "Registrar exame")) : null]))]);
  }
  await load();
}

async function updateReport(r, status, reload) {
  const payload = { status };
  if (status === "issued") {
    return formModal({ title: "Emitir laudo", fields: [{ name: "conclusion", label: "Conclusão do laudo", type: "textarea", required: true, full: true, rows: 6 }], submitLabel: "Emitir laudo",
      onSubmit: async (v) => { await api.patch(`/api/v1/reports/${r.id}`, { status, conclusion: v.conclusion }); toast("Laudo emitido", "ok"); reload(); } });
  }
  try { await api.patch(`/api/v1/reports/${r.id}`, payload); reload(); } catch (e) { toastError(e); reload(); }
}
