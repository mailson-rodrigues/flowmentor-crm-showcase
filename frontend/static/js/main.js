// Bootstrap: theme, session, shell (sidebar/topbar), routing and real-time events.
import { api, auth } from "./lib/api.js";
import { h, icon, mount, clear, avatar } from "./lib/dom.js";
import { state, can, canAny, setSession, applyTheme, toggleTheme, ROLE_LABEL } from "./lib/store.js";
import { toast, toastError, popMenu, spinner } from "./lib/ui.js";
import { connectSocket, disconnectSocket, on } from "./lib/socket.js";
import { router } from "./router.js";
import { startA11y } from "./lib/a11y.js";
import { renderLogin } from "./pages/login.js";

const root = document.getElementById("root");
applyTheme();
startA11y();

const NAV = [
  { section: "Visão geral" },
  { path: "/dashboard", label: "Dashboard", icon: "dashboard" },
  { section: "Comercial" },
  { path: "/pipeline", label: "Pipeline", icon: "pipeline", perm: "deals:read" },
  { path: "/accounts", label: "Contas institucionais", icon: "building", perm: "accounts:read" },
  { path: "/contacts", label: "Contatos", icon: "users", perm: "contacts:read" },
  { path: "/products", label: "Produtos & serviços", icon: "box", perm: "products:read" },
  { section: "Operação" },
  { path: "/cases", label: "Casos & laudos", icon: "flask", perm: "cases:read" },
  { path: "/contracts", label: "Contratos & faturas", icon: "file", perm: "contracts:read" },
  { section: "Atendimento WhatsApp" },
  { path: "/inbox", label: "Atendimentos", icon: "chat", perm: "inbox:use", badge: "queue" },
  { path: "/supervisor", label: "Supervisão ao vivo", icon: "eye", perm: "supervisor:live" },
  { path: "/templates", label: "Templates HSM", icon: "template", perm: "templates:read" },
  { path: "/analytics", label: "Indicadores (KPIs)", icon: "chart", perm: "kpi:view" },
  { section: "Gestão" },
  { path: "/integrations", label: "Webhooks & API", icon: "webhook", perm: "webhooks:manage" },
  { path: "/compliance", label: "LGPD & auditoria", icon: "shield", any: ["lgpd:manage", "audit:view"] },
  { path: "/settings", label: "Configurações", icon: "settings", any: ["org:manage", "whatsapp:manage", "users:manage"] },
];

const STATUS = { available: "Disponível", busy: "Ocupado", paused: "Em pausa", offline: "Offline" };
let queueBadge = null;

async function bootstrapSession() {
  const me = await api.get("/api/v1/auth/me");
  setSession(me.user, me.permissions);
}

