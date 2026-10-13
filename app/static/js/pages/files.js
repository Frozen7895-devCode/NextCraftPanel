// Gestionnaire de fichiers : parcours, opérations, téléversement, ZIP, corbeille, menu contextuel.
import { h, icon, mount, clear, toast, modal, confirmDialog, promptDialog, contextMenu, fmtBytes, fmtDate, fmtRel, emptyState, loading, badge, debounce } from "../util.js";
import { get, post, put, del, upload, downloadPost, downloadUrl, qs, ApiError } from "../api.js";

const join = (a, b) => (a ? `${a}/${b}` : b);
const parentOf = (p) => p.split("/").slice(0, -1).join("/");

export async function render(el, app) {
  const { can, navigate, params } = app;
  const writable = can("files.write"), deletable = can("files.delete");
  let cwd = (params.get("path") || "").replace(/^\/+/, "");
  let sort = "name", order = "asc", data = null, selected = new Set(), searchMode = null, lastIdx = -1;
  const body = h("div"), crumbs = h("div", { class: "crumbs", "aria-label": "Chemin" }), selbar = h("div", { class: "selbar", hidden: true }), progress = h("div", { class: "progress-line", hidden: true }, h("i")), banner = h("div");
  const fileInput = h("input", { type: "file", multiple: true, hidden: true, onchange: (e) => { uploadFiles([...e.target.files], false); e.target.value = ""; } });
  const zipInput = h("input", { type: "file", accept: ".zip,application/zip", hidden: true, onchange: (e) => { uploadFiles([...e.target.files], true); e.target.value = ""; } });
  const zone = h("div", { class: "dropzone card" });
  const searchBox = h("input", { class: "input", type: "search", placeholder: "Rechercher un fichier…", style: { width: "210px" }, "aria-label": "Rechercher un fichier", onkeydown: (e) => { if (e.key === "Enter") doSearch(e.target.value); if (e.key === "Escape") { e.target.value = ""; searchMode = null; draw(); } } });

  async function load(path = cwd) {
    mount(body, loading());
    try { data = await get(`/api/files/list${qs({ path, sort, order })}`); cwd = data.path; selected.clear(); searchMode = null; draw(); history.replaceState(null, "", `#/files${cwd ? "?path=" + encodeURIComponent(cwd) : ""}`); }
    catch (e) { mount(body, h("div", { class: "alert bad", style: { margin: "14px" } }, icon("alert"), e.message, " ", h("button", { class: "btn sm", onclick: () => { cwd = ""; load(""); } }, "Retour à la racine"))); }
  }
  const go = (p) => { cwd = p; load(p); };

  function drawCrumbs() {
    const parts = cwd ? cwd.split("/") : [];
    const items = [h("button", { onclick: () => go(""), "aria-label": "Racine du serveur" }, icon("home"), " ", data.root_name || "serveur")];
    parts.forEach((p, i) => { items.push(h("span", { class: "sep" }, "/"), h("button", { onclick: () => go(parts.slice(0, i + 1).join("/")) }, p)); });
    mount(crumbs, items);
  }
  function sortHead(label, key, cls = "") {
    const on = sort === key;
    return h("th", { class: `sortable ${cls}`, onclick: () => { if (sort === key) order = order === "asc" ? "desc" : "asc"; else { sort = key; order = "asc"; } load(); }, "aria-sort": on ? (order === "asc" ? "ascending" : "descending") : "none" }, label, on ? (order === "asc" ? " ▴" : " ▾") : "");
  }
  function entryRow(e, showPath) {
    const sel = selected.has(e.path);
    const chk = h("input", { type: "checkbox", checked: sel || null, "aria-label": `Sélectionner ${e.name}`, onclick: (ev) => ev.stopPropagation(), onchange: () => toggle(e.path) });
    if (sel) chk.checked = true;
    const tr = h("tr", { class: sel ? "sel" : "", dataset: { path: e.path },
      onclick: (ev) => { if (ev.target.closest("button,a,input")) return; if (ev.ctrlKey || ev.metaKey) toggle(e.path); else { selected = new Set([e.path]); drawRows(); } },
      oncontextmenu: (ev) => { ev.preventDefault(); if (!selected.has(e.path)) { selected = new Set([e.path]); drawRows(); } menuFor(ev.clientX, ev.clientY, e); } },
      h("td", null, chk),
      h("td", null, h("div", { class: `fname ${e.is_dir ? "dir" : ""}`, onclick: () => open(e), title: e.path, role: "link", tabindex: 0, onkeydown: (ev) => { if (ev.key === "Enter") open(e); } }, icon(e.is_dir ? "folder" : "file"), h("span", null, e.name), e.is_symlink ? badge("lien", "violet") : null),
        showPath ? h("div", { class: "small faint" }, parentOf(e.path) || "/") : null),
      h("td", { class: "num hide-sm" }, e.is_dir ? "—" : fmtBytes(e.size)), h("td", { class: "hide-sm nowrap", title: fmtDate(e.mtime) }, e.mtime ? fmtRel(e.mtime) : "—"),
      h("td", null, h("div", { class: "actions" }, h("button", { class: "btn ghost sm icon-only", "aria-label": `Actions pour ${e.name}`, onclick: (ev) => { const r = ev.currentTarget.getBoundingClientRect(); if (!selected.has(e.path)) { selected = new Set([e.path]); drawRows(); } menuFor(r.left - 160, r.bottom + 2, e); } }, icon("dots")))));
    return tr;
  }
  function toggle(path) { selected.has(path) ? selected.delete(path) : selected.add(path); drawRows(); }
  const list = () => (searchMode ? searchMode.results : data.entries);
  let tbodyEl = null, headChk = null;
  function drawRows() {
    if (!tbodyEl) return;
    clear(tbodyEl); const rows = list();
    rows.forEach((e) => tbodyEl.append(entryRow(e, !!searchMode)));
    if (!rows.length) tbodyEl.append(h("tr", null, h("td", { colspan: 5 }, emptyState("folder", searchMode ? "Aucun résultat" : "Dossier vide", searchMode ? null : writable ? "Déposez des fichiers ici ou utilisez « Téléverser »." : null))));
    if (headChk) headChk.checked = rows.length > 0 && rows.every((e) => selected.has(e.path));
    drawSelbar();
  }
  function drawSelbar() {
    selbar.hidden = selected.size === 0;
    if (!selected.size) return;
    const paths = [...selected];
    mount(selbar, h("b", null, `${paths.length} sélectionné(s)`),
      h("button", { class: "btn sm", onclick: () => downloadSel(paths) }, icon("download"), paths.length === 1 && !findEntry(paths[0])?.is_dir ? "Télécharger" : "Télécharger (ZIP)"),
      writable ? h("button", { class: "btn sm", onclick: () => transfer(paths, false) }, icon("copy"), "Copier vers…") : null,
      writable ? h("button", { class: "btn sm", onclick: () => transfer(paths, true) }, icon("move"), "Déplacer vers…") : null,
      deletable ? h("button", { class: "btn sm danger", onclick: () => remove(paths) }, icon("trash"), "Supprimer") : null,
      h("button", { class: "btn ghost sm right", onclick: () => { selected.clear(); drawRows(); } }, "Désélectionner"));
  }
  const findEntry = (p) => list().find((e) => e.path === p);
  function draw() {
    drawCrumbs();
    mount(banner, data.writable_blocked ? h("div", { class: "alert warn", style: { margin: "12px" } }, icon("lock"), `Écritures bloquées : ${data.writable_blocked}.`) : null);
    headChk = h("input", { type: "checkbox", "aria-label": "Tout sélectionner", onchange: (e) => { if (e.target.checked) list().forEach((x) => selected.add(x.path)); else selected.clear(); drawRows(); } });
    tbodyEl = h("tbody");
    mount(body, searchMode ? h("div", { class: "row", style: { padding: "10px 14px" } }, badge(`${searchMode.results.length} résultat(s)${searchMode.truncated ? " (tronqués)" : ""} pour « ${searchMode.q} »`, "info"), h("button", { class: "btn sm", onclick: () => { searchMode = null; searchBox.value = ""; draw(); } }, "Fermer la recherche")) : null,
      h("div", { class: "table-wrap" }, h("table", { class: "t ftable" }, h("thead", null, h("tr", null, h("th", null, headChk), sortHead("Nom", "name"), sortHead("Taille", "size", "num hide-sm"), sortHead("Modifié", "date", "hide-sm"), h("th", null))), tbodyEl)),
      data.truncated ? h("div", { class: "alert warn", style: { margin: "12px" } }, "Dossier très volumineux : seuls les premiers éléments sont affichés. Utilisez la recherche.") : null);
    drawRows();
  }

  // ---------- actions ----------
  function open(e) {
    if (e.is_dir) { go(e.path); return; }
    if (e.editable) navigate(`#/editor?path=${encodeURIComponent(e.path)}`);
    else { toast(`« ${e.name} » n'est pas modifiable comme texte (binaire ou trop volumineux) : téléchargement.`, "info"); downloadUrl(`/api/files/download${qs({ path: e.path })}`); }
  }
  function menuFor(x, y, e) {
    const many = selected.size > 1, paths = [...selected];
    const items = [];
    if (!many) {
      items.push({ label: e.is_dir ? "Ouvrir" : e.editable ? (writable ? "Modifier" : "Afficher") : "Télécharger", icon: e.is_dir ? "folder" : "edit", onClick: () => open(e) });
      if (!e.is_dir && e.editable) items.push({ label: "Télécharger", icon: "download", onClick: () => downloadSel([e.path]) });
      if (writable && e.name.toLowerCase().endsWith(".zip")) items.push({ label: "Extraire l'archive…", icon: "box", onClick: () => extract(e) });
      items.push("-");
      if (writable) items.push({ label: "Renommer", icon: "edit", onClick: () => rename(e) });
    } else items.push({ label: "Télécharger (ZIP)", icon: "download", onClick: () => downloadSel(paths) });
    if (e.is_dir && !many) items.push({ label: "Télécharger (ZIP)", icon: "download", onClick: () => downloadSel([e.path]) });
    if (writable) items.push({ label: "Copier vers…", icon: "copy", onClick: () => transfer(paths, false) }, { label: "Déplacer vers…", icon: "move", onClick: () => transfer(paths, true) });
    if (deletable) items.push("-", { label: "Supprimer", icon: "trash", danger: true, onClick: () => remove(paths) });
    contextMenu(x, y, items);
  }
  async function guard(fn) { try { return await fn(); } catch (e) { toast(e.message, "error"); return null; } }
  async function newItem(type) {
    const name = await promptDialog({ title: type === "dir" ? "Nouveau dossier" : "Nouveau fichier", label: "Nom", placeholder: type === "dir" ? "mon-dossier" : "fichier.txt", confirmLabel: "Créer" });
    if (!name) return;
    if (await guard(() => post("/api/files/create", { path: cwd, name, type }))) { toast(`« ${name} » créé.`, "ok"); load(); }
  }
  async function rename(e) {
    const name = await promptDialog({ title: "Renommer", label: "Nouveau nom", value: e.name, confirmLabel: "Renommer" });
    if (!name || name === e.name) return;
    if (await guard(() => post("/api/files/rename", { path: e.path, new_name: name }))) { toast("Renommé.", "ok"); searchMode ? doSearch(searchMode.q) : load(); }
  }
  async function remove(paths) {
    const names = paths.map((p) => p.split("/").pop());
    const ok = await confirmDialog({ title: "Supprimer", danger: true, confirmLabel: "Supprimer", message: h("div", { class: "stack" }, h("div", null, `Supprimer ${paths.length} élément(s) ?`), h("div", { class: "small mono", style: { maxHeight: "120px", overflow: "auto" } }, names.join(", ")), h("div", { class: "alert" }, icon("info"), "Les éléments sont déplacés dans la corbeille : ils pourront être restaurés pendant la durée de conservation configurée.")) });
    if (!ok) return;
    const r = await guard(() => post("/api/files/delete", { paths, confirm: true }));
    if (r) { toast(`${r.removed.length} élément(s) mis à la corbeille.`, "ok"); searchMode ? doSearch(searchMode.q) : load(); }
  }
  async function downloadSel(paths) {
    const f = paths.length === 1 ? findEntry(paths[0]) : null;
    if (f && !f.is_dir) { downloadUrl(`/api/files/download${qs({ path: f.path })}`); return; }
    try { toast("Préparation de l'archive ZIP…", "info"); await downloadPost("/api/files/download-zip", { paths }, "selection.zip"); } catch (e) { toast(e.message, "error"); }
  }
  async function pickFolder(title, start) {
    return new Promise((resolve) => {
      let cur = start; const listEl = h("div", { class: "list" }), path = h("div", { class: "mono small" });
      async function nav(p) { try { const d = await get(`/api/files/list${qs({ path: p })}`); cur = d.path; mount(path, "/" + cur); mount(listEl, d.parent !== null ? h("div", null, h("button", { class: "btn ghost sm", onclick: () => nav(d.parent) }, icon("up"), "Dossier parent")) : null, d.entries.filter((x) => x.is_dir).map((x) => h("div", null, h("button", { class: "btn ghost sm", onclick: () => nav(x.path) }, icon("folder"), x.name))), d.entries.some((x) => x.is_dir) ? null : h("div", { class: "muted small" }, "Aucun sous-dossier.")); } catch (e) { toast(e.message, "error"); } }
      const m = modal({ title, body: h("div", { class: "stack" }, h("div", { class: "row" }, "Destination : ", path), h("div", { style: { maxHeight: "300px", overflow: "auto" } }, listEl)), onClose: (r) => resolve(typeof r === "string" ? r : null),
        buttons: [{ label: "Annuler", value: null }, { label: "Choisir ce dossier", kind: "primary", onClick: (mm) => { mm.close(cur); return false; } }] });
      nav(start);
    });
  }
  async function transfer(paths, move) {
    const dest = await pickFolder(move ? "Déplacer vers…" : "Copier vers…", cwd);
    if (dest === null) return;
    const url = move ? "/api/files/move" : "/api/files/copy";
    try { await post(url, { paths, dest }); }
    catch (e) {
      if (e.data && e.data.conflict) { if (!(await confirmDialog({ title: "Éléments existants", message: `${e.message}. Les remplacer ? (les anciennes versions de fichiers restent dans l'historique)`, confirmLabel: "Remplacer", danger: true }))) return; if (!(await guard(() => post(url, { paths, dest, overwrite: true })))) return; }
      else { toast(e.message, "error"); return; }
    }
    toast(move ? "Déplacé." : "Copié.", "ok"); load();
  }
  async function extract(e) {
    const dest = await promptDialog({ title: "Extraire l'archive", label: "Dossier de destination (relatif à la racine du serveur)", value: join(cwd, e.name.replace(/\.zip$/i, "")), confirmLabel: "Extraire", hint: "Les chemins malveillants, liens symboliques et archives piégées sont refusés." });
    if (dest === null) return;
    try { const r = await post("/api/files/extract", { path: e.path, dest }); toast(`${r.extracted} fichier(s) extrait(s)${r.skipped_links ? ` (${r.skipped_links} lien(s) ignoré(s))` : ""}.`, "ok"); load(); }
    catch (er) {
      if (er.data && er.data.conflict && await confirmDialog({ title: "Fichiers existants", message: `${er.message}. Les remplacer ?`, confirmLabel: "Remplacer", danger: true })) { if (await guard(() => post("/api/files/extract", { path: e.path, dest, overwrite: true }))) { toast("Archive extraite.", "ok"); load(); } }
      else if (!(er.data && er.data.conflict)) toast(er.message, "error");
    }
  }
  async function uploadFiles(files, doExtract, overwrite = false) {
    if (!files.length) return;
    const fd = new FormData(); fd.append("path", cwd); fd.append("overwrite", overwrite ? "true" : "false"); fd.append("extract", doExtract ? "true" : "false");
    files.forEach((f) => fd.append("files", f, f.name));
    progress.hidden = false; const bar = progress.firstChild; bar.style.width = "0";
    try {
      const r = await upload("/api/files/upload", fd, (p) => { bar.style.width = Math.round(p * 100) + "%"; });
      const ok = r.results.filter((x) => x.ok), bad = r.results.filter((x) => !x.ok);
      if (ok.length) toast(`${ok.length} fichier(s) téléversé(s).`, "ok");
      const conflicts = bad.filter((x) => x.status === 409);
      bad.filter((x) => x.status !== 409).forEach((x) => toast(`${x.name} : ${x.error}`, "error"));
      if (conflicts.length && await confirmDialog({ title: "Fichiers existants", message: `${conflicts.map((c) => c.name).join(", ")} existe(nt) déjà. Remplacer ? (l'ancienne version sera conservée dans l'historique)`, confirmLabel: "Remplacer", danger: true })) { progress.hidden = true; return uploadFiles(files.filter((f) => conflicts.some((c) => c.name === f.name)), doExtract, true); }
    } catch (e) {
      if (e.status === 409 && !overwrite && files.length === 1 && await confirmDialog({ title: "Fichier existant", message: `« ${files[0].name} » existe déjà. Le remplacer ? (l'ancienne version sera conservée dans l'historique)`, confirmLabel: "Remplacer", danger: true })) { progress.hidden = true; return uploadFiles(files, doExtract, true); }
      else toast(e.message, "error");
    } finally { progress.hidden = true; }
    load();
  }
  async function doSearch(q) {
    q = q.trim(); if (q.length < 2) { toast("Saisissez au moins 2 caractères.", "warn"); return; }
    try { const r = await get(`/api/files/search${qs({ q, path: cwd })}`); searchMode = { q, results: r.results.map((x) => ({ ...x, mtime: null, editable: !x.is_dir && !/\.(jar|zip|dat|mca|png|gz)$/i.test(x.name) })), truncated: r.truncated }; selected.clear(); draw(); }
    catch (e) { toast(e.message, "error"); }
  }
  async function showTrash() {
    const holder = h("div");
    async function refresh() {
      mount(holder, loading());
      try {
        const items = await get("/api/files/trash");
        mount(holder, items.length ? h("div", { class: "table-wrap" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Élément"), h("th", { class: "num" }, "Taille"), h("th", null, "Supprimé"), h("th", null, "Expire"), h("th", null))),
          h("tbody", null, items.map((t) => h("tr", null, h("td", null, h("div", { class: "fname" }, icon(t.is_dir ? "folder" : "file"), h("span", { title: t.original_path }, t.original_path))), h("td", { class: "num" }, fmtBytes(t.size)),
            h("td", { class: "small" }, `${fmtRel(t.deleted_at)} · ${t.deleted_by}`), h("td", { class: "small" }, fmtRel(t.expires_at)),
            h("td", null, h("div", { class: "actions" }, writable ? h("button", { class: "btn sm", onclick: async () => { if (await guard(() => post(`/api/files/trash/${t.id}/restore`))) { toast("Restauré.", "ok"); refresh(); load(); } } }, "Restaurer") : null,
              h("button", { class: "btn sm danger", onclick: async () => { if (await confirmDialog({ title: "Supprimer définitivement", message: `« ${t.original_path} » sera effacé sans possibilité de restauration.`, danger: true, confirmLabel: "Supprimer définitivement" })) { await guard(() => del(`/api/files/trash/${t.id}`)); refresh(); } } }, icon("trash")))))))))
          : emptyState("trash", "La corbeille est vide"));
      } catch (e) { mount(holder, h("div", { class: "alert bad" }, e.message)); }
    }
    modal({ title: "Corbeille", size: "wide", body: holder, buttons: [{ label: "Vider la corbeille", kind: "danger", onClick: async () => { if (await confirmDialog({ title: "Vider la corbeille", message: "Tous les éléments seront définitivement supprimés.", danger: true, confirmLabel: "Tout supprimer" })) { await guard(() => del("/api/files/trash")); refresh(); } return false; } }, { label: "Fermer", value: true }] });
    refresh();
  }

  // ---------- squelette ----------
  zone.append(h("div", { class: "row", style: { padding: "12px 14px", borderBottom: "1px solid var(--line)" } },
    h("button", { class: "btn ghost sm icon-only", "aria-label": "Dossier parent", onclick: () => data && data.parent !== null && go(data.parent) }, icon("up")), crumbs,
    h("div", { class: "right row" }, searchBox, h("button", { class: "btn sm icon-only", "aria-label": "Actualiser", onclick: () => load() }, icon("refresh")))),
    h("div", { class: "row", style: { padding: "10px 14px", borderBottom: "1px solid var(--line)" } },
      writable ? [h("button", { class: "btn sm", onclick: () => newItem("file") }, icon("plus"), "Nouveau fichier"), h("button", { class: "btn sm", onclick: () => newItem("dir") }, icon("folder"), "Nouveau dossier"),
        h("button", { class: "btn sm primary", onclick: () => fileInput.click() }, icon("upload"), "Téléverser"), h("button", { class: "btn sm", onclick: () => zipInput.click() }, icon("box"), "Importer un ZIP et l'extraire")] : h("span", { class: "small muted" }, "Lecture seule : vous ne pouvez pas modifier les fichiers."),
      deletable ? h("button", { class: "btn sm right", onclick: showTrash }, icon("trash"), "Corbeille") : null), progress, banner, selbar, body, fileInput, zipInput);
  let dragDepth = 0;
  if (writable) {
    zone.addEventListener("dragenter", (e) => { if ([...e.dataTransfer.types].includes("Files")) { dragDepth++; zone.classList.add("drag"); } });
    zone.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) zone.classList.remove("drag"); });
    zone.addEventListener("dragover", (e) => { if ([...e.dataTransfer.types].includes("Files")) e.preventDefault(); });
    zone.addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; zone.classList.remove("drag"); uploadFiles([...e.dataTransfer.files], false); });
  }
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Fichiers du serveur"), h("p", null, "Navigation limitée au dossier du serveur. Chaque modification conserve la version précédente."))), zone);
  await load(cwd);
}
