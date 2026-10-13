// Rendu côte à côte du comparateur de configurations (lignes ajoutées / supprimées / modifiées).
import { h } from "./util.js";

export function renderDiff(container, data) {
  container.replaceChildren();
  const summary = h("div", { class: "row small", style: { marginBottom: "8px" } },
    h("span", { class: "badge ok" }, `+${data.added} ajoutée(s)`), h("span", { class: "badge bad" }, `−${data.removed} supprimée(s)`), h("span", { class: "badge warn" }, `${data.changed} modifiée(s)`),
    data.a_label ? h("span", { class: "muted" }, `${data.a_label} → ${data.b_label}`) : null);
  container.append(summary);
  if (data.identical) { container.append(h("div", { class: "alert ok" }, "Aucune différence : les deux contenus sont identiques.")); return; }
  const tbody = h("tbody");
  for (const r of data.rows) {
    if (r.type === "skip") { tbody.append(h("tr", { class: "skip" }, h("td", { colspan: 4 }, "⋯"))); continue; }
    tbody.append(h("tr", { class: r.type },
      h("td", { class: "no" }, r.a_no ?? ""), h("td", { class: "a" }, r.type === "add" ? "" : r.a),
      h("td", { class: "no" }, r.b_no ?? ""), h("td", { class: "b" }, r.type === "del" ? "" : r.b)));
  }
  container.append(h("div", { class: "diff" }, h("table", null, h("colgroup", null, h("col", { style: { width: "44px" } }), h("col"), h("col", { style: { width: "44px" } }), h("col")), tbody)));
}
