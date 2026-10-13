// Utilitaires d'interface : construction DOM sûre (jamais d'innerHTML), icônes, formats, modales, toasts.
const SVG_NS = "http://www.w3.org/2000/svg";

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}
export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function mount(el, ...children) { clear(el); append(el, children); return el; }

const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><polyline points="14 3 14 8 19 8"/>',
  users: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7"/><path d="M18 14.2a6.5 6.5 0 0 1 3.5 5.8"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8"/><line x1="10" y1="12" x2="14" y2="12"/>',
  clock: '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7"/><polyline points="3 4 3 9 8 9"/><polyline points="12 8 12 12 14.5 13.5"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6z"/>',
  sliders: '<line x1="4" y1="7" x2="14" y2="7"/><line x1="18" y1="7" x2="20" y2="7"/><circle cx="16" cy="7" r="2"/><line x1="4" y1="17" x2="6" y2="17"/><line x1="10" y1="17" x2="20" y2="17"/><circle cx="8" cy="17" r="2"/>',
  activity: '<polyline points="3 12 7 12 10 5 14 19 17 12 21 12"/>',
  bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
  play: '<polygon points="7 4 20 12 7 20"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><polyline points="20 4 20 11 13 11"/>',
  zap: '<polygon points="13 3 5 14 11 14 11 21 19 10 13 10"/>',
  logout: '<path d="M9 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4"/><polyline points="16 8 20 12 16 16"/><line x1="20" y1="12" x2="9" y2="12"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9"/><path d="M16 7l3 3"/>',
  download: '<path d="M12 3v12"/><polyline points="7 11 12 16 17 11"/><path d="M4 20h16"/>',
  upload: '<path d="M12 16V4"/><polyline points="7 8 12 3 17 8"/><path d="M4 20h16"/>',
  trash: '<polyline points="4 7 20 7"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><line x1="20" y1="20" x2="16" y2="16"/>',
  x: '<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/>',
  check: '<polyline points="4 12 10 18 20 6"/>',
  alert: '<path d="M12 4l9.5 16h-19z"/><line x1="12" y1="10" x2="12" y2="14"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  info: '<circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
  cube: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12L4 7.5M12 12v9"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
  move: '<path d="M3 12h13"/><polyline points="12 7 17 12 12 17"/><path d="M21 5v14"/>',
  up: '<polyline points="6 14 12 8 18 14"/><line x1="12" y1="8" x2="12" y2="20"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  menu: '<line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/>',
  pause: '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
  wrench: '<path d="M14.5 6.5a4 4 0 0 0 5 5L10 21l-3-3 9.5-9.5z"/><path d="M15 3.5l-3 3 3 3"/>',
  filter: '<polygon points="3 5 21 5 14 13 14 20 10 18 10 13"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  diff: '<path d="M8 3v12M8 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM16 21V9M16 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/>',
  save: '<path d="M5 3h11l4 4v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><rect x="8" y="3" width="7" height="5"/><rect x="7" y="13" width="10" height="8"/>',
  box: '<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><polyline points="3 8 12 13 21 8"/><line x1="12" y1="13" x2="12" y2="21"/>',
  chev: '<polyline points="6 9 12 15 18 9"/>',
  home: '<path d="M4 11l8-7 8 7v9a1 1 0 0 1-1 1h-4v-6H9v6H5a1 1 0 0 1-1-1z"/>',
  dots: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
};
export function icon(name, cls = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", cls);
  svg.innerHTML = ICONS[name] || ICONS.file; // contenu statique défini ci-dessus (jamais de données externes)
  return svg;
}

// ---------- formats ----------
const dtf = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "medium" });
const dtfShort = new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
export const fmtDate = (iso) => (iso ? dtf.format(new Date(iso)) : "—");
export const fmtDateShort = (iso) => (iso ? dtfShort.format(new Date(iso)) : "—");
export function fmtTime(iso) { return iso ? new Date(iso).toLocaleTimeString("fr-FR") : ""; }
export function fmtBytes(n) {
  if (n === null || n === undefined) return "—";
  const u = ["o", "Ko", "Mo", "Go", "To"]; let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}
