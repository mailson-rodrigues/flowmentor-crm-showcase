import { h, svg, icon, mount } from "../lib/dom.js";
import { api } from "../lib/api.js";
import { applyTheme } from "../lib/store.js";

function cells() {
  const el = svg("svg", { viewBox: "0 0 400 400", class: "cells", "aria-hidden": "true" });
  const dots = [[90, 120, 46, "#0EA5E9"], [230, 90, 30, "#14B8A6"], [300, 210, 58, "#0EA5E9"], [140, 280, 38, "#14B8A6"], [250, 330, 22, "#6366F1"], [60, 220, 18, "#6366F1"], [330, 90, 14, "#14B8A6"]];
  for (const [x, y, r, c] of dots) { el.append(svg("circle", { cx: x, cy: y, r, fill: c, opacity: ".18" }), svg("circle", { cx: x, cy: y, r: r * 0.55, fill: c, opacity: ".35" })); }
  el.append(svg("path", { d: "M20 260 C 90 190, 150 330, 210 240 S 330 180, 390 250", fill: "none", stroke: "#38BDF8", "stroke-width": 2, opacity: ".5" }));
  return el;
}

export function renderLogin(root, onSuccess) {
  applyTheme("light");
  const err = h("div", { class: "callout warn hidden", role: "alert" });
  const email = h("input", { class: "input", type: "email", name: "email", autocomplete: "username", placeholder: "voce@empresa.com.br", required: true });
  const pass = h("input", { class: "input", type: "password", name: "password", autocomplete: "current-password", placeholder: "Sua senha", required: true });
  const btn = h("button", { class: "btn primary", type: "submit", style: { width: "100%", height: "40px" } }, "Entrar");
  const form = h("form", { onSubmit: async (e) => {
    e.preventDefault(); err.classList.add("hidden"); btn.disabled = true; btn.textContent = "Entrando…";
    try { await api.login(email.value, pass.value); await onSuccess(); }
    catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); btn.disabled = false; btn.textContent = "Entrar"; }
  } },
    h("div", { class: "field" }, h("label", "E-mail"), email), h("div", { class: "field" }, h("label", "Senha"), pass), err, btn);

  mount(root, h("div", { class: "login" },
    h("section", { class: "login-hero" },
      cells(),
      h("div", { class: "brand", style: { padding: 0 } }, h("div", { class: "logo" }, (() => { const s = svg("svg", { viewBox: "0 0 24 24" }); s.append(svg("path", { d: "M3 14c2-6 5-6 7-2s4 4 7-2 M3 20c2-4 4-4 6-1" })); return s; })()), h("div", "Flowmentor")),
      h("div", h("h1", "Gestão comercial e atendimento para a citometria de alta complexidade."),
        h("p", { class: "mt-12" }, "Pipeline B2B, casos técnicos, laudos, contratos e WhatsApp oficial em uma única plataforma — com segurança e conformidade LGPD."),
        h("div", { class: "mt-16" }, ["WhatsApp Business (BSP)", "SLA em tempo real", "CSAT · NPS · CES", "Auditoria imutável", "LGPD"].map((t) => h("span", { class: "pill" }, icon("check", "sm"), t)))),
      h("div", { class: "muted", style: { fontSize: "12px" } }, "© Flowmentor · Dados protegidos com TLS e criptografia em repouso")),
    h("section", { class: "login-form" }, h("div", { class: "login-box" }, h("h2", "Bem-vindo de volta"), h("p", { class: "muted mb-16" }, "Acesse sua conta para continuar."), form,
      h("p", { class: "muted mt-16", style: { fontSize: "12px" } }, "O acesso é monitorado. Tentativas inválidas repetidas bloqueiam a conta temporariamente.")))));
  email.focus();
}
