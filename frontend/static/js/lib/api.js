// Cliente HTTP: Bearer JWT, refresh automático com rotação e erros padronizados.
const KEY = "fm_auth";
let refreshing = null;

export class ApiError extends Error {
  constructor(message, status, code, details) { super(message); this.status = status; this.code = code; this.details = details || {}; }
}

export const auth = {
  get() { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch { return null; } },
  set(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch { /* storage indisponível */ } },
  clear() { try { localStorage.removeItem(KEY); } catch { /* noop */ } },
};

async function refreshTokens() {
  const a = auth.get();
  if (!a?.refresh) throw new ApiError("Sessão expirada", 401, "no_refresh");
  refreshing ??= fetch("/api/v1/auth/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: a.refresh }) })
    .then(async (r) => {
      if (!r.ok) throw new ApiError("Sessão expirada", 401, "refresh_failed");
      const j = await r.json();
      auth.set({ access: j.access_token, refresh: j.refresh_token });
      return j;
    })
    .finally(() => { refreshing = null; });
  return refreshing;
}

async function request(method, path, { body, params, form, retry = true, raw = false } = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  const headers = {};
  const a = auth.get();
  if (a?.access) headers.Authorization = `Bearer ${a.access}`;
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const res = await fetch(url, { method, headers, body: payload });
  if (res.status === 401 && retry && a?.refresh && !path.includes("/auth/")) {
    try { await refreshTokens(); return request(method, path, { body, params, form, retry: false, raw }); }
    catch { auth.clear(); window.dispatchEvent(new Event("fm:logout")); throw new ApiError("Sessão expirada. Entre novamente.", 401, "expired"); }
  }
  if (res.status === 204) return null;
  if (raw && res.ok) return res;
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json().catch(() => null) : await res.text();
  if (!res.ok) {
    const e = data?.error || {};
    throw new ApiError(e.message || "Erro inesperado", res.status, e.code, e.details);
  }
  return data;
}

export const api = {
  get: (p, params) => request("GET", p, { params }),
  post: (p, body, params) => request("POST", p, { body: body ?? {}, params }),
  put: (p, body) => request("PUT", p, { body: body ?? {} }),
  patch: (p, body) => request("PATCH", p, { body: body ?? {} }),
  del: (p) => request("DELETE", p),
  upload: (p, form) => request("POST", p, { form }),
  raw: (p, params) => request("GET", p, { params, raw: true }),
  login: async (email, password) => {
    const r = await fetch("/api/v1/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) });
    const j = await r.json();
    if (!r.ok) throw new ApiError(j?.error?.message || "Falha no login", r.status, j?.error?.code);
    auth.set({ access: j.access_token, refresh: j.refresh_token });
    return j;
  },
  logout: async () => { const a = auth.get(); try { if (a?.refresh) await request("POST", "/api/v1/auth/logout", { body: { refresh_token: a.refresh }, retry: false }); } catch { /* ignora */ } auth.clear(); },
};

export async function download(path, filename, params) {
  const res = await api.raw(path, params);
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}
