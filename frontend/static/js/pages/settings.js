import { api } from "../lib/api.js";
import { h, icon, mount, dateTime, money } from "../lib/dom.js";
import { can, state, ROLE_LABEL } from "../lib/store.js";
import { badge, card, dataTable, form, formModal, openModal, pageHead, tabs, toast, toastError } from "../lib/ui.js";

const DAYS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"];

export default async function settings(ctx) {
  const tabsDef = [{ id: "org", label: "Organização", p: "org:manage" }, { id: "users", label: "Usuários", p: "users:manage" }, { id: "depts", label: "Departamentos", p: "org:manage" }, { id: "wa", label: "WhatsApp (BSP)", p: "whatsapp:manage" }, { id: "me", label: "Minha conta" }]
    .filter((t) => !t.p || can(t.p));
  let tab = ctx.query?.tab || tabsDef[0].id;
  const body = h("div");
  const root = h("div", pageHead("Configurações", "Horário de atendimento, roteamento, pesquisas, usuários, departamentos e conexão com o WhatsApp."), tabs(tabsDef, tab, (t) => { tab = t; load(); }), body);

  async function load() {
    mount(body, h("div", { class: "spinner" }));
    try { await ({ org: orgTab, users: usersTab, depts: deptsTab, wa: waTab, me: meTab })[tab](body, load); } catch (e) { mount(body, h("div", { class: "callout warn" }, e.message)); }
  }
  await load();
  return root;
}

