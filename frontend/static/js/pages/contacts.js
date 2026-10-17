import { api } from "../lib/api.js";
import { h, icon, mount, avatar, ago } from "../lib/dom.js";
import { can } from "../lib/store.js";
import { badge, card, dataTable, formModal, pageHead, searchBox, toast } from "../lib/ui.js";

export default async function contacts(ctx) {
  let q = "";
  const list = h("div");
  async function load() {
    const r = await api.get("/api/v1/contacts", { q, limit: 100 });
    mount(list, dataTable({ rows: r.items, empty: "Nenhum contato.", columns: [
      { label: "Contato", render: (c) => h("div", { class: "row" }, avatar(c.name), h("div", h("div", { class: "strong" }, c.name || "Sem nome"), h("small", c.job_title || ""))) },
      { label: "WhatsApp", render: (c) => h("div", { class: "row gap-4" }, h("span", { class: "mono" }, c.wa_id), c.masked ? badge("mascarado") : null,
        c.masked && can("pii:reveal") ? h("button", { class: "btn ghost sm icon", title: "Revelar (registra auditoria)", onClick: async (e) => {
          const r2 = await api.post(`/api/v1/contacts/${c.id}/reveal`); const t = e.currentTarget; t.previousSibling.previousSibling.textContent = r2.wa_id; t.remove(); toast("Acesso registrado na trilha de auditoria", "info"); } }, icon("eye")) : null) },
      { label: "E-mail", render: (c) => c.email || "—" }, { label: "Origem", render: (c) => c.source || "—" },
      { label: "Status", render: (c) => (c.opted_out_at ? badge("opt-out", "danger") : badge("ativo", "ok")) }, { label: "Última interação", render: (c) => ago(c.last_interaction_at) || "—" }] }));
    return r.total;
  }
  const root = h("div", pageHead("Contatos", "Médicos, prescritores, responsáveis técnicos e leads. Telefones são mascarados conforme o perfil (LGPD).",
    can("contacts:write") ? h("button", { class: "btn primary", onClick: () => formModal({ title: "Novo contato", fields: [
      { name: "name", label: "Nome", required: true }, { name: "wa_id", label: "WhatsApp", required: true, placeholder: "(62) 99999-0000" },
      { name: "email", label: "E-mail", type: "email" }, { name: "job_title", label: "Cargo" }, { name: "tags", label: "Tags", split: true, full: true }],
    onSubmit: async (v) => { await api.post("/api/v1/contacts", v); toast("Contato criado", "ok"); load(); } }) }, icon("plus"), "Novo contato") : null),
  card(null, [h("div", { class: "mb-12", style: { maxWidth: "360px" } }, searchBox("Buscar por nome, e-mail ou número…", (v) => { q = v; load(); })), list]));
  await load();
  return root;
}
