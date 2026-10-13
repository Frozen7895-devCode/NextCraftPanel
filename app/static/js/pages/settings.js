// Paramètres généraux : conservation, seuils d'alerte, mode maintenance, aide IA optionnelle.
import { h, icon, mount, toast, fmtDate, field, switchEl, badge } from "../util.js";
import { get, put } from "../api.js";

export async function render(el, app) {
  const { can } = app;
  const { settings: g, environment: env } = await get("/api/settings/general");
  const m = await get("/api/maintenance");
  const num = (k, o = {}) => h("input", { class: "input", type: "number", value: g[k], ...o });
  const f = { trash_retention_days: num("trash_retention_days", { min: 1, max: 365 }), file_versions_keep: num("file_versions_keep", { min: 1, max: 200 }), disk_min_free_gb: num("disk_min_free_gb", { min: 0, step: 0.5 }), disk_warn_percent: num("disk_warn_percent", { min: 50, max: 100 }), ram_warn_percent: num("ram_warn_percent", { min: 50, max: 100 }),
    notification_retention_days: num("notification_retention_days", { min: 1 }), audit_retention_days: num("audit_retention_days", { min: 7 }), metrics_retention_days: num("metrics_retention_days", { min: 1, max: 90 }) };
  const upd = { v: !!g.check_updates };
  async function saveGeneral() { try { await put("/api/settings/general", { ...Object.fromEntries(Object.entries(f).map(([k, i]) => [k, +i.value])), check_updates: upd.v }); toast("Paramètres enregistrés.", "ok"); } catch (e) { toast(e.message, "error"); } }

  // maintenance
  const mm = { enabled: !!m.enabled, block: !!m.block_file_ops, notify: true };
  const msg = h("input", { class: "input", value: m.message, maxlength: 300 });
  const action = h("select", { class: "select" }, [["none", "Aucune action"], ["stop", "Arrêter le serveur"], ["restart", "Redémarrer le serveur"]].map(([v, l]) => h("option", { value: v }, l)));
  const delay = h("input", { class: "input", type: "number", min: 0, max: 1440, value: 5 });
  async function saveMaint() {
    try { const r = await put("/api/maintenance", { enabled: mm.enabled, message: msg.value, block_file_ops: mm.block, action: mm.enabled ? action.value : "none", delay_minutes: +delay.value, notify_players: mm.notify });
      toast(mm.enabled ? (r.scheduled ? "Mode maintenance activé et action planifiée." : "Mode maintenance activé.") : "Mode maintenance désactivé.", "ok"); app.state.maintenance = r; } catch (e) { toast(e.message, "error"); }
  }
  // IA
  let aiBox = null;
  if (can("accounts.manage")) {
    const ai = await get("/api/settings/ai").catch(() => null);
    if (ai) {
      const key = h("input", { class: "input", type: "password", placeholder: ai.has_key ? "Clé enregistrée (laisser vide pour la conserver)" : "Clé API", autocomplete: "off" }), model = h("input", { class: "input", value: ai.model }), en = { v: ai.enabled };
      aiBox = h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Aide IA optionnelle pour le diagnostic"), ai.enabled ? badge("activée", "ok") : badge("désactivée")), h("div", { class: "card-b stack" },
        h("div", { class: "alert" }, icon("info"), "Désactivée par défaut. Aucun journal, adresse IP ou fichier de configuration n'est jamais envoyé automatiquement : chaque envoi exige une confirmation explicite depuis la page Diagnostic, avec masquage des IP, e-mails et chemins personnels."),
        switchEl(en.v, (v) => { en.v = v; }, "Activer l'aide IA (nécessite votre propre clé API)"), h("div", { class: "form-grid" }, field("Clé API", key, "Stockée dans la base du panel, jamais renvoyée à l'interface."), field("Modèle", model)),
        h("div", { class: "row" }, h("button", { class: "btn primary", onclick: async () => { try { const r = await put("/api/settings/ai", { enabled: en.v, api_key: key.value || null, model: model.value }); toast(r.enabled ? "Aide IA activée." : "Aide IA désactivée.", "ok"); key.value = ""; } catch (e) { toast(e.message, "error"); } } }, "Enregistrer"),
          h("button", { class: "btn danger", onclick: async () => { try { await put("/api/settings/ai", { enabled: false, api_key: "" }); toast("Clé supprimée.", "ok"); } catch (e) { toast(e.message, "error"); } } }, "Supprimer la clé"))));
    }
  }
  const card = (t, ...c) => h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, t)), h("div", { class: "card-b stack" }, ...c));
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Paramètres généraux"), h("p", null, "Conservation des données, seuils d'alerte et mode maintenance."))),
    h("div", { class: "stack", style: { gap: "16px" } },
      card("Mode maintenance", h("div", { class: "alert" }, icon("info"), "Affiche un bandeau à tous les utilisateurs du panel, notifie les joueurs en ligne et peut bloquer les écritures de fichiers pendant une opération critique."),
        switchEl(mm.enabled, (v) => { mm.enabled = v; }, "Mode maintenance actif"), field("Message personnalisable", msg), switchEl(mm.block, (v) => { mm.block = v; }, "Bloquer les opérations d'écriture du gestionnaire de fichiers"), switchEl(mm.notify, (v) => { mm.notify = v; }, "Prévenir les joueurs connectés avec une commande say"),
        h("div", { class: "form-grid" }, field("Action planifiée à l'activation", action), field("Dans (minutes)", delay, "Un arrêt propre avec avertissement des joueurs est exécuté.")), h("div", null, h("button", { class: "btn primary", onclick: saveMaint }, "Appliquer"))),
      card("Conservation et alertes", h("div", { class: "form-grid" }, field("Corbeille : conservation (jours)", f.trash_retention_days), field("Versions de fichiers conservées par fichier", f.file_versions_keep), field("Alerte disque : espace libre minimal (Go)", f.disk_min_free_gb), field("Alerte disque : occupation (%)", f.disk_warn_percent), field("Alerte RAM de la machine (%)", f.ram_warn_percent),
        field("Notifications : conservation (jours)", f.notification_retention_days), field("Historique des actions : conservation (jours)", f.audit_retention_days), field("Mesures (graphiques) : conservation (jours)", f.metrics_retention_days)),
        switchEl(upd.v, (v) => { upd.v = v; }, "Vérifier chaque jour si une nouvelle version de Minecraft est disponible (connexion sortante vers Mojang)"), h("div", null, h("button", { class: "btn primary", onclick: saveGeneral }, "Enregistrer"))),
      aiBox,
      card("Environnement (lecture seule — fichier .env)", h("dl", { class: "kv" }, [["Adresse d'écoute", `${env.host}:${env.port}`], ["Dossier du serveur", env.server_dir], ["Dossier des sauvegardes", env.backup_dir], ["Dossier des données", env.data_dir], ["Dossier des logs", env.log_dir], ["Durée de session", `${env.session_hours} h (inactivité : ${env.idle_minutes} min)`], ["Taille max. de téléversement", `${env.max_upload_mb} Mo`], ["Cookies sécurisés", env.secure_cookies], ["Hôtes autorisés", env.allowed_hosts.join(", ")]].flatMap(([k, v]) => [h("dt", null, k), h("dd", { class: "mono small" }, v)])))));
}
