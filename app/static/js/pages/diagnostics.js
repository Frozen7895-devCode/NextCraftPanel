// Diagnostic : analyse des logs par règles, indicateur de santé détaillé, rapports de crash, aide IA optionnelle.
import { h, icon, mount, toast, modal, confirmDialog, fmtDate, fmtRel, fmtDuration, emptyState, badge, loading } from "../util.js";
import { get, post, downloadUrl } from "../api.js";

const SEV_KIND = { error: "bad", warn: "warn", info: "info" };
const SEV_LABEL = { error: "Erreur", warn: "Avertissement", info: "Info" };

function healthCard(hl) {
  const color = { good: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)" }[hl.level];
  const components = hl.components.map((c) => h("div", null,
    h("div", { class: "row small", style: { justifyContent: "space-between", flexWrap: "nowrap" } }, h("b", null, c.name), h("span", { class: "mono muted" }, `${c.points}/${c.max}`)),
    h("div", { class: "small muted" }, c.reasons.join(" "))));
  const summary = h("div", null, h("b", null, `${hl.label} (${hl.score}/100)`),
    h("div", { class: "small muted" }, hl.problems.length ? "Points perdus, avec leurs raisons :" : "Aucun point perdu."));
  const ring = h("div", { class: "ring", style: { "--p": hl.score, "--c": color }, "data-v": hl.score });
  return h("div", { class: "card" },
    h("div", { class: "card-h" }, h("h2", null, "Indicateur de santé")),
    h("div", { class: "card-b" }, h("div", { class: "row", style: { alignItems: "flex-start", gap: "20px" } }, ring,
      h("div", { class: "grow stack", style: { gap: "10px", minWidth: "260px" } }, summary, components))));
}

function findingCard(f) {
  const solutions = h("ul", { style: { margin: "4px 0 0 18px", padding: 0 } }, f.solutions.map((s) => h("li", null, s)));
  const evidence = h("pre", { class: "console", style: { margin: "8px 0 0", padding: "8px 12px", whiteSpace: "pre-wrap", borderRadius: "8px" } }, f.evidence.join("\n"));
  return h("div", { class: "card", style: { background: "var(--panel-2)" } }, h("div", { class: "card-b stack", style: { gap: "10px" } },
    h("div", { class: "row" }, badge(SEV_LABEL[f.severity], SEV_KIND[f.severity]), h("h3", null, f.title), badge(`${f.count} occurrence(s)`), f.names.length ? badge(f.names.join(", "), "violet") : null),
    h("div", { class: "muted" }, f.explanation),
    h("div", null, h("div", { class: "lbl-t" }, "Solutions proposées"), solutions),
    h("details", null, h("summary", { class: "small muted", style: { cursor: "pointer" } }, `Lignes détectées (${f.sources.join(", ")})`), evidence)));
}

function crashTable(crashes) {
  if (!crashes.length) return emptyState("check", "Aucun crash enregistré");
  const rows = crashes.map((c) => h("tr", null,
    h("td", { class: "small", title: fmtDate(c.occurred_at) }, fmtRel(c.occurred_at)),
    h("td", { class: "mono" }, c.exit_code),
    h("td", null, fmtDuration(c.uptime_s)),
    h("td", { class: "small" }, c.summary),
    h("td", { class: "small" }, c.restart_info),
    h("td", null, h("button", { class: "btn sm", onclick: () => downloadUrl(`/api/server/crashes/${c.id}/report`) }, icon("download"), "Rapport"))));
  const head = h("thead", null, h("tr", null, h("th", null, "Date"), h("th", null, "Code"), h("th", null, "Durée"), h("th", null, "Résumé"), h("th", null, "Redémarrage auto"), h("th", null)));
  return h("div", { class: "table-wrap" }, h("table", { class: "t" }, head, h("tbody", null, rows)));
}

export async function render(el, app) {
  const { can } = app;
  const host = h("div", { class: "stack", style: { gap: "16px" } });
  let aiAvailable = false;
  if (can("accounts.manage")) { try { aiAvailable = (await get("/api/settings/ai")).enabled; } catch { /* non configurée */ } }

  async function askAi() {
    const message = h("div", { class: "stack" },
      h("div", null, "Des extraits de vos journaux (adresses IP, e-mails et chemins personnels masqués) seront envoyés au service IA configuré, avec la clé API que vous avez fournie."),
      h("div", { class: "small muted" }, "Rien n'est envoyé automatiquement : cette action est explicite et journalisée."));
    if (!(await confirmDialog({ title: "Envoyer des extraits à un service IA externe", message, confirmLabel: "Envoyer et analyser" }))) return;
    toast("Analyse IA en cours…", "info");
    try {
      const r = await post("/api/diagnostics/ai", { confirm: true });
      modal({ title: "Explication de l'aide IA", size: "wide", body: h("div", { style: { whiteSpace: "pre-wrap" } }, r.text), buttons: [{ label: "Fermer", value: true }] });
    } catch (e) { toast(e.message, "error"); }
  }

  async function run() {
    mount(host, loading("Analyse des journaux en cours…"));
    let r;
    try { r = await get("/api/diagnostics"); } catch (e) { mount(host, h("div", { class: "alert bad" }, e.message)); return; }
    const sources = Object.entries(r.sources).map(([k, v]) => `${k} (${v} lignes)`).join(" · ");
    const findings = r.clean
      ? h("div", { class: "alert ok" }, icon("check"), "Aucun problème connu détecté dans les journaux analysés.")
      : r.findings.map(findingCard);
    const analysis = h("div", { class: "card" },
      h("div", { class: "card-h" }, h("h2", null, "Analyse des journaux"),
        h("div", { class: "row" }, h("span", { class: "small muted" }, sources), h("button", { class: "btn sm", onclick: run }, icon("refresh"), "Relancer l'analyse"))),
      h("div", { class: "card-b stack" }, findings),
      aiAvailable && !r.clean ? h("div", { class: "card-b" }, h("button", { class: "btn", onclick: askAi }, icon("zap"), "Expliquer avec l'aide IA (envoi d'extraits de logs)")) : null);
    const crashes = can("diagnostics.view")
      ? h("div", { class: "card" }, h("div", { class: "card-h" }, h("h2", null, "Rapports de crash")), h("div", { class: "card-b" }, crashTable(r.crashes)))
      : null;
    mount(host, healthCard(r.health), analysis, crashes);
  }

  mount(el,
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Diagnostic et rapports"),
      h("p", null, "Détection des erreurs Java, plantages, conflits de plugins, problèmes de mémoire, de configuration et de chargement des mondes — sans service externe."))),
    host);
  await run();
}
