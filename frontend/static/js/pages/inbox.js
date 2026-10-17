import { api } from "../lib/api.js";
import { h, icon, mount, clear, avatar, ago, dateTime, timeOnly, dur, debounce, date, rich } from "../lib/dom.js";
import { can, state, STATUS_LABEL } from "../lib/store.js";
import { badge, confirmDialog, emptyState, formModal, openModal, popMenu, promptDialog, spinner, toast, toastError, segmented, searchBox } from "../lib/ui.js";
import { emit, isConnected } from "../lib/socket.js";

const VIEW_LABEL = { mine: "Minhas", queue: "Fila", bot: "Bot", open: "Todas abertas", closed: "Encerradas" };
const STATUS_KIND = { pending_bot: "", queued: "warn", in_progress: "info", pending_client: "", resolved: "ok", closed: "ok" };
const QUICK = ["Olá! Em que posso ajudar?", "Um instante, por favor, vou verificar para você.", "Poderia me enviar mais detalhes, por favor?", "Obrigado pelo contato! Posso ajudar em mais alguma coisa?"];

export default async function inbox(ctx) {
  const isSup = can("inbox:all");
  let view = ctx.query?.view || "mine", q = "", tickets = [], counts = {}, current = null, typingTimer = null, noteMode = false;
  let selectedId = ctx.params.id || null;
  let joined = null;
  const mediaCache = new Map();

  // ------- elementos
  const listEl = h("div", { class: "tickets" });
  const searchEl = searchBox("Buscar por nome, número ou assunto…", (v) => { q = v; loadList(); });
  const viewSeg = h("div");
  const chatEl = h("section", { class: "chat" }, h("div", { class: "empty", style: { margin: "auto" } }, icon("chat"), h("div", "Selecione um atendimento para começar.")));
  const infoEl = h("aside", { class: "info-panel" });
  const root = h("div", { class: "inbox" }, h("section", { class: "inbox-list" }, h("div", { class: "head" }, viewSeg, searchEl), listEl), chatEl, infoEl);

  const drawViews = () => mount(viewSeg, segmentedViews());
  function segmentedViews() {
    const items = [["mine", "Minhas"], ["queue", `Fila${counts.queue ? ` (${counts.queue})` : ""}`], ...(isSup ? [["bot", "Bot"], ["open", "Abertas"]] : []), ["closed", "Encerr."]];
    return h("div", { class: "seg", style: { width: "100%", display: "flex" } }, items.map(([id, label]) => h("button", { class: id === view ? "active" : "", style: { flex: 1 }, onClick: () => { view = id; drawViews(); loadList(); } }, label)));
  }

  // ------- lista
  async function loadList() {
    const r = await api.get("/api/v1/tickets", { view, q, limit: 80 });
    tickets = r.items; counts = { ...counts, ...r.counts };
    if (view !== "queue" && r.counts?.queue !== undefined) counts.queue = r.counts.queue;
    drawViews(); drawList();
  }
  function drawList() {
    if (!tickets.length) return mount(listEl, emptyState(view === "queue" ? "Fila vazia." : "Nenhum atendimento aqui.", "chat"));
    mount(listEl, tickets.map((t) => h("div", { class: `ticket-item ${t.id === selectedId ? "active" : ""}`, onClick: () => select(t.id) },
      h("div", { style: { position: "relative" } }, avatar(t.contact?.name || t.contact?.wa_id), t.sla.frt === "breached" || t.sla.solution === "breached" ? h("span", { class: "dot breached", style: { position: "absolute", right: "-2px", top: "-2px" } }) : t.sla.frt === "at_risk" ? h("span", { class: "dot at_risk", style: { position: "absolute", right: "-2px", top: "-2px" } }) : null),
      h("div", { class: "grow" }, h("div", { class: "name truncate" }, t.contact?.name || t.contact?.wa_id || "Contato"),
        h("div", { class: "prev truncate" }, t.last_message_preview || "—"),
        h("div", { class: "row gap-4 mt-8" }, badge(STATUS_LABEL[t.status], STATUS_KIND[t.status]), t.department_name ? badge(t.department_name) : null, t.priority === "urgent" || t.priority === "high" ? badge(t.priority === "urgent" ? "Urgente" : "Alta", "danger") : null)),
      h("div", { class: "meta" }, h("span", ago(t.last_message_at || t.created_at)), t.unread_count ? h("span", { class: "unread" }, t.unread_count) : null))));
  }

  // ------- selection
  async function select(id) {
    selectedId = id;
    history.replaceState(null, "", `#/inbox/${id}`);
    if (joined && joined !== id) emit("leave_ticket", { ticket_id: joined });
    emit("join_ticket", { ticket_id: id }); joined = id;
    drawList();
    mount(chatEl, h("div", { class: "spinner" }));
    await loadChat();
  }

  async function loadChat() {
    if (!selectedId) return;
    try {
      const [t, m] = await Promise.all([api.get(`/api/v1/tickets/${selectedId}`), api.get(`/api/v1/tickets/${selectedId}/messages`, { limit: 100 })]);
      current = { ticket: t, messages: m.messages, notes: m.notes };
      drawChat(); drawInfo();
      if (t.unread_count) { api.post(`/api/v1/tickets/${t.id}/read`).then(() => { t.unread_count = 0; const row = tickets.find((x) => x.id === t.id); if (row) { row.unread_count = 0; drawList(); } }).catch(() => {}); }
    } catch (e) { mount(chatEl, h("div", { class: "callout warn", style: { margin: "20px" } }, e.message)); }
  }

  // ------- chat
  const bodyEl = h("div", { class: "chat-body" });
  const typingEl = h("div", { class: "typing" });
  const ta = h("textarea", { placeholder: "Escreva uma mensagem… (Enter envia · Shift+Enter quebra linha)", rows: 1 });
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 140) + "px"; if (!noteMode && selectedId) { emit("typing", { ticket_id: selectedId, typing: true }); clearTimeout(typingTimer); typingTimer = setTimeout(() => emit("typing", { ticket_id: selectedId, typing: false }), 2500); } });
  const fileIn = h("input", { type: "file", class: "hidden", onChange: async () => { const f = fileIn.files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f); try { await api.upload(`/api/v1/tickets/${selectedId}/media`, fd); fileIn.value = ""; } catch (e) { toastError(e); } } });

  async function send() {
    const text = ta.value.trim();
    if (!text || !current) return;
    ta.value = ""; ta.style.height = "auto";
    try {
      if (noteMode) await api.post(`/api/v1/tickets/${selectedId}/notes`, { body: text });
      else await api.post(`/api/v1/tickets/${selectedId}/messages`, { type: "text", body: text });
      await refreshMessages();
    } catch (e) { ta.value = text; toastError(e); }
  }

  async function refreshMessages() {
    const m = await api.get(`/api/v1/tickets/${selectedId}/messages`, { limit: 100 });
    current.messages = m.messages; current.notes = m.notes; drawMessages();
    const t = await api.get(`/api/v1/tickets/${selectedId}`); current.ticket = t; drawHeader(); drawInfo(); loadList();
  }

  const headEl = h("div", { class: "chat-head" });
  const bannerEl = h("div");
  const composerEl = h("div", { class: "composer" });

  function drawChat() {
    mount(chatEl, headEl, bannerEl, bodyEl, typingEl, composerEl, fileIn);
    drawHeader(); drawMessages(); drawComposer();
  }

  function drawHeader() {
    const t = current.ticket, c = t.contact || {};
    const mine = t.assigned_agent_id === state.user.id;
    const closed = t.status === "closed" || t.status === "resolved";
    mount(headEl,
      avatar(c.name || c.wa_id, "lg"),
      h("div", { class: "grow" }, h("div", { class: "row" }, h("b", { style: { fontSize: "15px" } }, c.name || "Contato"), badge(STATUS_LABEL[t.status], STATUS_KIND[t.status]), t.bot_active ? badge("bot ativo", "ok") : null),
        h("div", { class: "row muted", style: { fontSize: "12px" } }, h("span", { class: "mono", id: "phoneTxt" }, c.wa_id), c.masked && can("pii:reveal") ? h("button", { class: "btn ghost sm icon", title: "Revelar telefone (auditado)", onClick: async () => { const r = await api.post(`/api/v1/contacts/${c.id}/reveal`); c.wa_id = r.wa_id; c.masked = false; headEl.querySelector("#phoneTxt").textContent = r.wa_id; toast("Acesso registrado na trilha de auditoria", "info"); } }, icon("eye")) : null,
          t.department_name ? ["· ", t.department_name] : null, t.agent_name ? ["· ", t.agent_name] : null)),
      h("div", { class: "row wrap" },
        !t.assigned_agent_id && !closed ? h("button", { class: "btn sm primary", onClick: () => act(() => api.post(`/api/v1/tickets/${t.id}/assign`, {}), "Atendimento assumido") }, "Assumir") : null,
        isSup && t.assigned_agent_id && !mine && !closed ? h("button", { class: "btn sm", onClick: () => act(() => api.post(`/api/v1/supervisor/tickets/${t.id}/barge-in`), "Você assumiu a conversa") }, icon("zap"), "Barge-in") : null,
        isSup && t.assigned_agent_id && !mine && !closed ? h("button", { class: "btn sm", onClick: async () => { const b = await promptDialog({ title: "Sussurrar ao atendente", label: "Orientação (o cliente nunca vê)", required: true }); if (b) act(() => api.post(`/api/v1/supervisor/tickets/${t.id}/whisper`, { body: b }), "Sussurro enviado"); } }, icon("whisper"), "Sussurrar") : null,
        !closed && can("inbox:transfer") ? h("button", { class: "btn sm", onClick: () => transfer(t) }, icon("repeat"), "Transferir") : null,
        !closed ? h("button", { class: "btn sm", onClick: (e) => popMenu(e.currentTarget, [
          { label: "Aguardando cliente", icon: "clock", onClick: () => act(() => api.post(`/api/v1/tickets/${t.id}/status`, { status: "pending_client" })) },
          { label: "Marcar resolvido", icon: "check", onClick: () => act(() => api.post(`/api/v1/tickets/${t.id}/status`, { status: "resolved" })) },
          { label: "Gerar resumo (IA)", icon: "zap", onClick: async () => { const r = await api.post(`/api/v1/tickets/${t.id}/summary`); current.ticket.summary = r.summary; drawInfo(); } },
          { label: "Vincular a conta do CRM", icon: "link", onClick: () => linkAccount(t) }]) }, icon("more")) : null,
        !closed && can("inbox:close") ? h("button", { class: "btn sm ok", onClick: async () => { if (await confirmDialog({ title: "Encerrar atendimento", message: "O cliente poderá receber uma pesquisa de satisfação em instantes. Encerrar?", confirmLabel: "Encerrar" })) act(() => api.post(`/api/v1/tickets/${t.id}/close`), "Atendimento encerrado"); } }, icon("check"), "Encerrar") : null,
        closed ? h("button", { class: "btn sm", onClick: () => act(() => api.post(`/api/v1/tickets/${t.id}/reopen`), "Atendimento reaberto") }, "Reabrir") : null));
    mount(bannerEl, !t.window_open && !closed ? h("div", { class: "window-banner" }, icon("alert"), h("span", { class: "grow" }, "A janela de 24h está encerrada: só é possível enviar templates aprovados (HSM)."), h("button", { class: "btn sm", onClick: () => sendTemplate() }, "Enviar template")) : null);
  }

  async function act(fn, okMsg) {
    try { await fn(); if (okMsg) toast(okMsg, "ok"); await refreshMessages(); } catch (e) { toastError(e); }
  }

  async function transfer(t) {
    const [depts, users] = await Promise.all([api.get("/api/v1/departments"), api.get("/api/v1/users")]);
    formModal({ title: "Transferir atendimento", extra: h("div", { class: "callout" }, "O histórico completo (mensagens, notas e resumo) acompanha o atendimento."),
      fields: [{ name: "department_id", label: "Departamento de destino", type: "select", full: true, options: [{ value: "", label: "— manter —" }, ...depts.items.map((d) => ({ value: d.id, label: d.name }))] },
        { name: "agent_id", label: "Agente específico (opcional)", type: "select", full: true, options: [{ value: "", label: "— qualquer agente do departamento —" }, ...users.items.filter((u) => ["agent", "consultor"].includes(u.role) && u.id !== t.assigned_agent_id).map((u) => ({ value: u.id, label: `${u.name} (${u.status === "available" ? "disponível" : u.status})` }))] },
        { name: "reason", label: "Motivo / contexto para o próximo atendente", type: "textarea", full: true }],
      onSubmit: async (v) => { if (!v.department_id && !v.agent_id) throw new Error("Escolha um departamento ou agente"); await api.post(`/api/v1/tickets/${t.id}/transfer`, v); toast("Atendimento transferido", "ok"); await loadList(); if (!isSup) { selectedId = null; mount(chatEl, emptyState("Atendimento transferido.", "check")); mount(infoEl); } else await refreshMessages(); } });
  }

  async function linkAccount(t) {
    const accs = await api.get("/api/v1/accounts", { limit: 200 });
    formModal({ title: "Vincular contato a uma conta", fields: [{ name: "account_id", label: "Conta institucional", type: "select", full: true, required: true, options: accs.items.map((a) => ({ value: a.id, label: a.trade_name || a.corporate_name })) }, { name: "role_description", label: "Função do contato", full: true }],
      onSubmit: async (v) => { await api.post(`/api/v1/tickets/${t.id}/link-account`, v); toast("Contato associado à conta", "ok"); } });
  }

  async function sendTemplate() {
    const { items } = await api.get("/api/v1/templates", { status: "approved" });
    if (!items.length) return toast("Nenhum template aprovado. Crie e aprove em Templates HSM.", "warn");
    const sel = h("select", { class: "select" }, items.map((t) => h("option", { value: t.id }, `${t.name} (${t.category})`)));
    const vars = h("div");
    const preview = h("pre", { class: "code" });
    let values = {};
    const draw = () => {
      const tpl = items.find((x) => x.id === sel.value);
      const body = tpl.components.find((c) => c.type.toUpperCase() === "BODY")?.text || "";
      const nums = [...new Set([...body.matchAll(/\{\{(\d+)\}\}/g)].map((m) => m[1]))];
      values = Object.fromEntries(nums.map((n) => [n, values[n] || (n === "1" ? current.ticket.contact?.name || "" : "")]));
      mount(vars, nums.map((n) => h("div", { class: "field" }, h("label", `Variável {{${n}}}`), h("input", { class: "input", value: values[n], onInput: (e) => { values[n] = e.target.value; paint(); } }))));
      const paint = () => { preview.textContent = body.replace(/\{\{(\d+)\}\}/g, (m, n) => values[n] || m); }; paint();
    };
    sel.addEventListener("change", () => { values = {}; draw(); }); draw();
    const m = openModal({ title: "Enviar template (HSM)", body: [h("div", { class: "field" }, h("label", "Template aprovado"), sel), vars, h("div", { class: "field" }, h("label", "Pré-visualização"), preview)],
      footer: [h("button", { class: "btn", onClick: () => m.close() }, "Cancelar"), h("button", { class: "btn primary", onClick: async () => {
        try { await api.post(`/api/v1/tickets/${selectedId}/messages`, { type: "template", template_id: sel.value, variables: values }); m.close(); toast("Template enviado", "ok"); await refreshMessages(); } catch (e) { toastError(e); } } }, icon("send"), "Enviar")] });
  }

  function drawComposer() {
    const t = current.ticket;
    const closed = t.status === "closed";
    ta.disabled = closed && !noteMode;
    composerEl.classList.toggle("note-mode", noteMode);
    ta.placeholder = noteMode ? "Nota interna — invisível para o cliente…" : t.window_open || closed ? "Escreva uma mensagem… (Enter envia · Shift+Enter quebra linha)" : "Janela de 24h encerrada — use um template";
    mount(composerEl,
      h("div", { class: "row mb-8" }, segmentedMode(), h("div", { class: "grow" }),
        !noteMode ? h("button", { class: "btn ghost sm", onClick: (e) => popMenu(e.currentTarget, QUICK.map((qr) => ({ label: qr, onClick: () => { ta.value = qr; ta.focus(); } }))) }, icon("zap"), "Respostas rápidas") : null,
        !noteMode ? h("button", { class: "btn ghost sm", onClick: () => sendTemplate() }, icon("template"), "Template") : null,
        !noteMode ? h("button", { class: "btn ghost sm icon", title: "Anexar arquivo", onClick: () => fileIn.click() }, icon("paperclip")) : null),
      h("div", { class: "row", style: { alignItems: "flex-end" } }, h("div", { class: "grow" }, ta), h("button", { class: `btn ${noteMode ? "" : "primary"} icon`, style: { height: "44px", width: "44px" }, onClick: send, title: "Enviar" }, icon("send"))));
  }
  const segmentedMode = () => h("div", { class: "seg" }, h("button", { class: noteMode ? "" : "active", onClick: () => { noteMode = false; drawComposer(); } }, "Responder"), h("button", { class: noteMode ? "active" : "", onClick: () => { noteMode = true; drawComposer(); } }, icon("note", "sm"), " Nota interna"));

  // ------- mensagens
  function drawMessages() {
    const items = [
      ...current.messages.map((m) => ({ kind: "msg", at: m.created_at, m })),
      ...current.notes.map((n) => ({ kind: "note", at: n.created_at, n })),
    ].sort((a, b) => new Date(a.at) - new Date(b.at));
    let lastDay = "";
    const nodes = [];
    for (const it of items) {
      const day = date(it.at);
      if (day !== lastDay) { nodes.push(h("div", { class: "day-sep" }, day)); lastDay = day; }
      nodes.push(it.kind === "msg" ? bubble(it.m) : noteBubble(it.n));
    }
    mount(bodyEl, nodes.length ? nodes : h("div", { class: "empty" }, "Sem mensagens."));
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function ticks(m) {
    if (m.direction !== "outbound") return null;
    const map = { queued: ["clock", ""], sent: ["check", ""], delivered: ["check", ""], read: ["check", "read"], failed: ["alert", "failed"] };
    const [ic, cls] = map[m.status] || ["check", ""];
    return h("span", { class: `ticks ${cls}`, title: m.error || m.status, dataset: { mid: m.id } }, icon(ic, "sm"), m.status === "delivered" || m.status === "read" ? icon("check", "sm") : null);
  }

  function bubble(m) {
    const out = m.direction === "outbound";
    const el = h("div", { class: `bubble ${out ? "out" : "in"} ${m.is_bot ? "bot" : ""}`, dataset: { id: m.id } });
    if (m.is_bot) el.append(h("span", { class: "who" }, "🤖 Assistente virtual"));
    else if (out && m.sender_user_id && m.sender_user_id !== state.user.id) el.append(h("span", { class: "who" }, "Atendente"));
    if (m.media_asset_id) el.append(mediaEl(m));
    if (m.body && !(m.media_asset_id && m.body === "[image]")) el.append(h("div", rich(m.body)));
    const p = m.payload || {};
    if (p.kind === "list") el.append(h("div", { class: "opts" }, p.rows.map((r) => h("div", r.title))));
    if (p.kind === "buttons") el.append(h("div", { class: "opts" }, p.buttons.map((b) => h("div", b[1]))));
    if (m.type === "template") el.append(h("div", { class: "muted", style: { fontSize: "11px" } }, "Template HSM"));
    el.append(h("div", { class: "time" }, timeOnly(m.created_at), ticks(m)));
    return el;
  }
  function mediaEl(m) {
    const box = h("div");
    (async () => {
      try {
        let info = mediaCache.get(m.media_asset_id);
        if (!info) { info = await api.get(`/api/v1/media/${m.media_asset_id}/url`); mediaCache.set(m.media_asset_id, info); }
        const mime = info.mime_type || "";
        if (mime.startsWith("image/")) box.append(h("img", { class: "media", src: info.url, alt: info.filename || "imagem", loading: "lazy" }));
        else if (mime.startsWith("audio/")) box.append(h("audio", { controls: true, src: info.url }));
        else box.append(h("a", { href: info.url, target: "_blank", rel: "noopener", class: "row" }, icon("file"), info.filename || "arquivo"));
      } catch { box.append(h("span", { class: "muted" }, "Mídia indisponível")); }
    })();
    return box;
  }
  function noteBubble(n) {
    const whisper = n.kind === "whisper";
    return h("div", { class: `bubble ${whisper ? "whisper" : "note"}` }, h("span", { class: "who", style: { color: whisper ? "#BE185D" : "var(--warn-text)" } }, whisper ? `💬 Sussurro de ${n.author_name} (só você vê)` : `📝 Nota interna — ${n.author_name}`), h("div", n.body), h("div", { class: "time" }, timeOnly(n.created_at)));
  }

  // ------- info panel
  async function drawInfo() {
    if (!current) return;
    const t = current.ticket, c = t.contact || {};
    const sla = t.sla;
    const slaLine = (label, st, rem) => h("div", { class: "kv" }, h("span", label), h("span", { class: "row gap-4" }, h("span", { class: `dot ${st === "done" ? "ok" : st}` }), st === "done" ? "cumprido" : rem !== undefined ? (rem >= 0 ? `restam ${dur(rem)}` : `estourado há ${dur(-rem)}`) : "—"));
    mount(infoEl,
      h("h4", "Contato"), h("div", { class: "row" }, avatar(c.name, "lg"), h("div", h("b", c.name || "—"), h("div", { class: "muted" }, c.job_title || ""))),
      h("div", { class: "kv mt-8" }, h("span", "E-mail"), h("span", c.email || "—")), h("div", { class: "kv" }, h("span", "Origem"), h("span", c.source || "—")), c.opted_out_at ? badge("Opt-out (LGPD)", "danger") : null,
      h("h4", "Atendimento"), h("div", { class: "kv" }, h("span", "Aberto"), h("span", dateTime(t.created_at))), h("div", { class: "kv" }, h("span", "Prioridade"), h("span", t.priority)),
      t.first_response_time_seconds != null ? h("div", { class: "kv" }, h("span", "1ª resposta"), h("span", dur(t.first_response_time_seconds))) : null,
      t.handling_time_seconds != null ? h("div", { class: "kv" }, h("span", "Duração (TMA)"), h("span", dur(t.handling_time_seconds))) : null,
      t.wait_time_seconds != null ? h("div", { class: "kv" }, h("span", "Tempo em fila"), h("span", dur(t.wait_time_seconds))) : null,
      h("h4", "SLA"), slaLine("1ª resposta", sla.frt, sla.frt_remaining_seconds), slaLine("Solução", sla.solution, sla.solution_remaining_seconds),
      t.summary ? [h("h4", "Resumo (IA)"), h("p", { style: { margin: 0, fontSize: "12.5px" } }, t.summary)] : null,
      t.deals?.length ? [h("h4", "Oportunidades"), t.deals.map((d) => h("div", h("a", { href: "#/pipeline" }, d.title), " ", badge(d.stage)))] : null,
      h("div", { id: "evBox" }));
    try {
      const ev = await api.get(`/api/v1/tickets/${t.id}/events`);
      const LABEL = { created: "Atendimento criado", status_change: "Status", agent_assigned: "Atribuído", transferred: "Transferido", handoff: "Handoff bot → humano", first_response: "1ª resposta", sla_breached: "SLA estourado", reopened: "Reaberto", barge_in: "Barge-in", whisper: "Sussurro", survey_sent: "Pesquisa enviada", agent_unassigned: "Desatribuído" };
      const box = infoEl.querySelector("#evBox");
      if (box && current?.ticket.id === t.id) mount(box, h("h4", "Histórico"), h("div", { class: "timeline" }, ev.items.slice(-12).map((e) => h("div", { class: `tl-item ${e.event_type === "sla_breached" ? "warn" : ""}` }, h("div", { style: { fontSize: "12.5px" } }, LABEL[e.event_type] || e.event_type, e.new_value && e.event_type === "status_change" ? `: ${STATUS_LABEL[e.new_value] || e.new_value}` : "", e.meta?.agent_name ? ` → ${e.meta.agent_name}` : ""), h("div", { class: "when" }, dateTime(e.created_at))))));
    } catch { /* no history */ }
  }

  // ------- tempo real
  ctx.on("message", (m) => {
    if (!current || m.ticket_id !== selectedId || current.messages.some((x) => x.id === m.id)) return;
    current.messages.push(m); drawMessages();
    if (m.direction === "inbound") api.post(`/api/v1/tickets/${selectedId}/read`).catch(() => {});
  });
  ctx.on("message_status", (s) => {
    if (!current || s.ticket_id !== selectedId) return;
    const m = current.messages.find((x) => x.id === s.message_id);
    if (m) { m.status = s.status; m.error = s.error; const old = bodyEl.querySelector(`.bubble[data-id="${m.id}"]`); if (old) old.replaceWith(bubble(m)); }
  });
  ctx.on("note", (n) => { if (current && n.ticket_id === selectedId && n.author_id !== state.user.id) refreshMessages(); });
  ctx.on("whisper", (w) => { if (current && w.ticket_id === selectedId) refreshMessages(); });
  const reloadList = debounce(() => { if (ctx.alive()) loadList(); }, 400);
  ctx.on("ticket_updated", reloadList);
  ctx.on("ticket_event", (e) => { reloadList(); if (current && e.ticket.ticket_id === selectedId && ctx.alive()) { api.get(`/api/v1/tickets/${selectedId}`).then((t) => { current.ticket = t; drawHeader(); drawComposer(); drawInfo(); }).catch(() => {}); } });
  ctx.on("typing", (t) => {
    if (t.ticket_id !== selectedId) return;
    typingEl.textContent = t.typing ? `${t.name} está digitando…` : "";
    clearTimeout(typingEl._t); typingEl._t = setTimeout(() => (typingEl.textContent = ""), 3500);
  });
  ctx.onCleanup(() => { if (joined) emit("leave_ticket", { ticket_id: joined }); clearTimeout(typingTimer); });

  drawViews();
  await loadList();
  if (selectedId) { const t0 = tickets.find((t) => t.id === selectedId); if (!t0 && isSup) view = "open"; await select(selectedId); }
  const poll = setInterval(() => { if (ctx.alive() && !isConnected()) loadList(); }, 15000);
  ctx.onCleanup(() => clearInterval(poll));
  return root;
}
