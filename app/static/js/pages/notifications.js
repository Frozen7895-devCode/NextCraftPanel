// Centre de notifications.
import { h, icon, mount, toast, fmtDate, fmtRel, emptyState, badge, loading, LEVEL_KIND } from "../util.js";
import { get, post, qs } from "../api.js";

const KIND_ICON = { crash: "alert", backup: "archive", disk: "alert", ram: "activity", security: "shield", task: "clock", server: "cube", maintenance: "wrench", update: "download", file: "file" };
const KIND_LABEL = { crash: "Crash", backup: "Sauvegarde", disk: "Disque", ram: "Mémoire", security: "Sécurité", task: "Tâche", server: "Serveur", maintenance: "Maintenance", update: "Mise à jour", file: "Fichier" };

export async function render(el, app) {
  let unreadOnly = false, data;
  const body = h("div"); const offs = [];
  async function load() { try { data = await get(`/api/notifications${qs({ unread_only: unreadOnly ? "true" : "", limit: 200 })}`); draw(); app.refreshUnread(); } catch (e) { mount(body, h("div", { class: "alert bad" }, e.message)); } }
  async function mark(ids) { try { await post("/api/notifications/read", ids ? { ids } : { all: true }); load(); } catch (e) { toast(e.message, "error"); } }
  function draw() {
    mount(body, h("div", { class: "card" }, data.items.length ? h("div", { class: "list", style: { padding: "4px 16px" } }, data.items.map((n) => h("div", { class: "row", style: { alignItems: "flex-start", flexWrap: "nowrap", opacity: n.read ? 0.6 : 1, padding: "12px 0" } },
      h("span", { class: `badge ${LEVEL_KIND[n.level] || ""}`, style: { padding: "6px" } }, icon(KIND_ICON[n.kind] || "bell")),
      h("div", { class: "grow" }, h("div", { class: "row", style: { gap: "8px" } }, h("b", null, n.title), badge(KIND_LABEL[n.kind] || n.kind), !n.read ? badge("nouveau", "info") : null), n.message ? h("div", { class: "muted small", style: { whiteSpace: "pre-wrap" } }, n.message) : null, h("div", { class: "faint small", title: fmtDate(n.created_at) }, fmtRel(n.created_at))),
      !n.read ? h("button", { class: "btn sm", onclick: () => mark([n.id]) }, icon("check"), "Marquer comme lue") : null))) : emptyState("bell", unreadOnly ? "Aucune notification non lue" : "Aucune notification")));
  }
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Notifications"), h("p", null, "Crashs, sauvegardes, espace disque, RAM, connexions suspectes, tâches, maintenance…")),
    h("div", { class: "row" }, h("div", { class: "seg" }, [[false, "Toutes"], [true, "Non lues"]].map(([v, l]) => h("button", { class: unreadOnly === v ? "on" : "", onclick: () => { unreadOnly = v; load(); } }, l))),
      h("button", { class: "btn", onclick: () => mark(null) }, icon("check"), "Tout marquer comme lu"), h("button", { class: "btn", onclick: async () => { await post("/api/notifications/clear-read"); toast("Notifications lues supprimées.", "ok"); load(); } }, icon("trash"), "Supprimer les lues"))), body);
  await load();
  offs.push(app.bus.on("notification", load));
  return () => offs.forEach((o) => o());
}
