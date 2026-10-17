// Estado global leve: sessão, permissões e tema.
export const state = { user: null, permissions: new Set(), meta: null };
const listeners = new Set();

export const can = (p) => state.permissions.has(p);
export const canAny = (...ps) => ps.some((p) => state.permissions.has(p));
export function setSession(user, permissions) { state.user = user; state.permissions = new Set(permissions || []); listeners.forEach((f) => f()); }
export const onSession = (f) => { listeners.add(f); return () => listeners.delete(f); };

export function applyTheme(t) {
  const theme = t || (() => { try { return localStorage.getItem("fm_theme"); } catch { return null; } })() || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("fm_theme", theme); } catch { /* noop */ }
  return theme;
}
export const toggleTheme = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

export const ROLE_LABEL = { admin: "Administrador", supervisor: "Supervisor", consultor: "Consultor Técnico", agent: "Atendente", auditor: "Auditor / DPO", bot: "Bot" };
export const STAGE_LABEL = {
  lead_inbound: "Lead inbound", prospeccao: "Prospecção", qualificacao_tecnica: "Qualificação técnica", reuniao_alinhamento: "Reunião de alinhamento",
  proposta_apresentada: "Proposta enviada", negociacao: "Negociação", fechado_ganho: "Fechado / Ganho", fechado_perdido: "Fechado / Perdido",
};
export const STATUS_LABEL = { pending_bot: "Com o bot", queued: "Na fila", in_progress: "Em atendimento", pending_client: "Aguardando cliente", resolved: "Resolvido", closed: "Encerrado" };
export const CASE_TYPE_LABEL = { consultoria: "Consultoria técnica", apoio_diagnostico: "Apoio diagnóstico", canal_medico: "Canal médico", capacitacao: "Capacitação" };
export const CASE_STATUS_LABEL = { open: "Aberto", in_progress: "Em andamento", waiting_client: "Aguardando cliente", review: "Em revisão", done: "Concluído", cancelled: "Cancelado" };
export const ACCOUNT_TYPE_LABEL = { hospital: "Hospital", laboratorio: "Laboratório", clinica: "Clínica", universidade: "Universidade", outro: "Outro" };
export const MODALITY_LABEL = { hourly: "Por hora", unit_volume: "Por volume", recurring_subscription: "Assinatura", fixed_project: "Projeto fechado" };