// ---------------------------------------------------------------- Organization
async function orgTab(body) {
  const { settings: s } = await api.get("/api/v1/org");
  const hours = JSON.parse(JSON.stringify(s.business_hours));
  const hoursBox = h("div");
  const drawHours = () => mount(hoursBox, DAYS.map((d, i) => { const w = hours[String(i)]?.[0]; const on = !!w;
    return h("div", { class: "row mb-8" }, h("label", { class: "switch" }, h("input", { type: "checkbox", "aria-label": `${d}: atendimento aberto`, checked: on, onChange: (e) => { hours[String(i)] = e.target.checked ? [["08:00", "18:00"]] : []; drawHours(); } }), h("span")), h("span", { style: { width: "80px" } }, d),
      on ? [h("input", { class: "input", type: "time", "aria-label": `${d}: abertura`, style: { width: "120px" }, value: w[0], onChange: (e) => (hours[String(i)][0][0] = e.target.value) }), "às", h("input", { class: "input", type: "time", "aria-label": `${d}: fechamento`, style: { width: "120px" }, value: w[1], onChange: (e) => (hours[String(i)][0][1] = e.target.value) })] : h("span", { class: "muted" }, "Fechado")); }));
  drawHours();
  const routing = form([{ name: "default_strategy", label: "Estratégia padrão de roteamento", type: "select", options: [["least_busy", "Menor carga (Least Busy)"], ["round_robin", "Circular (Round-Robin)"], ["skill_based", "Por habilidade (Skill-based)"]].map(([value, label]) => ({ value, label })) },
    { name: "sticky_agent_hours", label: "Afinidade com o último agente (horas)", type: "number", min: "0", hint: "0 desativa. Reencaminha o cliente ao agente que o atendeu recentemente." }], s.routing);
  const survey = form([{ name: "enabled", label: "Pesquisa de satisfação automática ativa", type: "checkbox", full: true }, { name: "type", label: "Método", type: "select", options: [["csat", "CSAT (1–5)"], ["nps", "NPS (0–10)"], ["ces", "CES (esforço 1–5)"]].map(([value, label]) => ({ value, label })) },
    { name: "grace_minutes", label: "Janela de carência após encerrar (min)", type: "number", min: "0", max: "60" }, { name: "suppress_days", label: "Anti-spam: não repetir por (dias)", type: "number", min: "0" }, { name: "ttl_hours", label: "Validade da resposta (horas)", type: "number", min: "1" },
    { name: "ask_comment", label: "Pedir comentário opcional", type: "checkbox" }, { name: "intro", label: "Mensagem de abertura", type: "textarea", full: true }], s.survey);
  const bot = form([{ name: "enabled", label: "Bot de triagem ativo", type: "checkbox", full: true }, { name: "greeting", label: "Saudação inicial", type: "textarea", full: true }, { name: "min_confidence", label: "Confiança mínima p/ resolver sozinho (0–1)", type: "number", step: "0.05", min: "0", max: "1" },
    { name: "handoff_keywords", label: "Palavras que acionam atendente (vírgula)", split: true, full: true }, { name: "after_hours_message", label: "Mensagem fora do expediente", type: "textarea", full: true }], { ...s.bot, handoff_keywords: s.bot.handoff_keywords.join(", ") });
  const billing = form([{ name: "pix_key", label: "Chave PIX para cobranças", placeholder: "financeiro@empresa.com.br" }, { name: "merchant_name", label: "Nome do recebedor (PIX)" }, { name: "merchant_city", label: "Cidade (PIX)" }], s.billing);
  const disc = form([{ name: "auto_approve_max_pct", label: "Desconto máximo sem aprovação (%)", type: "number", min: "0", max: "100", step: "0.5" }], s.discount);
  const crm = form([{ name: "auto_create_leads", label: "Criar lead/oportunidade automaticamente para números desconhecidos", type: "checkbox", full: true }], s.crm);
  const tiers = JSON.parse(JSON.stringify(s.discount.institutional_tiers));
  const tiersBox = h("div");
  const drawTiers = () => mount(tiersBox, [...tiers.map((t, i) => h("div", { class: "row mb-8" }, "A partir de R$", h("input", { class: "input", type: "number", "aria-label": `Faixa ${i + 1}: valor mínimo (R$)`, style: { width: "130px" }, value: t.min_value, onInput: (e) => (t.min_value = Number(e.target.value)) }), "→ desconto de", h("input", { class: "input", type: "number", step: "0.5", "aria-label": `Faixa ${i + 1}: desconto (%)`, style: { width: "90px" }, value: t.pct, onInput: (e) => (t.pct = Number(e.target.value)) }), "%", h("button", { class: "btn ghost sm icon", onClick: () => { tiers.splice(i, 1); drawTiers(); } }, icon("x")))), h("button", { class: "btn sm", onClick: () => { tiers.push({ min_value: 0, pct: 0 }); drawTiers(); } }, icon("plus"), "Faixa")]);
  drawTiers();

  const save = async () => {
    try {
      const rv = routing.get(), sv = survey.get(), bv = bot.get(), blv = billing.get(), dv = disc.get(), cv = crm.get();
      await api.put("/api/v1/org/settings", { settings: { business_hours: hours, routing: { ...rv, sticky_agent_hours: rv.sticky_agent_hours ?? 0 }, survey: { ...sv, intro: sv.intro || "" }, bot: { ...bv, handoff_keywords: bv.handoff_keywords }, billing: blv, crm: cv, discount: { auto_approve_max_pct: dv.auto_approve_max_pct ?? 10, institutional_tiers: tiers } } });
      toast("Configurações salvas", "ok");
    } catch (e) { toastError(e); }
  };
  mount(body, [h("div", { class: "grid c2" },
    h("div", { class: "col gap-16" }, card("Horário de atendimento (fuso America/Sao_Paulo)", hoursBox), card("Roteamento de atendimentos", routing.el), card("Automação (bot) e handoff", bot.el), card("Integração CRM", crm.el)),
    h("div", { class: "col gap-16" }, card("Pesquisa de satisfação (CSAT / NPS / CES)", survey.el), card("Descontos institucionais", [disc.el, h("label", { class: "strong" }, "Faixas automáticas por valor da proposta"), h("div", { class: "mt-8" }, tiersBox)]), card("Faturamento (PIX)", billing.el))),
  h("div", { class: "mt-16 row", style: { justifyContent: "flex-end" } }, h("button", { class: "btn primary", onClick: save }, icon("check"), "Salvar configurações"))]);
}

