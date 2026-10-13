// Console interactive : flux WebSocket du vrai processus, filtres, recherche, historique de commandes.
import { h, icon, mount, toast, confirmDialog, statusPill, fmtTime, contextMenu } from "../util.js";
import { post, downloadUrl } from "../api.js";

const MAX_LINES = 5000, MAX_DOM = 2500;
const LEVELS = ["INFO", "WARN", "ERROR", "CMD", "PANEL", "DEBUG"];
const QUICK = ["list", "save-all", "time set day", "weather clear", "whitelist list", "say Bonjour à tous !"];

export async function render(el, app) {
  const { state, can, bus } = app;
  const lines = []; let lastSeq = 0, clearedSeq = 0, autoscroll = true, q = "", socket = null, retry = 0, retryTimer = null, alive = true, canSend = can("console.send");
  const filters = Object.fromEntries(LEVELS.map((l) => [l, true]));
  const cmdHistory = (() => { try { return JSON.parse(sessionStorage.getItem("nexcraft-cmd-history") || "[]"); } catch { return []; } })();
  let hIdx = cmdHistory.length;

  const box = h("div", { class: "console", role: "log", "aria-live": "off", tabindex: "0" });
  const conn = h("span", { class: "conn" }, h("i"), h("span", null, "Connexion…"));
  const statusHost = h("span");
  const searchInput = h("input", { class: "input", type: "search", placeholder: "Rechercher dans les logs…", style: { width: "220px" }, "aria-label": "Rechercher dans les logs", oninput: (e) => { q = e.target.value.trim().toLowerCase(); rebuild(); } });
  const scrollBtn = h("button", { class: "btn sm", onclick: () => setAuto(!autoscroll) });
  const filterBtns = LEVELS.map((l) => h("button", { class: "on", dataset: { lvl: l }, title: `Afficher/masquer ${l}`, onclick: (e) => { filters[l] = !filters[l]; e.currentTarget.classList.toggle("on", filters[l]); rebuild(); } }, l));
  const input = h("input", { placeholder: canSend ? "Saisissez une commande Minecraft (↑/↓ pour l'historique)…" : "Lecture seule : vous ne pouvez pas envoyer de commandes", disabled: !canSend, autocomplete: "off", spellcheck: "false", "aria-label": "Commande Minecraft",
    onkeydown: (e) => {
      if (e.key === "Enter") { e.preventDefault(); submit(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); if (hIdx > 0) { hIdx--; input.value = cmdHistory[hIdx]; } }
      else if (e.key === "ArrowDown") { e.preventDefault(); if (hIdx < cmdHistory.length - 1) { hIdx++; input.value = cmdHistory[hIdx]; } else { hIdx = cmdHistory.length; input.value = ""; } }
    } });

  function setAuto(v) { autoscroll = v; mount(scrollBtn, icon(v ? "pause" : "play"), v ? "Suspendre le défilement" : "Reprendre le défilement"); if (v) box.scrollTop = box.scrollHeight; }
  function visible(l) { return l.seq > clearedSeq && filters[l.level] !== false && (!q || l.text.toLowerCase().includes(q)); }
  function lineEl(l) {
    const txt = h("span", { class: "tx" });
    if (q) { const low = l.text.toLowerCase(); let i = 0, j; while ((j = low.indexOf(q, i)) !== -1) { txt.append(l.text.slice(i, j), h("mark", null, l.text.slice(j, j + q.length))); i = j + q.length; } txt.append(l.text.slice(i)); }
    else txt.textContent = l.text;
    return h("div", { class: `cl ${l.level}` }, h("span", { class: "ts" }, fmtTime(l.ts)), txt);
  }
  let pending = [], raf = 0;
  function flush() {
    raf = 0; const frag = document.createDocumentFragment();
    for (const l of pending) frag.append(lineEl(l));
    pending = []; box.append(frag);
    while (box.childElementCount > MAX_DOM) box.firstElementChild.remove();
    if (autoscroll) box.scrollTop = box.scrollHeight;
  }
  function rebuild() { pending = []; box.replaceChildren(); const vis = lines.filter(visible).slice(-MAX_DOM); const frag = document.createDocumentFragment(); vis.forEach((l) => frag.append(lineEl(l))); box.append(frag); if (autoscroll) box.scrollTop = box.scrollHeight; }
  function addLine(l) {
    if (l.seq <= lastSeq) return; lastSeq = l.seq; lines.push(l); if (lines.length > MAX_LINES) lines.shift();
    if (visible(l)) { pending.push(l); if (!raf) raf = requestAnimationFrame(flush); }
  }
  box.addEventListener("scroll", () => { const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30; if (!atBottom && autoscroll) setAuto(false); else if (atBottom && !autoscroll) setAuto(true); });

  // ---------- WebSocket ----------
  function setConn(live, text) { conn.className = "conn" + (live ? " live" : ""); conn.lastChild.textContent = text; }
  function connect() {
    if (!alive) return;
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/console?since=${lastSeq}`;
    socket = new WebSocket(url);
    socket.onopen = () => { retry = 0; setConn(true, "En direct"); };
    socket.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "history") { m.lines.forEach(addLine); canSend = m.can_send && can("console.send"); input.disabled = !canSend; rebuild(); setStatus(m.status); }
      else if (m.type === "line") addLine(m.line);
      else if (m.type === "status") setStatus(m.status);
      else if (m.type === "confirm_required") askConfirm(m.command, m.message);
      else if (m.type === "error") toast(m.message, "error");
      else if (m.type === "ping") socket.send(JSON.stringify({ type: "ping" }));
    };
    socket.onclose = (ev) => {
      if (!alive) return; setConn(false, "Reconnexion…");
      if (ev.code === 4401) { setConn(false, "Accès refusé ou session expirée"); return; }
      retry = Math.min(retry + 1, 6); retryTimer = setTimeout(connect, Math.min(10000, 500 * 2 ** retry));
    };
  }
  function setStatus(s) { mount(statusHost, statusPill(s)); }
  async function askConfirm(cmd, message) { if (await confirmDialog({ title: "Commande sensible", message: h("div", null, message, h("div", { class: "mono", style: { marginTop: "8px" } }, "/" + cmd)), confirmLabel: "Exécuter", danger: true })) send(cmd, true); }
  async function send(cmd, confirm = false) {
    if (socket && socket.readyState === 1) { socket.send(JSON.stringify({ type: "command", command: cmd, confirm, id: Date.now() })); return; }
    try { await post("/api/console/command", { command: cmd, confirm }); } catch (e) { if (e.confirmRequired) askConfirm(cmd, e.message); else toast(e.message, "error"); }
  }
  function submit() {
    const cmd = input.value.trim(); if (!cmd || !canSend) return;
    if (cmdHistory[cmdHistory.length - 1] !== cmd) cmdHistory.push(cmd);
    if (cmdHistory.length > 200) cmdHistory.shift();
    try { sessionStorage.setItem("nexcraft-cmd-history", JSON.stringify(cmdHistory)); } catch { /* ignore */ }
    hIdx = cmdHistory.length; input.value = ""; setAuto(true); send(cmd);
  }

  const quick = h("button", { class: "btn sm", disabled: !canSend, onclick: (e) => { const r = e.currentTarget.getBoundingClientRect(); contextMenu(r.left, r.bottom + 4, QUICK.map((c) => ({ label: c, icon: "terminal", onClick: () => { input.value = c; input.focus(); } }))); } }, icon("terminal"), "Commandes rapides");
  const dl = h("button", { class: "btn sm", onclick: (e) => { const r = e.currentTarget.getBoundingClientRect(); contextMenu(r.left, r.bottom + 4, [
    { label: "Tampon de la console (session)", icon: "download", onClick: () => downloadUrl("/api/console/download?source=buffer") },
    ...(can("files.read") ? [{ label: "logs/latest.log du serveur", icon: "download", onClick: () => downloadUrl("/api/console/download?source=latest") }] : []),
    { label: "Journal complet du panel (console)", icon: "download", onClick: () => downloadUrl("/api/console/download?source=console") }]); } }, icon("download"), "Télécharger");
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Console"), h("p", null, "Sortie réelle du processus Minecraft ; les commandes sont envoyées à l'instance en cours d'exécution.")), h("div", { class: "row" }, statusHost, conn)),
    h("div", { class: "console-wrap" },
      h("div", { class: "row", style: { marginBottom: "10px" } }, searchInput, h("div", { class: "seg", role: "group", "aria-label": "Niveaux de log" }, filterBtns), h("div", { class: "right row" }, quick, scrollBtn,
        h("button", { class: "btn sm", title: "Vider l'affichage (les logs sur disque sont conservés)", onclick: () => { clearedSeq = lastSeq; rebuild(); toast("Affichage vidé (logs sur disque conservés).", "info"); } }, icon("trash"), "Vider l'affichage"), dl)),
      box, h("div", { class: "cmdbar" }, h("span", { class: "prompt" }, "›"), input, h("button", { class: "btn sm primary", disabled: !canSend, onclick: submit }, "Envoyer"))));
  setAuto(true); setStatus(state.status ? state.status.status : "stopped");
  const off = bus.on("status", (s) => setStatus(s.status));
  connect();
  if (canSend) input.focus();
  return () => { alive = false; clearTimeout(retryTimer); off(); if (socket) { socket.onclose = null; socket.close(); } cancelAnimationFrame(raf); };
}
