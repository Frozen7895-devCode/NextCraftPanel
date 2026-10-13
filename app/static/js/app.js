// Noyau de l'application : routeur, mise en page, WebSocket d'événements, actions serveur partagées.
import { h, icon, mount, clear, toast, modal, confirmDialog, statusPill, STATUS_LABEL, fmtDuration, badge, loading, LEVEL_KIND } from "./util.js";
import { api, get, post, setCsrf, setAuthHandlers } from "./api.js";
import { renderLogin, renderSetup, renderForcePassword } from "./auth.js";

export const state = { user: null, perms: new Set(), status: null, stats: null, unread: 0, maintenance: { enabled: false } };
export const can = (p) => state.perms.has(p);
const listeners = {};
export const bus = {
  on(evt, fn) { (listeners[evt] ||= new Set()).add(fn); return () => listeners[evt].delete(fn); },
  emit(evt, data) { (listeners[evt] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } }); },
};

const NAV = [
  ["Supervision", [["dashboard", "Tableau de bord", "dashboard", "server.view"], ["console", "Console", "terminal", "console.view"], ["players", "Joueurs", "users", "players.view"]]],
  ["Gestion", [["files", "Fichiers", "folder", "files.read"], ["backups", "Sauvegardes", "archive", "backups.view"], ["scheduler", "Planificateur", "clock", "tasks.view"], ["server", "Paramètres du serveur", "sliders", "server.view"]]],
  ["Surveillance", [["diagnostics", "Diagnostic", "activity", "diagnostics.view"], ["history", "Historique", "history", "audit.view"], ["notifications", "Notifications", "bell", "server.view"]]],
  ["Administration", [["accounts", "Comptes", "shield", "accounts.manage"], ["settings", "Paramètres généraux", "wrench", "settings.manage"]]],
];
const PAGES = {
  dashboard: ["Tableau de bord", "server.view", () => import("./pages/dashboard.js")],
  console: ["Console", "console.view", () => import("./pages/console.js")],
  files: ["Fichiers", "files.read", () => import("./pages/files.js")],
  editor: ["Éditeur", "files.read", () => import("./pages/editor.js")],
  players: ["Joueurs", "players.view", () => import("./pages/players.js")],
  backups: ["Sauvegardes", "backups.view", () => import("./pages/backups.js")],
  scheduler: ["Planificateur", "tasks.view", () => import("./pages/scheduler.js")],
  server: ["Paramètres du serveur", "server.view", () => import("./pages/server.js")],
  diagnostics: ["Diagnostic et rapports", "diagnostics.view", () => import("./pages/diagnostics.js")],
  history: ["Historique et journaux", "audit.view", () => import("./pages/history.js")],
  notifications: ["Notifications", "server.view", () => import("./pages/notifications.js")],
  accounts: ["Comptes et permissions", "accounts.manage", () => import("./pages/accounts.js")],
  settings: ["Paramètres généraux", "settings.manage", () => import("./pages/settings.js")],
  account: ["Mon compte", null, () => import("./pages/account.js")],
};

const root = document.getElementById("app");
let ws = null, wsTimer = null, wsRetry = 0, shell = null, cleanup = null, leaveGuard = null, currentHash = "", ignoreHash = false, loggedOut = false, navSeq = 0;

// ---------------------------------------------------------------- thème
export function applyTheme(t) {
  const theme = t || (() => { try { return localStorage.getItem("nexcraft-theme"); } catch { return null; } })() || "dark";
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("nexcraft-theme", theme); } catch { /* stockage indisponible */ }
  bus.emit("theme", theme);
  return theme;
}
export const currentTheme = () => document.documentElement.dataset.theme || "dark";

