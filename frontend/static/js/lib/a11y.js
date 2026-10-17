// Cross-cutting accessibility: gives an accessible name to dynamically mounted controls (icon-only buttons, filters without <label>).
// Runs in a lightweight MutationObserver; does not touch controls that already have a name.
const ICON_NAME = {
  edit: "Editar", trash: "Excluir", plus: "Adicionar", x: "Fechar", more: "Mais ações", send: "Enviar", paperclip: "Anexar arquivo", search: "Buscar", refresh: "Atualizar",
  download: "Baixar", check: "Confirmar", key: "Chave de API", link: "Copiar link", eye: "Visualizar", zap: "Assumir conversa", whisper: "Sussurrar", lock: "Bloqueado",
  menu: "Menu", chevron: "Expandir", moon: "Alternar tema", sun: "Alternar tema", logout: "Sair", star: "Favoritar", phone: "Telefone", note: "Nota",
};

const hasName = (el) => (el.textContent || "").trim() || el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || el.getAttribute("title");

function nearbyText(field) {
  const prev = field.previousElementSibling;
  if (prev && (prev.tagName === "LABEL" || prev.classList.contains("muted") || prev.classList.contains("label"))) return prev.textContent.trim();
  const parent = field.closest(".field");
  return parent?.querySelector("label")?.textContent.replace(/\s*\*$/, "").trim() || "";
}

export function enhance(root = document) {
  root.querySelectorAll("button:not([aria-label])").forEach((b) => {
    if (hasName(b)) return;
    const ic = b.querySelector("svg[data-icon]")?.getAttribute("data-icon");
    if (ic && ICON_NAME[ic]) b.setAttribute("aria-label", ICON_NAME[ic]);
  });
  root.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]), select, textarea").forEach((f) => {
    if (f.getAttribute("aria-label") || f.getAttribute("aria-labelledby") || f.getAttribute("title") || f.closest("label")) return;
    if (f.id && root.querySelector?.(`label[for="${CSS.escape(f.id)}"]`)) return;
    const text = nearbyText(f) || f.getAttribute("placeholder") || (f.tagName === "SELECT" ? f.options[0]?.textContent : "") || f.name;
    if (text) f.setAttribute("aria-label", text);
  });
}

export function startA11y() {
  let queued = false;
  const run = () => { queued = false; enhance(document.body); };
  new MutationObserver(() => { if (!queued) { queued = true; requestAnimationFrame(run); } }).observe(document.body, { childList: true, subtree: true });
  run();
}
