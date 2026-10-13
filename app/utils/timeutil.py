"""Utilitaires de dates : tout est stocké en UTC (naïf) et converti à l'affichage."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    """Sérialise une date UTC naïve en ISO 8601 avec suffixe Z."""
    return None if dt is None else dt.replace(microsecond=0).isoformat() + "Z"


def utc_to_local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone()


def local_to_utc(dt: datetime) -> datetime:
    """Convertit une date locale (naïve = heure locale, ou avec fuseau) en UTC naïve."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> tuple[int, int]:
    m = _HHMM.match(str(value).strip())
    if not m:
        raise ValueError("Heure invalide (format attendu : HH:MM)")
    return int(m.group(1)), int(m.group(2))


def compute_next_run(schedule: dict, after_utc: datetime) -> datetime | None:
    """Prochaine exécution (UTC naïve) strictement après ``after_utc``, ou None.

    Types : ``interval`` {minutes}, ``daily`` {time}, ``weekly`` {days:[0-6, 0=lundi], time},
    ``once`` {at: ISO UTC}. Les heures daily/weekly sont en heure locale de la machine.
    """
    kind = schedule.get("type")
    if kind == "interval":
        minutes = int(schedule.get("minutes", 0))
        if minutes < 1:
            raise ValueError("L'intervalle doit être d'au moins 1 minute")
        return after_utc + timedelta(minutes=minutes)
    if kind == "once":
        at = datetime.fromisoformat(str(schedule["at"]).replace("Z", "")).replace(tzinfo=None)
        return at if at > after_utc else None
    if kind in {"daily", "weekly"}:
        hh, mm = parse_hhmm(schedule.get("time", ""))
        days = schedule.get("days") or list(range(7))
        if kind == "daily":
            days = list(range(7))
        days = {int(d) for d in days}
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("Jours invalides (0 = lundi … 6 = dimanche)")
        local_after = utc_to_local(after_utc)
        for offset in range(0, 9):
            day = (local_after + timedelta(days=offset)).date()
            if day.weekday() not in days:
                continue
            candidate = datetime(day.year, day.month, day.day, hh, mm)  # heure locale naïve
            cand_utc = local_to_utc(candidate.astimezone())
            if cand_utc > after_utc:
                return cand_utc
        return None
    raise ValueError(f"Type de planification inconnu : {kind!r}")


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "indisponible"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d} j")
    if h:
        parts.append(f"{h} h")
    if m and not d:
        parts.append(f"{m} min")
    if not parts:
        parts.append(f"{s} s")
    return " ".join(parts)