// ---------------------------------------------------------------- démarrage
async function boot() {
  applyTheme();
  setAuthHandlers({ unauthorized: onUnauthorized, passwordChange: () => renderForcePassword(root, () => start()) });
  window.addEventListener("hashchange", onHash);
  window.addEventListener("beforeunload", (e) => { if (leaveGuard && leaveGuard.dirty && leaveGuard.dirty()) { e.preventDefault(); e.returnValue = ""; } });
  await start();
}
async function start() {
  loggedOut = false;
  let st;
  try { st = await api("GET", "/api/auth/status", undefined, { noAuthRedirect: true }); }
  catch (e) { mount(root, h("div", { class: "auth" }, h("div", { class: "auth-card" }, h("h1", null, "Panel injoignable"), h("p", { class: "lead" }, e.message), h("button", { class: "btn", onclick: () => location.reload() }, "Réessayer")))); return; }
  if (st.setup_required) return renderSetup(root, (r) => enter(r));
  if (!st.authenticated) return renderLogin(root, (r) => enter(r), st.maintenance.enabled ? `Maintenance : ${st.maintenance.message}` : "");
  setCsrf(st.csrf_token);
  if (st.user.must_change_password) return renderForcePassword(root, () => start());
  await enter({ user: st.user, csrf_token: st.csrf_token });
}
async function enter(r) {
  setCsrf(r.csrf_token);
  state.user = r.user; state.perms = new Set(r.user.permissions);
  if (r.user.must_change_password) return renderForcePassword(root, () => start());
  if (!location.hash || location.hash === "#") history.replaceState(null, "", "#/dashboard");
  buildShell();
  await Promise.allSettled([refreshStatus(), refreshUnread()]);
  connectWs();
  currentHash = "";
  onHash();
}
function onUnauthorized() {
  if (loggedOut) return; loggedOut = true;
  closeWs(); state.user = null;
  renderLogin(root, (r) => { loggedOut = false; enter(r); }, "Votre session a expiré ou a été révoquée : reconnectez-vous.");
}
export async function logout() {
  try { await post("/api/auth/logout"); } catch { /* déjà déconnecté */ }
  loggedOut = true; closeWs(); state.user = null; location.hash = "";
  renderLogin(root, (r) => { loggedOut = false; enter(r); });
}

// ---------------------------------------------------------------- données partagées
export async function refreshStatus() { try { state.status = await get("/api/server/status"); updateChrome(); bus.emit("status", state.status); } catch { /* affiché plus tard */ } }
export async function refreshUnread() { try { const d = await get("/api/notifications?unread_only=true&limit=1"); state.unread = d.unread; updateChrome(); } catch { /* ignore */ } }

