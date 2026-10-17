import { api } from "../lib/api.js";
import { h, icon, mount, money, date, dateTime, ago, avatar } from "../lib/dom.js";
import { can, ACCOUNT_TYPE_LABEL, STAGE_LABEL } from "../lib/store.js";
import { badge, card, confirmDialog, dataTable, emptyState, formModal, pageHead, tabs, toast, toastError } from "../lib/ui.js";
import { accountFields } from "./accounts.js";
import { openDeal } from "./pipeline.js";

const KIND = {
  whatsapp_ticket: ["chat", "Atendimento WhatsApp"], whatsapp_closed: ["check", "WhatsApp encerrado"], note: ["note", "Nota"], meeting: ["users", "Reunião"], call: ["phone", "Ligação"],
  task: ["clock", "Tarefa"], email: ["send", "E-mail"], stage_change: ["pipeline", "Pipeline"], deal_created: ["plus", "Oportunidade"], deal_won: ["star", "Negócio ganho"],
  case: ["flask", "Caso"], survey: ["star", "Pesquisa"], lead_form: ["link", "Formulário"], ticket_linked: ["link", "Vínculo"],
};

export default async function accountDetail(ctx) {
  const id = ctx.params.id;
  const root = h("div");
  let tab = "overview";
  async function load() {
    const [a, tl] = await Promise.all([api.get(`/api/v1/accounts/${id}`), api.get("/api/v1/timeline", { account_id: id })]);
    const body = h("div");
    const draw = () => {
      if (tab === "overview") mount(body, overview(a));
      else if (tab === "timeline") mount(body, timeline(tl.items, a));
      else mount(body, dealsTab(a));
    };
    mount(root, [
      pageHead(a.trade_name || a.corporate_name, [a.corporate_name, ACCOUNT_TYPE_LABEL[a.account_type], [a.city, a.state].filter(Boolean).join("/")].filter(Boolean).join(" · "),
        h("a", { class: "btn", href: "#/accounts" }, "← Contas"),
        can("accounts:write") ? h("button", { class: "btn", onClick: () => formModal({ title: "Editar conta", size: "wide", fields: accountFields(a), values: { ...a, tags: (a.tags || []).join(", ") }, onSubmit: async (v) => {
          await api.put(`/api/v1/accounts/${id}`, { ...v, account_type: v.account_type || a.account_type, status: v.status || a.status, institutional_discount_pct: v.institutional_discount_pct ?? 0, tax_id: v.tax_id && !v.tax_id.includes("*") ? v.tax_id : null }); toast("Conta atualizada", "ok"); load(); } }) }, icon("edit"), "Editar") : null,
        can("org:manage") ? h("button", { class: "btn danger", onClick: async () => { if (await confirmDialog({ title: "Excluir conta", message: "Isso remove contatos vinculados, oportunidades e atividades da conta. Deseja continuar?", danger: true, confirmLabel: "Excluir" })) { await api.del(`/api/v1/accounts/${id}`); ctx.navigate("#/accounts"); } } }, icon("trash")) : null),
      tabs([{ id: "overview", label: "Visão geral" }, { id: "deals", label: `Oportunidades (${a.deals.length})` }, { id: "timeline", label: "Timeline unificada" }], tab, (t) => { tab = t; draw(); }), body]);
    draw();
  }

  function overview(a) {
    return h("div", { class: "grid c-main" },
      h("div", { class: "col gap-16" },
        card("Interlocutores", [dataTable({ rows: a.contacts, empty: "Nenhum contato vinculado.", columns: [
          { label: "Contato", render: (c) => h("div", { class: "row" }, avatar(c.name), h("div", h("div", { class: "strong" }, c.name || "Sem nome", c.is_primary ? " ★" : ""), h("small", c.role_description || ""))) },
          { label: "Registro", render: (c) => c.council_registration || "—" },
          { label: "WhatsApp", render: (c) => h("div", { class: "row gap-4" }, h("span", { class: "mono" }, c.wa_id), c.masked && can("pii:reveal") ? h("button", { class: "btn ghost sm icon", title: "Revelar (auditado)", onClick: async (e) => {
            const r = await api.post(`/api/v1/contacts/${c.id}/reveal`); e.currentTarget.previousSibling.textContent = r.wa_id; e.currentTarget.remove(); toast("Acesso registrado na trilha de auditoria", "info"); } }, icon("eye")) : null) },
          { label: "E-mail", render: (c) => c.email || "—" },
          { label: "", render: (c) => (can("accounts:write") ? h("button", { class: "btn ghost sm icon", onClick: async () => { await api.del(`/api/v1/accounts/${a.id}/contacts/${c.link_id}`); load(); } }, icon("x")) : "") }] }),
        can("accounts:write") ? h("div", { class: "mt-12" }, h("button", { class: "btn sm", onClick: () => addContact(a.id, load) }, icon("plus"), "Adicionar interlocutor")) : null]),
        card("Notas e atividades", h("div", { class: "row wrap" }, ["note:Nota", "meeting:Reunião", "call:Ligação", "task:Tarefa"].map((s) => { const [k, l] = s.split(":"); return h("button", { class: "btn sm", onClick: () => addActivity(a.id, k, l, load) }, icon(KIND[k][0]), l); })))),
      h("div", { class: "col gap-16" }, card("Dados cadastrais", h("div", [kv("CNPJ", h("span", { class: "mono" }, a.tax_id || "—")), kv("Tipo", ACCOUNT_TYPE_LABEL[a.account_type]), kv("Situação", a.status), kv("Segmento", a.segment || "—"), kv("Site", a.website || "—"),
        kv("Desconto institucional", `${a.institutional_discount_pct || 0}%`), kv("Cadastro", date(a.created_at)), a.tags?.length ? h("div", { class: "row wrap mt-8" }, a.tags.map((t) => badge(t, "info"))) : null, a.notes ? h("p", { class: "muted mt-12" }, a.notes) : null]))));
  }

  function dealsTab(a) {
    return card(null, dataTable({ rows: a.deals, onRowClick: (d) => openDeal(d.id, load), empty: "Nenhuma oportunidade.", columns: [{ label: "Oportunidade", key: "title" }, { label: "Estágio", render: (d) => badge(STAGE_LABEL[d.stage]) }, { label: "Valor", num: true, render: (d) => money(d.final_contracted_value || d.expected_value) }] }), { tight: true });
  }

  function timeline(items) {
    if (!items.length) return card(null, emptyState("Sem eventos na timeline.", "clock"));
    return card("Timeline unificada (WhatsApp · CRM · casos)", h("div", { class: "timeline" }, items.map((a) => {
      const [ic, label] = KIND[a.kind] || ["file", a.kind];
      return h("div", { class: `tl-item ${a.kind === "deal_won" || a.kind === "whatsapp_closed" ? "ok" : ""}` }, h("div", { class: "row" }, icon(ic, "sm"), h("b", a.title), badge(label)),
        a.body ? h("div", { class: "muted", style: { marginTop: "3px" } }, a.body) : null, a.ticket_id ? h("a", { href: `#/inbox/${a.ticket_id}` }, "Abrir atendimento") : null, h("div", { class: "when" }, dateTime(a.created_at)));
    })));
  }
  await load();
  return root;
}

