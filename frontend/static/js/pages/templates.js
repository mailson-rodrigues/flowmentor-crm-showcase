import { api } from "../lib/api.js";
import { h, icon, mount, dateTime, debounce } from "../lib/dom.js";
import { can } from "../lib/store.js";
import { badge, card, confirmDialog, dataTable, openModal, pageHead, toast, toastError } from "../lib/ui.js";

const ST = { draft: ["Rascunho", ""], pending: ["Em análise", "warn"], approved: ["Aprovado", "ok"], rejected: ["Rejeitado", "danger"], paused: ["Pausado", "warn"], disabled: ["Desativado", "danger"] };
const CAT = { MARKETING: "Marketing", UTILITY: "Utilidade", AUTHENTICATION: "Autenticação" };

export default async function templates(ctx) {
  const holder = h("div");
  async function load() {
    const { items } = await api.get("/api/v1/templates");
    mount(holder, card(null, dataTable({ rows: items, empty: "Nenhum template. Crie o primeiro para iniciar conversas ativas fora da janela de 24h.", columns: [
      { label: "Template", render: (t) => h("div", h("b", { class: "mono" }, t.name), h("small", `${t.language} · ${CAT[t.category]}`)) },
      { label: "Prévia", render: (t) => h("div", { class: "muted truncate", style: { maxWidth: "380px" } }, t.components.find((c) => c.type.toUpperCase() === "BODY")?.text || "") },
      { label: "Status", render: (t) => h("div", badge(...ST[t.status]), t.rejection_reason ? h("small", { style: { color: "var(--danger)" } }, t.rejection_reason) : null) }, { label: "Enviado", render: (t) => dateTime(t.submitted_at) },
      { label: "", render: (t) => h("div", { class: "row gap-4" }, can("templates:manage") && ["draft", "rejected"].includes(t.status) ? h("button", { class: "btn sm primary", onClick: async () => { try { await api.post(`/api/v1/templates/${t.id}/submit`); toast("Template enviado à Meta", "ok"); load(); } catch (e) { toastError(e); } } }, "Submeter") : null,
        can("templates:manage") && t.status === "pending" ? h("button", { class: "btn sm", title: "Somente sandbox: simula a aprovação da Meta", onClick: async () => { await api.post(`/api/v1/templates/${t.id}/sandbox-approve`, {}); load(); } }, "Simular aprovação") : null,
        can("templates:manage") ? h("button", { class: "btn ghost sm icon", onClick: async () => { if (await confirmDialog({ title: "Excluir template", message: `Excluir "${t.name}"?`, danger: true, confirmLabel: "Excluir" })) { try { await api.del(`/api/v1/templates/${t.id}`); load(); } catch (e) { toastError(e); } } } }, icon("trash")) : null) }] }), { tight: true }));
  }
  const root = h("div", pageHead("Templates HSM", "Mensagens pré-aprovadas pela Meta (Marketing, Utilidade, Autenticação) para iniciar conversas fora da janela de 24h.",
    can("templates:manage") ? [h("button", { class: "btn", onClick: async () => { try { const r = await api.post("/api/v1/templates/sync"); toast(`Sincronizado (${r.mode}): ${r.created} novo(s), ${r.updated} atualizado(s)`, "ok"); load(); } catch (e) { toastError(e); } } }, icon("refresh"), "Sincronizar com a Meta"),
      h("button", { class: "btn primary", onClick: () => builder(load) }, icon("plus"), "Novo template")] : null), holder);
  await load();
  ctx.on("crm_event", (e) => { if (e.type === "template.status_changed") load(); });
  return root;
}