// ---------------------------------------------------------------- WebSocket d'événements
function connectWs() {
  closeWs();
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/events`;
  ws = new WebSocket(url);
  ws.onopen = () => { wsRetry = 0; setConn(true); };
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "hello") { state.status = m.status; state.stats = m.stats; updateChrome(); bus.emit("status", state.status); if (m.stats) bus.emit("stats", m.stats); }
    else if (m.type === "status") { state.status = m.status; updateChrome(); bus.emit("status", m.status); }
    else if (m.type === "stats") { state.stats = m.stats; bus.emit("stats", m.stats); }
    else if (m.type === "notification") {
      state.unread += 1; updateChrome(); bus.emit("notification", m.notification);
      toast(m.notification.message, LEVEL_KIND[m.notification.level] || "info", m.notification.title);
    } else if (m.type === "backup") bus.emit("backup", m);
  };
  ws.onclose = (ev) => {
    setConn(false);
    if (loggedOut || !state.user) return;
    if (ev.code === 4401 || ev.code === 1008) { api("GET", "/api/auth/me", undefined, { noAuthRedirect: true }).then(() => scheduleWs(), () => onUnauthorized()); return; }
    scheduleWs();
  };
}
function scheduleWs() { clearTimeout(wsTimer); wsRetry = Math.min(wsRetry + 1, 6); wsTimer = setTimeout(() => { if (state.user && !loggedOut) connectWs(); }, Math.min(15000, 800 * 2 ** wsRetry)); }
function closeWs() { clearTimeout(wsTimer); if (ws) { ws.onclose = null; try { ws.close(); } catch { /* ignore */ } ws = null; } }
function setConn(live) { const e = document.getElementById("conn-dot"); if (e) { e.className = "conn" + (live ? " live" : ""); e.title = live ? "Temps réel actif" : "Reconnexion en cours…"; } }

// ---------------------------------------------------------------- mise en page
function buildShell() {
  const nav = h("nav", { class: "nav", "aria-label": "Navigation principale" });
  for (const [group, items] of NAV) {
    const visible = items.filter(([, , , perm]) => can(perm));
    if (!visible.length) continue;
    nav.append(h("div", { class: "nav-group" }, group));
    for (const [id, label, ic] of visible) nav.append(h("a", { href: `#/${id}`, dataset: { nav: id }, onclick: () => closeDrawer() }, icon(ic), label, id === "notifications" ? h("span", { class: "badge-n", id: "nav-notif", hidden: true }, "0") : null));
  }
  const sidebar = h("aside", { class: "sidebar", id: "sidebar" },
    h("div", { class: "brand" }, icon("cube", "icon logo-mark"), h("div", null, h("b", null, "NexCraft Panel"), h("small", null, "Serveur Minecraft"))), nav,
    h("div", { class: "side-foot" }, h("div", { class: "side-status", id: "side-status" })));
  const title = h("h1", { id: "page-title" });
  const user = state.user;
  const menuBtn = h("button", { class: "btn ghost", "aria-haspopup": "menu", id: "user-btn", onclick: (e) => { e.stopPropagation(); toggleUserMenu(); } },
    h("span", { class: "avatar" }, user.username.slice(0, 2).toUpperCase()), h("span", { class: "hide-sm" }, user.username), icon("chev"));
  const topbar = h("header", { class: "topbar" },
    h("button", { class: "btn ghost icon-only menu-btn", "aria-label": "Menu", onclick: () => toggleDrawer() }, icon("menu")), title,
    h("div", { class: "right row", style: { gap: "8px", flexWrap: "nowrap" } },
      h("span", { id: "conn-dot", class: "conn hide-sm" }, h("i")), h("span", { id: "top-status" }), h("div", { id: "top-actions", class: "row hide-sm", style: { gap: "6px", flexWrap: "nowrap" } }),
      h("button", { class: "btn ghost icon-only bell", "aria-label": "Notifications", onclick: () => (location.hash = "#/notifications") }, icon("bell"), h("span", { class: "cnt", id: "bell-cnt", hidden: true }, "0")),
      h("div", { class: "rel", id: "user-menu-host" }, menuBtn)));
  const banner = h("div", { id: "banner", hidden: true });
  const content = h("main", { class: "content", id: "content", tabindex: "-1" });
  shell = { content, title };
  mount(root, h("div", { class: "app" }, sidebar, h("div", { class: "backdrop-nav", id: "backdrop-nav", onclick: closeDrawer }), h("div", { class: "main" }, topbar, banner, content)));
  document.addEventListener("click", () => document.getElementById("user-menu")?.remove());
  updateChrome();
}
function toggleDrawer() { document.getElementById("sidebar").classList.toggle("open"); document.getElementById("backdrop-nav").classList.toggle("open"); }
function closeDrawer() { document.getElementById("sidebar")?.classList.remove("open"); document.getElementById("backdrop-nav")?.classList.remove("open"); }
function toggleUserMenu() {
  const ex = document.getElementById("user-menu"); if (ex) { ex.remove(); return; }
  const host = document.getElementById("user-menu-host");
  const dark = currentTheme() === "dark";
  host.append(h("div", { class: "menu", id: "user-menu", role: "menu" },
    h("div", { style: { padding: "8px 10px" } }, h("b", null, state.user.username), h("div", { class: "small muted" }, { admin: "Administrateur", manager: "Gestionnaire", viewer: "Lecteur" }[state.user.role] || state.user.role)), h("hr"),
    h("a", { href: "#/account" }, icon("key"), "Mon compte et mot de passe"),
    h("button", { onclick: () => { applyTheme(dark ? "light" : "dark"); } }, icon(dark ? "sun" : "moon"), dark ? "Thème clair" : "Thème sombre"), h("hr"),
    h("button", { onclick: logout }, icon("logout"), "Se déconnecter")));
}

