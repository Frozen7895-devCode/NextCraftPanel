"""Indicateur de santé calculé à partir de données réelles, avec explication des points perdus."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..models import Backup, CrashReport
from ..utils.timeutil import human_duration, utcnow

if TYPE_CHECKING:
    from ..context import AppContext

KEEP_UP = re.compile(r"Can't keep up!|Is the server overloaded", re.I)


def compute_health(ctx: "AppContext") -> dict[str, Any]:
    mgr = ctx.manager
    comps: list[dict[str, Any]] = []

    # 1. Disponibilité du processus (40)
    reasons, pts = [], 40
    if mgr.status == "running":
        reasons.append("Le serveur est en ligne.")
    elif mgr.status in ("starting", "stopping"):
        pts = 25
        reasons.append(f"Le serveur est en transition ({'démarrage' if mgr.status == 'starting' else 'arrêt'} en cours).")
    else:
        pts = 0
        reasons.append("Le serveur est arrêté.")
    comps.append({"id": "process", "name": "Disponibilité du processus", "points": pts, "max": 40, "reasons": reasons})

    # 2. Stabilité récente (20)
    since = utcnow() - timedelta(hours=24)
    with ctx.db.scope() as db:
        crashes = db.scalars(select(CrashReport).where(CrashReport.occurred_at >= since)).all()
        last_ok = db.scalar(select(Backup).where(Backup.status == "ok").order_by(Backup.created_at.desc()))
        last_any = db.scalar(select(Backup).order_by(Backup.created_at.desc()))
        last_ok_at = last_ok.created_at if last_ok else None
        last_status = last_any.status if last_any else None
    n = len(crashes)
    pts = {0: 20, 1: 12, 2: 6}.get(n, 0)
    reasons = ["Aucun crash sur les dernières 24 h."] if n == 0 else [f"{n} crash(s) sur les dernières 24 h."]
    if mgr.proc is not None and mgr.started_at and (utcnow() - mgr.started_at).total_seconds() < 300 and n:
        reasons.append("Le serveur vient de redémarrer après un incident.")
    comps.append({"id": "stability", "name": "Stabilité récente", "points": pts, "max": 20, "reasons": reasons})

    # 3. Erreurs dans les journaux (20)
    recent = list(mgr.lines)[-500:]
    errors = [l for l in recent if l["level"] == "ERROR" and not l["text"].lstrip().startswith(("at ", "..."))]
    lag = sum(1 for l in recent if KEEP_UP.search(l["text"]))
    if not recent:
        pts, reasons = 20, ["Aucun journal disponible pour cette session : rien à signaler."]
    else:
        pts = 20 if not errors else 14 if len(errors) <= 5 else 8 if len(errors) <= 20 else 2
        reasons = [f"{len(errors)} erreur(s) dans les 500 dernières lignes."] if errors else ["Aucune erreur dans les 500 dernières lignes."]
        if lag:
            pts = max(0, pts - 4)
            reasons.append(f"{lag} avertissement(s) de surcharge (« Can't keep up »).")
    comps.append({"id": "logs", "name": "Erreurs dans les journaux", "points": pts, "max": 20, "reasons": reasons})

    # 4. Sauvegardes (20)
    if last_ok_at is None:
        pts, reasons = 0, ["Aucune sauvegarde réussie."]
    else:
        age = (utcnow() - last_ok_at).total_seconds()
        pts = 20 if age < 86400 else 14 if age < 3 * 86400 else 8 if age < 7 * 86400 else 2
        reasons = [f"Dernière sauvegarde réussie il y a {human_duration(age)}."]
    if last_status == "failed":
        pts = max(0, pts - 8)
        reasons.append("La dernière tentative de sauvegarde a échoué.")
    comps.append({"id": "backups", "name": "État des sauvegardes", "points": pts, "max": 20, "reasons": reasons})

    score = sum(c["points"] for c in comps)
    label, level = ("Bon", "good") if score >= 85 else ("Moyen", "warn") if score >= 60 else ("Dégradé", "bad")
    return {"score": score, "label": label, "level": level, "components": comps,
            "problems": [r for c in comps if c["points"] < c["max"] for r in c["reasons"]]}
