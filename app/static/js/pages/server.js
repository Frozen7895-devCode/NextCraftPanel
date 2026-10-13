// Paramètres du serveur : lancement, variables d'environnement, redémarrage auto, installation vérifiée, crashs.
import { h, icon, mount, toast, modal, confirmDialog, fmtDate, fmtRel, fmtDuration, emptyState, badge, field, switchEl, loading, fmtTime } from "../util.js";
import { get, post, put, downloadUrl } from "../api.js";

export async function render(el, app) {
  const { can, state, bus } = app;
  const edit = can("server.configure");
  let meta = await get("/api/server/config");
  const cfg = meta.config;
  const inp = (k, o = {}) => h("input", { class: "input", value: cfg[k] ?? "", disabled: !edit, ...o });
  const f = { java_path: inp("java_path"), jar_file: inp("jar_file"), xms: inp("xms", { placeholder: "1G" }), xmx: inp("xmx", { placeholder: "4G" }), jvm_args: inp("jvm_args", { placeholder: "-XX:+UseG1GC -XX:MaxGCPauseMillis=200" }), launch_args: inp("launch_args", { placeholder: "nogui" }),
    working_dir: inp("working_dir"), port: inp("port", { type: "number", min: 1, max: 65535 }), stop_timeout: inp("stop_timeout", { type: "number", min: 5, max: 600 }), mc_version: inp("mc_version", { placeholder: "1.21.1" }) };
  const stype = h("select", { class: "select", disabled: !edit }, meta.server_types.map((t) => h("option", { value: t, selected: cfg.server_type === t }, t)));
  const exit = { v: !!cfg.stop_on_panel_exit };
  const preview = h("code", { style: { display: "block", padding: "10px 12px", overflowX: "auto", whiteSpace: "pre" } });
  const updatePreview = () => mount(preview, `${f.java_path.value || "java"} -Xms${f.xms.value.toUpperCase()} -Xmx${f.xmx.value.toUpperCase()} -Dfile.encoding=UTF-8 ${f.jvm_args.value} -jar ${f.jar_file.value} ${f.launch_args.value}`.replace(/\s+/g, " "));
  Object.values(f).forEach((i) => i.addEventListener("input", updatePreview));

  // variables d'environnement
  const envRows = h("div", { class: "stack", style: { gap: "8px" } });
  const envState = Object.entries(cfg.env || {}).map(([k, v]) => ({ k, v }));
  function drawEnv() {
    mount(envRows, envState.map((r, i) => h("div", { class: "row", style: { flexWrap: "nowrap" } },
      h("select", { class: "select", style: { maxWidth: "220px" }, disabled: !edit, onchange: (e) => { r.k = e.target.value; } }, meta.env_allowlist.map((n) => h("option", { value: n, selected: r.k === n }, n))),
      h("input", { class: "input", value: r.v, disabled: !edit, "aria-label": `Valeur de ${r.k}`, oninput: (e) => { r.v = e.target.value; } }),
      edit ? h("button", { class: "btn sm danger icon-only", "aria-label": "Retirer", onclick: () => { envState.splice(i, 1); drawEnv(); } }, icon("x")) : null)),
      edit && meta.env_allowlist.length ? h("button", { class: "btn sm", style: { alignSelf: "flex-start" }, onclick: () => { envState.push({ k: meta.env_allowlist[0], v: "" }); drawEnv(); } }, icon("plus"), "Ajouter une variable") : null);
  }
  // redémarrage automatique
  const ar = cfg.auto_restart || {};
  const arf = { max_attempts: h("input", { class: "input", type: "number", min: 1, max: 50, value: ar.max_attempts, disabled: !edit }), base_delay: h("input", { class: "input", type: "number", min: 1, value: ar.base_delay, disabled: !edit }), max_delay: h("input", { class: "input", type: "number", min: 1, value: ar.max_delay, disabled: !edit }), reset_after: h("input", { class: "input", type: "number", min: 10, value: ar.reset_after, disabled: !edit }) };
  const arEnabled = { v: !!ar.enabled };

  async function save() {
    const env = {}; envState.forEach((r) => { if (r.k) env[r.k] = r.v; });
    const body = { java_path: f.java_path.value, jar_file: f.jar_file.value, xms: f.xms.value, xmx: f.xmx.value, jvm_args: f.jvm_args.value, launch_args: f.launch_args.value, working_dir: f.working_dir.value, port: +f.port.value, stop_timeout: +f.stop_timeout.value,
      server_type: stype.value, mc_version: f.mc_version.value, env, stop_on_panel_exit: exit.v, auto_restart: { enabled: arEnabled.v, max_attempts: +arf.max_attempts.value, base_delay: +arf.base_delay.value, max_delay: +arf.max_delay.value, reset_after: +arf.reset_after.value } };
    try { const r = await put("/api/server/config", body); toast(r.note || "Paramètres enregistrés.", "ok"); app.refreshStatus(); } catch (e) { toast(e.message, "error"); }
  }

  // installation
  const provider = h("select", { class: "select" }), version = h("select", { class: "select" }), verInfo = h("div", { class: "small muted" }), installBtn = h("button", { class: "btn primary", disabled: true, onclick: install }, icon("download"), "Télécharger et installer");
  let providers = null;
  async function loadProviders() { try { providers = await get("/api/server/providers"); mount(provider, h("option", { value: "" }, "Choisir un type…"), Object.entries(providers.providers).map(([k, l]) => h("option", { value: k }, l))); } catch { /* droit insuffisant */ } }
  provider.onchange = async () => {
    mount(version); installBtn.disabled = true; if (!provider.value) return;
    mount(verInfo, h("span", { class: "spin" }), " Chargement des versions…");
    try { const r = await get(`/api/server/versions?provider=${provider.value}`); mount(version, r.versions.map((v) => h("option", { value: v }, v))); installBtn.disabled = state.status && state.status.status !== "stopped"; mount(verInfo, provider.value === "fabric" ? "Fabric ne fournit pas de somme de contrôle : une confirmation explicite sera demandée." : "Le fichier sera vérifié (somme de contrôle officielle) avant installation."); }
    catch (e) { mount(verInfo, h("span", { class: "bad" }, e.message)); }
  };
  async function install(unverified = false) {
    const prov = provider.value, ver = version.value; if (!prov || !ver) return;
    installBtn.disabled = true; toast(`Téléchargement de ${prov} ${ver}…`, "info");
    try { const r = await post("/api/server/install", { provider: prov, version: ver, accept_unverified: unverified === true }); toast(`Installé : ${r.jar_file} (SHA-256 ${r.sha256.slice(0, 12)}…, ${r.verified ? "vérifié" : "non vérifié"}).`, "ok"); app.refreshStatus(); render(el, app); }
    catch (e) { if (e.confirmRequired && !unverified && await confirmDialog({ title: "Fichier non vérifiable", message: `${e.message} Installer quand même ?`, confirmLabel: "Installer sans vérification", danger: true })) return install(true); if (!e.confirmRequired) toast(e.message, "error"); }
    finally { installBtn.disabled = false; }
  }
  async function showLog(kind) {
    const host = h("div", null, loading());
    modal({ title: kind === "startup" ? "Journal de démarrage" : "Dernières erreurs et avertissements", size: "wide", body: host, buttons: [{ label: "Fermer", value: true }] });
    try { const r = await get(kind === "startup" ? "/api/server/startup-log" : "/api/server/errors"); mount(host, r.lines.length ? h("div", { class: "console", style: { borderRadius: "8px", maxHeight: "60vh" } }, r.lines.map((l) => h("div", { class: `cl ${l.level}` }, h("span", { class: "ts" }, fmtTime(l.ts)), l.text))) : emptyState("terminal", "Aucune ligne enregistrée depuis le dernier démarrage")); } catch (e) { mount(host, h("div", { class: "alert bad" }, e.message)); }
  }
  const crashHost = h("div");
  async function loadCrashes() {
    if (!can("diagnostics.view")) return;
    try { const cs = await get("/api/server/crashes"); mount(crashHost, cs.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Date"), h("th", null, "Code"), h("th", null, "Durée"), h("th", null, "Cause probable"), h("th", null, "Redémarrage auto"), h("th", null))),
      h("tbody", null, cs.map((c) => h("tr", null, h("td", { class: "small", title: fmtDate(c.occurred_at) }, fmtRel(c.occurred_at)), h("td", { class: "mono" }, c.exit_code), h("td", null, fmtDuration(c.uptime_s)), h("td", { class: "small", style: { maxWidth: "340px" } }, c.summary), h("td", { class: "small" }, c.restart_info), h("td", null, h("button", { class: "btn sm", onclick: () => downloadUrl(`/api/server/crashes/${c.id}/report`) }, icon("download"), "Rapport"))))))) : emptyState("check", "Aucun crash enregistré")); } catch { /* ignore */ }
  }

  const card = (title, ...children) => h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, title)), h("div", { class: "card-b stack" }, ...children));
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Paramètres du serveur"), h("p", null, `Dossier du serveur : ${meta.server_dir}`)), h("div", { class: "row" },
    h("button", { class: "btn", onclick: async () => { try { app.showPreflight(await get("/api/server/preflight")); } catch (e) { toast(e.message, "error"); } } }, icon("check"), "Vérifier avant démarrage"), edit ? h("button", { class: "btn primary", onclick: save }, icon("save"), "Enregistrer") : null)),
    !edit ? h("div", { class: "alert", style: { marginBottom: "14px" } }, icon("lock"), "Lecture seule : la permission « server.configure » est nécessaire pour modifier ces paramètres.") : null,
    h("div", { class: "stack", style: { gap: "16px" } },
      card("Lancement", h("div", { class: "form-grid" }, field("Chemin vers Java", f.java_path, "« java » (dans le PATH) ou chemin complet de l'exécutable."), field("Fichier JAR du serveur", f.jar_file, "Relatif au répertoire de travail."),
        field("Mémoire minimale (Xms)", f.xms, "Ex. 512M, 2G"), field("Mémoire maximale (Xmx)", f.xmx, "Ex. 4G. Vérifiée par rapport à la RAM de la machine."), field("Arguments JVM", f.jvm_args, "Pas de -Xms/-Xmx/-jar ici. Les caractères ; | & ` $ < > sont refusés."), field("Arguments de lancement", f.launch_args, "Ex. nogui"),
        field("Répertoire de travail", f.working_dir, "Relatif au dossier du serveur."), field("Port Minecraft", f.port, "Écrit dans server.properties."), field("Type de serveur", stype), field("Version de Minecraft", f.mc_version, "Utilisée pour vérifier la compatibilité de Java."), field("Délai d'arrêt propre (s)", f.stop_timeout, "Après ce délai, le processus est terminé puis tué."),
        h("div", { class: "full" }, switchEl(exit.v, (v) => { exit.v = v; }, "Arrêter proprement le serveur quand le panel s'arrête"))),
        h("div", null, h("div", { class: "lbl-t", style: { marginBottom: "6px" } }, "Commande de lancement résultante (aucun shell : liste d'arguments)"), preview)),
      card("Variables d'environnement autorisées", h("div", { class: "small muted" }, `Liste blanche : ${meta.env_allowlist.join(", ")} (modifiable via NEXCRAFT_ENV_ALLOWLIST dans .env).`), envRows),
      card("Redémarrage automatique après un crash", switchEl(arEnabled.v, (v) => { arEnabled.v = v; }, "Redémarrer automatiquement le serveur s'il s'arrête de façon inattendue"),
        h("div", { class: "form-grid" }, field("Tentatives consécutives maximum", arf.max_attempts), field("Délai initial (s)", arf.base_delay, "Le délai double à chaque crash consécutif : 10 s, 20 s, 40 s…"), field("Délai maximum (s)", arf.max_delay), field("Compteur remis à zéro après (s de fonctionnement stable)", arf.reset_after)),
        h("div", { class: "small muted" }, "Au-delà du nombre maximum de tentatives, le panel abandonne et vous notifie : aucune boucle infinie.")),
      edit ? card("Installer ou changer de version", h("div", { class: "alert" }, icon("info"), "Les fichiers sont téléchargés depuis les sources officielles (HTTPS uniquement, hôtes autorisés) et vérifiés par somme de contrôle. Forge/NeoForge : importez manuellement le JAR via le gestionnaire de fichiers."),
        state.status && state.status.status !== "stopped" ? h("div", { class: "alert warn" }, icon("alert"), "Arrêtez le serveur pour installer une autre version.") : null,
        h("div", { class: "form-grid" }, field("Type", provider), field("Version", version)), verInfo, h("div", null, installBtn)) : null,
      card("Journaux de démarrage et d'erreurs", h("div", { class: "row" }, h("button", { class: "btn", onclick: () => showLog("startup") }, icon("terminal"), "Journal de démarrage"), h("button", { class: "btn", onclick: () => showLog("errors") }, icon("alert"), "Dernières erreurs"))),
      can("diagnostics.view") ? card("Crashs enregistrés", crashHost) : null));
  drawEnv(); updatePreview();
  if (edit) await loadProviders();
  await loadCrashes();
}
