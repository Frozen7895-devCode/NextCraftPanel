// Comptes et permissions : création, rôles, permissions détaillées, mots de passe, sessions.
import { h, icon, mount, toast, modal, confirmDialog, fmtDate, fmtRel, emptyState, badge, field, loading, copyText } from "../util.js";
import { get, post, patch, del } from "../api.js";

const ROLE = { admin: ["Administrateur", "violet"], manager: ["Gestionnaire", "info"], viewer: ["Lecteur", ""] };

export async function render(el, app) {
  const meta = await get("/api/admin/meta");
  let users = [], sessions = [], tab = "users";
  const body = h("div"), me = app.state.user;

  async function load() { [users, sessions] = await Promise.all([get("/api/admin/users"), get("/api/admin/sessions")]); draw(); }
  const permGrid = (selected, disabled) => {
    const checks = {};
    const grid = h("div", { class: "perm-grid" }, Object.entries(meta.permissions).filter(([k]) => k !== "accounts.manage").map(([k, label]) => { const c = h("input", { type: "checkbox", disabled: disabled || null }); c.checked = selected.has(k); checks[k] = c; return h("label", { class: "check" }, c, h("span", null, label, h("small", null, k))); }));
    return { grid, get: () => Object.entries(checks).filter(([, c]) => c.checked).map(([k]) => k), set: (list) => Object.entries(checks).forEach(([k, c]) => { c.checked = list.includes(k); }) };
  };
  function createDialog() {
    const un = h("input", { class: "input", autocomplete: "off", maxlength: 32 }), pw = h("input", { class: "input", type: "password", autocomplete: "new-password" });
    const role = h("select", { class: "select" }, ["viewer", "manager", "admin"].map((r) => h("option", { value: r }, ROLE[r][0])));
    const perms = permGrid(new Set(meta.role_defaults.viewer), false);
    const permBox = h("div");
    const syncPerms = () => { perms.set(meta.role_defaults[role.value]); mount(permBox, role.value === "admin" ? h("div", { class: "alert" }, icon("info"), "Un administrateur possède toutes les permissions.") : [h("div", { class: "lbl-t", style: { marginBottom: "6px" } }, "Permissions (modifiables)"), perms.grid]); };
    role.onchange = syncPerms; syncPerms();
    modal({ title: "Nouveau compte", size: "wide", body: h("div", { class: "stack" }, h("div", { class: "form-grid" }, field("Nom d'utilisateur", un, "3 à 32 caractères : lettres, chiffres, . _ -"), field("Rôle", role), h("div", { class: "full" }, field("Mot de passe initial", pw, "10 caractères minimum, 3 types de caractères. L'utilisateur pourra le changer."))), permBox),
      buttons: [{ label: "Annuler", value: false }, { label: "Créer le compte", kind: "primary", onClick: async () => { try { await post("/api/admin/users", { username: un.value, password: pw.value, role: role.value, permissions: role.value === "admin" ? undefined : perms.get() }); toast("Compte créé.", "ok"); load(); return true; } catch (e) { toast(e.message, "error"); return false; } } }] });
  }
  function permsDialog(u) {
    const perms = permGrid(new Set(u.permissions), u.role === "admin");
    const role = h("select", { class: "select", disabled: u.id === me.id }, ["viewer", "manager", "admin"].map((r) => h("option", { value: r, selected: u.role === r }, ROLE[r][0])));
    modal({ title: `Rôle et permissions de ${u.username}`, size: "wide", body: h("div", { class: "stack" }, field("Rôle", role, u.id === me.id ? "Vous ne pouvez pas modifier votre propre rôle." : "Changer de rôle réinitialise les permissions aux valeurs par défaut du rôle."),
      u.role === "admin" ? h("div", { class: "alert" }, icon("info"), "Un administrateur possède toutes les permissions.") : [h("div", { class: "lbl-t" }, "Permissions"), perms.grid]),
      buttons: [{ label: "Annuler", value: false }, { label: "Enregistrer", kind: "primary", onClick: async () => { try { await patch(`/api/admin/users/${u.id}`, role.value !== u.role ? { role: role.value } : { permissions: perms.get() }); toast("Compte mis à jour.", "ok"); load(); return true; } catch (e) { toast(e.message, "error"); return false; } } }] });
  }
  async function resetPw(u) {
    if (!(await confirmDialog({ title: "Réinitialiser le mot de passe", message: `Un mot de passe temporaire sera généré pour ${u.username}. Ses sessions seront révoquées et il devra le changer à sa prochaine connexion.`, confirmLabel: "Réinitialiser", danger: true }))) return;
    try { const r = await post(`/api/admin/users/${u.id}/reset-password`, {}); modal({ title: "Mot de passe temporaire", size: "narrow", body: h("div", { class: "stack" }, h("div", { class: "alert warn" }, icon("alert"), "Affiché une seule fois. Transmettez-le de manière sécurisée."), h("div", { class: "row" }, h("code", { style: { fontSize: "15px", padding: "6px 10px" } }, r.temporary_password), h("button", { class: "btn sm", onclick: () => copyText(r.temporary_password) }, icon("copy"), "Copier"))), buttons: [{ label: "Fermer", value: true }] }); load(); } catch (e) { toast(e.message, "error"); }
  }
  async function toggle(u) { if (u.is_active && !(await confirmDialog({ title: "Désactiver le compte", message: `${u.username} ne pourra plus se connecter et ses sessions seront révoquées.`, confirmLabel: "Désactiver", danger: true }))) return; try { await patch(`/api/admin/users/${u.id}`, { is_active: !u.is_active }); load(); } catch (e) { toast(e.message, "error"); } }
  async function remove(u) { if (await confirmDialog({ title: "Supprimer le compte", message: `Le compte ${u.username} sera supprimé définitivement.`, confirmLabel: "Supprimer", danger: true })) { try { await del(`/api/admin/users/${u.id}`); toast("Compte supprimé.", "ok"); load(); } catch (e) { toast(e.message, "error"); } } }
  async function revoke(u) { try { const r = await post(`/api/admin/users/${u.id}/revoke-sessions`); toast(`${r.revoked} session(s) révoquée(s).`, "ok"); load(); } catch (e) { toast(e.message, "error"); } }
  async function revokeAll() { if (await confirmDialog({ title: "Révoquer toutes les sessions", message: "Tous les autres utilisateurs seront déconnectés immédiatement (votre session est conservée).", confirmLabel: "Tout révoquer", danger: true })) { try { const r = await post("/api/admin/sessions/revoke-all"); toast(`${r.revoked} session(s) révoquée(s).`, "ok"); load(); } catch (e) { toast(e.message, "error"); } } }

  function draw() {
    const tabs = h("div", { class: "tabs" }, [["users", `Comptes (${users.length})`], ["sessions", `Sessions actives (${sessions.length})`]].map(([id, l]) => h("button", { class: id === tab ? "on" : "", onclick: () => { tab = id; draw(); } }, l)));
    mount(body, tabs, tab === "users" ? h("div", { class: "card table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Utilisateur"), h("th", null, "Rôle"), h("th", null, "Permissions"), h("th", null, "Dernière connexion"), h("th", null, "Statut"), h("th", null))),
      h("tbody", null, users.map((u) => h("tr", null, h("td", null, h("div", { class: "row", style: { gap: "10px", flexWrap: "nowrap" } }, h("span", { class: "avatar" }, u.username.slice(0, 2).toUpperCase()), h("div", null, h("b", null, u.username), u.id === me.id ? h("div", { class: "small faint" }, "vous") : null))),
        h("td", null, badge(...ROLE[u.role])), h("td", { class: "small muted" }, u.role === "admin" ? "toutes" : `${u.permissions.length} / ${Object.keys(meta.permissions).length - 1}`), h("td", { class: "small" }, u.last_login_at ? fmtRel(u.last_login_at) : "jamais"), h("td", null, u.is_active ? badge("actif", "ok") : badge("désactivé", "bad"), u.must_change_password ? badge("mot de passe à changer", "warn") : null),
        h("td", null, h("div", { class: "actions" }, h("button", { class: "btn sm", onclick: () => permsDialog(u) }, "Rôle et permissions"), h("button", { class: "btn sm", onclick: () => resetPw(u) }, icon("key"), "Mot de passe"), h("button", { class: "btn sm", onclick: () => revoke(u) }, "Révoquer sessions"),
          u.id !== me.id ? [h("button", { class: "btn sm", onclick: () => toggle(u) }, u.is_active ? "Désactiver" : "Activer"), h("button", { class: "btn sm danger icon-only", "aria-label": "Supprimer", onclick: () => remove(u) }, icon("trash"))] : null)))))))
      : h("div", { class: "card table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Utilisateur"), h("th", null, "Adresse IP"), h("th", null, "Navigateur"), h("th", null, "Connecté"), h("th", null, "Dernière activité"))),
        h("tbody", null, sessions.map((s) => h("tr", null, h("td", null, s.username, s.current ? badge("vous", "info") : null), h("td", { class: "mono small" }, s.ip), h("td", { class: "small muted", style: { maxWidth: "320px", overflow: "hidden", textOverflow: "ellipsis" }, title: s.user_agent }, s.user_agent), h("td", { class: "small" }, fmtDate(s.created_at)), h("td", { class: "small" }, fmtRel(s.last_seen_at))))))));
  }
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Comptes et permissions"), h("p", null, "Administrateur : tout. Gestionnaire : serveur, console et fichiers selon les permissions. Lecteur : consultation seule.")),
    h("div", { class: "row" }, h("button", { class: "btn danger", onclick: revokeAll }, icon("lock"), "Révoquer toutes les sessions"), h("button", { class: "btn primary", onclick: createDialog }, icon("plus"), "Nouveau compte"))), body);
  await load();
}
