import { api, download } from "../lib/api.js";
import { h, icon, mount, dateTime, date } from "../lib/dom.js";
import { can } from "../lib/store.js";
import { badge, card, confirmDialog, dataTable, formModal, kpi, openModal, pageHead, searchBox, tabs, toast, toastError } from "../lib/ui.js";

const RST = { open: ["Aberta", "warn"], in_progress: ["Em andamento", "info"], completed: ["Concluída", "ok"], rejected: ["Rejeitada", ""] };

export default async function compliance(ctx) {
  const canDsr = can("lgpd:manage"), canAudit = can("audit:view");
  let tab = canDsr ? "summary" : "audit";
  const body = h("div");
  let auditFilter = { action: "", resource_type: "" };

  async function load() {
    if (tab === "summary") {
      const s = await api.get("/api/v1/lgpd/summary");
      return mount(body, [h("div", { class: "grid c4 mb-16" }, kpi("Solicitações abertas", s.open_requests, `${s.overdue_requests} fora do prazo (15 dias)`, "shield", s.overdue_requests ? "danger" : "ok"), kpi("Consentimentos ativos", s.active_consents, "base legal registrada", "check"),
        kpi("Contatos anonimizados", s.anonymized_contacts, "direito ao esquecimento", "lock"), kpi("Opt-outs", s.opted_out_contacts, "não recebem mensagens", "alert")),
      card("Práticas de proteção em vigor", h("div", { class: "grid c2" }, [["TLS 1.3 em trânsito e HSTS", "Cabeçalhos de segurança e CSP estrita em todas as respostas."], ["Criptografia de envelope (AES-256-GCM)", "CNPJ e tokens cifrados na aplicação, com chaves rotativas."], ["Mascaramento por perfil", "Telefones e documentos mascarados; revelar gera registro de auditoria."],
        ["Auditoria imutável encadeada", "Cada evento carrega hash do anterior: adulterações são detectadas."], ["Opt-out por palavra-chave", "“PARAR” no WhatsApp revoga consentimentos automaticamente."], ["Retenção de mídias", "Binários são expurgados após o prazo e URLs expiram em minutos."]].map(([t, d]) => h("div", { class: "row", style: { alignItems: "flex-start" } }, icon("check", "lg"), h("div", h("b", t), h("div", { class: "muted" }, d))))))]);
    }
    if (tab === "requests") {
      const { items } = await api.get("/api/v1/lgpd/requests");
      return mount(body, card(null, dataTable({ rows: items, empty: "Nenhuma solicitação de titular.", columns: [
        { label: "Tipo", render: (r) => h("div", h("b", r.type_label), h("small", r.requester_name || "")) }, { label: "Status", render: (r) => h("div", badge(...RST[r.status]), r.overdue ? badge("atrasada", "danger") : null) }, { label: "Prazo legal", render: (r) => date(r.due_at) },
        { label: "Criada", render: (r) => dateTime(r.created_at) }, { label: "", render: (r) => h("div", { class: "row gap-4" }, r.status !== "completed" ? h("button", { class: "btn sm primary", onClick: async () => { if (await confirmDialog({ title: "Processar solicitação", message: `Executar agora: ${r.type_label}? ${["anonymization", "deletion"].includes(r.request_type) ? "Esta ação é irreversível." : ""}`, confirmLabel: "Processar", danger: ["anonymization", "deletion"].includes(r.request_type) })) { try { await api.post(`/api/v1/lgpd/requests/${r.id}/process`); toast("Solicitação concluída", "ok"); load(); } catch (e) { toastError(e); } } } }, "Processar") : null,
          r.result ? h("button", { class: "btn sm", onClick: () => openModal({ title: "Resultado", size: "wide", body: h("pre", { class: "code" }, JSON.stringify(r.result.export ? { resumo: r.result.summary } : r.result, null, 2)) }) }, "Ver resultado") : null) }] }), { tight: true }));
    }
    if (tab === "consents") {
      const { items } = await api.get("/api/v1/lgpd/consents");
      return mount(body, card(null, dataTable({ rows: items, empty: "Nenhum consentimento registrado.", columns: [{ label: "Base legal", key: "legal_basis" }, { label: "Finalidade", render: (c) => h("span", { class: "muted" }, c.purpose) }, { label: "Origem", render: (c) => badge(c.consent_source, "info") }, { label: "Registrado", render: (c) => dateTime(c.opt_in_timestamp) },
        { label: "Status", render: (c) => (c.revoked_at ? badge(`Revogado ${date(c.revoked_at)}`, "danger") : badge("Ativo", "ok")) }, { label: "", render: (c) => (!c.revoked_at ? h("button", { class: "btn sm danger", onClick: async () => { await api.post(`/api/v1/lgpd/consents/${c.id}/revoke`); load(); } }, "Revogar") : "") }] }), { tight: true }));
    }
    if (tab === "audit") {
      const [r, v] = await Promise.all([api.get("/api/v1/lgpd/audit-logs", { limit: 100, action: auditFilter.action || undefined, resource_type: auditFilter.resource_type || undefined }), api.get("/api/v1/lgpd/audit-logs/verify")]);
      return mount(body, [h("div", { class: `callout ${v.valid ? "ok" : "warn"}` }, v.valid ? `✔ Cadeia de auditoria íntegra — ${v.checked} registro(s) verificados (hash encadeado, append-only).` : `✖ Integridade comprometida a partir do registro #${v.broken_at_seq}!`),
        card(null, [h("div", { class: "row wrap mb-12" }, h("input", { class: "input", style: { width: "200px" }, placeholder: "Ação (ex.: VIEW_PHONE)", value: auditFilter.action, onChange: (e) => { auditFilter.action = e.target.value.trim().toUpperCase(); load(); } }),
          h("input", { class: "input", style: { width: "200px" }, placeholder: "Recurso (ex.: contacts)", value: auditFilter.resource_type, onChange: (e) => { auditFilter.resource_type = e.target.value.trim(); load(); } }), h("div", { class: "grow" }),
          can("export:data") ? h("button", { class: "btn", onClick: () => download("/api/v1/lgpd/audit-logs/export.csv", "auditoria-flowmentor.csv").catch(toastError) }, icon("download"), "Exportar CSV") : null),
        dataTable({ rows: r.items, empty: "Sem registros.", columns: [{ label: "#", num: true, key: "seq" }, { label: "Quando", render: (a) => dateTime(a.created_at) }, { label: "Usuário", render: (a) => a.actor_name || h("span", { class: "muted" }, "sistema") }, { label: "Ação", render: (a) => h("span", { class: "mono strong" }, a.action) },
          { label: "Recurso", render: (a) => a.resource_type }, { label: "IP", render: (a) => h("span", { class: "mono" }, a.ip_address) }, { label: "Detalhe", render: (a) => (a.new_state || a.old_state ? h("button", { class: "btn ghost sm", onClick: () => openModal({ title: `Registro #${a.seq}`, size: "wide", body: h("pre", { class: "code" }, JSON.stringify({ antes: a.old_state, depois: a.new_state, hash: a.hash }, null, 2)) }) }, "ver") : "") }] })])]);
    }
  }
  const root = h("div", pageHead("LGPD & auditoria", "Direitos do titular (arts. 18 e 19), consentimentos e trilha de auditoria imutável.",
    canDsr ? h("button", { class: "btn primary", onClick: newRequest }, icon("plus"), "Nova solicitação de titular") : null),
  tabs([...(canDsr ? [{ id: "summary", label: "Resumo" }, { id: "requests", label: "Solicitações do titular" }, { id: "consents", label: "Consentimentos" }] : []), ...(canAudit ? [{ id: "audit", label: "Trilha de auditoria" }] : [])], tab, (t) => { tab = t; load(); }), body);

  async function newRequest() {
    const types = (await api.get("/api/v1/lgpd/request-types")).items;
    formModal({ title: "Nova solicitação de titular", size: "wide", fields: [{ name: "wa_id", label: "WhatsApp do titular", required: true, placeholder: "(62) 99999-0000" }, { name: "request_type", label: "Tipo de solicitação", type: "select", options: types.map((t) => ({ value: t.type, label: t.label })) },
      { name: "requester_name", label: "Nome do solicitante" }, { name: "details", label: "Detalhes", type: "textarea", full: true }],
    onSubmit: async (v) => { await api.post("/api/v1/lgpd/requests", v); toast("Solicitação registrada (prazo legal: 15 dias)", "ok"); [...root.querySelectorAll(".tab")].find((b) => b.textContent.startsWith("Solicitações"))?.click(); } });
  }
  await load();
  return root;
}
