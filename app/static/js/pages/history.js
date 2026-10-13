// Historique des actions (audit) et journaux de connexion.
import { h, icon, mount, toast, fmtDate, fmtRel, emptyState, badge, loading, debounce, LEVEL_KIND, field } from "../util.js";
import { get, qs, downloadUrl } from "../api.js";

const CATS = [["", "Toutes les actions"], ["file.", "Fichiers (modifiés, supprimés, renommés, téléversés)"], ["server.", "Démarrages, arrêts et configuration"], ["console.", "Commandes"], ["account.", "Comptes"], ["backup.", "Sauvegardes"], ["task.", "Tâches"], ["player.", "Joueurs"], ["auth.", "Authentification"], ["settings.", "Paramètres"]];

export async function render(el, app) {
  const { can } = app;
  let tab = "audit", offset = 0, q = "", level = "", total = 0;
  const body = h("div");
  const LIMIT = 50;

  async function loadAudit() {
    mount(body, loading());
    try {
      const d = await get(`/api/audit${qs({ limit: LIMIT, offset, q, level })}`); total = d.total;
      mount(body, h("div", { class: "card" }, d.items.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Date"), h("th", null, "Utilisateur"), h("th", null, "Action"), h("th", null, "Cible"), h("th", null, "Détail"), h("th", { class: "hide-sm" }, "IP"))),
        h("tbody", null, d.items.map((e) => h("tr", null, h("td", { class: "small nowrap", title: fmtRel(e.created_at) }, fmtDate(e.created_at)), h("td", null, e.username), h("td", null, badge(e.action, LEVEL_KIND[e.level] || "")), h("td", { class: "small mono", style: { maxWidth: "260px", overflow: "hidden", textOverflow: "ellipsis" }, title: e.target }, e.target), h("td", { class: "small muted", style: { maxWidth: "320px" } }, e.detail), h("td", { class: "small mono hide-sm" }, e.ip)))))) : emptyState("history", "Aucune entrée"),
        h("div", { class: "row", style: { padding: "10px 14px", justifyContent: "space-between" } }, h("span", { class: "small muted" }, `${total} entrée(s)`), h("div", { class: "row" }, h("button", { class: "btn sm", disabled: offset === 0, onclick: () => { offset = Math.max(0, offset - LIMIT); loadAudit(); } }, "Précédent"), h("button", { class: "btn sm", disabled: offset + LIMIT >= total, onclick: () => { offset += LIMIT; loadAudit(); } }, "Suivant")))));
    } catch (e) { mount(body, h("div", { class: "alert bad" }, e.message)); }
  }
  async function loadLogins() {
    mount(body, loading());
    try { const d = await get("/api/audit/logins?limit=200"); mount(body, h("div", { class: "card table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Date"), h("th", null, "Compte saisi"), h("th", null, "Adresse IP"), h("th", null, "Résultat"), h("th", null, "Raison"))),
      h("tbody", null, d.map((l) => h("tr", null, h("td", { class: "small nowrap" }, fmtDate(l.created_at)), h("td", null, l.username), h("td", { class: "mono small" }, l.ip), h("td", null, l.success ? badge("réussie", "ok") : badge("échec", "bad")), h("td", { class: "small muted" }, l.reason))))))); } catch (e) { mount(body, h("div", { class: "alert bad" }, e.message)); }
  }
  function logsTab() {
    mount(body, h("div", { class: "card" }, h("div", { class: "card-b stack" }, h("div", { class: "muted" }, "Téléchargez les journaux bruts pour archivage ou analyse externe."),
      h("div", { class: "row" }, h("button", { class: "btn", onclick: () => downloadUrl("/api/admin/logs/panel") }, icon("download"), "Journal du panel (nexcraft.log)"),
        can("console.view") ? h("button", { class: "btn", onclick: () => downloadUrl("/api/console/download?source=console") }, icon("download"), "Journal de la console Minecraft") : null,
        can("console.view") && can("files.read") ? h("button", { class: "btn", onclick: () => downloadUrl("/api/console/download?source=latest") }, icon("download"), "logs/latest.log du serveur") : null),
      h("div", { class: "small muted" }, "Emplacements sur le disque : dossier « logs » du panel (nexcraft.log, security.log, minecraft-console.log).")))); }
  function draw() {
    const tabs = [["audit", "Actions"], ...(can("accounts.manage") ? [["logins", "Connexions"]] : []), ["logs", "Journaux"]];
    const bar = h("div", { class: "tabs" }, tabs.map(([id, l]) => h("button", { class: id === tab ? "on" : "", onclick: () => { tab = id; draw(); } }, l)));
    const tools = tab === "audit" ? h("div", { class: "row", style: { marginBottom: "12px" } },
      h("select", { class: "select", style: { maxWidth: "340px" }, "aria-label": "Catégorie", onchange: (e) => { q = e.target.value; search.value = ""; offset = 0; loadAudit(); } }, CATS.map(([v, l]) => h("option", { value: v }, l))),
      h("select", { class: "select", style: { maxWidth: "180px" }, "aria-label": "Niveau", onchange: (e) => { level = e.target.value; offset = 0; loadAudit(); } }, [["", "Tous les niveaux"], ["info", "Information"], ["warning", "Avertissement"], ["error", "Erreur"]].map(([v, l]) => h("option", { value: v }, l))), search) : null;
    mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Historique et journaux"), h("p", null, "Fichiers modifiés, supprimés, renommés, téléversements, démarrages/arrêts, commandes, configuration, comptes…"))), bar, tools, body);
    if (tab === "audit") loadAudit(); else if (tab === "logins") loadLogins(); else logsTab();
  }
  const search = h("input", { class: "input", type: "search", placeholder: "Rechercher (action, cible, utilisateur…)", style: { maxWidth: "300px" }, "aria-label": "Rechercher", oninput: debounce((e) => { q = e.target.value; offset = 0; loadAudit(); }, 300) });
  draw();
}
