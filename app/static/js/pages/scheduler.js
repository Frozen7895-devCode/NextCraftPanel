// Planificateur : tâches, historique d'exécution, mode économie.
import { h, icon, mount, toast, modal, confirmDialog, fmtDate, fmtRel, emptyState, badge, field, switchEl } from "../util.js";
import { get, post, put, patch, del } from "../api.js";

const DAYS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"];
const KIND_ICON = { backup: "archive", restart: "refresh", start: "play", stop: "stop", command: "terminal", notification: "bell", healthcheck: "activity" };
const STATUS = { ok: ["réussie", "ok"], failed: ["échec", "bad"], skipped: ["ignorée", "warn"], missed: ["manquée", "warn"], running: ["en cours", "info"] };
const describe = (s) => (s.type === "interval" ? (s.minutes % 60 === 0 ? `Toutes les ${s.minutes / 60} h` : `Toutes les ${s.minutes} min`) : s.type === "daily" ? `Chaque jour à ${s.time}` : s.type === "weekly" ? `${s.days.map((d) => DAYS[d]).join(", ")} à ${s.time}` : `Une fois le ${fmtDate(s.at)}`);

export async function render(el, app) {
  const { can, bus } = app;
  const manage = can("tasks.manage");
  let data = await get("/api/tasks"), eco = await get("/api/tasks/economy/config");
  const tasksHost = h("div", { class: "card" }), runsHost = h("div", { class: "card" }), ecoHost = h("div", { class: "card" });
  const timers = [];

  async function refresh() { try { data = await get("/api/tasks"); drawTasks(); drawRuns(); } catch { /* ignore */ } }
  async function run(t) { try { await post(`/api/tasks/${t.id}/run`); toast(`« ${t.name} » lancée.`, "info"); setTimeout(refresh, 1200); } catch (e) { toast(e.message, "error"); } }

  function taskDialog(t) {
    const edit = !!t;
    const name = h("input", { class: "input", value: t ? t.name : "", maxlength: 100 });
    const kind = h("select", { class: "select", disabled: edit }, Object.entries(data.kinds).map(([k, l]) => h("option", { value: k, selected: t && t.kind === k }, l)));
    const stype = h("select", { class: "select" }, [["interval", "Intervalle"], ["daily", "Tous les jours"], ["weekly", "Certains jours"], ["once", "Une seule fois"]].map(([v, l]) => h("option", { value: v, selected: t && t.schedule.type === v }, l)));
    const sch = t ? t.schedule : { type: "interval", minutes: 60 };
    const minutes = h("input", { class: "input", type: "number", min: 1, value: sch.minutes || 60 }), time = h("input", { class: "input", type: "time", value: sch.time || "04:00" });
    const at = h("input", { class: "input", type: "datetime-local" });
    const dayChecks = DAYS.map((d, i) => { const c = h("input", { type: "checkbox" }); c.checked = sch.days ? sch.days.includes(i) : true; return c; });
    const p = t ? t.payload : {};
    const command = h("input", { class: "input", value: p.command || "", placeholder: "say Sauvegarde dans 5 minutes" }), warn = h("input", { class: "input", type: "number", min: 0, max: 600, value: p.warn_seconds ?? 60 });
    const ntitle = h("input", { class: "input", value: p.title || "" }), nmsg = h("input", { class: "input", value: p.message || "" }), nlevel = h("select", { class: "select" }, ["info", "success", "warning", "error"].map((l) => h("option", { value: l, selected: p.level === l }, l)));
    const note = h("input", { class: "input", value: p.note || "" });
    const enabled = { v: t ? t.enabled : true };
    const rows = { interval: field("Toutes les (minutes)", minutes), daily: field("Heure (heure locale du serveur)", time), weekly: h("div", { class: "stack" }, field("Heure (heure locale du serveur)", time.cloneNode(true)), h("div", null, h("div", { class: "lbl-t" }, "Jours"), h("div", { class: "row" }, DAYS.map((d, i) => h("label", { class: "check" }, dayChecks[i], d))))), once: field("Date et heure", at) };
    const weeklyTime = rows.weekly.firstChild.querySelector("input");
    const schedBox = h("div"), payBox = h("div");
    const drawSched = () => mount(schedBox, rows[stype.value]);
    const drawPay = () => {
      const k = kind.value;
      mount(payBox, k === "command" ? field("Commande Minecraft", command, "Envoyée à la console du serveur en cours d'exécution.") : k === "restart" || k === "stop" ? field("Avertir les joueurs (secondes avant l'action, 0 = sans avertissement)", warn) : k === "notification" ? h("div", { class: "form-grid" }, field("Titre", ntitle), field("Niveau", nlevel), h("div", { class: "full" }, field("Message", nmsg))) : k === "backup" ? field("Note de la sauvegarde", note) : k === "healthcheck" ? h("div", { class: "small muted" }, "Vérifie que le processus tourne et que le port répond ; crée une notification en cas de problème.") : null);
    };
    stype.onchange = drawSched; kind.onchange = drawPay; drawSched(); drawPay();
    modal({ title: edit ? `Modifier « ${t.name} »` : "Nouvelle tâche planifiée", size: "wide", body: h("div", { class: "stack" }, h("div", { class: "form-grid" }, field("Nom", name), field("Action", kind)), h("div", { class: "form-grid" }, field("Fréquence", stype), schedBox), payBox, switchEl(enabled.v, (v) => { enabled.v = v; }, "Tâche active")),
      buttons: [{ label: "Annuler", value: false }, { label: edit ? "Enregistrer" : "Créer", kind: "primary", onClick: async () => {
        const type = stype.value; const schedule = type === "interval" ? { type, minutes: +minutes.value } : type === "daily" ? { type, time: time.value } : type === "weekly" ? { type, time: weeklyTime.value, days: dayChecks.map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0) } : { type, at: at.value ? new Date(at.value).toISOString().replace(/\.\d+Z$/, "") : "" };
        const k = kind.value; const payload = k === "command" ? { command: command.value } : k === "restart" || k === "stop" ? { warn_seconds: +warn.value } : k === "notification" ? { title: ntitle.value, message: nmsg.value, level: nlevel.value } : k === "backup" ? { note: note.value } : {};
        try { if (edit) await patch(`/api/tasks/${t.id}`, { name: name.value, schedule, payload, enabled: enabled.v }); else await post("/api/tasks", { name: name.value, kind: k, schedule, payload, enabled: enabled.v }); toast("Tâche enregistrée.", "ok"); refresh(); return true; } catch (e) { toast(e.message, "error"); return false; } } }] });
  }

  function drawTasks() {
    const rows = data.tasks;
    mount(tasksHost, h("div", { class: "card-h" }, h("h2", null, "Tâches planifiées"), manage ? h("button", { class: "btn sm primary", onclick: () => taskDialog(null) }, icon("plus"), "Nouvelle tâche") : null),
      rows.length ? h("div", { class: "table-wrap", style: { marginTop: "8px" } }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Tâche"), h("th", null, "Planification"), h("th", null, "Prochaine exécution"), h("th", null, "Dernier résultat"), h("th", null, "Active"), h("th", null))),
        h("tbody", null, rows.map((t) => h("tr", null, h("td", null, h("div", { class: "row", style: { gap: "8px", flexWrap: "nowrap" } }, icon(KIND_ICON[t.kind] || "clock"), h("div", null, h("b", null, t.name), h("div", { class: "small faint" }, t.kind_label, t.system_tag ? ` · géré par « ${t.system_tag} »` : "")))),
          h("td", { class: "small" }, describe(t.schedule)), h("td", { class: "small", title: fmtDate(t.next_run_at) }, t.enabled && t.next_run_at ? fmtRel(t.next_run_at) : "—"),
          h("td", null, t.last_status ? badge(...STATUS[t.last_status] || [t.last_status]) : h("span", { class: "faint small" }, "jamais exécutée"), t.last_run_at ? h("div", { class: "small faint" }, fmtRel(t.last_run_at)) : null),
          h("td", null, manage && !t.system_tag ? switchEl(t.enabled, async (v) => { try { await patch(`/api/tasks/${t.id}`, { enabled: v }); refresh(); } catch (e) { toast(e.message, "error"); refresh(); } }) : t.enabled ? badge("oui", "ok") : badge("non")),
          h("td", null, manage ? h("div", { class: "actions" }, h("button", { class: "btn sm", onclick: () => run(t) }, icon("play"), "Lancer"), !t.system_tag ? h("button", { class: "btn sm icon-only", "aria-label": "Modifier", onclick: () => taskDialog(t) }, icon("edit")) : null,
            !t.system_tag ? h("button", { class: "btn sm danger icon-only", "aria-label": "Supprimer", onclick: async () => { if (await confirmDialog({ title: "Supprimer la tâche", message: `« ${t.name} » sera supprimée.`, danger: true, confirmLabel: "Supprimer" })) { await del(`/api/tasks/${t.id}`).catch((e) => toast(e.message, "error")); refresh(); } } }, icon("trash")) : null) : null)))))) : emptyState("clock", "Aucune tâche planifiée"));
  }
  function drawRuns() {
    mount(runsHost, h("div", { class: "card-h" }, h("h2", null, "Historique d'exécution"), h("button", { class: "btn sm", onclick: refresh }, icon("refresh"), "Actualiser")),
      data.runs.length ? h("div", { class: "table-wrap", style: { marginTop: "8px" } }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Tâche"), h("th", null, "Début"), h("th", null, "Résultat"), h("th", null, "Détail"))),
        h("tbody", null, data.runs.map((r) => h("tr", null, h("td", null, r.task_name), h("td", { class: "small nowrap", title: fmtDate(r.started_at) }, fmtDate(r.started_at)), h("td", null, badge(...(STATUS[r.status] || [r.status]))), h("td", { class: "small muted" }, r.message)))))) : emptyState("history", "Aucune exécution pour le moment"));
  }
  function drawEco() {
    const en = { v: eco.enabled };
    const start = h("input", { class: "input", type: "time", value: eco.start_time }), stop = h("input", { class: "input", type: "time", value: eco.stop_time }), warn = h("input", { class: "input", type: "number", min: 0, max: 600, value: eco.warn_seconds });
    const days = DAYS.map((d, i) => { const c = h("input", { type: "checkbox" }); c.checked = eco.days.includes(i); return c; });
    mount(ecoHost, h("div", { class: "card-h" }, h("h2", null, "Mode économie"), eco.enabled ? badge("actif", "ok") : badge("désactivé")), h("div", { class: "card-b stack" },
      h("div", { class: "alert" }, icon("info"), eco.note), switchEl(en.v, (v) => { en.v = v; }, "Démarrer et arrêter automatiquement le serveur selon ces horaires"),
      h("div", { class: "form-grid" }, field("Démarrage à", start), field("Arrêt à", stop), field("Avertir les joueurs avant l'arrêt (secondes)", warn, "Un message est envoyé puis le serveur s'arrête proprement (sauvegarde du monde).")),
      h("div", null, h("div", { class: "lbl-t" }, "Jours actifs"), h("div", { class: "row" }, DAYS.map((d, i) => h("label", { class: "check" }, days[i], d)))),
      manage && can("server.control") ? h("button", { class: "btn primary", style: { alignSelf: "flex-start" }, onclick: async () => { try { eco = await put("/api/tasks/economy/config", { enabled: en.v, start_time: start.value, stop_time: stop.value, days: days.map((c, i) => (c.checked ? i : -1)).filter((i) => i >= 0), warn_seconds: +warn.value }); toast("Mode économie enregistré.", "ok"); drawEco(); refresh(); } catch (e) { toast(e.message, "error"); } } }, "Enregistrer") : h("div", { class: "small muted" }, "Permissions « tasks.manage » et « server.control » requises pour modifier.")));
  }
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Planificateur"), h("p", null, "Redémarrages, sauvegardes, commandes et vérifications. Une tâche ne s'exécute jamais deux fois pour la même échéance, même après un redémarrage du panel.")), h("span", { class: "badge info" }, `Heure locale du serveur : ${new Date().toLocaleTimeString("fr-FR")}`)),
    h("div", { class: "stack", style: { gap: "16px" } }, tasksHost, ecoHost, runsHost));
  drawTasks(); drawRuns(); drawEco();
  timers.push(setInterval(refresh, 20000));
  return () => timers.forEach(clearInterval);
}
