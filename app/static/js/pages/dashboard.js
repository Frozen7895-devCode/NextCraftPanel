// Tableau de bord : état, statistiques réelles, graphiques historiques, santé, alertes, événements.
import { h, icon, mount, toast, statusPill, fmtDuration, fmtDate, fmtRel, fmtBytes, copyText, badge, STATUS_LABEL, LEVEL_KIND, emptyState, loading } from "../util.js";
import { get, post, put } from "../api.js";
import { lineChart } from "../charts.js";

const fmtMem = (mb) => (mb === null || mb === undefined ? null : mb >= 1024 ? `${(mb / 1024).toFixed(2)} Go` : `${Math.round(mb)} Mo`);
const na = () => h("span", { class: "na" }, "indisponible");

export async function render(el, app) {
  const { state, can, bus } = app;
  let data = await get("/api/dashboard");
  let minutes = 60;
  let series = [];
  const refs = {};
  const timers = [];
  const offs = [];

  async function loadHistory() {
    try { const rows = await get(`/api/dashboard/history?minutes=${minutes}`); series = rows.map((r) => ({ t: new Date(r.ts).getTime(), cpu: r.running ? r.cpu : null, ram: r.running ? r.ram_mb : null, players: r.running ? r.players : null })); } catch { series = []; }
    drawCharts();
  }
  function drawCharts() {
    const since = Date.now() - minutes * 60000;
    const pts = series.filter((p) => p.t >= since);
    lineChart(refs.cCpu, { points: pts.map((p) => ({ t: p.t, v: p.cpu })), color: "#3b82ff", unit: " %", label: "CPU du processus" });
    lineChart(refs.cRam, { points: pts.map((p) => ({ t: p.t, v: p.ram })), color: "#8b5cf6", unit: " Mo", label: "RAM du processus" });
    lineChart(refs.cPl, { points: pts.map((p) => ({ t: p.t, v: p.players })), color: "#34d399", integer: true, label: "Joueurs connectés" });
  }

  // ---------- Bandeau principal ----------
  function heroMeta(s) {
    const addr = s.port ? `${s.lan_ip || "localhost"}:${s.port}` : null;
    const item = (k, v) => h("div", null, h("div", { class: "k" }, k), h("div", { class: "v" }, v));
    return [
      item("Version Minecraft", s.mc_version || na()),
      item("Type de serveur", [s.server_type || "—", s.brand ? h("div", { class: "small faint" }, s.brand) : null]),
      item("Adresse et port", addr ? h("span", { class: "row", style: { gap: "6px", flexWrap: "nowrap" } }, h("span", { class: "mono" }, addr), h("button", { class: "btn ghost sm icon-only", "aria-label": "Copier l'adresse", onclick: () => copyText(addr) }, icon("copy"))) : na()),
      item("Joueurs", s.status === "running" || s.status === "starting" ? `${s.player_count}${s.max_players ? " / " + s.max_players : ""}` : na()),
      item("Temps de fonctionnement", s.uptime_s !== null && s.uptime_s !== undefined ? fmtDuration(s.uptime_s) : na()),
      item("Dernier démarrage", s.last_start ? h("span", { title: fmtDate(s.last_start) }, fmtRel(s.last_start)) : na()),
    ];
  }
  function controls(s) {
    if (!can("server.control")) return h("span", { class: "small muted" }, "Lecture seule : contrôle du serveur non autorisé.");
    const b = [];
    if (s.status === "stopped") b.push(h("button", { class: "btn primary", onclick: () => app.serverAction("start") }, icon("play"), "Démarrer"));
    else {
      b.push(h("button", { class: "btn", disabled: s.status === "stopping", onclick: () => app.serverAction("restart") }, icon("refresh"), "Redémarrer"));
      b.push(h("button", { class: "btn danger", disabled: s.status === "stopping", onclick: () => app.serverAction("stop") }, icon("stop"), "Arrêter"));
      b.push(h("button", { class: "btn ghost", onclick: () => app.serverAction("kill") }, icon("zap"), "Forcer l'arrêt"));
    }
    b.push(h("button", { class: "btn ghost", onclick: async () => { try { app.showPreflight(await get("/api/server/preflight")); } catch (e) { toast(e.message, "error"); } } }, icon("check"), "Vérifier avant démarrage"));
    return b;
  }
  function renderHero() {
    const s = state.status || data.server;
    const le = s.last_exit;
    mount(refs.hero,
      h("div", { class: "hero-top" }, h("span", { class: `dot ${s.status}`, style: { width: "14px", height: "14px" } }), h("div", { class: "state" }, STATUS_LABEL[s.status]),
        s.status === "stopped" && le ? h("span", { class: `badge ${le.expected ? "" : "bad"}` }, le.expected ? `Dernier arrêt : code ${le.code}` : `Crash (code ${le.code})`) : null,
        s.next_auto_restart ? badge(`Redémarrage auto ${fmtRel(s.next_auto_restart)}`, "warn") : null,
        h("div", { class: "right row" }, controls(s))),
      h("div", { class: "hero-meta" }, heroMeta(s)),
      s.orphan_pid ? h("div", { class: "alert warn", style: { marginTop: "14px" } }, icon("alert"), `Un processus serveur d'une session précédente (PID ${s.orphan_pid}) tourne encore. Ouvrez « Vérifier avant démarrage » pour l'arrêter.`) : null);
  }

  // ---------- Statistiques ----------
  function stat(label, ic) { const v = h("div", { class: "val" }), sub = h("div", { class: "sub" }); return { el: h("div", { class: "card stat" }, h("div", { class: "lbl" }, icon(ic), label), v, sub), v, sub }; }
  const sCpu = stat("CPU du processus Minecraft", "activity"), sRam = stat("RAM du processus (mémoire physique)", "box"), sPl = stat("Joueurs connectés", "users"), sSys = stat("Machine hôte", "cube");
  function renderStats(st) {
    const run = st && st.running;
    mount(sCpu.v, run && st.cpu !== null ? [String(st.cpu), h("small", null, "% de la machine")] : na());
    mount(sCpu.sub, run ? "Moyenne de tous les cœurs" : "Le serveur est arrêté");
    mount(sRam.v, run && st.ram_mb !== null ? fmtMem(st.ram_mb) : na());
    mount(sRam.sub, run ? `Xmx configuré : ${st.xmx || "—"} (mesure physique, pas le tas Java)` : "Le serveur est arrêté");
    const sv = state.status || data.server;
    mount(sPl.v, sv.status === "running" || sv.status === "starting" ? [String(sv.player_count), sv.max_players ? h("small", null, `/ ${sv.max_players}`) : null] : na());
    mount(sPl.sub, sv.status === "running" ? (sv.players.length ? sv.players.slice(0, 4).join(", ") + (sv.players.length > 4 ? "…" : "") : "Personne en ligne") : "Serveur hors ligne");
    const sys = st && st.system;
    mount(sSys.v, sys ? [`${sys.ram_percent}`, h("small", null, "% RAM utilisée")] : na());
    mount(sSys.sub, sys ? `Disque : ${sys.disk_free_gb ?? "?"} Go libres (${sys.disk_percent ?? "?"} %) · CPU ${sys.cpu_percent} %` : "");
  }

  // ---------- Santé ----------
  function renderHealth(hl) {
    const color = { good: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)" }[hl.level];
    mount(refs.health,
      h("div", { class: "row", style: { alignItems: "flex-start", flexWrap: "nowrap" } },
        h("div", { class: "ring", style: { "--p": hl.score, "--c": color }, "data-v": hl.score, role: "img", "aria-label": `Santé ${hl.score} sur 100` }),
        h("div", { class: "grow" }, h("h3", null, `Santé du serveur : ${hl.label}`), h("div", { class: "small muted" }, "Calculée à partir de données réelles (processus, stabilité, journaux, sauvegardes)."))),
      h("div", { class: "stack", style: { marginTop: "14px", gap: "12px" } }, hl.components.map((c) => {
        const pct = (c.points / c.max) * 100, kind = pct >= 85 ? "ok" : pct >= 50 ? "warn" : "bad";
        return h("div", null, h("div", { class: "row small", style: { justifyContent: "space-between", flexWrap: "nowrap" } }, h("b", null, c.name), h("span", { class: "mono muted" }, `${c.points}/${c.max}`)),
          h("div", { class: `bar ${kind}`, style: { margin: "4px 0" } }, h("i", { style: { width: pct + "%" } })), h("div", { class: "small muted" }, c.reasons.join(" ")));
      })));
  }

  // ---------- Colonne droite ----------
  function renderSide() {
    const s = state.status || data.server;
    mount(refs.players, s.status === "running" && s.players.length
      ? h("div", { class: "list" }, s.players.map((n) => h("div", { class: "row", style: { gap: "10px" } }, h("span", { class: "mc-head" }, n[0].toUpperCase()), h("b", null, n))))
      : emptyState("users", s.status === "running" ? "Aucun joueur connecté" : "Liste indisponible", s.status === "running" ? null : "Le serveur est arrêté."));
    const b = data.backup;
    const last = b.last_ok, att = b.last_attempt;
    mount(refs.backup,
      last ? h("div", null, h("div", { class: "row" }, badge("Réussie", "ok"), last.consistent ? badge("cohérente", "violet") : badge("non garantie cohérente", "warn")), h("div", { class: "small muted", style: { marginTop: "6px" } }, `${fmtDate(last.created_at)} (${fmtRel(last.created_at)}) · ${fmtBytes(last.size)}`)) : h("div", { class: "muted" }, "Aucune sauvegarde réussie pour le moment."),
      att && att.status === "failed" ? h("div", { class: "alert bad", style: { marginTop: "10px" } }, icon("alert"), `La dernière tentative a échoué : ${att.error || "erreur inconnue"}`) : null,
      can("backups.manage") ? h("button", { class: "btn sm", style: { marginTop: "12px" }, onclick: async (e) => { try { await post("/api/backups", {}); toast("Sauvegarde lancée en arrière-plan.", "info"); } catch (er) { toast(er.message, "error"); } } }, icon("archive"), "Sauvegarder maintenant") : null);
    mount(refs.alerts, data.alerts.length ? h("div", { class: "list" }, data.alerts.map((a) => h("div", null, h("div", { class: "row", style: { gap: "8px" } }, badge(a.level === "error" ? "Erreur" : "Alerte", LEVEL_KIND[a.level]), h("b", null, a.title)), h("div", { class: "small muted" }, a.message), h("div", { class: "small faint" }, fmtRel(a.created_at)))))
      : h("div", { class: "muted small" }, "Aucune alerte non lue."));
    if (data.last_crash && refs.crash) mount(refs.crash, h("div", { class: "alert bad" }, icon("alert"), h("div", null, h("b", null, `Dernier crash : ${fmtRel(data.last_crash.occurred_at)} (code ${data.last_crash.exit_code})`), h("div", { class: "small" }, data.last_crash.summary), can("diagnostics.view") ? h("a", { href: "#/diagnostics" }, "Ouvrir le diagnostic") : null)));
    if (refs.events) mount(refs.events, data.events.length ? h("div", { class: "list" }, data.events.map((e) => h("div", { class: "small" }, h("div", { class: "row", style: { gap: "6px", flexWrap: "nowrap" } }, h("b", { class: "nowrap" }, e.username), h("span", { class: "muted" }, e.action), e.level === "warning" || e.level === "error" ? badge("!", "warn") : null), h("div", { class: "faint", style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, title: e.target }, `${e.target ? e.target + " · " : ""}${fmtRel(e.created_at)}`)))) : h("div", { class: "muted small" }, "Aucun événement."));
  }

  // ---------- Squelette ----------
  refs.hero = h("div", { class: "hero" }); refs.health = h("div", { class: "card-b" });
  refs.cCpu = h("div"); refs.cRam = h("div"); refs.cPl = h("div");
  refs.players = h("div", { class: "card-b" }); refs.backup = h("div", { class: "card-b" }); refs.alerts = h("div", { class: "card-b" }); refs.crash = h("div");
  refs.events = can("audit.view") ? h("div", { class: "card-b" }) : null;
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Période" }, [[15, "15 min"], [60, "1 h"], [360, "6 h"], [1440, "24 h"]].map(([m, l]) => h("button", { class: m === minutes ? "on" : "", onclick: (e) => { minutes = m; seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === e.target)); loadHistory(); } }, l)));
  const chartCard = (title, node) => h("div", { class: "card" }, h("div", { class: "card-h" }, h("h3", null, title)), h("div", { class: "card-b" }, node));
  const maint = data.maintenance.enabled ? h("div", { class: "alert warn" }, icon("wrench"), h("div", null, h("b", null, "Mode maintenance actif. "), data.maintenance.message)) : null;
  mount(el, h("div", { class: "stack", style: { gap: "16px" } },
    maint, refs.hero, refs.crash,
    h("div", { class: "grid g4" }, sCpu.el, sRam.el, sPl.el, sSys.el),
    h("div", { class: "grid g-main" },
      h("div", { class: "stack", style: { gap: "16px" } },
        h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Historique des ressources"), seg),
          h("div", { class: "card-b stack" }, h("div", null, h("div", { class: "small muted" }, "CPU du processus (% de la machine)"), refs.cCpu), h("div", null, h("div", { class: "small muted" }, "RAM du processus (Mo)"), refs.cRam), h("div", null, h("div", { class: "small muted" }, "Joueurs connectés"), refs.cPl))),
        h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Indicateur de santé")), refs.health)),
      h("div", { class: "stack", style: { gap: "16px" } },
        h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Joueurs en ligne")), refs.players),
        h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Dernière sauvegarde")), refs.backup),
        h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Alertes importantes"), h("a", { href: "#/notifications", class: "small" }, "Tout voir")), refs.alerts),
        refs.events ? h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Derniers événements"), can("audit.view") ? h("a", { href: "#/history", class: "small" }, "Historique") : null), refs.events) : null))));

  renderHero(); renderStats(state.stats || data.stats); renderHealth(data.health); renderSide(); await loadHistory();

  // ---------- Temps réel ----------
  offs.push(bus.on("status", () => { renderHero(); renderStats(state.stats || data.stats); renderSide(); }));
  offs.push(bus.on("stats", (st) => {
    renderStats(st);
    const t = new Date(st.ts).getTime();
    if (!series.length || t - series[series.length - 1].t >= 4000) {
      series.push({ t, cpu: st.running ? st.cpu : null, ram: st.running ? st.ram_mb : null, players: st.running ? st.players : null });
      if (series.length > 4000) series.shift();
      if (series.length <= 3) drawCharts(); // premiers points : affichage immédiat
    }
  }));
  timers.push(setInterval(drawCharts, 10000));
  async function refreshData() { try { data = await get("/api/dashboard"); renderHealth(data.health); renderSide(); } catch { /* sera réessayé */ } }
  timers.push(setInterval(refreshData, 30000));
  offs.push(bus.on("backup", refreshData)); offs.push(bus.on("notification", refreshData));
  return () => { timers.forEach(clearInterval); offs.forEach((o) => o()); };
}
