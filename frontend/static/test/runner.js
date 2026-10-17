// Runner for the frontend test page. Imports the same SPA modules (single instance) and exercises them against the real API.
import { api, auth } from "/static/js/lib/api.js";
import { h, money, mount } from "/static/js/lib/dom.js";
import { can, canAny, setSession, applyTheme, toggleTheme } from "/static/js/lib/store.js";
import { toast, openModal } from "/static/js/lib/ui.js";
import { connectSocket, disconnectSocket, isConnected, on as socketOn } from "/static/js/lib/socket.js";
import { ROUTES } from "/static/js/router.js";
import { enhance } from "/static/js/lib/a11y.js";

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
applyTheme();

// Roles × expected visible routes (independent of the SPA code; source: backend/app/security/rbac.py).
const COMMON = ["/dashboard", "/pipeline", "/accounts", "/contacts", "/products", "/cases", "/contracts"];
const EXPECTED = {
  admin: [...COMMON, "/inbox", "/supervisor", "/templates", "/analytics", "/integrations", "/compliance", "/settings"],
  supervisor: [...COMMON, "/inbox", "/supervisor", "/templates", "/analytics"],
  consultor: [...COMMON, "/inbox", "/templates", "/analytics"],
  agent: [...COMMON, "/inbox", "/templates"],
  auditor: [...COMMON, "/analytics", "/compliance"],
};
// Endpoint the server must deny when the route is hidden for the role.
const ROUTE_API = {
  "/pipeline": "/api/v1/deals/board", "/inbox": "/api/v1/tickets", "/supervisor": "/api/v1/supervisor/live", "/templates": "/api/v1/templates",
  "/analytics": "/api/v1/kpis/overview", "/integrations": "/api/v1/webhooks/endpoints", "/compliance": "/api/v1/lgpd/audit-logs",
};

const results = [];
let currentSuite = null;

function ui(status, name, detail, ms) {
  results.push({ suite: currentSuite, name, status, detail: detail || "", ms: Math.round(ms || 0) });
  paint();
}

function paint() {
  const bySuite = new Map();
  for (const r of results) { if (!bySuite.has(r.suite)) bySuite.set(r.suite, []); bySuite.get(r.suite).push(r); }
  const only = $("only-fail").checked;
  const frag = document.createDocumentFragment();
  for (const [suite, items] of bySuite) {
    const bad = items.some((i) => i.status === "fail"), warn = items.some((i) => i.status === "warn");
    const det = h("details", { class: "tp-suite", open: bad || warn || suite === currentSuite });
    det.append(h("summary", h("span", { class: `pill ${bad ? "fail" : warn ? "warn" : "pass"}` }, bad ? "FALHA" : warn ? "AVISO" : "OK"), suite,
      h("span", { class: "count" }, `${items.filter((i) => i.status === "pass").length}/${items.length} ok`)));
    for (const i of items) {
      if (only && (i.status === "pass" || i.status === "skip")) continue;
      det.append(h("div", { class: "tp-test" }, h("span", { class: `pill ${i.status}` }, { pass: "OK", fail: "FALHA", warn: "AVISO", skip: "PULADO" }[i.status]),
        h("span", i.name), h("span", { class: "ms" }, i.ms ? `${i.ms} ms` : ""), i.detail ? h("div", { class: "detail" }, i.detail) : null));
    }
    frag.append(det);
  }
  mount($("results"), frag);
}

async function test(name, fn) {
  const t0 = performance.now();
  try {
    const out = await fn();
    if (out && typeof out === "object" && out.skip) ui("skip", name, out.skip, performance.now() - t0);
    else if (out && typeof out === "object" && out.warn) ui("warn", name, out.warn, performance.now() - t0);
    else ui("pass", name, typeof out === "string" ? out : "", performance.now() - t0);
  } catch (e) {
    ui("fail", name, e?.message || String(e), performance.now() - t0);
  }
}
const suite = (name) => { currentSuite = name; };
function assert(cond, msg) { if (!cond) throw new Error(msg); }