export function updateChrome() {
  if (!shell) return;
  const s = state.status;
  const top = document.getElementById("top-status"), side = document.getElementById("side-status"), actions = document.getElementById("top-actions");
  if (top) mount(top, s ? statusPill(s.status) : h("span", { class: "skeleton", style: { width: "90px", display: "inline-block" } }));
  if (side) mount(side, s ? [h("span", { class: `dot ${s.status}` }), h("div", { class: "s-main" }, h("div", { class: "s-title" }, STATUS_LABEL[s.status]),
    h("div", { class: "s-sub" }, s.status === "running" ? `${s.player_count}${s.max_players ? "/" + s.max_players : ""} joueur(s) · ${fmtDuration(s.uptime_s)}` : s.mc_version ? `Minecraft ${s.mc_version}` : "Aucune version détectée"))] : null);
  if (actions && s && can("server.control")) {
    mount(actions, s.status === "stopped"
      ? h("button", { class: "btn sm primary", onclick: () => serverAction("start") }, icon("play"), "Démarrer")
      : [h("button", { class: "btn sm", disabled: s.status === "stopping", onclick: () => serverAction("restart") }, icon("refresh"), "Redémarrer"),
         h("button", { class: "btn sm danger", disabled: s.status === "stopping", onclick: () => serverAction("stop") }, icon("stop"), "Arrêter")]);
  }
  const cnt = document.getElementById("bell-cnt"), navn = document.getElementById("nav-notif");
  for (const e of [cnt, navn]) if (e) { e.hidden = !state.unread; e.textContent = state.unread > 99 ? "99+" : state.unread; }
}
export async function refreshBanner() {
  try {
    const m = await get("/api/maintenance"); state.maintenance = m;
    const b = document.getElementById("banner"); if (!b) return;
    b.hidden = !m.enabled; b.className = m.enabled ? "banner" : "";
    clear(b); if (m.enabled) append(b, icon("wrench"), h("span", null, h("b", null, "Mode maintenance : "), m.message), m.block_file_ops ? badge("écritures de fichiers bloquées", "warn") : null);
  } catch { /* ignore */ }
}
function append(el, ...c) { c.flat().forEach((x) => x && el.append(x)); }

// ---------------------------------------------------------------- routeur
export function navigate(hash) { location.hash = hash; }
export function setLeaveGuard(g) { leaveGuard = g; }
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [name, query = ""] = raw.split("?");
  return { name: name || "dashboard", params: new URLSearchParams(query) };
}
async function onHash() {
  if (ignoreHash) { ignoreHash = false; return; }
  if (!state.user) return;
  if (leaveGuard) {
    let ok = true;
    if (leaveGuard.dirty && leaveGuard.dirty()) ok = await confirmDialog({ title: "Modifications non enregistrées", message: "Vous avez des modifications non enregistrées. Quitter cette page les fera perdre.", confirmLabel: "Quitter sans enregistrer", danger: true });
    if (!ok) { ignoreHash = true; location.hash = currentHash; return; }
    leaveGuard = null;
  }
  currentHash = location.hash;
  const my = ++navSeq; // jeton : une navigation plus récente rend les précédentes obsolètes
  const { name, params } = parseHash();
  const page = PAGES[name];
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === (name === "editor" ? "files" : name)));
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  if (!page) { mount(shell.content, h("div", { class: "empty" }, "Page introuvable.")); shell.title.textContent = "Introuvable"; return; }
  const [title, perm, loader] = page;
  shell.title.textContent = title; document.title = `${title} — NexCraft Panel`;
  if (perm && !can(perm)) { mount(shell.content, h("div", { class: "empty" }, icon("lock"), h("div", null, "Accès refusé"), h("div", { class: "small faint" }, "Votre compte n'a pas la permission nécessaire pour cette page."))); return; }
  mount(shell.content, loading());
  refreshBanner();
  try {
    const mod = await loader();
    if (my !== navSeq) return;
    const host = h("div", null);
    mount(shell.content, host);
    const ctx = { state, can, bus, navigate, params, setTitle: (t) => { shell.title.textContent = t; }, setLeaveGuard, serverAction, showPreflight, refreshStatus, refreshUnread, updateChrome };
    const out = await mod.render(host, ctx);
    if (my !== navSeq) { if (typeof out === "function") { try { out(); } catch (e) { console.error(e); } } return; }
    cleanup = typeof out === "function" ? out : null;
    window.scrollTo(0, 0);
  } catch (e) {
    if (my !== navSeq) return;
    console.error(e);
    mount(shell.content, h("div", { class: "alert bad" }, icon("alert"), `Impossible d'afficher cette page : ${e.message}`));
  }
}

