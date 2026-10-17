import { api } from "../lib/api.js";
import { h, icon, mount, money, moneyShort, date, dateTime, num, avatar, debounce, ago } from "../lib/dom.js";
import { can, STAGE_LABEL, MODALITY_LABEL } from "../lib/store.js";
import { badge, card, confirmDialog, dataTable, emptyState, formModal, openDrawer, pageHead, promptDialog, segmented, searchBox, toast, toastError, openModal, form, progress } from "../lib/ui.js";
import { download } from "../lib/api.js";

const STAGE_KIND = { fechado_ganho: "ok", fechado_perdido: "danger", negociacao: "warn" };

export default async function pipeline(ctx) {
  let view = "board";
  let owner = "";
  let users = [];
  try { users = (await api.get("/api/v1/users")).items; } catch { /* sem permissão */ }
  const holder = h("div");
  const ownerSel = h("select", { class: "select", style: { width: "190px" }, onChange: (e) => { owner = e.target.value; load(); } },
    h("option", { value: "" }, "Todos os responsáveis"), users.filter((u) => ["agent", "supervisor", "consultor", "admin"].includes(u.role)).map((u) => h("option", { value: u.id }, u.name)));
  const head = pageHead("Pipeline comercial", "Arraste as oportunidades entre os estágios. Fechar como ganho gera contrato e casos operacionais automaticamente.",
    can("deals:all") ? ownerSel : null, segmented([{ id: "board", label: "Kanban" }, { id: "table", label: "Tabela" }], view, (v) => { view = v; load(); }),
    can("deals:write") ? h("button", { class: "btn primary", onClick: () => newDeal(load) }, icon("plus"), "Nova oportunidade") : null);
  const root = h("div", head, holder);

  async function load() {
    if (view === "board") {
      const { columns } = await api.get("/api/v1/deals/board", { assigned_user_id: owner || undefined });
      mount(holder, renderBoard(columns, load));
    } else {
      const r = await api.get("/api/v1/deals", { assigned_user_id: owner || undefined, limit: 200 });
      const box = h("div");
      const search = searchBox("Buscar oportunidade…", async (q) => { const rr = await api.get("/api/v1/deals", { q, assigned_user_id: owner || undefined, limit: 200 }); mount(box, table(rr.items)); });
      const table = (items) => dataTable({ rows: items, onRowClick: (d) => openDeal(d.id, load), columns: [
        { label: "Oportunidade", render: (d) => h("div", h("div", { class: "strong" }, d.title), h("small", d.account_name)) },
        { label: "Estágio", render: (d) => badge(STAGE_LABEL[d.stage], STAGE_KIND[d.stage] || "info") },
        { label: "Valor", num: true, render: (d) => money(d.final_contracted_value || d.expected_value) }, { label: "Desc.", num: true, render: (d) => (Number(d.discount_percentage) ? `${num(d.discount_percentage, 1)}%` : "—") },
        { label: "Prob.", num: true, render: (d) => `${d.probability}%` }, { label: "Previsão", render: (d) => date(d.expected_close_date) },
        { label: "Atualizado", render: (d) => ago(d.updated_at) }] });
      mount(holder, card(null, [h("div", { class: "mb-12", style: { maxWidth: "340px" } }, search), mount(box, table(r.items))]));
    }
  }
  await load();
  ctx.on("crm_event", (e) => { if (e.type.startsWith("deal.") && ctx.alive()) load(); });
  return root;
}