export function fmtDuration(s) {
  if (s === null || s === undefined) return "indisponible";
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d} j ${hh} h`;
  if (hh) return `${hh} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${s} s`;
}
export function fmtRel(iso) {
  if (!iso) return "jamais";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 0) { const f = -s; return f < 3600 ? `dans ${Math.ceil(f / 60)} min` : f < 86400 ? `dans ${Math.round(f / 3600)} h` : `dans ${Math.round(f / 86400)} j`; }
  if (s < 60) return "à l'instant";
  if (s < 3600) return `il y a ${Math.floor(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.floor(s / 3600)} h`;
  return `il y a ${Math.floor(s / 86400)} j`;
}
export const STATUS_LABEL = { running: "En ligne", stopped: "Arrêté", starting: "Démarrage…", stopping: "Arrêt en cours…" };

// ---------- notifications visuelles ----------
let toastBox;
export function toast(message, kind = "info", title = "") {
  if (!toastBox) { toastBox = h("div", { class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(toastBox); }
  const ic = { ok: "check", success: "check", info: "info", warn: "alert", warning: "alert", bad: "alert", error: "alert" }[kind] || "info";
  const t = h("div", { class: `toast ${kind}` }, icon(ic), h("div", { class: "grow" }, title ? h("b", null, title) : null, message));
  toastBox.append(t);
  const timer = setTimeout(() => t.remove(), kind === "error" || kind === "bad" ? 9000 : 5000);
  t.addEventListener("click", () => { clearTimeout(timer); t.remove(); });
  return t;
}

export function modal({ title, body, buttons = [], size = "", onClose, dismissable = true }) {
  const bodyEl = h("div", { class: "modal-b" }, body);
  const footer = h("div", { class: "modal-f" });
  const box = h("div", { class: `modal ${size}`, role: "dialog", "aria-modal": "true", "aria-label": title }, h("div", { class: "modal-h" }, h("h2", null, title), dismissable ? h("button", { class: "btn ghost sm icon-only", "aria-label": "Fermer", onclick: () => close() }, icon("x")) : null), bodyEl, footer);
  const overlay = h("div", { class: "overlay" }, box);
  let closed = false;
  function close(result) { if (closed) return; closed = true; overlay.remove(); document.removeEventListener("keydown", onKey, true); onClose && onClose(result); }
  function onKey(e) { if (e.key === "Escape" && dismissable) { e.stopPropagation(); close(); } }
  document.addEventListener("keydown", onKey, true);
  if (dismissable) overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) close(); });
  const api = { el: box, body: bodyEl, close, footer, buttons: {} };
  for (const b of buttons) {
    const btn = h("button", { class: `btn ${b.kind || ""}`, type: "button", onclick: async () => {
      if (b.onClick) { btn.disabled = true; try { const r = await b.onClick(api); if (r !== false) close(r); } finally { btn.disabled = false; } } else close(b.value);
    } }, b.icon ? icon(b.icon) : null, b.label);
    api.buttons[b.id || b.label] = btn;
    footer.append(btn);
  }
  document.body.append(overlay);
  setTimeout(() => (bodyEl.querySelector("input:not([type=checkbox]),select,textarea") || footer.querySelector(".primary, .danger") || box).focus?.(), 30);
  return api;
}
export function confirmDialog({ title = "Confirmation", message, confirmLabel = "Confirmer", danger = false, requireCheck = "" }) {
  return new Promise((resolve) => {
    let chk;
    const body = h("div", { class: "stack" }, typeof message === "string" ? h("p", { style: { margin: 0 } }, message) : message,
      requireCheck ? h("label", { class: "check" }, (chk = h("input", { type: "checkbox" })), requireCheck) : null);
    const m = modal({ title, body, size: "narrow", onClose: (r) => resolve(r === true), buttons: [
      { label: "Annuler", value: false },
      { label: confirmLabel, kind: danger ? "danger" : "primary", id: "ok", onClick: () => { if (requireCheck && !chk.checked) { toast("Cochez la case pour confirmer.", "warn"); return false; } return true; } },
    ] });
    if (requireCheck) m.buttons.ok.disabled = false;
  });
}
export function promptDialog({ title, label, value = "", placeholder = "", confirmLabel = "Valider", type = "text", hint = "" }) {
  return new Promise((resolve) => {
    const input = h("input", { class: "input", type, value, placeholder, autocomplete: "off" });
    const submit = () => { m.close(input.value); };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    const m = modal({ title, size: "narrow", body: h("div", { class: "field" }, h("label", null, label), input, hint ? h("div", { class: "hint" }, hint) : null),
      onClose: (r) => resolve(typeof r === "string" ? r : null), buttons: [{ label: "Annuler", value: null }, { label: confirmLabel, kind: "primary", onClick: () => { submit(); return false; } }] });
    input.select?.();
  });
}
export function contextMenu(x, y, items) {
  document.querySelectorAll(".ctx").forEach((n) => n.remove());
  const menu = h("div", { class: "ctx", role: "menu" });
  for (const it of items) {
    if (it === "-") { menu.append(h("hr")); continue; }
    menu.append(h("button", { class: it.danger ? "danger" : "", role: "menuitem", onclick: () => { menu.remove(); it.onClick(); } }, icon(it.icon || "file"), it.label));
  }
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.max(6, Math.min(x, innerWidth - r.width - 6)) + "px";
  menu.style.top = Math.max(6, Math.min(y, innerHeight - r.height - 6)) + "px";
  const off = (e) => { if (!menu.contains(e.target) || e.type === "scroll") { menu.remove(); cleanup(); } };
  const cleanup = () => { document.removeEventListener("mousedown", off, true); document.removeEventListener("keydown", esc, true); window.removeEventListener("scroll", off, true); };
  const esc = (e) => { if (e.key === "Escape") { menu.remove(); cleanup(); } };
  setTimeout(() => { document.addEventListener("mousedown", off, true); document.addEventListener("keydown", esc, true); window.addEventListener("scroll", off, true); }, 0);
  return menu;
}

export function loading(text = "Chargement…") { return h("div", { class: "loading" }, h("span", { class: "spin" }), text); }
export function emptyState(iconName, text, sub) { return h("div", { class: "empty" }, icon(iconName), h("div", null, text), sub ? h("div", { class: "small faint" }, sub) : null); }
export function badge(text, kind = "") { return h("span", { class: `badge ${kind}` }, text); }
export function statusPill(status) { return h("span", { class: `pill ${status}` }, h("span", { class: `dot ${status}` }), STATUS_LABEL[status] || status); }
export function field(label, input, hint) { return h("div", { class: "field" }, h("label", null, label), input, hint ? h("div", { class: "hint" }, hint) : null); }
export function switchEl(checked, onChange, label) {
  const i = h("input", { type: "checkbox", checked: checked || null, onchange: (e) => onChange && onChange(e.target.checked) });
  if (checked) i.checked = true;
  return h("label", { class: "check" }, h("span", { class: "switch" }, i, h("span")), label || null);
}
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copié dans le presse-papiers.", "ok"); }
  catch { const t = h("textarea", { style: { position: "fixed", opacity: 0 } }); t.value = text; document.body.append(t); t.select(); try { document.execCommand("copy"); toast("Copié.", "ok"); } catch { toast("Copie impossible.", "error"); } t.remove(); }
}
export function debounce(fn, ms = 250) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function busyButton(btn, fn) {
  return async (...args) => {
    if (btn.disabled) return;
    btn.disabled = true; const sp = h("span", { class: "spin" }); btn.prepend(sp);
    try { return await fn(...args); } finally { sp.remove(); btn.disabled = false; }
  };
}
export const LEVEL_KIND = { error: "bad", warning: "warn", warn: "warn", success: "ok", info: "info" };