const kv = (k, v) => h("div", { class: "kv" }, h("span", k), h("span", v));

function addContact(accountId, reload) {
  formModal({ title: "Adicionar interlocutor", fields: [
    { name: "name", label: "Nome", required: true }, { name: "wa_id", label: "WhatsApp (DDD + número)", required: true, placeholder: "(62) 99999-0000" },
    { name: "email", label: "E-mail", type: "email" }, { name: "role_description", label: "Cargo / função", placeholder: "Médico Hematologista" },
    { name: "council_registration", label: "Registro no conselho", placeholder: "CRM-GO 12345" }, { name: "is_primary", label: "Interlocutor principal", type: "checkbox" }],
  onSubmit: async (v) => { await api.post(`/api/v1/accounts/${accountId}/contacts`, { new_contact: { wa_id: v.wa_id, name: v.name, email: v.email, job_title: v.role_description }, role_description: v.role_description, council_registration: v.council_registration, is_primary: v.is_primary }); toast("Interlocutor adicionado", "ok"); reload(); } });
}

function addActivity(accountId, kind, label, reload) {
  formModal({ title: label, fields: [{ name: "title", label: "Título", required: true, full: true }, { name: "body", label: "Detalhes", type: "textarea", full: true }, ...(kind === "task" ? [{ name: "due_at", label: "Prazo", type: "datetime-local" }] : [])],
    onSubmit: async (v) => { await api.post("/api/v1/activities", { kind, title: v.title, body: v.body, account_id: accountId, due_at: v.due_at ? new Date(v.due_at).toISOString() : null }); toast("Registrado na timeline", "ok"); reload(); } });
}
