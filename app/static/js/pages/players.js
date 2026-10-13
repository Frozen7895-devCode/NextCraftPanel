// Gestion des joueurs : en ligne, historique, liste blanche, opérateurs, bannis, IP bannies.
import { h, icon, mount, toast, confirmDialog, promptDialog, fmtDate, fmtRel, emptyState, loading, badge, statusPill, field } from "../util.js";
import { get, post } from "../api.js";

export async function render(el, app) {
  const { can, bus } = app;
  let data = await get("/api/players");
  let tab = "online";
  const body = h("div"), banner = h("div");
  const offs = [], timers = [];

  async function act(action, target, { reason, ask, label } = {}) {
    if (ask) { reason = await promptDialog({ title: ask.title, label: "Motif (facultatif)", placeholder: "ex. comportement inapproprié", confirmLabel: ask.confirm }); if (reason === null) return; }
    try { const r = await post("/api/players/action", { action, target, reason }); toast(r.message, "ok"); }
    catch (e) {
      if (e.confirmRequired) {
        if (!(await confirmDialog({ title: "Confirmer l'action", message: `${label || action} : ${target}`, confirmLabel: "Confirmer", danger: true }))) return;
        try { const r = await post("/api/players/action", { action, target, reason, confirm: true }); toast(r.message, "ok"); } catch (e2) { toast(e2.message, "error"); return; }
      } else { toast(e.message, "error"); return; }
    }
    setTimeout(refresh, 700);
  }
  const running = () => data.server_status === "running";
  function addBar(placeholder, label, action, perm, extra) {
    if (!can(perm)) return null;
    const i = h("input", { class: "input", placeholder, maxlength: 17, style: { maxWidth: "240px" }, "aria-label": placeholder, onkeydown: (e) => { if (e.key === "Enter") go(); } });
    const go = () => { if (i.value.trim()) { act(action, i.value.trim(), extra); i.value = ""; } };
    return h("div", { class: "row", style: { marginBottom: "12px" } }, i, h("button", { class: "btn primary", onclick: go, disabled: !running() }, icon("plus"), label), !running() ? h("span", { class: "small muted" }, "Le serveur doit être en ligne pour ajouter (UUID requis).") : null);
  }
  const table = (cols, rows, empty) => rows.length ? h("div", { class: "table-wrap card" }, h("table", { class: "t" }, h("thead", null, h("tr", null, cols.map((c) => h("th", null, c)))), h("tbody", null, rows))) : h("div", { class: "card" }, emptyState("users", empty));
  const head = (n) => h("span", { class: "row", style: { gap: "10px", flexWrap: "nowrap" } }, h("span", { class: "mc-head" }, (n || "?")[0].toUpperCase()), h("b", null, n));
  const listNote = (l) => (l.error ? h("div", { class: "alert bad", style: { marginBottom: "10px" } }, icon("alert"), l.error) : null);

  const tabs = () => [["online", "En ligne", data.online.length], ["history", "Historique", data.history.length], ["whitelist", "Liste blanche", data.whitelist.items.length], ["ops", "Opérateurs", data.ops.items.length], ["banned", "Bannis", data.banned.items.length],
    ...(can("players.ips") ? [["ips", "IP bannies", data.banned_ips.items.length]] : [])];
  function draw() {
    mount(banner, data.server_status !== "running" ? h("div", { class: "alert warn", style: { marginBottom: "14px" } }, icon("alert"), "Le serveur n'est pas en ligne : la liste des joueurs connectés est indisponible. Les listes (liste blanche, opérateurs, bannis) proviennent des fichiers du serveur.") : null);
    const tb = h("div", { class: "tabs", role: "tablist" }, tabs().map(([id, label, n]) => h("button", { class: id === tab ? "on" : "", role: "tab", "aria-selected": id === tab, onclick: () => { tab = id; draw(); } }, `${label} `, h("span", { class: "badge", style: { marginLeft: "4px" } }, n))));
    let content;
    if (tab === "online") {
      content = table(["Joueur", "Connecté", "Actions"], data.online.map((p) => h("tr", null, h("td", null, head(p.name)), h("td", null, fmtRel(p.since)),
        h("td", null, h("div", { class: "actions" }, can("players.manage") ? [h("button", { class: "btn sm", onclick: () => act("kick", p.name, { ask: { title: `Expulser ${p.name}`, confirm: "Expulser" } }) }, "Expulser"), h("button", { class: "btn sm danger", onclick: () => act("ban", p.name, { ask: { title: `Bannir ${p.name}`, confirm: "Bannir" }, label: "Bannir" }) }, "Bannir")] : null,
          can("players.op") ? h("button", { class: "btn sm", onclick: () => act("op", p.name, { label: "Donner le statut opérateur" }) }, "OP") : null)))),
        data.server_status === "running" ? "Aucun joueur connecté." : "Serveur hors ligne : liste indisponible.");
    } else if (tab === "history") {
      content = table(["Pseudo", "UUID", "Statut", "Première connexion", "Dernière connexion"], data.history.map((p) => h("tr", null, h("td", null, head(p.name)), h("td", { class: "mono small" }, p.uuid || h("span", { class: "faint" }, "indisponible")),
        h("td", null, p.online ? badge("en ligne", "ok") : badge("hors ligne")), h("td", { class: "small" }, fmtDate(p.first_seen)), h("td", { class: "small", title: fmtDate(p.last_seen) }, fmtRel(p.last_seen)))), "Aucun joueur enregistré depuis l'installation du panel.");
    } else if (tab === "whitelist") {
      content = h("div", null, listNote(data.whitelist), addBar("Pseudo à autoriser", "Ajouter à la liste blanche", "whitelist_add", "players.manage"),
        table(["Joueur", "UUID", ""], data.whitelist.items.map((p) => h("tr", null, h("td", null, head(p.name)), h("td", { class: "mono small" }, p.uuid || ""), h("td", null, can("players.manage") ? h("div", { class: "actions" }, h("button", { class: "btn sm danger", onclick: () => act("whitelist_remove", p.name, { label: "Retirer de la liste blanche" }) }, "Retirer")) : null))), data.whitelist.available ? "La liste blanche est vide." : "Fichier whitelist.json absent."));
    } else if (tab === "ops") {
      content = h("div", null, listNote(data.ops), addBar("Pseudo du futur opérateur", "Donner le statut OP", "op", "players.op"),
        table(["Joueur", "Niveau", "UUID", ""], data.ops.items.map((p) => h("tr", null, h("td", null, head(p.name)), h("td", null, badge(`niveau ${p.level ?? "?"}`, "violet")), h("td", { class: "mono small" }, p.uuid || ""), h("td", null, can("players.op") ? h("div", { class: "actions" }, h("button", { class: "btn sm danger", onclick: () => act("deop", p.name, { label: "Retirer le statut opérateur" }) }, "Retirer OP")) : null))), data.ops.available ? "Aucun opérateur." : "Fichier ops.json absent."));
    } else if (tab === "banned") {
      content = h("div", null, listNote(data.banned), addBar("Pseudo à bannir", "Bannir", "ban", "players.manage", { ask: { title: "Bannir un joueur", confirm: "Bannir" }, label: "Bannir" }),
        table(["Joueur", "Motif", "Banni le", "Par", ""], data.banned.items.map((p) => h("tr", null, h("td", null, head(p.name)), h("td", null, p.reason || ""), h("td", { class: "small" }, p.created || ""), h("td", { class: "small" }, p.source || ""), h("td", null, can("players.manage") ? h("div", { class: "actions" }, h("button", { class: "btn sm", onclick: () => act("unban", p.name, { label: "Débannir" }) }, "Débannir")) : null))), "Aucun joueur banni."));
    } else {
      content = h("div", null, listNote(data.banned_ips), addBar("Adresse IP ou pseudo", "Bannir l'IP", "ban_ip", "players.ips", { ask: { title: "Bannir une adresse IP", confirm: "Bannir" }, label: "Bannir l'IP" }),
        table(["Adresse IP", "Motif", "Banni le", ""], data.banned_ips.items.map((p) => h("tr", null, h("td", { class: "mono" }, p.ip), h("td", null, p.reason || ""), h("td", { class: "small" }, p.created || ""), h("td", null, h("div", { class: "actions" }, h("button", { class: "btn sm", onclick: () => act("unban_ip", p.ip, { label: "Débannir l'IP" }) }, "Débannir"))))), "Aucune IP bannie."));
    }
    mount(body, tb, content);
  }
  async function refresh() { try { data = await get("/api/players"); draw(); } catch { /* garde l'affichage */ } }
  mount(el, h("div", { class: "page-head" }, h("div", null, h("h1", null, "Joueurs"), h("p", null, "Les actions passent par les commandes Minecraft du serveur en cours d'exécution.")), h("div", { class: "row" }, statusPill(data.server_status), data.max_players ? badge(`${data.online.length}/${data.max_players} connectés`) : null)), banner, body);
  draw();
  offs.push(bus.on("status", refresh)); timers.push(setInterval(refresh, 20000));
  return () => { offs.forEach((o) => o()); timers.forEach(clearInterval); };
}
