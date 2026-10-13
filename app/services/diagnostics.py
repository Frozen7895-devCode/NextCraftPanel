"""Assistant de diagnostic fondé sur des règles (aucun service externe par défaut).

Les explications et solutions sont rattachées à des messages réellement trouvés dans les
journaux ; rien n'est inventé. Une aide IA optionnelle existe (``ai_explain``) : elle n'envoie
rien automatiquement et exige une action explicite d'un administrateur.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .settings_store import get_setting

if TYPE_CHECKING:
    from ..context import AppContext

SEV_ORDER = {"error": 0, "warn": 1, "info": 2}

RULES: list[dict[str, Any]] = [
    dict(id="oom", sev="error", title="Mémoire Java insuffisante (OutOfMemoryError)",
         pat=r"OutOfMemoryError|GC overhead limit exceeded",
         why="La JVM a épuisé la mémoire qui lui est allouée : trop de plugins/mods, de chunks chargés ou un Xmx trop bas.",
         fixes=["Augmentez la mémoire maximale (Xmx) dans « Paramètres du serveur » si la machine le permet.",
                "Réduisez view-distance et simulation-distance dans server.properties.",
                "Désactivez ou mettez à jour les plugins/mods gourmands (chercher les derniers messages avant l'erreur)."]),
    dict(id="java_version", sev="error", title="Version de Java incompatible",
         pat=r"UnsupportedClassVersionError|class file version (\d+)\.\d+|requires Java (\d+)",
         why="Le serveur ou un plugin a été compilé pour une version de Java plus récente que celle utilisée.",
         fixes=["Installez la version de Java requise (Java 17 pour 1.18–1.20.4, Java 21 pour 1.20.5+).",
                "Indiquez le chemin de ce Java dans « Paramètres du serveur » > Chemin Java."]),
    dict(id="heap_reserve", sev="error", title="Impossible de réserver la mémoire demandée",
         pat=r"Could not reserve enough space|Invalid maximum heap size|Too small (maximum|initial) heap|Initial heap size set to a larger value",
         why="Les valeurs Xms/Xmx sont invalides ou dépassent la mémoire disponible (Java 32 bits, RAM insuffisante…).",
         fixes=["Réduisez Xmx/Xms et vérifiez que Xms ≤ Xmx.", "Utilisez un Java 64 bits."]),
    dict(id="bad_jar", sev="error", title="Fichier JAR du serveur introuvable ou corrompu",
         pat=r"Unable to access jarfile|Invalid or corrupt jarfile|Could not find or load main class|Error: Unable to initialize main class",
         why="Java n'a pas pu lire le JAR du serveur indiqué.",
         fixes=["Vérifiez le nom du JAR dans « Paramètres du serveur ».", "Réinstallez la version via l'assistant ou réimportez le fichier."]),
    dict(id="eula", sev="error", title="CLUF Minecraft non accepté",
         pat=r"You need to agree to the EULA",
         why="Le serveur refuse de démarrer tant que eula.txt ne contient pas eula=true.",
         fixes=["Acceptez le CLUF depuis le rapport de démarrage ou éditez eula.txt (eula=true)."]),
    dict(id="port", sev="error", title="Port réseau déjà utilisé",
         pat=r"FAILED TO BIND TO PORT|Address already in use|Perhaps a server is already running on that port",
         why="Un autre programme (ou une autre instance du serveur) écoute déjà sur ce port.",
         fixes=["Arrêtez l'autre processus ou changez server-port dans « Paramètres du serveur »."]),
    dict(id="plugin_dep", sev="error", title="Dépendance de plugin manquante",
         pat=r"Unknown dependency ([\w\-.]+)|Missing dependency ([\w\-.]+)|requires plugin ([\w\-.]+)|Could not find required dependency ([\w\-.]+)",
         why="Un plugin a besoin d'un autre plugin/mod qui n'est pas installé (ou pas chargé avant lui).",
         fixes=["Installez la dépendance citée dans le dossier plugins/ (ou mods/).", "Vérifiez que sa version est compatible."]),
    dict(id="plugin_load", sev="error", title="Échec de chargement d'un plugin",
         pat=r"Could not load '([^']+)'|Error occurred while enabling ([\w\-. ]+)|Failed to load plugin ([\w\-.]+)|InvalidPluginException|InvalidDescriptionException",
         why="Un plugin est corrompu, incompatible avec cette version ou mal configuré.",
         fixes=["Mettez à jour le plugin pour votre version du serveur.", "Retirez-le temporairement pour confirmer qu'il est à l'origine du problème."]),
    dict(id="plugin_dup", sev="warn", title="Conflit de plugins (nom ambigu / doublon)",
         pat=r"Ambiguous plugin name `?([^`']+)'?|is already loaded|Duplicate plugin",
         why="Deux fichiers JAR déclarent le même plugin : un seul sera utilisé.",
         fixes=["Supprimez la version en double dans le dossier plugins/."]),
    dict(id="mod_conflict", sev="error", title="Conflit ou dépendance de mods",
         pat=r"Mod resolution failed|Duplicate mod|Incompatible mod set|Mixin apply failed|mandatory dependencies",
         why="Le chargeur de mods a détecté des mods incompatibles, en double ou des dépendances manquantes.",
         fixes=["Lisez le message juste avant pour identifier le mod, puis mettez-le à jour ou retirez-le."]),
    dict(id="world_load", sev="error", title="Problème de chargement du monde",
         pat=r"Failed to load level|Failed to read level data|Exception reading .*level\.dat|Unable to load world|Failed to load chunk|Failed to load (?:region|chunk)",
         why="Les données du monde sont illisibles ou corrompues, ou le monde a été créé avec une version plus récente.",
         fixes=["Restaurez une sauvegarde récente depuis la page « Sauvegardes ».", "Vérifiez level-name dans server.properties."]),
    dict(id="config_err", sev="warn", title="Erreur de configuration",
         pat=r"InvalidConfigurationException|YAMLException|JsonSyntaxException|Failed to parse|Could not load .*\.(yml|json|toml)|Failed to load properties",
         why="Un fichier de configuration contient une erreur de syntaxe (indentation YAML, virgule JSON…).",
         fixes=["Ouvrez le fichier cité dans l'éditeur (la validation JSON signale les erreurs).",
                "Comparez avec une version précédente grâce à l'historique des fichiers."]),
    dict(id="startup_fail", sev="error", title="Échec de démarrage / crash du serveur",
         pat=r"Failed to start the minecraft server|Encountered an unexpected exception|This crash report has been saved|Crash report saved|Server crashed",
         why="Le serveur a rencontré une exception fatale. Le rapport de crash contient la cause.",
         fixes=["Consultez les lignes « Caused by » dans le rapport de crash ci-dessous."]),
    dict(id="watchdog", sev="error", title="Serveur bloqué (Watchdog / tick trop long)",
         pat=r"A single server tick took|The server has stopped responding|Watchdog",
         why="Un tick a duré anormalement longtemps : plugin bloquant, disque lent ou surcharge.",
         fixes=["Identifiez le plugin cité dans la trace (« Thread Dump »).", "Vérifiez les performances du disque et du processeur."]),
    dict(id="tps", sev="warn", title="Serveur surchargé (« Can't keep up »)",
         pat=r"Can't keep up!|Is the server overloaded",
         why="Le serveur n'arrive pas à traiter 20 ticks par seconde : lag.",
         fixes=["Réduisez view-distance / simulation-distance.", "Limitez les entités et les redstone complexes, ou allouez plus de ressources."]),
    dict(id="permission", sev="error", title="Permissions de fichiers insuffisantes",
         pat=r"Permission denied|AccessDeniedException|Access is denied|FileSystemException",
         why="Le processus Java n'a pas le droit de lire ou d'écrire un fichier.",
         fixes=["Donnez à l'utilisateur qui lance le panel les droits sur le dossier du serveur."]),
    dict(id="disk", sev="error", title="Disque plein",
         pat=r"No space left on device|not enough space on the disk",
         why="Il n'y a plus d'espace libre pour écrire les données.",
         fixes=["Supprimez d'anciennes sauvegardes/journaux, videz la corbeille ou libérez de l'espace."]),
]
for r in RULES:
    r["_re"] = re.compile(r["pat"], re.I)


def _tail_lines(path: Path, max_bytes: int = 600_000, max_lines: int = 4000) -> list[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read()
        return data.decode("utf-8", "replace").splitlines()[-max_lines:]
    except OSError:
        return []


def analyze_lines(lines: list[str], source: str = "console") -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for i, line in enumerate(lines):
        for rule in RULES:
            m = rule["_re"].search(line)
            if not m:
                continue
            f = found.setdefault(rule["id"], {"count": 0, "evidence": [], "names": [], "source": source})
            f["count"] += 1
            if len(f["evidence"]) < 3:
                f["evidence"].append(line.strip()[:400])
            elif f["count"] > 3:
                f["evidence"][-1] = line.strip()[:400]  # on garde la dernière occurrence
            for g in m.groups():
                g = (g or "").strip().rstrip(".,;:")  # « Vault. » -> « Vault »
                if g and g not in f["names"] and len(f["names"]) < 5:
                    f["names"].append(g)
    return found


def run_diagnostics(ctx: "AppContext") -> dict[str, Any]:
    mgr = ctx.manager
    sources: dict[str, list[str]] = {"console du panel": [l["text"] for l in list(mgr.lines)[-3000:]]}
    root = ctx.settings.server_dir
    latest = root / "logs" / "latest.log"
    if latest.is_file():
        sources["logs/latest.log"] = _tail_lines(latest)
    crash_dir = root / "crash-reports"
    crash_files = sorted(crash_dir.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True) if crash_dir.is_dir() else []
    if crash_files:
        sources[f"crash-reports/{crash_files[0].name}"] = _tail_lines(crash_files[0], max_bytes=200_000, max_lines=400)

    merged: dict[str, dict[str, Any]] = {}
    for name, lines in sources.items():
        for rid, f in analyze_lines(lines, name).items():
            if rid not in merged or f["count"] > merged[rid]["count"]:
                prev_names = merged.get(rid, {}).get("names", [])
                f["names"] = list(dict.fromkeys(prev_names + f["names"]))[:5]
                f["sources"] = sorted(set(merged.get(rid, {}).get("sources", [])) | {name})
                merged[rid] = f
            else:
                merged[rid]["sources"] = sorted(set(merged[rid].get("sources", [])) | {name})
    by_id = {r["id"]: r for r in RULES}
    findings = []
    for rid, f in merged.items():
        r = by_id[rid]
        findings.append({"id": rid, "severity": r["sev"], "title": r["title"], "explanation": r["why"], "solutions": r["fixes"],
                         "count": f["count"], "evidence": f["evidence"], "names": f["names"], "sources": f.get("sources", [f["source"]])})
    findings.sort(key=lambda x: (SEV_ORDER[x["severity"]], -x["count"]))
    with ctx.db.scope() as db:
        from ..models import CrashReport
        from sqlalchemy import select
        crashes = db.scalars(select(CrashReport).order_by(CrashReport.occurred_at.desc()).limit(10)).all()
        crash_list = [{"id": c.id, "occurred_at": c.occurred_at.isoformat() + "Z", "exit_code": c.exit_code,
                       "uptime_s": c.uptime_s, "summary": c.summary, "restart_info": c.restart_info,
                       "report_name": Path(c.report_path).name if c.report_path else None} for c in crashes]
    return {"findings": findings, "sources": {k: len(v) for k, v in sources.items()}, "crashes": crash_list,
            "clean": not findings}


# ---------------------------------------------------------------- aide IA optionnelle
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_HOME = re.compile(r"(/home/|/Users/|C:\\Users\\)[^/\\\s]+", re.I)


def redact(text: str) -> str:
    text = _IP.sub("[IP]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    return _HOME.sub(r"\1[USER]", text)


def ai_explain(ctx: "AppContext", findings: list[dict[str, Any]], transport: httpx.BaseTransport | None = None) -> str:
    with ctx.db.scope() as db:
        ai = get_setting(db, "ai")
    if not ai.get("enabled") or not ai.get("api_key"):
        raise ValueError("L'aide IA n'est pas activée (clé API non configurée).")
    evidence = "\n\n".join(f"## {f['title']}\n" + "\n".join(redact(e) for e in f["evidence"]) for f in findings[:5])
    prompt = ("Voici des extraits (adresses IP, e-mails et chemins personnels masqués) des journaux d'un serveur Minecraft, "
              "avec le problème détecté. Explique en français, simplement, la cause probable et les étapes de correction.\n\n" + evidence)
    with httpx.Client(timeout=60, transport=transport) as client:
        r = client.post("https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": ai["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                        json={"model": ai.get("model") or "claude-sonnet-4-6", "max_tokens": 1000,
                              "messages": [{"role": "user", "content": prompt}]})
    if r.status_code != 200:
        raise ValueError(f"Le service IA a répondu {r.status_code}.")
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