function buildShell() {
  const collapsed = (() => { try { return localStorage.getItem("fm_collapsed") === "1"; } catch { return false; } })();
  const app = h("div", { class: `app ${collapsed ? "collapsed" : ""}` });
  const content = h("main", { class: "content", id: "content" });
  const title = h("h1", "Flowmentor");
  const nav = h("nav", { class: "nav", "aria-label": "Principal" });

  for (const item of NAV) {
    if (item.section) { nav.append(h("div", { class: "nav-section" }, item.section)); continue; }
    if (item.perm && !can(item.perm)) continue;
    if (item.any && !canAny(...item.any)) continue;
    const a = h("a", { href: `#${item.path}`, dataset: { path: item.path }, title: item.label }, icon(item.icon, "lg"), h("span", item.label));
    if (item.badge === "queue") { queueBadge = h("span", { class: "badge-count hidden" }, "0"); a.append(queueBadge); }
    nav.append(a);
  }

  const side = h("aside", { class: "sidebar", id: "sidebar" },
    h("div", { class: "brand" }, h("div", { class: "logo" }, (() => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("viewBox", "0 0 24 24"); const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", "M3 14c2-6 5-6 7-2s4 4 7-2 M3 20c2-4 4-4 6-1"); s.append(p); return s; })()),
      h("div", "Flowmentor", h("small", "CRM · Citometria"))),
    nav,
    h("div", { class: "side-user" }, avatar(state.user.name), h("div", { class: "info grow" }, h("div", { class: "name truncate" }, state.user.name), h("div", { class: "role muted" }, ROLE_LABEL[state.user.role] || state.user.role))));

  const statusBtn = h("button", { class: "status-pill" });
  const drawStatus = () => mount(statusBtn, h("span", { class: `dot ${state.user.status}` }), STATUS[state.user.status] || "Offline");
  drawStatus();
  statusBtn.addEventListener("click", () => popMenu(statusBtn, Object.entries(STATUS).map(([k, label]) => ({ label, onClick: async () => {
    try { await api.put("/api/v1/auth/me/status", { status: k }); state.user.status = k; drawStatus(); toast(`Status: ${label}`, "ok"); } catch (e) { toastError(e); }
  } }))));

  const themeBtn = h("button", { class: "btn ghost icon", title: "Alternar tema", onClick: () => { const t = toggleTheme(); mount(themeBtn, icon(t === "dark" ? "sun" : "moon")); } }, icon(document.documentElement.dataset.theme === "dark" ? "sun" : "moon"));
  const modeTag = h("span", { class: "mode-tag hidden", title: "Meta em modo sandbox: nenhuma mensagem real é enviada" }, "SANDBOX");
  api.get("/api/v1/meta").then((m) => { state.meta = m; if (m.meta_mode === "sandbox") modeTag.classList.remove("hidden"); }).catch(() => {});

  const topbar = h("header", { class: "topbar" },
    h("button", { class: "btn ghost icon", "aria-label": "Menu", "aria-controls": "sidebar", onClick: () => {
      if (matchMedia("(max-width: 820px)").matches) { app.classList.toggle("nav-open"); return; }  // celular: menu em gaveta
      app.classList.toggle("collapsed"); try { localStorage.setItem("fm_collapsed", app.classList.contains("collapsed") ? "1" : "0"); } catch { /* noop */ }
    } }, icon("menu")),
    title, modeTag, h("div", { class: "grow" }), statusBtn, themeBtn,
    h("button", { class: "btn ghost sm", onClick: async () => { await api.logout(); disconnectSocket(); location.hash = ""; start(); } }, icon("logout"), "Sair"));

  const backdrop = h("div", { class: "nav-backdrop", onClick: () => app.classList.remove("nav-open") });
  nav.addEventListener("click", (e) => { if (e.target.closest("a")) app.classList.remove("nav-open"); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") app.classList.remove("nav-open"); });
  app.append(side, backdrop, h("div", { class: "main" }, topbar, content));
  return { app, content, title, nav };
}

async function refreshQueueBadge() {
  if (!queueBadge || !can("inbox:use")) return;
  try {
    const r = await api.get("/api/v1/tickets", { view: "queue", limit: 1 });
    queueBadge.textContent = r.total; queueBadge.classList.toggle("hidden", !r.total);
  } catch { /* silencioso */ }
}

function wireRealtime() {
  connectSocket();
  on("whisper", (w) => toast(w.body, "warn", `💬 Sussurro de ${w.author_name || "supervisor"}`));
  on("barge_in", () => toast("Um supervisor assumiu uma de suas conversas.", "warn"));
  on("sla_alert", (a) => { if (a.level === "breached") toast(`SLA estourado (${a.kind || a.scope})`, "error"); else toast(`SLA em risco (${a.kind || a.scope})`, "warn"); });
  on("ticket_event", (e) => { if (["ticket.queued", "ticket.created", "ticket.assigned", "ticket.closed", "ticket.handoff"].includes(e.type)) refreshQueueBadge(); });
  on("survey", (s) => { if (s.type === "survey.detractor") toast(`Avaliação baixa recebida (nota ${s.rating}).`, "warn", "Detrator identificado"); });
  refreshQueueBadge();
}

async function start() {
  if (!auth.get()?.access) { mount(root); return renderLogin(root, async () => start()); }
  mount(root, spinner());
  try { await bootstrapSession(); } catch { auth.clear(); return start(); }
  const shell = buildShell();
  mount(root, shell.app);
  wireRealtime();
  router(shell);
}

window.addEventListener("fm:logout", () => { disconnectSocket(); start(); });
start();
