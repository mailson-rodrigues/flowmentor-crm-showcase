import { api } from "../lib/api.js";
import { h, money, moneyShort, num, pct, dur } from "../lib/dom.js";
import { state, STAGE_LABEL, CASE_TYPE_LABEL } from "../lib/store.js";
import { card, kpi, pageHead, progress } from "../lib/ui.js";
import { barChart, donut, hbars } from "../lib/charts.js";

export default async function dashboard(ctx) {
  const d = await api.get("/api/v1/dashboard");
  const root = h("div");
  const hour = new Date().getHours();
  root.append(pageHead(`${hour < 12 ? "Bom dia" : hour < 18 ? "Boa tarde" : "Boa noite"}, ${state.user.name.split(" ")[0]}`, "Visão consolidada de vendas, operação, contratos e atendimento."));

  if (d.commercial) {
    const c = d.commercial;
    root.append(h("div", { class: "grid c4 mb-16" },
      kpi("Pipeline em aberto", moneyShort(c.pipeline_value), `${c.open_deals} oportunidades`, "pipeline"),
      kpi("Previsão ponderada", moneyShort(c.weighted_forecast), "valor × probabilidade do estágio", "target", "ok"),
      kpi("Ganhos no mês", moneyShort(c.won_month_value), `${c.won_month_count} negócio(s) fechado(s)`, "dollar", "ok"),
      kpi("Taxa de conversão", c.win_rate === null ? "—" : pct(c.win_rate), c.avg_ticket ? `Ticket médio ${moneyShort(c.avg_ticket)}` : "ganhos ÷ (ganhos + perdidos)", "zap")));
    const funnel = Object.entries(c.by_stage).filter(([k]) => k !== "fechado_perdido").map(([k, v]) => ({ label: `${STAGE_LABEL[k]} (${v.count})`, value: v.value, color: k === "fechado_ganho" ? "var(--mint-500)" : undefined }));
    const reasons = Object.entries(c.loss_reasons).map(([label, value]) => ({ label, value }));
    root.append(h("div", { class: "grid c-main mb-16" },
      card("Funil de vendas (valor por estágio)", hbars({ items: funnel, fmt: moneyShort })),
      card("Motivos de perda", reasons.length ? donut({ items: reasons, center: String(reasons.reduce((a, r) => a + r.value, 0)) }) : h("div", { class: "muted" }, "Nenhuma perda registrada."))));
  }

  if (d.support) {
    const s = d.support;
    root.append(h("h2", { class: "mb-12" }, "Atendimento WhatsApp"));
    root.append(h("div", { class: "grid c5 mb-16" },
      kpi("Na fila", s.queue, "aguardando agente", "clock", s.queue > 5 ? "warn" : ""), kpi("Em atendimento", s.in_progress, `${s.bot} com o bot`, "chat"),
      kpi("Hoje", s.created_today, `${s.closed_today} encerrados`, "users"), kpi("1ª resposta (hoje)", dur(s.avg_frt_today), "tempo médio (FRT)", "zap", "ok"),
      kpi("CSAT (7 dias)", s.csat_7d ? num(s.csat_7d, 2) + " / 5" : "—", `${s.agents_online} agente(s) online`, "star", "ok")));
    root.append(h("div", { class: "grid c-main mb-16" },
      card("Tickets criados — últimos 7 dias", barChart({ labels: s.daily.map((x) => x.day), series: [{ name: "Tickets", color: "#0EA5E9", values: s.daily.map((x) => x.tickets) }] }))
      , d.operations ? card("Casos ativos por eixo", donut({ items: Object.entries(d.operations.by_type).map(([k, v]) => ({ label: CASE_TYPE_LABEL[k], value: v })), center: String(d.operations.open_cases) })) : h("div")));
  }

  const row2 = [];
  if (d.operations) {
    const o = d.operations;
    const usage = o.hours_contracted ? (o.hours_consumed / o.hours_contracted) * 100 : 0;
    row2.push(card("Operação técnica", h("div", { class: "col gap-12" },
      h("div", { class: "row between" }, h("span", "Casos em aberto"), h("b", o.open_cases)),
      h("div", { class: "row between" }, h("span", "Casos com SLA estourado"), h("b", { class: o.breached_cases ? "" : "muted" }, o.breached_cases)),
      h("div", { class: "row between" }, h("span", "Laudos pendentes"), h("b", `${o.pending_reports} (${o.urgent_reports} urgentes)`)),
      h("div", { class: "row between" }, h("span", "Laudos atrasados"), h("b", o.overdue_reports)),
      h("div", h("div", { class: "row between mb-8" }, h("span", "Horas consumidas vs. contratadas"), h("b", `${num(o.hours_consumed, 1)} / ${num(o.hours_contracted, 1)} h`)),
        progress(usage, usage > 100 ? "danger" : usage > 80 ? "warn" : "ok")))));
  }
  if (d.finance) {
    const f = d.finance;
    row2.push(card("Contratos & faturamento", h("div", { class: "col gap-12" },
      h("div", { class: "row between" }, h("span", "Contratos ativos"), h("b", f.active_contracts)),
      h("div", { class: "row between" }, h("span", "Receita recorrente mensal (MRR)"), h("b", money(f.mrr))),
      h("div", { class: "row between" }, h("span", "A receber"), h("b", money(f.open_amount))),
      h("div", { class: "row between" }, h("span", "Em atraso"), h("b", { style: { color: f.overdue_amount ? "var(--danger)" : "" } }, money(f.overdue_amount))),
      h("div", { class: "row between" }, h("span", "Recebido no mês"), h("b", money(f.paid_month))),
      h("div", { class: "row between" }, h("span", "Contratos vencendo em 30 dias"), h("b", f.expiring_30d)))));
  }
  if (row2.length) root.append(h("div", { class: `grid c${row2.length}` }, row2));
  return root;
}
