// Hash router with on-demand loading, permission control and listener cleanup.
import { h, mount } from "./lib/dom.js";
import { can, canAny } from "./lib/store.js";
import { spinner, emptyState } from "./lib/ui.js";
import { on as socketOn } from "./lib/socket.js";

export const ROUTES = [
  { path: "/dashboard", title: "Dashboard", page: () => import("./pages/dashboard.js") },
  { path: "/pipeline", title: "Pipeline comercial", perm: "deals:read", page: () => import("./pages/pipeline.js") },
  { path: "/accounts", title: "Contas institucionais", perm: "accounts:read", page: () => import("./pages/accounts.js") },
  { path: "/accounts/:id", title: "Conta institucional", perm: "accounts:read", page: () => import("./pages/account_detail.js"), nav: "/accounts" },
  { path: "/contacts", title: "Contatos", perm: "contacts:read", page: () => import("./pages/contacts.js") },
  { path: "/products", title: "Produtos & serviços", perm: "products:read", page: () => import("./pages/products.js") },
  { path: "/cases", title: "Casos & laudos", perm: "cases:read", page: () => import("./pages/cases.js") },
  { path: "/contracts", title: "Contratos & faturas", perm: "contracts:read", page: () => import("./pages/contracts.js") },
  { path: "/inbox", title: "Atendimentos", perm: "inbox:use", page: () => import("./pages/inbox.js"), flush: true },
  { path: "/inbox/:id", title: "Atendimentos", perm: "inbox:use", page: () => import("./pages/inbox.js"), flush: true, nav: "/inbox" },
  { path: "/supervisor", title: "Supervisão ao vivo", perm: "supervisor:live", page: () => import("./pages/supervisor.js") },
  { path: "/templates", title: "Templates HSM", perm: "templates:read", page: () => import("./pages/templates.js") },
  { path: "/analytics", title: "Indicadores (KPIs)", perm: "kpi:view", page: () => import("./pages/analytics.js") },
  { path: "/integrations", title: "Webhooks & Open API", perm: "webhooks:manage", page: () => import("./pages/integrations.js") },
  { path: "/compliance", title: "LGPD & auditoria", any: ["lgpd:manage", "audit:view"], page: () => import("./pages/compliance.js") },
  { path: "/settings", title: "Configurações", any: ["org:manage", "whatsapp:manage", "users:manage"], page: () => import("./pages/settings.js") },
];

function match(hash) {
  const path = (hash.replace(/^#/, "") || "/dashboard").split("?")[0];
  for (const r of ROUTES) {
    const names = [];
    const re = new RegExp("^" + r.path.replace(/:([a-z]+)/g, (_, n) => { names.push(n); return "([^/]+)"; }) + "/?$");
    const m = path.match(re);
    if (m) return { route: r, params: Object.fromEntries(names.map((n, i) => [n, decodeURIComponent(m[i + 1])])), query: Object.fromEntries(new URLSearchParams(hash.split("?")[1] || "")) };
  }
  return null;
}

export function router(shell) {
  let cleanups = [];
  let token = 0;

  async function render() {
    const my = ++token;
    cleanups.forEach((f) => { try { f(); } catch { /* noop */ } });
    cleanups = [];
    document.querySelectorAll(".menu").forEach((m) => m.remove());
    const hit = match(location.hash);
    if (!hit) { location.hash = "#/dashboard"; return; }
    const { route, params, query } = hit;
    shell.nav.querySelectorAll("a").forEach((a) => a.classList.toggle("active", a.dataset.path === (route.nav || route.path)));
    shell.title.textContent = route.title;
    shell.content.className = `content ${route.flush ? "flush" : ""}`;
    if ((route.perm && !can(route.perm)) || (route.any && !canAny(...route.any))) {
      return mount(shell.content, emptyState("Você não tem permissão para acessar esta área.", "lock"));
    }
    mount(shell.content, spinner());
    try {
      const mod = await route.page();
      const ctx = {
        params, query, content: shell.content,
        onCleanup: (f) => cleanups.push(f),
        on: (event, fn) => cleanups.push(socketOn(event, fn)),
        navigate: (hash) => { location.hash = hash; },
        alive: () => my === token,
      };
      const node = await mod.default(ctx);
      if (my !== token) return;
      mount(shell.content, node);
    } catch (e) {
      console.error(e);
      if (my === token) mount(shell.content, h("div", { class: "callout warn" }, `Não foi possível carregar a página: ${e.message}`));
    }
  }
  window.addEventListener("hashchange", render);
  render();
}
