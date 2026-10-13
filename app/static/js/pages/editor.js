// Éditeur de fichiers (Monaco, avec repli en zone de texte), comparaison, historique des versions.
import { h, icon, mount, toast, modal, confirmDialog, promptDialog, fmtBytes, fmtDate, fmtRel, emptyState, loading, badge } from "../util.js";
import { get, put, post, qs, downloadUrl } from "../api.js";
import { renderDiff } from "../diff.js";
import { applyTheme, currentTheme } from "../app.js";

let monacoPromise = null;
function loadMonaco() {
  if (window.monaco) return Promise.resolve(window.monaco);
  monacoPromise ||= new Promise((resolve, reject) => {
    const s = document.createElement("script"); s.src = "/static/vendor/monaco/vs/loader.js";
    s.onerror = () => reject(new Error("loader.js introuvable"));
    s.onload = () => {
      const base = `${location.origin}/static/vendor/monaco/`;
      window.require.config({ paths: { vs: `${base}vs` }, "vs/nls": { availableLanguages: { "*": "fr" } } });
      window.MonacoEnvironment = { getWorkerUrl: () => URL.createObjectURL(new Blob([`self.MonacoEnvironment={baseUrl:'${base}'};importScripts('${base}vs/base/worker/workerMain.js');`], { type: "text/javascript" })) };
      window.require(["vs/editor/editor.main"], () => resolve(window.monaco), reject);
    };
    document.head.append(s);
    setTimeout(() => reject(new Error("délai de chargement dépassé")), 20000);
  }).catch((e) => { monacoPromise = null; throw e; });
  return monacoPromise;
}

