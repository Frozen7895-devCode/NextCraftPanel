"""Comparaison ligne à ligne de deux textes (comparateur de configurations)."""
from __future__ import annotations

import difflib
from typing import Any

MAX_LINES = 20000


def side_by_side(a: str, b: str, context: int = 3) -> dict[str, Any]:
    al, bl = a.splitlines(), b.splitlines()
    if len(al) > MAX_LINES or len(bl) > MAX_LINES:
        raise ValueError(f"Fichiers trop longs pour la comparaison (maximum {MAX_LINES} lignes).")
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    rows: list[dict[str, Any]] = []
    added = removed = changed = 0
    for gi, group in enumerate(sm.get_grouped_opcodes(context)):
        if gi:
            rows.append({"type": "skip"})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows += [{"type": "equal", "a_no": i + 1, "b_no": j + 1, "a": al[i], "b": bl[j]}
                         for i, j in zip(range(i1, i2), range(j1, j2))]
            elif tag == "delete":
                removed += i2 - i1
                rows += [{"type": "del", "a_no": i + 1, "b_no": None, "a": al[i], "b": ""} for i in range(i1, i2)]
            elif tag == "insert":
                added += j2 - j1
                rows += [{"type": "add", "a_no": None, "b_no": j + 1, "a": "", "b": bl[j]} for j in range(j1, j2)]
            else:  # replace
                n = min(i2 - i1, j2 - j1)
                changed += n
                rows += [{"type": "change", "a_no": i1 + k + 1, "b_no": j1 + k + 1, "a": al[i1 + k], "b": bl[j1 + k]}
                         for k in range(n)]
                for i in range(i1 + n, i2):
                    removed += 1
                    rows.append({"type": "del", "a_no": i + 1, "b_no": None, "a": al[i], "b": ""})
                for j in range(j1 + n, j2):
                    added += 1
                    rows.append({"type": "add", "a_no": None, "b_no": j + 1, "a": "", "b": bl[j]})
    return {"identical": a == b, "added": added, "removed": removed, "changed": changed, "rows": rows}
