import { api } from "../lib/api.js";
import { h, mount, dur, num, pct, dateTime } from "../lib/dom.js";
import { badge, card, dataTable, emptyState, kpi, pageHead, progress, segmented } from "../lib/ui.js";
import { barChart, donut, lineChart, gauge } from "../lib/charts.js";

const iso = (d) => d.toISOString().slice(0, 10);

export default async function analytics(ctx) {
  let range = 30, dept = "";
  const depts = (await api.get("/api/v1/departments")).items;
  const body = h("div");
  async function load() {
    mount(body, h("div", { class: "spinner" }));
    const to = new Date(), from = new Date(Date.now() - (range - 1) * 86400000);
    const [k, sv] = await Promise.all([api.get("/api/v1/kpis/overview", { date_from: iso(from), date_to: iso(to), department_id: dept || undefined }), api.get("/api/v1/kpis/surveys", { date_from: iso(from), date_to: iso(to), limit: 30 })]);
    const q = k.quality, labels = k.throughput.map((b) => b.bucket.slice(range <= 2 ? 11 : 5));
    mount(body, [
      h("div", { class: "grid c4 mb-16" }, kpi("FRT — 1ª resposta", dur(k.speed.frt_avg_seconds), `mediana ${dur(k.speed.frt_median_seconds)} · p90 ${dur(k.speed.frt_p90_seconds)}`, "zap"),
        kpi("TMA / AHT", dur(k.resolution.aht_avg_seconds), `mediana ${dur(k.resolution.aht_median_seconds)}`, "clock"), kpi("FCR — resolução no 1º contato", pct(k.resolution.fcr_pct), "sem reabertura em 24h", "check", "ok"),
        kpi("SLA de 1ª resposta", pct(k.sla.frt_compliance_pct), `${k.sla.breaches} violação(ões) no período`, "alert", k.sla.breaches ? "warn" : "ok")),
      h("div", { class: "grid c4 mb-16" }, kpi("Tickets", k.totals.tickets, `${k.totals.closed} encerrados · ${k.totals.bot_resolved} resolvidos pelo bot`, "chat"), kpi("CSAT médio", q.csat_avg ? `${num(q.csat_avg, 2)} / 5` : "—", q.csat_satisfied_pct !== null ? `${pct(q.csat_satisfied_pct)} satisfeitos (4–5)` : "sem respostas", "star", "ok"),
        kpi("NPS", q.nps ?? "—", q.nps !== null ? `${q.nps_promoters} promotores · ${q.nps_detractors} detratores` : "sem respostas", "target"), kpi("CES médio", q.ces_avg ? num(q.ces_avg, 2) + " / 5" : "—", `taxa de resposta ${pct(q.response_rate)}`, "repeat")),
      h("div", { class: "grid c-main mb-16" }, card("Throughput por período", k.throughput.length ? lineChart({ labels, series: [{ name: "Criados", color: "#0EA5E9", values: k.throughput.map((b) => b.created) }, { name: "Encerrados", color: "#14B8A6", values: k.throughput.map((b) => b.closed) }, { name: "Transferidos", color: "#F59E0B", values: k.throughput.map((b) => b.transferred) }] }) : emptyState("Sem dados no período.", "chart")),
        card("Volume de mensagens", k.throughput.length ? barChart({ labels, stacked: true, series: [{ name: "Recebidas", color: "#6366F1", values: k.throughput.map((b) => b.messages_in) }, { name: "Enviadas", color: "#0EA5E9", values: k.throughput.map((b) => b.messages_out) }] }) : emptyState("Sem dados.", "chart"))),
      h("div", { class: "grid c3 mb-16" }, card("Distribuição de notas (CSAT)", barChart({ labels: ["1", "2", "3", "4", "5"], series: [{ name: "Respostas", color: "#14B8A6", values: Object.values(q.rating_distribution) }], height: 170 })),
        card("Sentimento dos comentários", Object.keys(q.sentiment).length ? donut({ items: [{ label: "Positivo", value: q.sentiment.positive || 0, color: "#14B8A6" }, { label: "Neutro", value: q.sentiment.neutral || 0, color: "#94A3B8" }, { label: "Negativo", value: q.sentiment.negative || 0, color: "#EF4444" }] }) : h("div", { class: "muted" }, "Sem comentários classificados.")),
        card("Por departamento", dataTable({ rows: k.departments, empty: "—", columns: [{ label: "Depto.", key: "name" }, { label: "Tickets", num: true, key: "tickets" }, { label: "FRT", num: true, render: (d) => dur(d.avg_frt_seconds) }, { label: "TMA", num: true, render: (d) => dur(d.avg_aht_seconds) }] }))),
      card("Desempenho e ocupação por agente", dataTable({ rows: k.agents, empty: "Sem atendimentos no período.", columns: [
        { label: "Agente", key: "name", cls: "strong" }, { label: "Tickets", num: true, key: "tickets" }, { label: "Encerrados", num: true, key: "closed" }, { label: "FRT médio", num: true, render: (a) => dur(a.avg_frt_seconds) }, { label: "TMA médio", num: true, render: (a) => dur(a.avg_aht_seconds) },
        { label: "CSAT", num: true, render: (a) => (a.csat_avg ? num(a.csat_avg, 2) : "—") }, { label: "Logado", num: true, render: (a) => dur(a.logged_seconds) }, { label: "Em pausa", num: true, render: (a) => dur(a.paused_seconds) },
        { label: "Ocupação", render: (a) => (a.occupancy_pct === null ? "—" : h("div", { style: { minWidth: "110px" } }, h("div", { class: "muted", style: { fontSize: "11.5px" } }, pct(a.occupancy_pct)), progress(a.occupancy_pct, a.occupancy_pct > 90 ? "warn" : "ok"))) }] })),
      h("div", { class: "mt-16" }, card("Avaliações recentes (detratores primeiro)", dataTable({ rows: sv.items, empty: "Nenhuma avaliação respondida no período.", columns: [
        { label: "Nota", render: (s) => badge(`${s.type.toUpperCase()} ${s.rating}`, s.rating <= (s.type === "nps" ? 6 : 2) ? "danger" : s.rating >= (s.type === "nps" ? 9 : 4) ? "ok" : "warn") }, { label: "Cliente", key: "contact_name" }, { label: "Agente", render: (s) => s.agent_name || "—" },
        { label: "Comentário", render: (s) => (s.comment ? h("span", s.comment, " ", s.sentiment ? badge(s.sentiment === "positive" ? "positivo" : s.sentiment === "negative" ? "negativo" : "neutro", s.sentiment === "positive" ? "ok" : s.sentiment === "negative" ? "danger" : "") : null) : h("span", { class: "muted" }, "—")) },
        { label: "Quando", render: (s) => dateTime(s.answered_at) }, { label: "", render: (s) => h("a", { class: "btn sm", href: `#/inbox/${s.ticket_id}` }, "Ver atendimento") }] }))),
    ]);
  }
  const root = h("div", pageHead("Indicadores de atendimento", "KPIs calculados automaticamente: velocidade, resolução, eficiência, volume, qualidade e ocupação.",
    h("select", { class: "select", style: { width: "190px" }, onChange: (e) => { dept = e.target.value; load(); } }, h("option", { value: "" }, "Todos os departamentos"), depts.map((d) => h("option", { value: d.id }, d.name))),
    segmented([{ id: "1", label: "Hoje" }, { id: "7", label: "7 dias" }, { id: "30", label: "30 dias" }, { id: "90", label: "90 dias" }], "30", (v) => { range = Number(v); load(); })), body);
  await load();
  return root;
}