// ---------------------------------------------------------------- Users
async function usersTab(body, reload) {
  const [{ items }, depts, roles] = await Promise.all([api.get("/api/v1/users"), api.get("/api/v1/departments"), api.get("/api/v1/roles")]);
  const dname = Object.fromEntries(depts.items.map((d) => [d.id, d.name]));
  const fields = (u) => [{ name: "name", label: "Nome", required: true }, { name: "email", label: "E-mail", type: "email", required: true }, { name: "role", label: "Perfil", type: "select", options: roles.roles.map((r) => ({ value: r.role, label: ROLE_LABEL[r.role] || r.role })) },
    { name: "password", label: u ? "Nova senha (opcional)" : "Senha inicial", type: "password", hint: "Mín. 10 caracteres, com maiúscula, minúscula, número e símbolo." }, { name: "skills", label: "Habilidades (roteamento skill-based)", split: true, placeholder: "consultoria, apoio_diagnostico" },
    { name: "max_concurrent_chats", label: "Conversas simultâneas", type: "number", min: "1", max: "50", value: 5 }, { name: "is_active", label: "Ativo", type: "checkbox" }];
  const depBox = (u) => { const sel = new Set(u?.department_ids || []); const w = h("div", { class: "grid c2" }, depts.items.map((d) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: sel.has(d.id), onChange: (e) => (e.target.checked ? sel.add(d.id) : sel.delete(d.id)) }), d.name))); return { w, get: () => [...sel] }; };
  const edit = (u) => { const dep = depBox(u); formModal({ title: u ? "Editar usuário" : "Novo usuário", size: "wide", fields: fields(u), values: u ? { ...u, skills: (u.skills || []).join(", "), password: "" } : { is_active: true, role: "agent" }, extra: h("div", h("label", { class: "strong" }, "Departamentos"), h("div", { class: "mt-8" }, dep.w)),
    onSubmit: async (v) => { const payload = { ...v, department_ids: dep.get(), skills: v.skills || [] }; if (!payload.password) delete payload.password; if (u) await api.patch(`/api/v1/users/${u.id}`, payload); else await api.post("/api/v1/users", payload); toast("Usuário salvo", "ok"); reload(); } }); };
  mount(body, card(null, [h("div", { class: "mb-12" }, h("button", { class: "btn primary", onClick: () => edit(null) }, icon("plus"), "Novo usuário")), dataTable({ rows: items, columns: [
    { label: "Usuário", render: (u) => h("div", h("b", u.name), h("small", u.email)) }, { label: "Perfil", render: (u) => badge(ROLE_LABEL[u.role] || u.role, "info") }, { label: "Departamentos", render: (u) => u.department_ids.map((i) => dname[i]).join(", ") || "—" },
    { label: "Habilidades", render: (u) => h("div", { class: "row wrap gap-4" }, (u.skills || []).map((s) => badge(s))) }, { label: "Status", render: (u) => (u.is_active ? badge("Ativo", "ok") : badge("Inativo", "danger")) }, { label: "Último acesso", render: (u) => dateTime(u.last_login_at) },
    { label: "", render: (u) => h("button", { class: "btn ghost sm icon", onClick: () => edit(u) }, icon("edit")) }] })]));
}

// ---------------------------------------------------------------- Departamentos
async function deptsTab(body, reload) {
  const { items } = await api.get("/api/v1/departments");
  const fields = [{ name: "name", label: "Nome", required: true }, { name: "routing_strategy", label: "Roteamento", type: "select", options: [["least_busy", "Menor carga"], ["round_robin", "Circular"], ["skill_based", "Por habilidade"]].map(([value, label]) => ({ value, label })) },
    { name: "sla_first_response_seconds", label: "SLA 1ª resposta (segundos)", type: "number", min: "10", value: 300 }, { name: "sla_solution_seconds", label: "SLA solução (segundos)", type: "number", min: "60", value: 3600 },
    { name: "required_skills", label: "Skills exigidas", split: true }, { name: "after_hours_message", label: "Mensagem fora do expediente", type: "textarea", full: true }, { name: "is_default", label: "Departamento padrão (entrada de novos tickets)", type: "checkbox", full: true }];
  const edit = (d) => formModal({ title: d ? "Editar departamento" : "Novo departamento", size: "wide", fields, values: d ? { ...d, required_skills: (d.required_skills || []).join(", ") } : {}, onSubmit: async (v) => {
    const p = { ...v, required_skills: v.required_skills || [], sla_first_response_seconds: v.sla_first_response_seconds || 300, sla_solution_seconds: v.sla_solution_seconds || 3600, business_hours: d?.business_hours || null }; if (d) await api.put(`/api/v1/departments/${d.id}`, p); else await api.post("/api/v1/departments", p); reload(); } });
  const mm = (s) => (s >= 3600 ? `${s / 3600}h` : `${Math.round(s / 60)}min`);
  mount(body, card(null, [h("div", { class: "mb-12" }, h("button", { class: "btn primary", onClick: () => edit(null) }, icon("plus"), "Novo departamento")), dataTable({ rows: items, columns: [
    { label: "Departamento", render: (d) => h("div", h("b", d.name), d.is_default ? badge("padrão", "info") : null) }, { label: "Roteamento", key: "routing_strategy" }, { label: "SLA 1ª resposta", render: (d) => mm(d.sla_first_response_seconds) }, { label: "SLA solução", render: (d) => mm(d.sla_solution_seconds) },
    { label: "", render: (d) => h("div", { class: "row gap-4" }, h("button", { class: "btn ghost sm icon", onClick: () => edit(d) }, icon("edit")), h("button", { class: "btn ghost sm icon", onClick: async () => { try { await api.del(`/api/v1/departments/${d.id}`); reload(); } catch (e) { toastError(e); } } }, icon("trash"))) }] })]));
}