function renderBoard(columns, reload) {
  const board = h("div", { class: "board" });
  for (const col of columns) {
    const lane = h("div", { class: `lane ${col.stage === "fechado_ganho" ? "won" : col.stage === "fechado_perdido" ? "lost" : ""}`, dataset: { stage: col.stage } });
    lane.append(h("div", { class: "lane-head" }, h("div", { class: "t" }, STAGE_LABEL[col.stage], badge(String(col.count))),
      h("div", { class: "sum" }, moneyShort(col.total_value), col.weighted_value && !col.stage.startsWith("fechado") ? ` · pond. ${moneyShort(col.weighted_value)}` : ""), h("div", { class: "bar" })));
    const body = h("div", { class: "lane-body" });
    col.deals.forEach((d) => body.append(dealCard(d, reload)));
    if (!col.deals.length) body.append(h("div", { class: "muted center", style: { padding: "14px 0", fontSize: "12px" } }, "Solte aqui"));
    lane.append(body);
    if (can("deals:write")) {
      lane.addEventListener("dragover", (e) => { e.preventDefault(); lane.classList.add("drop"); });
      lane.addEventListener("dragleave", (e) => { if (!lane.contains(e.relatedTarget)) lane.classList.remove("drop"); });
      lane.addEventListener("drop", async (e) => {
        e.preventDefault(); lane.classList.remove("drop");
        const id = e.dataTransfer.getData("text/plain");
        if (!id) return;
        await moveDeal(id, col.stage, reload);
      });
    }
    board.append(lane);
  }
  return board;
}

export async function moveDeal(id, stage, reload) {
  try {
    let loss;
    if (stage === "fechado_perdido") {
      loss = await promptDialog({ title: "Motivo da perda", label: "Por que a oportunidade foi perdida?", placeholder: "Ex.: preço, concorrente, sem orçamento…", required: true, confirmLabel: "Registrar perda" });
      if (!loss) return;
    }
    const r = await api.put(`/api/v1/deals/${id}/stage`, { stage, loss_reason: loss });
    if (stage === "fechado_ganho") toast("Negócio ganho! Contrato e casos operacionais foram gerados. 🎉", "ok", "Parabéns");
    reload?.();
    return r;
  } catch (e) { toastError(e); reload?.(); }
}

function dealCard(d, reload) {
  const el = h("div", { class: "deal-card", draggable: can("deals:write") ? "true" : "false", onClick: () => openDeal(d.id, reload) },
    h("div", { class: "t" }, d.title), h("div", { class: "acc" }, d.account_name),
    h("div", { class: "foot" }, h("span", { class: "val" }, money(d.final_contracted_value || d.expected_value)),
      h("div", { class: "row gap-4" }, Number(d.discount_percentage) ? badge(`-${num(d.discount_percentage, 0)}%`, "warn") : null, badge(`${d.probability}%`, "info"))));
  el.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", d.id); e.dataTransfer.effectAllowed = "move"; setTimeout(() => el.classList.add("dragging"), 0); });
  el.addEventListener("dragend", () => el.classList.remove("dragging"));
  return el;
}

async function newDeal(reload) {
  const [accs, prods] = await Promise.all([api.get("/api/v1/accounts", { limit: 200 }), api.get("/api/v1/products", { active_only: true })]);
  const items = [];
  const itemsBox = h("div", { class: "mt-8" });
  const prodSel = h("select", { class: "select" }, prods.items.map((p) => h("option", { value: p.id }, `${p.name} — ${money(p.base_unit_price)}/${p.unit_label || "un"}`)));
  const qty = h("input", { class: "input", type: "number", min: "0.5", step: "0.5", value: "1", style: { width: "90px" } });
  const draw = () => mount(itemsBox, items.length ? items.map((it, i) => h("div", { class: "row between", style: { padding: "5px 0" } }, h("span", `${it.name} × ${it.qty}`), h("button", { class: "btn ghost sm icon", onClick: () => { items.splice(i, 1); draw(); } }, icon("x")))) : h("div", { class: "muted" }, "Nenhum item adicionado (opcional)."));
  draw();
  const extra = h("div", h("h3", { class: "mb-8" }, "Itens"), h("div", { class: "row" }, prodSel, qty, h("button", { class: "btn", onClick: () => { const p = prods.items.find((x) => x.id === prodSel.value); items.push({ product_service_id: p.id, quantity: Number(qty.value), name: p.name, qty: qty.value }); draw(); } }, icon("plus"), "Adicionar")), itemsBox);
  formModal({ title: "Nova oportunidade", size: "wide", extra, submitLabel: "Criar oportunidade",
    fields: [{ name: "account_id", label: "Conta institucional", type: "select", required: true, full: true, options: accs.items.map((a) => ({ value: a.id, label: a.trade_name || a.corporate_name })) },
      { name: "title", label: "Título", required: true, full: true }, { name: "source", label: "Origem", type: "select", options: ["whatsapp", "indicacao", "evento", "site", "outbound"].map((s) => ({ value: s, label: s })) },
      { name: "expected_close_date", label: "Previsão de fechamento", type: "date" }],
    onSubmit: async (v) => { await api.post("/api/v1/deals", { ...v, items: items.map(({ product_service_id, quantity }) => ({ product_service_id, quantity })) }); toast("Oportunidade criada", "ok"); reload?.(); } });
}