function builder(reload) {
  const f = { name: "", language: "pt_BR", category: "UTILITY", header: "", body: "Olá {{1}}, o laudo do exame {{2}} já está disponível na plataforma da Flowmentor.", footer: "Flowmentor — Citometria de Fluxo", buttons: [] };
  const samples = {};
  const out = h("div"), preview = h("div", { class: "bubble out", style: { maxWidth: "100%", alignSelf: "stretch" } });
  const inp = (key, props = {}) => h("input", { class: "input", value: f[key], onInput: (e) => { f[key] = e.target.value; validate(); }, ...props });
  const bodyTa = h("textarea", { class: "textarea", rows: 5, onInput: (e) => { f.body = e.target.value; validate(); } }, f.body);
  const catSel = h("select", { class: "select", onChange: (e) => { f.category = e.target.value; validate(); } }, Object.entries(CAT).map(([k, v]) => h("option", { value: k, selected: k === f.category }, v)));
  const btnBox = h("div");
  const sampleBox = h("div");

  const components = () => {
    const c = [];
    if (f.header.trim()) c.push({ type: "HEADER", format: "TEXT", text: f.header });
    c.push({ type: "BODY", text: f.body });
    if (f.footer.trim()) c.push({ type: "FOOTER", text: f.footer });
    if (f.buttons.length) c.push({ type: "BUTTONS", buttons: f.buttons.map((b) => (b.type === "URL" ? { type: "URL", text: b.text, url: b.value } : b.type === "PHONE_NUMBER" ? { type: "PHONE_NUMBER", text: b.text, phone_number: b.value } : { type: "QUICK_REPLY", text: b.text })) });
    return c;
  };
  const payload = () => ({ name: f.name, language: f.language, category: f.category, components: components(), sample_values: samples });

  const drawButtons = () => mount(btnBox, [...f.buttons.map((b, i) => h("div", { class: "row mb-8" }, h("select", { class: "select", style: { width: "150px" }, onChange: (e) => { b.type = e.target.value; drawButtons(); validate(); } },
    [["QUICK_REPLY", "Resposta rápida"], ["URL", "Link (URL)"], ["PHONE_NUMBER", "Telefone"]].map(([v, l]) => h("option", { value: v, selected: v === b.type }, l))),
  h("input", { class: "input", placeholder: "Texto (até 25)", value: b.text, onInput: (e) => { b.text = e.target.value; validate(); } }), b.type !== "QUICK_REPLY" ? h("input", { class: "input", placeholder: b.type === "URL" ? "https://…" : "+5562…", value: b.value || "", onInput: (e) => { b.value = e.target.value; validate(); } }) : null,
  h("button", { class: "btn ghost sm icon", onClick: () => { f.buttons.splice(i, 1); drawButtons(); validate(); } }, icon("x")))),
  f.buttons.length < 10 ? h("button", { class: "btn sm", onClick: () => { f.buttons.push({ type: "QUICK_REPLY", text: "" }); drawButtons(); } }, icon("plus"), "Adicionar botão") : null]);

  const validate = debounce(async () => {
    const nums = [...new Set([...(f.body + " " + f.header).matchAll(/\{\{(\d+)\}\}/g)].map((m) => m[1]))];
    mount(sampleBox, nums.length ? [h("label", { class: "strong" }, "Exemplos das variáveis (exigidos pela Meta)"), ...nums.map((n) => h("div", { class: "field", style: { marginTop: "6px" } }, h("input", { class: "input", placeholder: `Exemplo para {{${n}}}`, value: samples[n] || "", onInput: (e) => { samples[n] = e.target.value; paint(); } })))] : null);
    paint();
    try {
      const r = await api.post("/api/v1/templates/validate", payload());
      mount(out, [...r.errors.map((e) => h("div", { class: "callout warn" }, "✖ ", e)), ...r.warnings.map((w) => h("div", { class: "callout" }, "⚠ ", w)), !r.errors.length ? h("div", { class: "callout ok" }, "✔ Template válido para submissão.") : null]);
    } catch (e) { mount(out, h("div", { class: "callout warn" }, e.message)); }
  }, 350);
  const paint = () => mount(preview, f.header ? h("b", f.header.replace(/\{\{(\d+)\}\}/g, (m, n) => samples[n] || m)) : null, h("div", f.body.replace(/\{\{(\d+)\}\}/g, (m, n) => samples[n] || m)), f.footer ? h("div", { class: "muted", style: { fontSize: "11.5px", marginTop: "4px" } }, f.footer) : null,
    f.buttons.length ? h("div", { class: "opts" }, f.buttons.map((b) => h("div", { class: "center" }, b.text || "…"))) : null);

  const m = openModal({ title: "Novo template HSM", size: "xl", body: h("div", { class: "grid c2" },
    h("div", h("div", { class: "form-grid" }, h("div", { class: "field" }, h("label", "Nome (minúsculas e _)"), inp("name", { placeholder: "laudo_disponivel" })), h("div", { class: "field" }, h("label", "Categoria"), catSel)),
      h("div", { class: "field" }, h("label", "Cabeçalho (opcional)"), inp("header", { maxlength: 60 })), h("div", { class: "field" }, h("label", "Corpo"), bodyTa, h("div", { class: "hint" }, "Use {{1}}, {{2}}… para variáveis. Não inicie nem termine com variável.")),
      h("div", { class: "field" }, h("label", "Rodapé (opcional)"), inp("footer", { maxlength: 60 })), h("div", { class: "field" }, h("label", "Botões"), btnBox), sampleBox),
    h("div", h("label", { class: "strong" }, "Pré-visualização"), h("div", { class: "chat-body", style: { borderRadius: "10px", margin: "6px 0 12px", padding: "16px", minHeight: "150px" } }, preview), h("label", { class: "strong" }, "Validação (regras da Meta)"), h("div", { class: "mt-8" }, out))),
  footer: [h("button", { class: "btn", onClick: () => m.close() }, "Cancelar"),
    h("button", { class: "btn", onClick: () => save(false) }, "Salvar rascunho"), h("button", { class: "btn primary", onClick: () => save(true) }, icon("send"), "Salvar e submeter à Meta")] });
  async function save(submit) {
    try { await api.post("/api/v1/templates", { ...payload(), submit }); toast(submit ? "Template enviado para aprovação" : "Rascunho salvo", "ok"); m.close(); reload(); }
    catch (e) { toastError(e); if (e.details?.errors) mount(out, e.details.errors.map((x) => h("div", { class: "callout warn" }, "✖ ", x))); }
  }
  drawButtons(); validate();
}
