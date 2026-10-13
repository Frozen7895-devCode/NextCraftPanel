// Écrans d'authentification : connexion, configuration initiale, changement de mot de passe obligatoire.
import { h, icon, mount, toast, field } from "./util.js";
import { api, post, setCsrf } from "./api.js";

const logo = () => h("div", { class: "brand" }, icon("cube", "icon logo-mark"), h("div", null, h("b", null, "NexCraft Panel"), h("small", null, "Gestion de serveur Minecraft")));

function authShell(root, ...children) {
  mount(root, h("div", { class: "auth" }, h("div", { class: "auth-card" }, logo(), ...children)));
}

export function renderLogin(root, onSuccess, notice) {
  const user = h("input", { class: "input", name: "username", autocomplete: "username", required: true, autofocus: true });
  const pass = h("input", { class: "input", name: "password", type: "password", autocomplete: "current-password", required: true });
  const err = h("div", { class: "alert bad", hidden: true, role: "alert" });
  const btn = h("button", { class: "btn primary", type: "submit", style: { width: "100%", height: "38px" } }, "Se connecter");
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault(); err.hidden = true; btn.disabled = true;
    try { const r = await post("/api/auth/login", { username: user.value, password: pass.value }, { noAuthRedirect: true }); setCsrf(r.csrf_token); onSuccess(r); }
    catch (ex) { err.textContent = ex.message; err.hidden = false; pass.value = ""; pass.focus(); }
    finally { btn.disabled = false; }
  } }, field("Nom d'utilisateur", user), field("Mot de passe", pass), err, btn);
  authShell(root, h("h1", null, "Connexion"), h("p", { class: "lead" }, "Accédez à votre panneau de contrôle."),
    notice ? h("div", { class: "alert warn", style: { marginBottom: "12px" } }, notice) : null, form);
  setTimeout(() => user.focus(), 30);
}

export function renderSetup(root, onSuccess) {
  const user = h("input", { class: "input", name: "username", autocomplete: "username", required: true, minlength: 3, maxlength: 32 });
  const pass = h("input", { class: "input", name: "password", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const pass2 = h("input", { class: "input", name: "password2", type: "password", autocomplete: "new-password", required: true });
  const err = h("div", { class: "alert bad", hidden: true, role: "alert" });
  const btn = h("button", { class: "btn primary", type: "submit", style: { width: "100%", height: "38px" } }, "Créer le compte administrateur");
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault(); err.hidden = true;
    if (pass.value !== pass2.value) { err.textContent = "Les deux mots de passe ne correspondent pas."; err.hidden = false; return; }
    btn.disabled = true;
    try { const r = await post("/api/auth/setup", { username: user.value, password: pass.value }, { noAuthRedirect: true }); setCsrf(r.csrf_token); toast("Compte administrateur créé. Bienvenue !", "ok"); onSuccess(r); }
    catch (ex) { err.textContent = ex.message; err.hidden = false; }
    finally { btn.disabled = false; }
  } }, field("Nom d'utilisateur", user, "3 à 32 caractères : lettres, chiffres, . _ -"), field("Mot de passe", pass, "10 caractères minimum, avec au moins 3 types parmi minuscules, majuscules, chiffres, symboles."),
    field("Confirmer le mot de passe", pass2), err, btn);
  authShell(root, h("h1", null, "Configuration initiale"), h("p", { class: "lead" }, "Créez le premier compte administrateur. Aucun compte par défaut n'existe."),
    h("div", { class: "alert", style: { marginBottom: "12px" } }, icon("lock"), "Cette étape n'est possible que depuis la machine qui héberge le panel."), form);
  setTimeout(() => user.focus(), 30);
}

export function renderForcePassword(root, onDone) {
  const cur = h("input", { class: "input", type: "password", autocomplete: "current-password", required: true });
  const n1 = h("input", { class: "input", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const n2 = h("input", { class: "input", type: "password", autocomplete: "new-password", required: true });
  const err = h("div", { class: "alert bad", hidden: true, role: "alert" });
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault(); err.hidden = true;
    if (n1.value !== n2.value) { err.textContent = "Les deux mots de passe ne correspondent pas."; err.hidden = false; return; }
    try { await post("/api/auth/password", { current_password: cur.value, new_password: n1.value }, { noAuthRedirect: true }); toast("Mot de passe modifié.", "ok"); onDone(); }
    catch (ex) { err.textContent = ex.message; err.hidden = false; }
  } }, field("Mot de passe temporaire", cur), field("Nouveau mot de passe", n1), field("Confirmer", n2), err, h("button", { class: "btn primary", type: "submit" }, "Changer le mot de passe"));
  authShell(root, h("h1", null, "Nouveau mot de passe requis"), h("p", { class: "lead" }, "Un administrateur a réinitialisé votre mot de passe. Choisissez-en un nouveau pour continuer."), form);
}