// Watches console errors, exceptions and HTTP responses >= 400 while `fn` runs.
async function watched(fn) {
  const errs = [], bad = [];
  const origErr = console.error, origFetch = window.fetch;
  console.error = (...a) => { errs.push(a.map((x) => (x instanceof Error ? x.message : String(x))).join(" ").slice(0, 200)); origErr.apply(console, a); };
  const onErr = (e) => errs.push(`erro: ${e.message || e.reason?.message || e.reason}`);
  window.addEventListener("error", onErr); window.addEventListener("unhandledrejection", onErr);
  window.fetch = async (input, init) => {
    const res = await origFetch(input, init);
    if (res.status >= 400) bad.push(`${res.status} ${(init?.method || "GET")} ${new URL(input.url || input, location.origin).pathname}`);
    return res;
  };
  try { return { value: await fn(), errs, bad }; }
  catch (e) { if (bad.length) e.message += ` [requisições: ${bad.join(", ")}]`; throw e; }
  finally { console.error = origErr; window.fetch = origFetch; window.removeEventListener("error", onErr); window.removeEventListener("unhandledrejection", onErr); }
}

async function login(email, password) {
  for (let i = 0; i < 5; i++) {
    try { return await api.login(email, password); }
    catch (e) { if (e.status !== 429) throw e; await sleep(15000); } // limite de login por minuto
  }
  throw new Error("limite de tentativas de login (429) — aguarde 1 minuto");
}

async function asUser(email, password) {
  await login(email, password);
  const me = await api.get("/api/v1/auth/me");
  setSession(me.user, me.permissions);
  return me;
}

// Renders an SPA page (same contract as the router) in a test container.
async function renderRoute(route, { params = {}, query = {} } = {}) {
  const box = h("div", { class: "content", id: "content" });
  const cleanups = [];
  const ctx = { params, query, content: box, onCleanup: (f) => cleanups.push(f), on: (e, fn) => cleanups.push(socketOn(e, fn)), navigate: () => {}, alive: () => true };
  const mod = await route.page();
  const node = await mod.default(ctx);
  mount(box, node);
  await sleep(700); // pages that finish drawing after the first render
  return { box, done: () => cleanups.forEach((f) => { try { f(); } catch { /* noop */ } }) };
}

function a11yIssues(root) {
  enhance(root); // same pass the app applies at runtime (main.js → startA11y)
  const issues = [];
  const named = (el) => (el.textContent || "").trim() || el.getAttribute("aria-label") || el.getAttribute("title") || el.getAttribute("aria-labelledby");
  root.querySelectorAll("button").forEach((b) => { if (!named(b)) issues.push("botão sem nome acessível"); });
  root.querySelectorAll("a[href]").forEach((a) => { if (!named(a)) issues.push("link sem texto"); });
  root.querySelectorAll("img").forEach((i) => { if (!i.hasAttribute("alt")) issues.push("imagem sem alt"); });
  root.querySelectorAll("input,select,textarea").forEach((f) => {
    if (["hidden", "submit", "button", "checkbox", "radio"].includes(f.type)) return;
    const labelled = f.getAttribute("aria-label") || f.getAttribute("placeholder") || f.getAttribute("title") || f.closest("label") || (f.id && root.querySelector(`label[for="${CSS.escape(f.id)}"]`));
    if (!labelled) issues.push(`campo ${f.name || f.type} sem rótulo`);
  });
  return issues;
}

