import { api } from "../lib/api.js";
import { h, icon, mount, avatar, dur, ago, num, debounce } from "../lib/dom.js";
import { STATUS_LABEL } from "../lib/store.js";
import { badge, card, emptyState, kpi, pageHead, progress, popMenu, promptDialog, toast, toastError } from "../lib/ui.js";

const AG = { available: "Disponível", busy: "Ocupado", paused: "Em pausa", offline: "Offline" };

export default async function supervisor(ctx) {
  const root = h("div");
  const live = h("span", { class: "badge ok" }, h("span", { class: "dot available" }), "ao vivo");
  async function load() {
    if (!ctx.alive()) return;
    let d;
    try { d = await api.get("/api/v1/supervisor/live"); } catch (e) { return mount(root, h("div", { class: "callout warn" }, e.message)); }
    const t = d.totals;
    mount(root, [
      pageHead("Supervisão ao vivo", "Filas, conversas abertas, agentes e alertas de SLA em tempo real. Você pode sussurrar ao atendente ou assumir a conversa (barge-in).", live),
      h("div", { class: "grid c5 mb-16" }, kpi("Na fila", t.waiting, `${t.open} abertos no total`, "clock", t.waiting ? "warn" : ""), kpi("Em atendimento", t.in_progress, `${t.bot} com o bot`, "chat"),
        kpi("Agentes online", t.agents_online, `${d.agents.length} cadastrados`, "users", "ok"), kpi("1ª resposta (hoje)", dur(t.avg_frt_today), `${t.created_today} criados · ${t.closed_today} encerrados`, "zap"), kpi("CSAT (hoje)", t.csat_today ?? "—", "média das avaliações", "star", "ok")),
      d.sla_alerts.length ? card("Alertas de SLA", d.sla_alerts.map((a) => h("div", { class: `alert-row ${a.level}` }, icon("alert"), h("div", { class: "grow" }, h("b", a.contact_name || "Contato"), ` — ${a.department || "sem departamento"}`, a.agent ? ` · ${a.agent}` : " · sem agente"),
        badge(a.level === "breached" ? "SLA estourado" : "SLA em risco", a.level === "breached" ? "danger" : "warn"), h("a", { class: "btn sm", href: `#/inbox/${a.ticket_id}` }, "Abrir")))) : null,
      h("div", { class: "grid c-main mt-16" },
        h("div", { class: "col gap-16" },
          card("Filas por departamento", h("div", { class: "grid c3" }, d.queues.map((q) => h("div", { class: "queue-card" }, h("div", { class: "muted" }, q.name), h("div", { class: "n" }, q.waiting), h("div", { class: "muted", style: { fontSize: "12px" } }, q.waiting ? `mais antigo: ${dur(q.oldest_wait_seconds)}` : "sem espera"), h("div", { class: "row gap-4 mt-8" }, badge(`${q.in_progress} em atend.`, "info"), q.bot ? badge(`${q.bot} bot`) : null))))),
          card("Conversas abertas", d.tickets.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", h("tr", ["Contato", "Departamento", "Agente", "Status", "Espera", "SLA", ""].map((x) => h("th", x)))),
            h("tbody", d.tickets.map((tk) => h("tr", h("td", h("div", { class: "strong" }, tk.contact?.name || "—"), h("small", tk.last_message_preview || "")), h("td", tk.department_name || "—"), h("td", tk.agent_name || h("span", { class: "muted" }, "—")),
              h("td", badge(STATUS_LABEL[tk.status], tk.status === "queued" ? "warn" : "info")), h("td", dur(tk.wait_seconds)), h("td", h("span", { class: `dot ${tk.sla.frt === "done" ? tk.sla.solution : tk.sla.frt}` })),
              h("td", h("div", { class: "row gap-4" }, h("a", { class: "btn sm", href: `#/inbox/${tk.id}` }, "Abrir"), tk.agent_name ? h("button", { class: "btn sm", title: "Sussurrar", onClick: async () => { const b = await promptDialog({ title: `Sussurrar a ${tk.agent_name}`, label: "Orientação (o cliente nunca vê)", required: true }); if (b) { try { await api.post(`/api/v1/supervisor/tickets/${tk.id}/whisper`, { body: b }); toast("Sussurro enviado", "ok"); } catch (e) { toastError(e); } } } }, icon("whisper")) : null,
                h("button", { class: "btn sm", title: "Assumir conversa (barge-in)", onClick: async () => { try { await api.post(`/api/v1/supervisor/tickets/${tk.id}/barge-in`); ctx.navigate(`#/inbox/${tk.id}`); } catch (e) { toastError(e); } } }, icon("zap"))))))))) : emptyState("Nenhuma conversa aberta.", "chat"))),
        card("Agentes", h("div", { class: "col" }, d.agents.map((a) => h("div", { class: "agent-card" }, avatar(a.name), h("div", { class: "grow" }, h("div", { class: "strong" }, a.name), h("div", { class: "muted", style: { fontSize: "12px" } }, a.skills?.join(", ") || "sem skills")),
          h("div", { style: { width: "96px" } }, h("div", { class: "row between", style: { fontSize: "11.5px" } }, h("span", { class: "row gap-4" }, h("span", { class: `dot ${a.status}` }), AG[a.status]), h("b", `${a.active_chats}/${a.max_chats}`)), progress((a.active_chats / a.max_chats) * 100, a.active_chats >= a.max_chats ? "danger" : "ok"))))))),
    ]);
  }
  const reload = debounce(load, 600);
  for (const ev of ["ticket_event", "ticket_updated", "agent_status", "sla_alert", "survey"]) ctx.on(ev, reload);
  const iv = setInterval(load, 10000);
  ctx.onCleanup(() => clearInterval(iv));
  await load();
  return root;
}
