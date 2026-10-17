import { api } from "../lib/api.js";
import { h, icon, mount, date } from "../lib/dom.js";
import { can, ACCOUNT_TYPE_LABEL } from "../lib/store.js";
import { badge, card, dataTable, formModal, pageHead, searchBox, toast } from "../lib/ui.js";

const STATUS = { prospect: ["Prospect", "info"], customer: ["Cliente", "ok"], inactive: ["Inativo", ""] };

export const accountFields = (a = {}) => [
  { name: "corporate_name", label: "Razão social", required: true, full: true }, { name: "trade_name", label: "Nome fantasia" },
  { name: "tax_id", label: "CNPJ", placeholder: "00.000.000/0000-00", hint: "Armazenado com criptografia de envelope (AES-256-GCM)." },
  { name: "account_type", label: "Tipo", type: "select", options: Object.entries(ACCOUNT_TYPE_LABEL).map(([value, label]) => ({ value, label })) },
  { name: "status", label: "Situação", type: "select", options: [["prospect", "Prospect"], ["customer", "Cliente"], ["inactive", "Inativo"]].map(([value, label]) => ({ value, label })) },
  { name: "city", label: "Cidade" }, { name: "state", label: "UF", maxlength: 2 }, { name: "website", label: "Site" },
  { name: "institutional_discount_pct", label: "Desconto institucional padrão (%)", type: "number", step: "0.5", min: "0", max: "100", value: 0 },
  { name: "segment", label: "Segmento" }, { name: "tags", label: "Tags (separadas por vírgula)", split: true, full: true }, { name: "notes", label: "Observações", type: "textarea", full: true },
];

export default async function accounts(ctx) {
  let q = "", type = "", status = "";
  const list = h("div");
  async function load() {
    const r = await api.get("/api/v1/accounts", { q, account_type: type, status, limit: 100 });
    mount(list, dataTable({ rows: r.items, onRowClick: (a) => ctx.navigate(`#/accounts/${a.id}`), empty: "Nenhuma conta encontrada.", columns: [
      { label: "Instituição", render: (a) => h("div", h("div", { class: "strong" }, a.trade_name || a.corporate_name), h("small", a.trade_name ? a.corporate_name : "")) },
      { label: "Tipo", render: (a) => ACCOUNT_TYPE_LABEL[a.account_type] }, { label: "CNPJ", render: (a) => h("span", { class: "mono" }, a.tax_id || "—") },
      { label: "Local", render: (a) => [a.city, a.state].filter(Boolean).join("/") || "—" }, { label: "Situação", render: (a) => badge(...STATUS[a.status]) },
      { label: "Desc. inst.", num: true, render: (a) => (Number(a.institutional_discount_pct) ? `${a.institutional_discount_pct}%` : "—") }, { label: "Criada em", render: (a) => date(a.created_at) }] }));
    return r.total;
  }
  const sel = (opts, fn) => h("select", { class: "select", style: { width: "160px" }, onChange: (e) => { fn(e.target.value); load(); } }, opts.map(([v, l]) => h("option", { value: v }, l)));
  const root = h("div", pageHead("Contas institucionais", "Hospitais, laboratórios, clínicas e universidades atendidas pela Flowmentor.",
    can("accounts:write") ? h("button", { class: "btn primary", onClick: () => formModal({ title: "Nova conta institucional", size: "wide", fields: accountFields(), onSubmit: async (v) => {
      const a = await api.post("/api/v1/accounts", v); toast("Conta criada", "ok"); ctx.navigate(`#/accounts/${a.id}`); } }) }, icon("plus"), "Nova conta") : null),
  card(null, [h("div", { class: "row wrap mb-12" }, searchBox("Buscar por nome ou CNPJ…", (v) => { q = v; load(); }),
    sel([["", "Todos os tipos"], ...Object.entries(ACCOUNT_TYPE_LABEL)], (v) => (type = v)), sel([["", "Todas as situações"], ["prospect", "Prospect"], ["customer", "Cliente"], ["inactive", "Inativo"]], (v) => (status = v))), list]));
  await load();
  return root;
}