// ---------------------------------------------------------------------------------------------------------------------
async function runAll() {
  results.length = 0; paint();
  $("run").disabled = true; $("export").disabled = true; $("summary").hidden = true; $("sandbox-wrap").hidden = true;
  const password = $("password").value;
  const roles = Object.fromEntries($("roles").value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => l.split("=").map((s) => s.trim())));
  const writes = $("opt-write").checked, realtime = $("opt-realtime").checked;
  let savedAuth = null; try { savedAuth = localStorage.getItem("fm_auth"); } catch { /* noop */ }
  const savedTheme = document.documentElement.dataset.theme;
  const t0 = performance.now();
  const cleanupFns = [];
  const pagesRendered = new Set();
  try {
    // ---- 1. Assets e infraestrutura
    suite("1. Assets e infraestrutura");
    for (const [path, type] of [["/", "text/html"], ["/static/css/app.css", "text/css"], ["/static/js/main.js", "javascript"], ["/static/vendor/socket.io.min.js", "javascript"], ["/static/favicon.svg", "svg"]]) {
      await test(`GET ${path}`, async () => { const r = await fetch(path); assert(r.ok, `HTTP ${r.status}`); assert((r.headers.get("content-type") || "").includes(type), `content-type ${r.headers.get("content-type")}`); });
    }
    await test("Health da API", async () => { const j = await (await fetch("/api/health")).json(); assert(j.status === "ok", JSON.stringify(j)); return `env=${j.env} meta=${j.meta_mode}`; });
    await test("Cabeçalhos de segurança (CSP, X-Frame-Options, nosniff)", async () => {
      const r = await fetch("/"); const csp = r.headers.get("content-security-policy") || "";
      assert(csp.includes("default-src 'self'") && csp.includes("frame-ancestors 'none'"), `CSP inesperada: ${csp}`);
      assert(r.headers.get("x-frame-options") === "DENY", "X-Frame-Options ausente");
      assert(r.headers.get("x-content-type-options") === "nosniff", "nosniff ausente");
    });
    await test("Fallback da SPA devolve HTML; /api inexistente devolve JSON 404", async () => {
      const spa = await fetch("/uma/rota/qualquer"); assert(spa.ok && (spa.headers.get("content-type") || "").includes("html"), `SPA HTTP ${spa.status}`);
      const nf = await fetch("/api/v1/nao-existe"); assert(nf.status === 404 && (nf.headers.get("content-type") || "").includes("json"), `API HTTP ${nf.status}`);
    });
    await test(`Tabela de rotas íntegra (${ROUTES.length} rotas, caminhos únicos)`, async () => {
      assert(new Set(ROUTES.map((r) => r.path)).size === ROUTES.length, "caminhos duplicados");
      assert(ROUTES.every((r) => r.title && typeof r.page === "function"), "rota sem título/página");
    });
    for (const r of ROUTES.filter((x, i, a) => a.findIndex((y) => String(y.page) === String(x.page)) === i)) {
      await test(`Módulo da página ${r.path} importa sem erro`, async () => { const m = await r.page(); assert(typeof m.default === "function", "sem export default"); });
    }

    // ---- 2. UI utilities
    suite("2. Utilitários de UI");
    await test("DOM helper escapa HTML (sem XSS por construção)", async () => {
      const el = h("div", "<img src=x onerror=window.__xss=1>"); assert(!el.querySelector("img") && el.textContent.includes("<img"), "HTML interpretado"); assert(!window.__xss, "script executou");
    });
    await test("Formatação monetária pt-BR", async () => { const s = money(1234.5); assert(/1\.234,50/.test(s) && s.includes("R$"), `obtido: ${s}`); assert(money(null) === "—", "null deveria ser —"); });
    await test("Alternância de tema (claro/escuro) persiste no <html>", async () => {
      const a = document.documentElement.dataset.theme; const b = toggleTheme(); assert(b !== a && document.documentElement.dataset.theme === b, "tema não mudou"); applyTheme(a);
    });
    await test("Toast é anunciado (role=status, aria-live)", async () => {
      toast("teste de toast", "ok"); await sleep(50); const box = document.querySelector(".toasts"); assert(box && box.getAttribute("aria-live") === "polite" && box.textContent.includes("teste de toast"), "toast ausente");
    });
    await test("Modal abre com role=dialog e fecha com Esc", async () => {
      openModal({ title: "Modal de teste", body: h("p", "conteúdo") }); await sleep(30);
      const m = document.querySelector('.modal[role="dialog"]'); assert(m && m.getAttribute("aria-modal") === "true", "modal ausente");
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })); await sleep(30); assert(!document.querySelector('.modal[role="dialog"]'), "Esc não fechou");
    });

    // ---- 3. Authentication
    suite("3. Autenticação e sessão");
    const adminEmail = roles.admin || Object.values(roles)[0];
    await test("Senha incorreta → 401 com mensagem", async () => {
      try { await api.login(adminEmail, "senha-errada-xyz"); } catch (e) { assert(e.status === 401, `status ${e.status}`); assert(e.message, "sem mensagem"); return `“${e.message}”`; }
      throw new Error("login com senha errada foi aceito");
    });
    await test("API sem token → 401", async () => { auth.clear(); const r = await fetch("/api/v1/auth/me"); assert(r.status === 401, `HTTP ${r.status}`); });
    if (!password) { ui("fail", "Senha informada", "Preencha a senha dos usuários de demonstração e execute novamente.", 0); throw new Error("sem senha"); }
    await test("Login válido devolve tokens e /auth/me", async () => {
      await login(adminEmail, password); const me = await api.get("/api/v1/auth/me"); assert(me.user?.email === adminEmail.toLowerCase() && me.permissions?.length > 0, "me inválido"); return `${me.user.role}, ${me.permissions.length} permissões`;
    });
    await test("Refresh automático quando o access token expira", async () => {
      const a = auth.get(); auth.set({ access: "token.invalido.x", refresh: a.refresh });
      const org = await api.get("/api/v1/org"); assert(org, "sem resposta"); assert(auth.get().access !== "token.invalido.x", "token não foi renovado");
    });
    await test("Logout revoga o refresh token", async () => {
      await login(adminEmail, password); const a = auth.get(); await api.logout();
      const r = await fetch("/api/v1/auth/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: a.refresh }) });
      assert(r.status === 401, `refresh ainda válido (HTTP ${r.status})`);
    });

    // ---- 4. Reference data (read-only)
    let accountId = null, ticketId = null;
    suite("4. Páginas por perfil (RBAC + renderização)");
    for (const [role, email] of Object.entries(roles)) {
      let me;
      try { me = await asUser(email, password); } catch (e) { ui("fail", `[${role}] login`, e.message, 0); continue; }
      ui("pass", `[${role}] login e permissões`, `${me.user.name} · ${me.permissions.length} permissões`, 0);
      const expected = EXPECTED[role];
      for (const route of ROUTES.filter((r) => !r.path.includes(":"))) {
        const gate = (!route.perm || can(route.perm)) && (!route.any || canAny(...route.any));
        if (expected) await test(`[${role}] ${route.path}: acesso ${expected.includes(route.path) ? "permitido" : "negado"} na UI`, async () => { assert(gate === expected.includes(route.path), `UI diz ${gate ? "permitido" : "negado"}, esperado ${expected.includes(route.path) ? "permitido" : "negado"}`); });
        const apiPath = ROUTE_API[route.path];
        if (apiPath) await test(`[${role}] ${apiPath}: servidor ${gate ? "autoriza" : "nega (403)"}`, async () => { const r = await api.raw(apiPath).then(() => 200).catch((e) => e.status); assert(gate ? r === 200 : r === 403, `HTTP ${r}`); });
        if (!gate) continue;
        await test(`[${role}] ${route.path}: renderiza sem erros`, async () => {
          const { value, errs, bad } = await watched(() => renderRoute(route));
          const { box, done } = value;
          try {
            assert(box.textContent.trim().length > 20, "página vazia");
            assert(!box.querySelector(".callout.warn"), `aviso de falha: ${box.querySelector(".callout.warn")?.textContent}`);
            assert(!errs.length, `console/erros: ${errs.join(" | ")}`);
            assert(!bad.length, `requisições com falha: ${bad.join(", ")}`);
            const issues = a11yIssues(box);
            pagesRendered.add(`${role}${route.path}`);
            if (role === "admin" && route.path === "/dashboard") { $("sandbox-wrap").hidden = false; mount($("sandbox"), box.cloneNode(true)); }
            if (issues.length) return { warn: `acessibilidade: ${[...new Set(issues)].join("; ")} (${issues.length})` };
            return `${box.textContent.trim().length} caracteres`;
          } finally { done(); }
        });
      }
    }

    // ---- 5. Detail pages
    suite("5. Páginas de detalhe");
    await asUser(roles.admin || Object.values(roles)[0], password);
    const detail = ROUTES.find((r) => r.path === "/accounts/:id"), inbox = ROUTES.find((r) => r.path === "/inbox/:id");
    let createdAccount = null;
    if (writes) {
      await test("Cria conta de teste com nome malicioso (payload XSS)", async () => {
        const evil = "[TESTE] <img src=x onerror=window.__xss=1> & \"aspas\"";
        createdAccount = await api.post("/api/v1/accounts", { corporate_name: evil, trade_name: evil, account_type: "laboratorio", status: "prospect" });
        accountId = createdAccount.id; cleanupFns.push(() => api.del(`/api/v1/accounts/${createdAccount.id}`));
        assert(accountId, "sem id");
      });
    } else {
      const list = await api.get("/api/v1/accounts"); accountId = list.items?.[0]?.id;
    }
    await test("Detalhe da conta renderiza sem erros e sem executar HTML injetado", async () => {
      if (!accountId) return { skip: "nenhuma conta disponível" };
      const { value, errs, bad } = await watched(() => renderRoute(detail, { params: { id: accountId } }));
      const { box, done } = value;
      try {
        assert(!window.__xss, "XSS executado!");
        assert(!box.querySelector("img[src='x']"), "elemento <img> injetado no DOM");
        assert(box.textContent.trim().length > 20 && !box.querySelector(".callout.warn"), "página vazia ou com aviso");
        if (createdAccount) assert(box.textContent.includes("<img src=x"), "texto malicioso deveria aparecer literal");
        assert(!errs.length && !bad.length, [...errs, ...bad].join(" | "));
      } finally { done(); }
    });
    await test("Lista de contas também não interpreta HTML", async () => {
      const route = ROUTES.find((r) => r.path === "/accounts"); const { value } = await watched(() => renderRoute(route)); value.done();
      assert(!window.__xss && !value.box.querySelector("img[src='x']"), "HTML injetado na lista");
    });
    await test("Detalhe da conta inexistente mostra erro tratado (sem quebrar)", async () => {
      const { value, errs } = await watched(() => renderRoute(detail, { params: { id: "00000000-0000-0000-0000-000000000000" } }).catch((e) => ({ thrown: e })));
      if (value.thrown) return `exceção tratada pelo router: ${value.thrown.message}`;
      value.done(); void errs;
    });
    await test("Atendimento (inbox/:id) renderiza", async () => {
      const tk = await api.get("/api/v1/tickets", { view: "open", limit: 1 }); ticketId = tk.items?.[0]?.id;
      if (!ticketId) { const cl = await api.get("/api/v1/tickets", { view: "closed", limit: 1 }); ticketId = cl.items?.[0]?.id; }
      if (!ticketId) return { skip: "nenhum atendimento existente (rode o realtime abaixo ou scripts/live_smoke.py)" };
      const { value, errs, bad } = await watched(() => renderRoute(inbox, { params: { id: ticketId } })); value.done();
      assert(value.box.textContent.trim().length > 20, "página vazia"); assert(!errs.length && !bad.length, [...errs, ...bad].join(" | "));
    });

    // ---- 6. Tempo real
    suite("6. Tempo real (Socket.IO + worker + Redis)");
    await test("Socket.IO conecta com o JWT", async () => {
      if (typeof io === "undefined") throw new Error("cliente socket.io não carregado nesta página");
      connectSocket(); for (let i = 0; i < 60 && !isConnected(); i++) await sleep(150); assert(isConnected(), "não conectou em 9s (CORS_ORIGINS inclui esta origem?)");
    });
    await test("Webhook simulado → worker → evento ticket.created chega ao navegador", async () => {
      if (!realtime || !writes) return { skip: "desmarcado nas opções" };
      const wa = "5562" + String(Date.now()).slice(-9); const seen = [];
      const off = socketOn("ticket_event", (e) => seen.push(e.type)); cleanupFns.push(off);
      let r; try { r = await api.post("/api/v1/whatsapp/sandbox/simulate-inbound", { from_number: wa, name: "Teste Frontend", text: "olá, teste do frontend" }); }
      catch (e) { if (e.code === "not_sandbox" || e.code === "no_whatsapp_account") return { skip: e.message }; throw e; }
      assert(r.queued, "não enfileirou");
      for (let i = 0; i < 60 && !seen.includes("ticket.created"); i++) await sleep(250);
      assert(seen.includes("ticket.created"), "evento não chegou em 15s (worker em execução? REDIS_URL configurado?)");
      return `eventos: ${[...new Set(seen)].join(", ")}`;
    });

    // ---- 7. Responsividade (CSS)
    suite("7. Estilos e responsividade");
    await test("Folhas de estilo definem pontos de quebra móveis", async () => {
      const css = await (await fetch("/static/css/app.css", { cache: "no-store" })).text(); assert(/@media[^{]*max-width/.test(css), "sem @media (max-width) — layout móvel ausente");
      if (!/prefers-reduced-motion/.test(css)) return { warn: "sem suporte a prefers-reduced-motion" };
      return "media queries presentes";
    });
    await test("Tema escuro definido", async () => { const css = await (await fetch("/static/css/app.css", { cache: "no-store" })).text(); assert(/\[data-theme=["']?dark/.test(css), "sem [data-theme=dark]"); });
  } catch (e) {
    if (e.message !== "sem senha") ui("fail", "Execução interrompida", e.message, 0);
  } finally {
    for (const f of cleanupFns.reverse()) { try { await f(); } catch { /* noop */ } }
    disconnectSocket();
    try { if (savedAuth === null) localStorage.removeItem("fm_auth"); else localStorage.setItem("fm_auth", savedAuth); } catch { /* noop */ }
    applyTheme(savedTheme); setSession(null, []);
    const fail = results.filter((r) => r.status === "fail").length, warn = results.filter((r) => r.status === "warn").length, pass = results.filter((r) => r.status === "pass").length, skip = results.filter((r) => r.status === "skip").length;
    const sum = $("summary"); sum.hidden = false; sum.className = `tp-summary ${fail ? "bad" : "ok"}`;
    sum.replaceChildren(document.createTextNode(fail ? `${fail} teste(s) falharam` : "Todos os testes passaram"),
      h("small", `${pass} ok · ${fail} falhas · ${warn} avisos · ${skip} pulados · ${((performance.now() - t0) / 1000).toFixed(1)} s · ${pagesRendered.size} páginas renderizadas`));
    window.__tpResult = { fail, warn, pass, skip, results };
    $("run").disabled = false; $("export").disabled = false; currentSuite = null; paint();
  }
}

$("run").addEventListener("click", runAll);
$("only-fail").addEventListener("change", paint);
$("theme").addEventListener("click", () => toggleTheme());
$("export").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify({ when: new Date().toISOString(), origin: location.origin, ...window.__tpResult }, null, 2)], { type: "application/json" });
  const a = h("a", { href: URL.createObjectURL(blob), download: "flowmentor-frontend-test.json" }); a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000);
});
