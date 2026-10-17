import { api } from "../lib/api.js";
import { h, icon, mount, money, date, dateTime, num } from "../lib/dom.js";
import { can, MODALITY_LABEL } from "../lib/store.js";
import { badge, card, dataTable, formModal, kpi, openDrawer, openModal, pageHead, progress, tabs, toast, toastError } from "../lib/ui.js";

const INV = { open: ["Em aberto", "info"], paid: ["Paga", "ok"], overdue: ["Vencida", "danger"], cancelled: ["Cancelada", ""] };
const CT = { draft: "Rascunho", active: "Ativo", suspended: "Suspenso", ended: "Encerrado", cancelled: "Cancelado" };

export default async function contracts(ctx) {
  let tab = "contracts";
  const body = h("div");
  let accounts = [];
  try { accounts = (await api.get("/api/v1/accounts", { limit: 200 })).items; } catch { /* noop */ }

  async function load() {
    if (tab === "invoices") {
      const r = await api.get("/api/v1/invoices");
      return mount(body, [h("div", { class: "grid c3 mb-16" }, kpi("A receber", money(r.totals.open), "faturas em aberto", "dollar"), kpi("Vencidas", money(r.totals.overdue), "exigem cobrança", "alert", r.totals.overdue ? "danger" : ""), kpi("Recebido", money(r.totals.paid), "total pago", "check", "ok")),
        card(null, dataTable({ rows: r.items, empty: "Nenhuma fatura.", columns: [
          { label: "Fatura", render: (i) => h("div", h("b", i.number), h("small", i.description || "")) }, { label: "Cliente", key: "account_name" }, { label: "Vencimento", render: (i) => date(i.due_date) },
          { label: "Valor", num: true, render: (i) => money(i.amount) }, { label: "Status", render: (i) => badge(...INV[i.status]) }, { label: "NFS-e", render: (i) => (i.nfse_number ? i.nfse_number : badge(i.nfse_status || "—")) },
          { label: "", render: (i) => h("div", { class: "row gap-4" }, i.pix_payload ? h("button", { class: "btn sm", onClick: () => showPix(i) }, "PIX") : null,
            can("contracts:write") && i.status !== "paid" && i.status !== "cancelled" ? h("button", { class: "btn sm ok", onClick: async () => { await api.post(`/api/v1/invoices/${i.id}/pay`, { payment_method: "pix" }); toast("Pagamento registrado", "ok"); load(); } }, "Baixar") : null) }] }), { tight: true })]);
    }
    const r = await api.get("/api/v1/contracts");
    mount(body, card(null, dataTable({ rows: r.items, onRowClick: (c) => openContract(c.id, load), empty: "Nenhum contrato. Contratos são gerados automaticamente ao ganhar uma oportunidade.", columns: [
      { label: "Contrato", render: (c) => h("div", h("b", c.title), h("small", `${c.number} · ${c.account_name}`)) }, { label: "Modalidade", render: (c) => MODALITY_LABEL[c.modality] },
      { label: "Vigência", render: (c) => `${date(c.start_date)} → ${date(c.end_date)}` }, { label: "Valor total", num: true, render: (c) => money(c.total_value) },
      { label: "Recorrência", num: true, render: (c) => (c.recurring_amount ? `${money(c.recurring_amount)}/mês` : "—") },
      { label: "Banco de horas", render: (c) => (c.hours.contracted ? h("div", { style: { minWidth: "110px" } }, h("div", { class: "muted", style: { fontSize: "11.5px" } }, `${num(c.hours.consumed, 1)} / ${num(c.hours.contracted, 1)} h`), progress(c.hours.pct, c.hours.pct > 100 ? "danger" : c.hours.pct > 80 ? "warn" : "ok")) : "—") },
      { label: "Status", render: (c) => badge(CT[c.status], c.status === "active" ? "ok" : "") }] }), { tight: true }));
  }
  const root = h("div", pageHead("Contratos & faturamento", "Recorrência, banco de horas, faturas com PIX (copia e cola) e NFS-e.",
    can("contracts:write") ? [h("button", { class: "btn", onClick: async () => { const r = await api.post("/api/v1/billing/run-cycle"); toast(`Ciclo executado: ${r.generated} fatura(s) gerada(s), ${r.marked_overdue} marcada(s) vencida(s).`, "ok"); load(); } }, icon("repeat"), "Executar ciclo de cobrança"),
      h("button", { class: "btn primary", onClick: () => formModal({ title: "Novo contrato", size: "wide", fields: [
        { name: "account_id", label: "Conta", type: "select", required: true, full: true, options: accounts.map((a) => ({ value: a.id, label: a.trade_name || a.corporate_name })) }, { name: "title", label: "Título", required: true, full: true },
        { name: "modality", label: "Modalidade", type: "select", options: Object.entries(MODALITY_LABEL).map(([value, label]) => ({ value, label })) }, { name: "start_date", label: "Início", type: "date", required: true, value: new Date().toISOString().slice(0, 10) },
        { name: "end_date", label: "Fim", type: "date" }, { name: "total_value", label: "Valor total (R$)", type: "number", step: "0.01", value: 0 }, { name: "recurring_amount", label: "Valor recorrente mensal (R$)", type: "number", step: "0.01" },
        { name: "billing_day", label: "Dia de faturamento", type: "number", min: "1", max: "28", value: 5 }, { name: "hours_contracted", label: "Banco de horas", type: "number", step: "0.5", value: 0 }, { name: "hourly_rate", label: "Hora técnica (R$)", type: "number", step: "0.01", value: 300 }],
      onSubmit: async (v) => { await api.post("/api/v1/contracts", { ...v, total_value: v.total_value || 0, hours_contracted: v.hours_contracted || 0, hourly_rate: v.hourly_rate || 300, billing_day: v.billing_day || 5 }); toast("Contrato criado", "ok"); load(); } }) }, icon("plus"), "Novo contrato")] : null),
  tabs([{ id: "contracts", label: "Contratos" }, { id: "invoices", label: "Faturas" }], tab, (t) => { tab = t; load(); }), body);
  await load();
  return root;
}