// ---------------------------------------------------------------- actions serveur partagées
export function showPreflight(report) {
  const ic = { ok: ["check", "ok"], warn: ["alert", "warn"], error: ["x", "bad"] };
  const body = h("div", { class: "stack" },
    h("div", { class: `alert ${report.can_start ? "ok" : "bad"}` }, icon(report.can_start ? "check" : "alert"), report.can_start ? (report.warnings ? `Le serveur peut démarrer (${report.warnings} avertissement(s)).` : "Tout est prêt : le serveur peut démarrer.") : `${report.errors} problème(s) bloquant(s) empêchent le démarrage.`),
    h("div", { class: "list" }, report.checks.map((c) => h("div", { class: "row", style: { alignItems: "flex-start", flexWrap: "nowrap" } },
      h("span", { class: `badge ${ic[c.status][1]}`, style: { marginTop: "2px" } }, icon(ic[c.status][0], "icon"), ""),
      h("div", { class: "grow" }, h("b", null, c.label), h("div", { class: "small muted" }, c.message), c.fix && c.status !== "ok" ? h("div", { class: "small", style: { color: "var(--blue-2)" } }, "→ " + c.fix) : null,
        c.action === "accept-eula" && can("server.configure") ? h("button", { class: "btn sm", style: { marginTop: "6px" }, onclick: async (e) => { if (await confirmDialog({ title: "Accepter le CLUF Minecraft", message: h("div", null, "En continuant, vous acceptez le contrat de licence utilisateur final de Mojang : ", h("a", { href: "https://aka.ms/MinecraftEULA", target: "_blank", rel: "noopener noreferrer" }, "https://aka.ms/MinecraftEULA")), confirmLabel: "J'accepte le CLUF" })) { try { await post("/api/server/accept-eula", { confirm: true }); toast("CLUF accepté (eula.txt mis à jour).", "ok"); m.close(); } catch (er) { toast(er.message, "error"); } } } }, "Lire et accepter le CLUF") : null,
        c.action === "kill-orphan" && can("server.control") ? h("button", { class: "btn sm danger", style: { marginTop: "6px" }, onclick: async () => { try { await post("/api/server/kill-orphan"); toast("Processus orphelin arrêté.", "ok"); m.close(); } catch (er) { toast(er.message, "error"); } } }, "Arrêter le processus orphelin") : null,
        c.action === "install" ? h("a", { class: "btn sm", style: { marginTop: "6px" }, href: "#/server", onclick: () => m.close() }, "Ouvrir l'assistant d'installation") : null)))));
  const m = modal({ title: "Rapport avant démarrage", body, size: "wide", buttons: [{ label: "Fermer", value: false }] });
  return m;
}
export async function serverAction(kind) {
  const s = state.status || {};
  try {
    if (kind === "start") {
      const r = await post("/api/server/start"); state.status = r; updateChrome(); toast("Démarrage lancé…", "info"); return;
    }
    const n = s.player_count || 0;
    const warn = n ? ` ${n} joueur(s) connecté(s) seront déconnectés.` : "";
    if (kind === "stop") {
      if (!(await confirmDialog({ title: "Arrêter le serveur", message: "Arrêt propre : le monde est sauvegardé puis le serveur s'arrête." + warn, confirmLabel: "Arrêter", danger: true }))) return;
      toast("Arrêt en cours…", "info"); await post("/api/server/stop"); toast("Serveur arrêté.", "ok");
    } else if (kind === "restart") {
      if (!(await confirmDialog({ title: "Redémarrer le serveur", message: "Arrêt propre puis nouveau démarrage." + warn, confirmLabel: "Redémarrer", danger: !!n }))) return;
      toast("Redémarrage en cours…", "info"); await post("/api/server/restart"); toast("Serveur redémarré.", "ok");
    } else if (kind === "kill") {
      if (!(await confirmDialog({ title: "Forcer l'arrêt", message: h("div", { class: "stack" }, h("div", { class: "alert bad" }, icon("alert"), "Le processus sera tué immédiatement, sans sauvegarde. Les dernières modifications du monde peuvent être perdues et le monde peut être corrompu."), "Utilisez cette option seulement si l'arrêt propre ne fonctionne pas."), confirmLabel: "Forcer l'arrêt", danger: true, requireCheck: "Je comprends les risques de perte de données" }))) return;
      await post("/api/server/kill", { confirm: true }); toast("Processus arrêté de force.", "warn");
    }
  } catch (e) {
    if (e.data && e.data.preflight) { showPreflight(e.data.preflight); toast(e.message, "error"); } else toast(e.message, "error");
  } finally { refreshStatus(); }
}

boot();
