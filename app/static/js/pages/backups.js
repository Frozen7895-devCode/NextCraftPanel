// Sauvegardes : création (cohérente), réglages automatiques, intégrité, restauration confirmée.
import { h, icon, mount, toast, modal, confirmDialog, fmtBytes, fmtDate, fmtRel, emptyState, loading, badge, field, switchEl } from "../util.js";
import { get, post, put, del, downloadUrl } from "../api.js";

export async function render(el, app) {
  const { can, bus } = app;
  const manage = can("backups.manage"), restore = can("backups.restore");
  let list = await get("/api/backups"), cfg = await get("/api/backups/settings");
  const body = h("div", { class: "stack", style: { gap: "16px" } });
  const offs = [], timers = [];

  async function refresh() { try { list = await get("/api/backups"); drawList(); } catch { /* ignore */ } }
  const kindBadge = (k) => (k === "auto" ? badge("auto", "info") : k === "safety" ? badge("sécurité", "violet") : badge("manuelle"));
  const statusBadge = (b) => (b.status === "ok" ? badge("réussie", "ok") : b.status === "running" ? badge("en cours…", "warn") : badge("échec", "bad"));

  async function createDialog() {
    const note = h("input", { class: "input", placeholder: "Note (facultatif)", maxlength: 200 });
    const entries = cfg.entries;
    const defaults = new Set(cfg.settings.include.length ? cfg.settings.include : cfg.default_includes);
    const checks = entries.map((e) => { const i = h("input", { type: "checkbox", value: e.name }); i.checked = defaults.has(e.name); return i; });
    modal({ title: "Nouvelle sauvegarde", size: "wide", body: h("div", { class: "stack" }, field("Note", note),
      h("div", null, h("div", { class: "lbl-t", style: { marginBottom: "6px" } }, "Éléments à sauvegarder"), h("div", { class: "perm-grid" }, entries.map((e, i) => h("label", { class: "check" }, checks[i], icon(e.is_dir ? "folder" : "file"), e.name)))),
      h("div", { class: "alert" }, icon("info"), "Si le serveur tourne, il est mis en pause d'écriture (save-off / save-all flush) pendant l'archivage pour garantir la cohérence des mondes.")),
      buttons: [{ label: "Annuler", value: false }, { label: "Lancer la sauvegarde", kind: "primary", onClick: async () => { const inc = checks.filter((c) => c.checked).map((c) => c.value); if (!inc.length) { toast("Sélectionnez au moins un élément.", "warn"); return false; } try { await post("/api/backups", { note: note.value, includes: inc }); toast("Sauvegarde lancée en arrière-plan.", "info"); setTimeout(refresh, 600); return true; } catch (e) { toast(e.message, "error"); return false; } } }] });
  }
  async function verify(b) { try { const r = await post(`/api/backups/${b.id}/verify`); toast(r.message, r.ok ? "ok" : "error"); refresh(); } catch (e) { toast(e.message, "error"); } }
  async function restoreDialog(b) {
    const stop = h("input", { type: "checkbox" }), again = h("input", { type: "checkbox" }), understand = h("input", { type: "checkbox" });
    const run = app.state.status && app.state.status.status !== "stopped";
    if (run) stop.checked = true;
    modal({ title: `Restaurer ${b.filename}`, size: "wide", body: h("div", { class: "stack" },
      h("div", { class: "alert bad" }, icon("alert"), h("div", null, h("b", null, "Cette opération remplace les données actuelles."), h("div", null, `Les éléments suivants seront remplacés par ceux de la sauvegarde du ${fmtDate(b.created_at)} : `, h("span", { class: "mono" }, b.includes.join(", ")), ". Tout ce qui a été modifié depuis sera perdu, sauf dans la sauvegarde de sécurité créée automatiquement juste avant."))),
      run ? h("label", { class: "check" }, stop, "Arrêter proprement le serveur avant de restaurer (obligatoire : il tourne actuellement)") : null,
      h("label", { class: "check" }, again, "Redémarrer le serveur après la restauration"),
      h("label", { class: "check" }, understand, "Je comprends que les données actuelles seront remplacées")),
      buttons: [{ label: "Annuler", value: false }, { label: "Restaurer", kind: "danger", onClick: async () => {
        if (!understand.checked) { toast("Cochez la confirmation pour continuer.", "warn"); return false; }
        if (run && !stop.checked) { toast("Le serveur doit être arrêté : cochez l'option d'arrêt propre.", "warn"); return false; }
        try { await post(`/api/backups/${b.id}/restore`, { confirm: true, stop_server: stop.checked, restart: again.checked }); toast("Restauration lancée : suivez les notifications.", "info"); setTimeout(refresh, 1500); return true; } catch (e) { toast(e.message, "error"); return false; } } }] });
  }
  async function remove(b) { if (await confirmDialog({ title: "Supprimer la sauvegarde", message: `${b.filename} sera supprimée définitivement.`, danger: true, confirmLabel: "Supprimer" })) { try { await del(`/api/backups/${b.id}`); toast("Sauvegarde supprimée.", "ok"); refresh(); } catch (e) { toast(e.message, "error"); } } }

  const listHost = h("div", { class: "card" });
  function drawList() {
    const items = list.items;
    mount(listHost,
      list.busy ? h("div", { class: "alert warn", style: { margin: "14px" } }, h("span", { class: "spin" }), "Une sauvegarde ou une restauration est en cours…") : null,
      items.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Date"), h("th", null, "Type"), h("th", null, "Statut"), h("th", { class: "num" }, "Taille"), h("th", null, "Intégrité"), h("th", { class: "hide-sm" }, "Contenu"), h("th", null))),
        h("tbody", null, items.map((b) => h("tr", null,
          h("td", null, h("div", null, fmtDate(b.created_at)), h("div", { class: "small faint" }, `${b.created_by}${b.note ? " · " + b.note : ""}`)),
          h("td", null, kindBadge(b.kind)), h("td", null, statusBadge(b), b.status === "failed" ? h("div", { class: "small bad", title: b.error }, (b.error || "").slice(0, 60)) : null), h("td", { class: "num" }, b.status === "ok" ? fmtBytes(b.size) : "—"),
          h("td", null, b.status !== "ok" ? "—" : b.verify_ok === true ? h("span", { title: `Vérifiée ${fmtRel(b.verified_at)}` }, badge("intègre", "ok")) : b.verify_ok === false ? badge("corrompue", "bad") : badge("non vérifiée"), b.status === "ok" ? h("div", { class: "small faint" }, b.consistent ? "cohérente" : "cohérence non garantie") : null),
          h("td", { class: "hide-sm small mono", style: { maxWidth: "220px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, title: b.includes.join(", ") }, b.includes.join(", ")),
          h("td", null, b.status === "ok" ? h("div", { class: "actions" }, h("button", { class: "btn sm", onclick: () => verify(b) }, "Vérifier"),
            manage ? h("button", { class: "btn sm icon-only", "aria-label": "Télécharger", title: "Télécharger", onclick: () => downloadUrl(`/api/backups/${b.id}/download`) }, icon("download")) : null,
            restore ? h("button", { class: "btn sm", onclick: () => restoreDialog(b) }, icon("history"), "Restaurer") : null,
            manage ? h("button", { class: "btn sm danger icon-only", "aria-label": "Supprimer", title: "Supprimer", onclick: () => remove(b) }, icon("trash")) : null) : manage ? h("div", { class: "actions" }, h("button", { class: "btn sm danger icon-only", "aria-label": "Supprimer", onclick: () => remove(b) }, icon("trash"))) : null)))))) : emptyState("archive", "Aucune sauvegarde", manage ? "Créez la première sauvegarde ci-dessus." : null));
  }

  // ---------- Réglages ----------
  function settingsCard() {
    const s = cfg.settings, auto = s.auto;
    const keep = h("input", { class: "input", type: "number", min: 1, max: 200, value: s.keep_last }), age = h("input", { class: "input", type: "number", min: 0, max: 3650, value: s.max_age_days });
    const hours = h("input", { class: "input", type: "number", min: 1, max: 720, value: auto.every_hours });
    let enabled = auto.enabled, consistent = s.consistent;
    const sel = new Set(s.include); const checks = cfg.entries.map((e) => { const i = h("input", { type: "checkbox", value: e.name }); i.checked = sel.has(e.name); return i; });
    const dis = !manage;
    return h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Sauvegardes automatiques et rétention")), h("div", { class: "card-b stack" },
      h("div", { class: "row" }, switchEl(enabled, (v) => { enabled = v; }, "Sauvegarde automatique"), h("span", { class: "muted small" }, "toutes les"), h("div", { style: { width: "90px" } }, hours), h("span", { class: "muted small" }, "heure(s)")),
      switchEl(consistent, (v) => { consistent = v; }, "Sauvegarde cohérente (save-off / save-all flush quand le serveur tourne)"),
      h("div", { class: "form-grid" }, field("Nombre de sauvegardes à conserver", keep, "Les plus anciennes sont supprimées automatiquement."), field("Âge maximum (jours, 0 = illimité)", age, "La sauvegarde la plus récente n'est jamais supprimée par l'âge.")),
      h("div", null, h("div", { class: "lbl-t", style: { marginBottom: "6px" } }, "Éléments sauvegardés par défaut"), h("div", { class: "small muted", style: { marginBottom: "6px" } }, cfg.settings.include.length ? "Sélection personnalisée." : `Sélection automatique actuelle : ${cfg.default_includes.join(", ") || "aucun"}. Cochez des éléments pour la figer.`), h("div", { class: "perm-grid" }, cfg.entries.map((e, i) => h("label", { class: "check" }, checks[i], icon(e.is_dir ? "folder" : "file"), e.name)))),
      manage ? h("div", { class: "row" }, h("button", { class: "btn primary", onclick: async () => { try { cfg.settings = await put("/api/backups/settings", { include: checks.filter((c) => c.checked).map((c) => c.value), keep_last: +keep.value, max_age_days: +age.value, consistent, auto_enabled: enabled, auto_every_hours: +hours.value }); toast("Réglages enregistrés.", "ok"); } catch (e) { toast(e.message, "error"); } } }, "Enregistrer les réglages"), h("span", { class: "small muted" }, `Dossier des archives : ${list.backup_dir}`)) : null));
  }
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Sauvegardes"), h("p", null, "Archives ZIP vérifiables. Avant toute restauration, l'état actuel est sauvegardé automatiquement.")),
    manage ? h("button", { class: "btn primary", onclick: createDialog }, icon("plus"), "Nouvelle sauvegarde") : null), body);
  mount(body, listHost, settingsCard());
  drawList();
  offs.push(bus.on("backup", refresh), bus.on("notification", refresh)); timers.push(setInterval(refresh, 15000));
  return () => { offs.forEach((o) => o()); timers.forEach(clearInterval); };
}