// ---------------------------------------------------------------- WhatsApp
async function waTab(body, reload) {
  const [{ items, mode }, cfg] = await Promise.all([api.get("/api/v1/whatsapp/accounts"), api.get("/api/v1/whatsapp/signup/config")]);
  const depts = (await api.get("/api/v1/departments")).items;
  const startSignup = () => {
    if (!cfg.app_id || mode === "sandbox") return sandboxSignup();
    const load = () => new Promise((res) => { if (window.FB) return res(); window.fbAsyncInit = () => { FB.init({ appId: cfg.app_id, autoLogAppEvents: true, xfbml: false, version: cfg.graph_version }); res(); }; const s = document.createElement("script"); s.async = true; s.src = "https://connect.facebook.net/en_US/sdk.js"; document.body.append(s); });
    load().then(() => {
      let sessionInfo = null;
      const onMsg = (ev) => { if (!ev.origin.includes("facebook.com")) return; try { const d = JSON.parse(ev.data); if (d.type === "WA_EMBEDDED_SIGNUP" && d.event === "FINISH") sessionInfo = d.data; } catch { /* outros eventos */ } };
      window.addEventListener("message", onMsg);
      FB.login((resp) => { setTimeout(async () => { window.removeEventListener("message", onMsg);
        if (!resp.authResponse?.code || !sessionInfo) return toast("Onboarding cancelado ou incompleto.", "warn");
        try { await api.post("/api/v1/whatsapp/signup/complete", { code: resp.authResponse.code, waba_id: sessionInfo.waba_id, phone_number_id: sessionInfo.phone_number_id }); toast("Número conectado com sucesso!", "ok"); reload(); } catch (e) { toastError(e); } }, 600); },
      { config_id: cfg.config_id, response_type: "code", override_default_response_type: true, extras: { setup: {}, featureType: "", sessionInfoVersion: "3" } });
    });
  };
  const sandboxSignup = () => formModal({ title: "Conectar número (sandbox)", extra: h("div", { class: "callout" }, "Modo sandbox: o fluxo do Embedded Signup é simulado e nenhuma mensagem real é enviada. Em produção, configure META_APP_ID e META_EMBEDDED_SIGNUP_CONFIG_ID e META_MODE=live para abrir o fluxo oficial da Meta."),
    fields: [{ name: "label", label: "Nome da conexão", value: "WhatsApp principal" }, { name: "waba_id", label: "WABA ID", required: true, value: "100000000000011" }, { name: "phone_number_id", label: "Phone number ID", required: true, value: "100000000000012" }],
    onSubmit: async (v) => { await api.post("/api/v1/whatsapp/signup/complete", { ...v, code: "sandbox-code" }); toast("Número conectado (sandbox)", "ok"); reload(); } });
  const manual = () => formModal({ title: "Conexão manual (System User Token)", fields: [{ name: "label", label: "Nome", value: "WhatsApp" }, { name: "waba_id", label: "WABA ID", required: true }, { name: "phone_number_id", label: "Phone number ID", required: true }, { name: "access_token", label: "System User Access Token", type: "password", required: true, hint: "Armazenado com criptografia de envelope; nunca é exibido novamente." }],
    onSubmit: async (v) => { await api.post("/api/v1/whatsapp/accounts/manual", v); toast("Conta conectada", "ok"); reload(); } });
  const simulate = () => formModal({ title: "Simular mensagem de cliente (sandbox)", extra: h("div", { class: "callout" }, "Injeta no pipeline real um webhook idêntico ao da Meta. Abra a mesa de atendimento para acompanhar."),
    fields: [{ name: "from_number", label: "Número do cliente", value: "5562988880000", required: true }, { name: "name", label: "Nome no WhatsApp", value: "Dra. Cliente Teste" }, { name: "text", label: "Mensagem", type: "textarea", value: "Olá, gostaria de um orçamento para validação de painéis de citometria.", full: true }],
    onSubmit: async (v) => { await api.post("/api/v1/whatsapp/sandbox/simulate-inbound", v); toast("Mensagem injetada! Veja em Atendimentos.", "ok"); } });

  mount(body, [h("div", { class: "grid c-main" }, h("div", { class: "col gap-16" },
    card("Números conectados (WABA)", [dataTable({ rows: items, empty: "Nenhum número conectado. Use o Embedded Signup para vincular seu WABA.", columns: [
      { label: "Número", render: (a) => h("div", h("b", a.display_phone_number || a.phone_number_id), h("small", a.verified_name || a.label)) }, { label: "WABA", render: (a) => h("span", { class: "mono" }, a.waba_id) },
      { label: "Qualidade", render: (a) => badge(a.quality_rating || "—", a.quality_rating === "GREEN" ? "ok" : a.quality_rating ? "warn" : "") }, { label: "Limite", render: (a) => a.messaging_limit || "—" },
      { label: "Token", render: (a) => h("div", a.token_set ? badge("cifrado", "ok") : badge("ausente", "danger"), a.token_rotated_at ? h("small", `rotacionado ${dateTime(a.token_rotated_at)}`) : null) }, { label: "Status", render: (a) => badge(a.status, a.status === "active" ? "ok" : "danger") },
      { label: "Bot", render: (a) => h("label", { class: "switch" }, h("input", { type: "checkbox", checked: a.bot_enabled, onChange: async (e) => { await api.patch(`/api/v1/whatsapp/accounts/${a.id}`, { bot_enabled: e.target.checked }); } }), h("span")) },
      { label: "Depto. padrão", render: (a) => h("select", { class: "select", style: { width: "150px" }, onChange: async (e) => { await api.patch(`/api/v1/whatsapp/accounts/${a.id}`, { default_department_id: e.target.value || null }); toast("Atualizado", "ok"); } }, h("option", { value: "" }, "—"), depts.map((d) => h("option", { value: d.id, selected: d.id === a.default_department_id }, d.name))) },
      { label: "", render: (a) => h("div", { class: "row gap-4" }, h("button", { class: "btn sm", title: "Verificar saúde do número", onClick: async () => { await api.get(`/api/v1/whatsapp/accounts/${a.id}/health`); toast("Saúde verificada", "ok"); reload(); } }, icon("refresh")),
        h("button", { class: "btn sm", title: "Rotacionar token agora", onClick: async () => { await api.post(`/api/v1/whatsapp/accounts/${a.id}/rotate-token`); toast("Token rotacionado", "ok"); reload(); } }, icon("key")),
        h("button", { class: "btn sm danger", onClick: async () => { await api.del(`/api/v1/whatsapp/accounts/${a.id}`); reload(); } }, "Desconectar")) }] }),
    h("div", { class: "row mt-12" }, h("button", { class: "btn primary", onClick: startSignup }, icon("phone"), "Conectar número (Embedded Signup)"), h("button", { class: "btn", onClick: manual }, "Conexão manual"))])),
  h("div", { class: "col gap-16" }, card("Webhook da Meta", [h("div", { class: "kv" }, h("span", "Modo"), h("span", badge(mode, mode === "live" ? "ok" : "warn"))), h("div", { class: "field mt-8" }, h("label", "URL de callback"), h("input", { class: "input mono", readOnly: true, value: cfg.webhook_url })),
    h("div", { class: "hint muted" }, "Configure esta URL e o token de verificação (META_WEBHOOK_VERIFY_TOKEN) no App da Meta. Assine o campo ", h("b", "messages"), ". A assinatura X-Hub-Signature-256 é validada e o endpoint responde 200 em milissegundos, enfileirando o processamento.")]),
  mode === "sandbox" ? card("Simulador (sandbox)", [h("p", { class: "muted" }, "Teste o fluxo completo — bot, handoff, roteamento, SLA e pesquisa — sem credenciais da Meta."), h("button", { class: "btn", onClick: simulate }, icon("send"), "Simular mensagem recebida")]) : null))]);
}

// ---------------------------------------------------------------- Minha conta
async function meTab(body) {
  const f = form([{ name: "current_password", label: "Senha atual", type: "password", required: true }, { name: "new_password", label: "Nova senha", type: "password", required: true, hint: "Mín. 10 caracteres, com maiúscula, minúscula, número e símbolo." }]);
  mount(body, h("div", { class: "grid c2" }, card("Perfil", h("div", [h("div", { class: "kv" }, h("span", "Nome"), h("b", state.user.name)), h("div", { class: "kv" }, h("span", "E-mail"), h("b", state.user.email)), h("div", { class: "kv" }, h("span", "Perfil"), badge(ROLE_LABEL[state.user.role], "info")),
    h("h4", { class: "mt-16 mb-8" }, "Permissões"), h("div", { class: "row wrap gap-4" }, [...state.permissions].sort().map((p) => badge(p)))])),
  card("Alterar senha", [f.el, h("button", { class: "btn primary", onClick: async () => { try { const v = f.get(); await api.post("/api/v1/auth/change-password", v); toast("Senha alterada. Faça login novamente.", "ok"); setTimeout(() => window.dispatchEvent(new Event("fm:logout")), 900); } catch (e) { toastError(e); } } }, "Alterar senha")])));
}