export async function openDeal(id, reload) {
  const body = h("div");
  const drawer = openDrawer({ title: "Oportunidade", body, onClose: () => reload?.() });
  async function load() {
    const d = await api.get(`/api/v1/deals/${id}`);
    const closed = d.stage === "fechado_ganho" || d.stage === "fechado_perdido";
    const sug = d.discount_suggestion;
    mount(drawer.body, [
      h("div", { class: "row between mb-12" }, h("div", h("h2", d.title), h("a", { href: `#/accounts/${d.account_id}`, onClick: () => drawer.close() }, d.account_name)), badge(STAGE_LABEL[d.stage], STAGE_KIND[d.stage] || "info")),
      can("deals:write") && !closed ? h("div", { class: "row wrap mb-16" }, h("label", { class: "muted" }, "Mover para:"),
        h("select", { class: "select", style: { width: "220px" }, onChange: async (e) => { if (e.target.value) { await moveDeal(id, e.target.value, null); load(); } } },
          h("option", { value: "" }, STAGE_LABEL[d.stage]), Object.entries(STAGE_LABEL).filter(([k]) => k !== d.stage).map(([k, v]) => h("option", { value: k }, v)))) : null,
      d.loss_reason ? h("div", { class: "callout warn" }, "Motivo da perda: ", d.loss_reason) : null,
      h("div", { class: "grid c3 mb-16" },
        h("div", { class: "card kpi" }, h("div", { class: "label" }, "Subtotal"), h("div", { class: "value", style: { fontSize: "19px" } }, money(d.expected_value))),
        h("div", { class: "card kpi" }, h("div", { class: "label" }, "Desconto institucional"), h("div", { class: "value", style: { fontSize: "19px" } }, `${num(d.discount_percentage, 1)}%`)),
        h("div", { class: "card kpi ok" }, h("div", { class: "label" }, "Valor final"), h("div", { class: "value", style: { fontSize: "19px" } }, money(d.final_contracted_value)))),
      card("Itens", [
        dataTable({ rows: d.items, empty: "Sem itens. Adicione serviços do catálogo.", columns: [
          { label: "Serviço", render: (i) => h("div", h("div", { class: "strong" }, i.description), h("small", `${i.sku} · ${MODALITY_LABEL[i.billing_modality] || ""}`)) },
          { label: "Qtd", num: true, render: (i) => num(i.quantity, 2) }, { label: "Unitário", num: true, render: (i) => money(i.unit_price) }, { label: "Subtotal", num: true, render: (i) => money(i.subtotal) },
          { label: "", render: (i) => (!closed && can("deals:write") ? h("button", { class: "btn ghost sm icon", title: "Remover", onClick: async () => { await api.del(`/api/v1/deals/${id}/items/${i.id}`); load(); } }, icon("trash")) : "") }] }),
        !closed && can("deals:write") ? h("div", { class: "mt-12" }, h("button", { class: "btn sm", onClick: () => addItem(id, load) }, icon("plus"), "Adicionar item")) : null], { tight: false }),
      !closed && can("deals:write") ? card("Desconto", h("div", { class: "col" },
        h("div", { class: "callout" }, `Sugestão automática: ${num(sug.suggested_pct, 1)}% (faixa por valor ${num(sug.tier_pct, 1)}% · desconto da conta ${num(sug.account_pct, 1)}%) → ${money(sug.suggested_final_value)}. Descontos acima de ${num(sug.auto_approve_max_pct, 0)}% exigem aprovação do Supervisor Comercial.`),
        h("div", { class: "row" }, h("button", { class: "btn", onClick: async () => { try { await api.post(`/api/v1/deals/${id}/discount/apply-suggestion`); toast("Desconto aplicado", "ok"); load(); } catch (e) { toastError(e); } } }, "Aplicar sugestão"),
          h("input", { class: "input", id: "discIn", type: "number", min: "0", max: "100", step: "0.5", value: d.discount_percentage, style: { width: "100px" } }),
          h("button", { class: "btn", onClick: async () => { try { await api.patch(`/api/v1/deals/${id}`, { discount_percentage: Number(drawer.el.querySelector("#discIn").value) }); toast("Desconto atualizado", "ok"); load(); } catch (e) { toastError(e); } } }, "Definir %")))) : null,
      h("div", { class: "mt-16" }, card("Propostas contratuais", [
        d.proposals.length ? dataTable({ rows: d.proposals, columns: [{ label: "Número", key: "number" }, { label: "Versão", key: "version" }, { label: "Status", render: (p) => badge(p.status, p.status === "accepted" ? "ok" : p.status === "sent" ? "info" : "") },
          { label: "Validade", render: (p) => date(p.valid_until) },
          { label: "", render: (p) => h("div", { class: "row gap-4" },
            h("button", { class: "btn sm", onClick: async () => { const r = await api.post(`/api/v1/proposals/${p.id}/share-link`); window.open(r.url, "_blank", "noopener"); } }, icon("eye"), "Abrir"),
            p.status === "draft" ? h("button", { class: "btn sm primary", onClick: async () => { await api.post(`/api/v1/proposals/${p.id}/send`); toast("Proposta marcada como enviada", "ok"); load(); } }, "Marcar enviada") : null,
            p.status === "sent" ? h("button", { class: "btn sm ok", onClick: async () => { await api.post(`/api/v1/proposals/${p.id}/accept`); load(); } }, "Aceita") : null) }] }) : h("div", { class: "muted" }, "Nenhuma proposta gerada."),
        can("deals:write") && d.items.length && !closed ? h("div", { class: "mt-12" }, h("button", { class: "btn primary sm", onClick: async () => { try { await api.post(`/api/v1/deals/${id}/proposals`, { valid_days: 15 }); toast("Proposta gerada", "ok"); load(); } catch (e) { toastError(e); } } }, icon("file"), "Gerar proposta")) : null])),
      d.tickets.length ? h("div", { class: "mt-16" }, card("Atendimentos WhatsApp vinculados", d.tickets.map((t) => h("div", { class: "row between", style: { padding: "5px 0" } }, h("a", { href: `#/inbox/${t.id}`, onClick: () => drawer.close() }, t.subject || "Atendimento", " ", h("span", { class: "muted" }, dateTime(t.created_at))), badge(t.status))))) : null,
      h("div", { class: "mt-16" }, card("Histórico de estágios", h("div", { class: "timeline" }, d.history.map((x) => h("div", { class: "tl-item" }, h("div", `${x.from_stage ? STAGE_LABEL[x.from_stage] + " → " : ""}${STAGE_LABEL[x.to_stage]}`), h("div", { class: "when" }, dateTime(x.created_at))))))),
    ]);
  }
  await load();
}

async function addItem(dealId, reload) {
  const prods = (await api.get("/api/v1/products", { active_only: true })).items;
  formModal({ title: "Adicionar item", fields: [{ name: "product_service_id", label: "Serviço", type: "select", full: true, options: prods.map((p) => ({ value: p.id, label: `${p.name} — ${money(p.base_unit_price)}` })) },
    { name: "quantity", label: "Quantidade", type: "number", value: 1, step: "0.5", min: "0.5", required: true }],
  onSubmit: async (v) => { await api.post(`/api/v1/deals/${dealId}/items`, v); reload(); } });
}