export async function render(el, app) {
  const { can, navigate, params } = app;
  const path = params.get("path") || "";
  const canWrite = can("files.write");
  if (!path) { mount(el, h("div", { class: "empty" }, "Aucun fichier sélectionné. ", h("a", { href: "#/files" }, "Ouvrir le gestionnaire de fichiers"))); return; }
  mount(el, loading("Ouverture du fichier…"));
  let file;
  try { file = await get(`/api/files/read${qs({ path })}`); }
  catch (e) {
    mount(el, h("div", { class: "stack" }, h("div", { class: "alert bad" }, icon("alert"), e.message), h("div", { class: "row" }, h("a", { class: "btn", href: `#/files?path=${encodeURIComponent(path.split("/").slice(0, -1).join("/"))}` }, "Retour aux fichiers"), can("files.read") ? h("button", { class: "btn", onclick: () => downloadUrl(`/api/files/download${qs({ path })}`) }, icon("download"), "Télécharger") : null)));
    return;
  }
  let saved = file.content, mtime = file.mtime_ns, dirty = false, editor = null, ta = null, disposed = false, mon = null, offTheme = null;
  const dirtyBadge = h("span", { class: "badge warn", hidden: true }, "● modifications non enregistrées");
  const status = h("div", { class: "small muted", style: { padding: "6px 2px", display: "flex", gap: "16px", flexWrap: "wrap" } });
  const host = h("div", { class: "editor-host" }, loading("Chargement de l'éditeur…"));
  const parent = path.split("/").slice(0, -1).join("/");
  const getValue = () => (editor ? editor.getValue() : ta.value);
  function setDirty() { dirty = getValue() !== saved; dirtyBadge.hidden = !dirty; saveBtn.disabled = !canWrite || !dirty; revertBtn.disabled = !dirty; compareBtn.disabled = !dirty; updateStatus(); }
  let syntaxMsg = "";
  function updateStatus(pos) {
    let markers = 0;
    if (editor && mon) markers = mon.editor.getModelMarkers({ resource: editor.getModel().uri }).filter((m) => m.severity >= 4).length;
    else if (file.language === "json") { try { JSON.parse(getValue() || "null"); syntaxMsg = ""; } catch (e) { syntaxMsg = e.message; markers = 1; } }
    mount(status, h("span", null, file.language), h("span", null, "UTF-8"), h("span", null, fmtBytes(new Blob([getValue()]).size)), pos ? h("span", null, `Ln ${pos.lineNumber}, Col ${pos.column}`) : null,
      markers ? h("span", { class: "bad" }, `${markers} erreur(s) de syntaxe${syntaxMsg ? " : " + syntaxMsg : ""}`) : file.language === "json" ? h("span", { class: "ok" }, "JSON valide") : h("span", { class: "faint" }, "Validation de syntaxe disponible pour JSON"));
  }
  async function save(force = false) {
    if (!canWrite || (!dirty && !force)) return;
    const content = getValue();
    try {
      const r = await put("/api/files/write", { path, content, expected_mtime_ns: mtime, force });
      saved = content; mtime = r.mtime_ns; setDirty(); toast("Fichier enregistré (ancienne version conservée dans l'historique).", "ok");
    } catch (e) {
      if (e.data && e.data.conflict) conflictDialog(content); else toast(e.message, "error");
    }
  }
  function conflictDialog(mine) {
    const diffHost = h("div");
    modal({ title: "Le fichier a été modifié ailleurs", size: "wide", body: h("div", { class: "stack" }, h("div", { class: "alert warn" }, icon("alert"), "Quelqu'un (ou le serveur lui-même) a modifié ce fichier depuis votre ouverture. Comparez avant de décider."), diffHost),
      buttons: [{ label: "Annuler", value: false }, { label: "Voir les différences", onClick: async () => { try { const cur = await get(`/api/files/read${qs({ path })}`); const d = await post("/api/files/diff-text", { original: cur.content, modified: mine }); d.a_label = "Version sur le disque"; d.b_label = "Votre version"; renderDiff(diffHost, d); } catch (e) { toast(e.message, "error"); } return false; } },
        { label: "Écraser avec ma version", kind: "danger", onClick: async () => { await save(true); return true; } }] });
  }
  async function showDiff() {
    const hostEl = h("div", null, loading());
    modal({ title: "Modifications non enregistrées", size: "wide", body: hostEl, buttons: [{ label: "Fermer", value: true }] });
    try { const d = await post("/api/files/diff-text", { original: saved, modified: getValue() }); renderDiff(hostEl, d); } catch (e) { mount(hostEl, h("div", { class: "alert bad" }, e.message)); }
  }
  async function showHistory() {
    const holder = h("div", null, loading());
    const m = modal({ title: `Historique : ${file.name}`, size: "wide", body: holder, buttons: [{ label: "Comparer avec un autre fichier…", onClick: async () => { const other = await promptDialog({ title: "Comparer avec un autre fichier", label: "Chemin du fichier à comparer", placeholder: "plugins/exemple/config.yml", confirmLabel: "Comparer" }); if (other) { try { const d = await get(`/api/files/diff-files${qs({ a: path, b: other })}`); const dh = h("div"); renderDiff(dh, d); modal({ title: "Comparaison de fichiers", size: "wide", body: dh, buttons: [{ label: "Fermer", value: true }] }); } catch (e) { toast(e.message, "error"); } } return false; } }, { label: "Fermer", value: true }] });
    try {
      const vs = await get(`/api/files/versions${qs({ path })}`);
      mount(holder, vs.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Date"), h("th", null, "Auteur"), h("th", null, "Action"), h("th", { class: "num" }, "Taille"), h("th", null))),
        h("tbody", null, vs.map((v) => h("tr", null, h("td", { title: fmtDate(v.created_at) }, fmtRel(v.created_at)), h("td", null, v.username), h("td", null, badge(v.action)), h("td", { class: "num" }, fmtBytes(v.size)),
          h("td", null, h("div", { class: "actions" }, h("button", { class: "btn sm", onclick: async () => { const dh = h("div", null, loading()); modal({ title: `Version #${v.id} → version actuelle (sur disque)`, size: "wide", body: dh, buttons: [{ label: "Fermer", value: true }] }); try { renderDiff(dh, await get(`/api/files/diff${qs({ path, a: v.id, b: "current" })}`)); } catch (e) { mount(dh, h("div", { class: "alert bad" }, e.message)); } } }, icon("diff"), "Comparer"),
            canWrite ? h("button", { class: "btn sm", onclick: async () => { if (!(await confirmDialog({ title: "Restaurer cette version", message: dirty ? "Vos modifications non enregistrées seront remplacées. La version actuelle du disque est conservée dans l'historique." : "La version actuelle du disque sera conservée dans l'historique.", confirmLabel: "Restaurer" }))) return; try { await post(`/api/files/versions/${v.id}/restore`); const f2 = await get(`/api/files/read${qs({ path })}`); saved = f2.content; mtime = f2.mtime_ns; setValue(f2.content); setDirty(); toast("Version restaurée.", "ok"); m.close(); } catch (e) { toast(e.message, "error"); } } }, "Restaurer") : null)))))))
        : emptyState("history", "Aucune version antérieure", "Une version est conservée à chaque enregistrement."));
    } catch (e) { mount(holder, h("div", { class: "alert bad" }, e.message)); }
  }
  function setValue(v) { if (editor) editor.setValue(v); else ta.value = v; }

  const saveBtn = h("button", { class: "btn primary", disabled: true, onclick: () => save() }, icon("save"), "Enregistrer");
  const revertBtn = h("button", { class: "btn", disabled: true, onclick: async () => { if (await confirmDialog({ title: "Annuler les modifications", message: "Revenir au contenu enregistré ?", confirmLabel: "Annuler les modifications", danger: true })) { setValue(saved); setDirty(); } } }, "Annuler les modifications");
  const compareBtn = h("button", { class: "btn", disabled: true, onclick: showDiff }, icon("diff"), "Voir les changements");
  const findBtn = h("button", { class: "btn", onclick: () => { if (editor) { editor.focus(); editor.getAction("editor.action.startFindReplace").run(); } else toast("La recherche/remplacement avancée nécessite l'éditeur Monaco (Ctrl+F dans le navigateur sinon).", "info"); } }, icon("search"), "Rechercher / remplacer");
  mount(el, h("div", { class: "editor-shell" },
    h("div", { class: "page-head", style: { marginBottom: "10px" } }, h("div", null, h("h1", { style: { fontSize: "18px" } }, file.name), h("p", { class: "mono small" }, "/" + file.path)),
      h("div", { class: "row" }, dirtyBadge, !canWrite ? badge("lecture seule", "info") : null, h("a", { class: "btn", href: `#/files?path=${encodeURIComponent(parent)}` }, icon("folder"), "Fichiers"), h("button", { class: "btn", onclick: showHistory }, icon("history"), "Historique et comparaison"), findBtn, compareBtn, revertBtn, saveBtn)),
    host, status));
  app.setLeaveGuard({ dirty: () => dirty });
  const onKey = (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); } };
  document.addEventListener("keydown", onKey);

  try {
    mon = await loadMonaco();
    if (disposed) return;
    host.replaceChildren();
    mon.languages.json.jsonDefaults.setDiagnosticsOptions({ validate: true, allowComments: false, schemaValidation: "ignore" });
    const dark = () => (currentTheme() === "light" ? "vs" : "vs-dark");
    editor = mon.editor.create(host, { value: file.content, language: file.language, theme: dark(), readOnly: !canWrite, automaticLayout: true, minimap: { enabled: file.content.length > 4000 }, scrollBeyondLastLine: false, fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 13, lineNumbers: "on", renderWhitespace: "selection", tabSize: file.language === "yaml" ? 2 : 4, wordWrap: "off" });
    editor.onDidChangeModelContent(() => setDirty());
    editor.onDidChangeCursorPosition((e) => updateStatus(e.position));
    mon.editor.onDidChangeMarkers(() => updateStatus());
    editor.addCommand(mon.KeyMod.CtrlCmd | mon.KeyCode.KeyS, () => save());
    offTheme = app.bus.on("theme", () => mon.editor.setTheme(dark()));
  } catch (err) {
    host.replaceChildren();
    ta = h("textarea", { spellcheck: "false", "aria-label": `Contenu de ${file.name}`, readonly: !canWrite || null, oninput: () => setDirty(), onkeydown: (e) => { if (e.key === "Tab" && canWrite) { e.preventDefault(); const s = ta.selectionStart; ta.setRangeText("    ", s, ta.selectionEnd, "end"); setDirty(); } } });
    ta.value = file.content; host.append(ta);
    toast(`Éditeur avancé indisponible (${err.message}) : mode texte simple activé.`, "warn");
  }
  setDirty(); updateStatus();
  return () => { disposed = true; document.removeEventListener("keydown", onKey); if (offTheme) offTheme(); if (editor) editor.dispose(); app.setLeaveGuard(null); };
}