function showPix(inv) {
  const code = h("textarea", { class: "textarea mono", readOnly: true, rows: 5 }, inv.pix_payload);
  openModal({ title: `PIX — ${inv.number}`, body: [h("p", `Valor: `, h("b", money(inv.amount)), " · Vencimento: ", date(inv.due_date)), h("div", { class: "field" }, h("label", "Pix copia e cola (BR Code)"), code)],
    footer: h("button", { class: "btn primary", onClick: async () => { try { await navigator.clipboard.writeText(inv.pix_payload); toast("Código copiado", "ok"); } catch { code.select(); } } }, icon("link"), "Copiar código") });
}

async function openContract(id, reload) {
  const holder = h("div");
  const drawer = openDrawer({ title: "Contrato", body: holder, onClose: () => reload?.() });
  async function load() {
    const c = await api.get(`/api/v1/contracts/${id}`);
    mount(holder, [
      h("div", { class: "row between mb-12" }, h("div", h("h2", c.title), h("div", { class: "muted" }, `${c.number} · `, h("a", { href: `#/accounts/${c.account_id}`, onClick: () => drawer.close() }, c.account_name))), badge(CT[c.status], c.status === "active" ? "ok" : "")),
      h("div", { class: "grid c3 mb-16" }, kpi("Valor total", money(c.total_value), MODALITY_LABEL[c.modality], "dollar"), kpi("Recorrência", c.recurring_amount ? money(c.recurring_amount) : "—", c.next_billing_date ? `próx. fatura ${date(c.next_billing_date)}` : "", "repeat"), kpi("Hora técnica", money(c.hourly_rate), "referência R$ 300/h", "clock")),
      c.hours.contracted ? card("Banco de horas", [h("div", { class: "row between mb-8" }, h("span", `${num(c.hours.consumed, 1)} h consumidas`), h("b", `${num(c.hours.contracted, 1)} h contratadas · restam ${num(c.hours.remaining, 1)} h`)), progress(c.hours.pct, c.hours.pct > 100 ? "danger" : c.hours.pct > 80 ? "warn" : "ok"),
        c.hours.overage ? h("div", { class: "callout warn mt-12" }, `Excedente de ${num(c.hours.overage, 1)} h a faturar.`) : null]) : null,
      can("contracts:write") ? h("div", { class: "row wrap mt-12 mb-16" }, h("button", { class: "btn", onClick: async () => { try { const r = await api.post(`/api/v1/contracts/${id}/invoice-from-usage`); toast(r.message || `Fatura ${r.number} gerada`, "ok"); load(); } catch (e) { toastError(e); } } }, icon("file"), "Faturar horas consumidas"),
        h("select", { class: "select", style: { width: "170px" }, onChange: async (e) => { await api.put(`/api/v1/contracts/${id}/status`, { status: e.target.value }); toast("Status atualizado", "ok"); load(); } }, Object.entries(CT).map(([k, v]) => h("option", { value: k, selected: k === c.status }, v)))) : null,
      card("Faturas", dataTable({ rows: c.invoices, empty: "Nenhuma fatura emitida.", columns: [{ label: "Número", key: "number" }, { label: "Vencimento", render: (i) => date(i.due_date) }, { label: "Valor", num: true, render: (i) => money(i.amount) }, { label: "Status", render: (i) => badge(...INV[i.status]) }] })),
      h("div", { class: "mt-16" }, card("Casos vinculados", dataTable({ rows: c.cases, empty: "Nenhum caso.", columns: [{ label: "Caso", render: (x) => `${x.number} — ${x.title}` }, { label: "Horas", render: (x) => `${num(x.hours_consumed, 1)} / ${num(x.hours_contracted, 1)} h` }] }))),
    ]);
  }
  await load();
}
