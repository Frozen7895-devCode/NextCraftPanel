// Mon compte : mot de passe, sessions.
import { h, icon, mount, toast, confirmDialog, fmtDate, fmtRel, field, badge } from "../util.js";
import { get, post } from "../api.js";

export async function render(el, app) {
  const u = app.state.user;
  const cur = h("input", { class: "input", type: "password", autocomplete: "current-password" }), n1 = h("input", { class: "input", type: "password", autocomplete: "new-password" }), n2 = h("input", { class: "input", type: "password", autocomplete: "new-password" });
  const sessHost = h("div");
  async function loadSessions() {
    try {
      const ss = await get("/api/auth/sessions");
      const rows = ss.map((s) => h("tr", null,
        h("td", { class: "mono small" }, s.ip, s.current ? badge("cette session", "info") : null),
        h("td", { class: "small muted", style: { maxWidth: "340px", overflow: "hidden", textOverflow: "ellipsis" }, title: s.user_agent }, s.user_agent),
        h("td", { class: "small" }, fmtDate(s.created_at)),
        h("td", { class: "small" }, fmtRel(s.last_seen_at))));
      const head = h("thead", null, h("tr", null, h("th", null, "Adresse IP"), h("th", null, "Navigateur"), h("th", null, "Connecté"), h("th", null, "Dernière activité")));
      mount(sessHost, h("div", { class: "table-wrap" }, h("table", { class: "t" }, head, h("tbody", null, rows))));
    } catch (e) { mount(sessHost, h("div", { class: "alert bad" }, e.message)); }
  }
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Mon compte"), h("p", null, `${u.username} · ${{ admin: "Administrateur", manager: "Gestionnaire", viewer: "Lecteur" }[u.role]}`))),
    h("div", { class: "grid g2" },
      h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Changer le mot de passe")), h("div", { class: "card-b stack" }, field("Mot de passe actuel", cur), field("Nouveau mot de passe", n1, "10 caractères minimum, au moins 3 types de caractères."), field("Confirmer", n2),
        h("button", { class: "btn primary", style: { alignSelf: "flex-start" }, onclick: async () => { if (n1.value !== n2.value) { toast("Les deux mots de passe ne correspondent pas.", "error"); return; } try { const r = await post("/api/auth/password", { current_password: cur.value, new_password: n1.value }); toast(`Mot de passe modifié (${r.revoked_sessions} autre(s) session(s) déconnectée(s)).`, "ok"); cur.value = n1.value = n2.value = ""; loadSessions(); } catch (e) { toast(e.message, "error"); } } }, "Modifier le mot de passe"))),
      h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Permissions de votre compte")), h("div", { class: "card-b" }, h("div", { class: "row" }, u.permissions.map((p) => h("span", { class: "badge mono" }, p)))))),
    h("div", { class: "card", style: { marginTop: "16px" } }, h("div", { class: "card-h" }, h("h2", null, "Mes sessions actives"), h("button", { class: "btn sm danger", onclick: async () => { if (await confirmDialog({ title: "Se déconnecter partout", message: "Toutes vos sessions, y compris celle-ci, seront fermées.", confirmLabel: "Tout fermer", danger: true })) { await post("/api/auth/logout-all"); location.reload(); } } }, icon("logout"), "Se déconnecter partout")), h("div", { class: "card-b" }, sessHost)));
  await loadSessions();
}
