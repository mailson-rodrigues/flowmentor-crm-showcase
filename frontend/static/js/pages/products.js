import { api } from "../lib/api.js";
import { h, icon, mount, money } from "../lib/dom.js";
import { can, MODALITY_LABEL } from "../lib/store.js";
import { badge, card, formModal, pageHead, toast } from "../lib/ui.js";

const AXES = [
  ["Consultoria de Processos", "Consultoria técnica de processos e qualidade", "Validação de painéis imunofenotípicos, padronização EuroFlow/OMS e POPs.", "flask"],
  ["Apoio Diagnóstico", "Apoio diagnóstico remoto & laudos", "Processamento de .FCS, laudos de alta complexidade e SLAs de urgência.", "chart"],
  ["Canal Médico", "Canal médico & discussão multidisciplinar", "Reuniões clínicas, interconsultas e suporte a hematologistas e residentes.", "users"],
  ["Capacitação", "Capacitação técnica & treinamentos", "Assinaturas anuais e planos institucionais da escola de citometria.", "star"],
];

export default async function products(ctx) {
  const root = h("div");
  async function load() {
    const { items } = await api.get("/api/v1/products");
    const fields = (p = {}) => [
      { name: "sku", label: "SKU", required: true }, { name: "name", label: "Nome", required: true },
      { name: "module_category", label: "Eixo de atuação", type: "select", options: AXES.map((a) => ({ value: a[0], label: a[1] })) },
      { name: "billing_modality", label: "Modalidade de cobrança", type: "select", options: Object.entries(MODALITY_LABEL).map(([value, label]) => ({ value, label })) },
      { name: "base_unit_price", label: "Preço unitário base (R$)", type: "number", step: "0.01", min: "0", required: true }, { name: "unit_label", label: "Unidade", placeholder: "hora, exame, mês…" },
      { name: "description", label: "Descrição", type: "textarea", full: true }, { name: "is_active", label: "Ativo", type: "checkbox" }];
    mount(root, [pageHead("Produtos & serviços", "Catálogo Flowmentor: quatro eixos de atuação e suas modalidades de cobrança.",
      can("products:manage") ? h("button", { class: "btn primary", onClick: () => formModal({ title: "Novo serviço", size: "wide", fields: fields(), values: { is_active: true }, onSubmit: async (v) => { await api.post("/api/v1/products", v); toast("Serviço criado", "ok"); load(); } }) }, icon("plus"), "Novo serviço") : null),
      h("div", { class: "col gap-16" }, AXES.map(([cat, title, desc, ic]) => card(null, [
        h("div", { class: "row mb-12" }, h("div", { class: "kpi", style: { padding: 0 } }, h("div", { class: "icon-bg", style: { position: "static" } }, icon(ic, "lg"))), h("div", h("h2", title), h("div", { class: "muted" }, desc))),
        h("div", { class: "grid c3" }, items.filter((p) => p.module_category === cat).map((p) => h("div", { class: "card", style: { padding: "12px 14px", opacity: p.is_active ? 1 : 0.55 } },
          h("div", { class: "row between" }, h("b", p.name), badge(MODALITY_LABEL[p.billing_modality], "info")), h("div", { class: "muted mt-8", style: { minHeight: "32px", fontSize: "12.5px" } }, p.description || ""),
          h("div", { class: "row between mt-8" }, h("span", { class: "strong", style: { fontSize: "17px" } }, money(p.base_unit_price), h("small", { class: "muted" }, ` / ${p.unit_label || "un"}`)),
            can("products:manage") ? h("button", { class: "btn ghost sm icon", onClick: () => formModal({ title: "Editar serviço", size: "wide", fields: fields(p), values: p, onSubmit: async (v) => { await api.put(`/api/v1/products/${p.id}`, v); load(); } }) }, icon("edit")) : h("span", { class: "mono muted" }, p.sku))))),
        !items.some((p) => p.module_category === cat) ? h("div", { class: "muted" }, "Nenhum serviço cadastrado neste eixo.") : null]))) ]);
  }
  await load();
  return root;
}
