import { api } from "../lib/api.js";
import { h, icon, mount, dateTime } from "../lib/dom.js";
import { badge, card, confirmDialog, dataTable, formModal, openModal, pageHead, tabs, toast, toastError } from "../lib/ui.js";

const DLV = { success: ["Entregue", "ok"], pending: ["Pendente", "info"], failed: ["Falhou (retentando)", "warn"], dead: ["Esgotado", "danger"] };

export default async function integrations(ctx) {
  let tab = "endpoints";
  const body = h("div");
  const events = (await api.get("/api/v1/webhooks/events")).events;

  async function load() {
    if (tab === "endpoints") {
      const { items } = await api.get("/api/v1/webhooks/endpoints");
      return mount(body, [card(null, dataTable({ rows: items, empty: "Nenhum endpoint registrado.", columns: [
        { label: "Endpoint", render: (e) => h("div", h("b", e.name), h("small", { class: "mono" }, e.url)) }, { label: "Eventos", render: (e) => h("div", { class: "row wrap gap-4" }, e.events.slice(0, 4).map((x) => badge(x, "info")), e.events.length > 4 ? badge(`+${e.events.length - 4}`) : null) },
        { label: "Status", render: (e) => h("div", e.is_active ? badge("Ativo", "ok") : badge("Pausado", "danger"), e.consecutive_failures ? h("small", `${e.consecutive_failures} falha(s) seguidas`) : null) },
        { label: "", render: (e) => h("div", { class: "row gap-4" }, h("button", { class: "btn sm", onClick: async () => { const r = await api.post(`/api/v1/webhooks/endpoints/${e.id}/test`); toast(r.success ? `OK (HTTP ${r.status})` : `Falhou: ${r.response || r.status}`, r.success ? "ok" : "error", "Teste de webhook"); load(); } }, "Testar"),
          h("button", { class: "btn sm", onClick: async () => { await api.post(`/api/v1/webhooks/endpoints/${e.id}/toggle`); load(); } }, e.is_active ? "Pausar" : "Ativar"),
          h("button", { class: "btn sm", onClick: async () => { const r = await api.post(`/api/v1/webhooks/endpoints/${e.id}/rotate-secret`); showSecret(r.secret); } }, icon("key"), "Novo segredo"),
          h("button", { class: "btn ghost sm icon", onClick: async () => { if (await confirmDialog({ title: "Excluir endpoint", message: "Excluir este endpoint e seu histórico de entregas?", danger: true, confirmLabel: "Excluir" })) { await api.del(`/api/v1/webhooks/endpoints/${e.id}`); load(); } } }, icon("trash"))) }] }), { tight: true }),
      h("div", { class: "mt-16" }, card("Como validar a assinatura", [h("p", { class: "muted" }, "Cada entrega traz o cabeçalho ", h("span", { class: "mono" }, "X-Flowmentor-Signature: t=<unix>,v1=<hex>"), ". Calcule HMAC-SHA256 com o segredo do endpoint sobre ", h("span", { class: "mono" }, "<t>.<corpo bruto>"), " e compare em tempo constante. Rejeite timestamps com mais de 5 minutos."),
        h("pre", { class: "code" }, `import hmac, hashlib\n\ndef valido(secret, header, body: bytes):\n    parts = dict(p.split("=") for p in header.split(","))\n    esperado = hmac.new(secret.encode(), parts["t"].encode() + b"." + body, hashlib.sha256).hexdigest()\n    return hmac.compare_digest(esperado, parts["v1"])`)]))]);
    }
    if (tab === "deliveries") {
      const { items } = await api.get("/api/v1/webhooks/deliveries", { limit: 100 });
      return mount(body, card(null, dataTable({ rows: items, empty: "Nenhuma entrega registrada ainda.", columns: [
        { label: "Evento", render: (d) => h("span", { class: "mono" }, d.event_type) }, { label: "Status", render: (d) => badge(...DLV[d.status]) }, { label: "HTTP", render: (d) => d.response_status ?? "—" }, { label: "Tentativas", num: true, key: "attempts" },
        { label: "Resposta", render: (d) => h("span", { class: "muted truncate", style: { maxWidth: "260px", display: "inline-block" } }, d.response_body || "") }, { label: "Quando", render: (d) => dateTime(d.created_at) },
        { label: "", render: (d) => h("button", { class: "btn sm", onClick: async () => { const r = await api.post(`/api/v1/webhooks/deliveries/${d.id}/redeliver`); toast(r.success ? "Reenviado com sucesso" : `Falhou (HTTP ${r.status ?? "—"})`, r.success ? "ok" : "error"); load(); } }, "Reenviar") }] }), { tight: true }));
    }
    if (tab === "keys") {
      const { items } = await api.get("/api/v1/api-keys");
      return mount(body, [card(null, dataTable({ rows: items, empty: "Nenhuma chave de API.", columns: [{ label: "Nome", key: "name", cls: "strong" }, { label: "Prefixo", render: (k) => h("span", { class: "mono" }, k.prefix + "…") }, { label: "Escopos", render: (k) => h("div", { class: "row wrap gap-4" }, k.scopes.map((s) => badge(s))) },
        { label: "Último uso", render: (k) => dateTime(k.last_used_at) }, { label: "Status", render: (k) => (k.revoked_at ? badge("Revogada", "danger") : badge("Ativa", "ok")) },
        { label: "", render: (k) => (k.revoked_at ? "" : h("button", { class: "btn sm danger", onClick: async () => { await api.del(`/api/v1/api-keys/${k.id}`); load(); } }, "Revogar")) }] }), { tight: true }),
      h("div", { class: "mt-16" }, card("Captura de leads por formulário (Open API)", [h("p", { class: "muted" }, "Envie leads de landing pages/WordPress diretamente para o pipeline com ", h("span", { class: "mono" }, "POST /api/v1/public/leads"), " e o cabeçalho ", h("span", { class: "mono" }, "X-API-Key"), ". Um exemplo em PHP está em ", h("span", { class: "mono" }, "integrations/php/lead-form.php"), "."),
        h("pre", { class: "code" }, `curl -X POST ${location.origin}/api/v1/public/leads \\\n  -H "X-API-Key: fm_..." -H "Content-Type: application/json" \\\n  -d '{"name":"Maria","phone":"62999990000","company":"Lab X","interest":"Consultoria","consent":true}'`)]))]);
    }
  }
  const root = h("div", pageHead("Webhooks & Open API", "Dispare automações externas quando eventos ocorrem (deal.won, case.created, sla.breached…) e integre sistemas via chaves de API.",
    h("button", { class: "btn", onClick: () => openModal({ title: "Catálogo de eventos", body: h("div", events.map((e) => h("div", { class: "kv" }, h("span", { class: "mono" }, e.type), h("span", e.description)))) }) }, "Catálogo de eventos"),
    h("button", { class: "btn primary", onClick: () => (tab === "keys" ? newKey() : newEndpoint()) }, icon("plus"), "Novo")),
  tabs([{ id: "endpoints", label: "Endpoints" }, { id: "deliveries", label: "Entregas" }, { id: "keys", label: "Chaves de API" }], tab, (t) => { tab = t; load(); }), body);

  function newEndpoint() {
    const sel = new Set(["deal.won"]);
    const list = h("div", { class: "grid c2", style: { maxHeight: "260px", overflow: "auto" } }, events.map((e) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: sel.has(e.type), onChange: (x) => (x.target.checked ? sel.add(e.type) : sel.delete(e.type)) }), h("span", { class: "mono", style: { fontSize: "12px" } }, e.type))));
    formModal({ title: "Novo endpoint de webhook", size: "wide", extra: h("div", h("label", { class: "strong" }, "Eventos assinados"), list), fields: [{ name: "name", label: "Nome", required: true }, { name: "url", label: "URL (HTTPS)", required: true, placeholder: "https://erp.suaempresa.com/hooks/flowmentor", full: true }],
      onSubmit: async (v) => { if (!sel.size) throw new Error("Selecione ao menos um evento"); const r = await api.post("/api/v1/webhooks/endpoints", { ...v, events: [...sel] }); showSecret(r.secret); load(); } });
  }
  function newKey() {
    const scopes = ["accounts:read", "contacts:read", "contacts:write", "deals:read", "deals:write", "cases:read", "cases:write", "products:read"];
    formModal({ title: "Nova chave de API", fields: [{ name: "name", label: "Nome", required: true, placeholder: "Site institucional" }, { name: "scopes", label: "Escopos (separados por vírgula)", split: true, full: true, value: "contacts:write, deals:write" }], values: { scopes: "contacts:write, deals:write" },
      extra: h("div", { class: "hint muted" }, "Escopos disponíveis: ", scopes.join(", ")), onSubmit: async (v) => { const r = await api.post("/api/v1/api-keys", v); showSecret(r.key, "Chave de API"); load(); } });
  }
  await load();
  return root;
}

function showSecret(secret, label = "Segredo de assinatura") {
  const ta = h("textarea", { class: "textarea mono", readOnly: true, rows: 2 }, secret);
  openModal({ title: label, body: [h("div", { class: "callout warn" }, "Guarde este valor agora: ele não será exibido novamente."), ta], footer: h("button", { class: "btn primary", onClick: async () => { try { await navigator.clipboard.writeText(secret); toast("Copiado", "ok"); } catch { ta.select(); } } }, "Copiar") });
}
