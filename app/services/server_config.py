"""Validation de la configuration du serveur Minecraft et construction de la commande Java.

Principes de sécurité :
* la commande est toujours une *liste* d'arguments exécutée sans shell ;
* le répertoire de travail et le JAR doivent se trouver dans le dossier serveur configuré ;
* les arguments JVM / de lancement sont filtrés (pas de métacaractères, pas de -jar/-cp
  détournés, mémoire gérée uniquement par les champs dédiés) ;
* les variables d'environnement sont limitées à une liste blanche.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings

MEM_RE = re.compile(r"^(\d{1,6})([MmGg])$")
_FORBIDDEN_CHARS = re.compile(r"[;|&`$<>\n\r\x00]")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_ARG_OK = re.compile(r"^[\w@%+=:,./\\ ()\-\[\]\"']+$")
SERVER_TYPES = ("vanilla", "paper", "purpur", "fabric", "forge", "neoforge", "spigot", "other")


class ConfigError(Exception):
    def __init__(self, errors: list[str] | str) -> None:
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__(" ; ".join(self.errors))


@dataclass
class LaunchSpec:
    cmd: list[str]
    cwd: Path
    env: dict[str, str]
    jar: Path


def mem_to_mb(value: str) -> int:
    m = MEM_RE.match(str(value).strip())
    if not m:
        raise ConfigError(f"Mémoire invalide « {value} » (exemples : 512M, 2G).")
    n = int(m.group(1))
    return n * 1024 if m.group(2).upper() == "G" else n


def resolve_inside(root: Path, rel: str, what: str = "chemin") -> Path:
    """Résout ``rel`` sous ``root`` en refusant toute sortie (.., liens symboliques, absolu)."""
    rel = (rel or ".").strip().replace("\\", "/")
    if "\x00" in rel:
        raise ConfigError(f"Le {what} contient un caractère interdit.")
    root_real = root.resolve()
    target = (root_real / rel).resolve()
    if target != root_real and root_real not in target.parents:
        raise ConfigError(f"Le {what} doit rester dans le dossier du serveur ({root_real}).")
    return target


def parse_args(text: str, kind: str) -> list[str]:
    """Découpe une chaîne d'arguments en liste, en filtrant les valeurs dangereuses."""
    text = (text or "").strip()
    if not text:
        return []
    if _FORBIDDEN_CHARS.search(text):
        raise ConfigError(f"Les arguments {kind} ne peuvent pas contenir ; | & ` $ < > ni retour à la ligne.")
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise ConfigError(f"Arguments {kind} invalides : {exc}") from exc
    for t in tokens:
        if len(t) > 300 or not _ARG_OK.match(t):
            raise ConfigError(f"Argument {kind} refusé : « {t[:60]} ».")
    if kind == "JVM":
        for t in tokens:
            low = t.lower()
            if not t.startswith("-"):
                raise ConfigError(f"Argument JVM invalide « {t} » : il doit commencer par « - ».")
            if low.startswith(("-xms", "-xmx")):
                raise ConfigError("Utilisez les champs « Mémoire minimale/maximale » plutôt que -Xms/-Xmx.")
            if low in {"-jar", "-cp", "-classpath", "--class-path"} or low.startswith("-jar"):
                raise ConfigError("Les options -jar et -classpath sont gérées par le panel.")
    return tokens


def validate_env(settings: Settings, env: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (env or {}).items():
        k = str(k).strip()
        if not _ENV_NAME.match(k):
            raise ConfigError(f"Nom de variable d'environnement invalide : « {k} ».")
        if k not in settings.env_allowlist:
            raise ConfigError(
                f"La variable « {k} » n'est pas autorisée. Autorisées : {', '.join(settings.env_allowlist)}. "
                "(modifiable via NEXCRAFT_ENV_ALLOWLIST)")
        v = str(v)
        if "\x00" in v or "\n" in v or len(v) > 500:
            raise ConfigError(f"Valeur invalide pour la variable « {k} ».")
        out[k] = v
    return out


def resolve_java(java_path: str) -> str:
    """Retourne le chemin absolu de l'exécutable Java, ou lève ConfigError."""
    jp = (java_path or "java").strip()
    if _FORBIDDEN_CHARS.search(jp):
        raise ConfigError("Le chemin Java contient des caractères interdits.")
    if os.sep in jp or "/" in jp or (os.name == "nt" and ":" in jp):
        p = Path(jp).expanduser()
        if not p.is_file():
            raise ConfigError(f"Java introuvable à l'emplacement indiqué : {jp}")
        found = str(p.resolve())
    else:
        found = shutil.which(jp)
        if not found:
            raise ConfigError("Java est introuvable dans le PATH. Installez Java ou indiquez son chemin complet.")
    if not re.match(r"^javaw?(\.exe)?$", Path(found).name, re.I):
        raise ConfigError("Le fichier indiqué ne s'appelle pas « java » (ou java.exe) : refus par sécurité.")
    if not os.access(found, os.X_OK):
        raise ConfigError(f"Le fichier Java n'est pas exécutable : {found}")
    return found


def detect_java(java_exe: str) -> dict[str, Any]:
    """Exécute ``java -version`` et en extrait la version majeure."""
    try:
        res = subprocess.run([java_exe, "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"Impossible d'exécuter Java : {exc}") from exc
    text = (res.stderr or "") + (res.stdout or "")
    m = re.search(r'version "([^"]+)"', text)
    if not m:
        raise ConfigError("Impossible de lire la version de Java (sortie inattendue de « java -version »).")
    ver = m.group(1)
    parts = re.split(r"[._\-+]", ver)
    major = int(parts[1]) if parts[0] == "1" and len(parts) > 1 and parts[1].isdigit() else int(re.match(r"\d+", parts[0]).group(0))
    return {"path": java_exe, "version": ver, "major": major}


def required_java_for_mc(mc_version: str | None) -> int | None:
    """Java minimal pour une version « 1.x.y » (None si inconnue ou nommage récent)."""
    if not mc_version:
        return None
    m = re.match(r"^1\.(\d+)(?:\.(\d+))?", mc_version)
    if not m:
        return None
    minor, patch = int(m.group(1)), int(m.group(2) or 0)
    if minor > 20 or (minor == 20 and patch >= 5):
        return 21
    if minor >= 18:
        return 17
    if minor == 17:
        return 16
    return 8


def jar_required_java(jar: Path) -> int | None:
    """Version de Java minimale déduite de la classe principale du JAR (en-tête .class)."""
    try:
        with zipfile.ZipFile(jar) as z:
            mf = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace").replace("\r\n ", "").replace("\n ", "")
            m = re.search(r"^Main-Class:\s*(\S+)", mf, re.M)
            if not m:
                return None
            head = z.read(m.group(1).replace(".", "/") + ".class")[:8]
        if head[:4] != b"\xca\xfe\xba\xbe":
            return None
        major = int.from_bytes(head[6:8], "big")
        return major - 44 if major >= 45 else None
    except (KeyError, OSError, zipfile.BadZipFile):
        return None


def normalize_config(settings: Settings, data: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Valide les champs modifiables et retourne la configuration complète normalisée."""
    errors: list[str] = []
    cfg = {**current}
    for key in ("java_path", "jar_file", "xms", "xmx", "jvm_args", "launch_args", "working_dir",
                "server_type", "mc_version"):
        if key in data and data[key] is not None:
            cfg[key] = str(data[key]).strip()
    for key in ("port", "stop_timeout"):
        if key in data and data[key] is not None:
            try:
                cfg[key] = int(data[key])
            except (TypeError, ValueError):
                errors.append(f"Valeur numérique invalide pour « {key} ».")
    if "stop_on_panel_exit" in data:
        cfg["stop_on_panel_exit"] = bool(data["stop_on_panel_exit"])
    if "env" in data:
        try:
            cfg["env"] = validate_env(settings, data["env"] or {})
        except ConfigError as e:
            errors += e.errors
    if "auto_restart" in data and isinstance(data["auto_restart"], dict):
        ar = {**current.get("auto_restart", {})}
        for k, lo, hi in (("max_attempts", 1, 50), ("base_delay", 1, 3600), ("max_delay", 1, 86400), ("reset_after", 10, 86400)):
            if k in data["auto_restart"]:
                try:
                    ar[k] = min(hi, max(lo, int(data["auto_restart"][k])))
                except (TypeError, ValueError):
                    errors.append(f"Valeur invalide pour redémarrage auto « {k} ».")
        if "enabled" in data["auto_restart"]:
            ar["enabled"] = bool(data["auto_restart"]["enabled"])
        cfg["auto_restart"] = ar

    if cfg["server_type"] not in SERVER_TYPES:
        errors.append(f"Type de serveur inconnu : {cfg['server_type']} (valeurs : {', '.join(SERVER_TYPES)}).")
    if not (1 <= int(cfg["port"]) <= 65535):
        errors.append("Le port doit être compris entre 1 et 65535.")
    if not (5 <= int(cfg["stop_timeout"]) <= 600):
        errors.append("Le délai d'arrêt doit être compris entre 5 et 600 secondes.")
    try:
        xms, xmx = mem_to_mb(cfg["xms"]), mem_to_mb(cfg["xmx"])
        if xms < 64 or xmx < 128:
            errors.append("Mémoire trop faible : minimum 64M (Xms) et 128M (Xmx).")
        if xms > xmx:
            errors.append("La mémoire minimale ne peut pas dépasser la mémoire maximale.")
    except ConfigError as e:
        errors += e.errors
    for kind, key in (("JVM", "jvm_args"), ("de lancement", "launch_args")):
        try:
            parse_args(cfg[key], "JVM" if kind == "JVM" else "de lancement")
        except ConfigError as e:
            errors += e.errors
    try:
        resolve_inside(settings.server_dir, cfg["working_dir"], "répertoire de travail")
    except ConfigError as e:
        errors += e.errors
    jar = cfg["jar_file"]
    if not jar or not jar.lower().endswith(".jar") or _FORBIDDEN_CHARS.search(jar):
        errors.append("Le fichier serveur doit être un fichier .jar (chemin relatif au répertoire de travail).")
    if _FORBIDDEN_CHARS.search(cfg["java_path"]):
        errors.append("Le chemin Java contient des caractères interdits.")
    if errors:
        raise ConfigError(errors)
    return cfg


def build_launch(settings: Settings, cfg: dict[str, Any]) -> LaunchSpec:
    """Construit la commande de lancement validée. Lève ConfigError sinon."""
    java = resolve_java(cfg["java_path"])
    cwd = resolve_inside(settings.server_dir, cfg["working_dir"], "répertoire de travail")
    if not cwd.is_dir():
        raise ConfigError(f"Le répertoire de travail n'existe pas : {cwd}")
    jar = resolve_inside(cwd, cfg["jar_file"], "fichier JAR")
    if settings.server_dir.resolve() not in jar.parents:
        raise ConfigError("Le fichier JAR doit se trouver dans le dossier du serveur.")
    if not jar.is_file():
        raise ConfigError(f"Fichier serveur introuvable : {jar.name}. Installez une version ou importez un JAR.")
    xms, xmx = mem_to_mb(cfg["xms"]), mem_to_mb(cfg["xmx"])
    if xms > xmx:
        raise ConfigError("La mémoire minimale ne peut pas dépasser la mémoire maximale.")
    cmd = [java, f"-Xms{cfg['xms'].upper()}", f"-Xmx{cfg['xmx'].upper()}", "-Dfile.encoding=UTF-8",
           *parse_args(cfg["jvm_args"], "JVM"), "-jar", str(jar), *parse_args(cfg["launch_args"], "de lancement")]
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEXCRAFT_")}  # ne pas fuiter la config du panel
    env.update(validate_env(settings, cfg.get("env")))
    return LaunchSpec(cmd=cmd, cwd=cwd, env=env, jar=jar)


def read_properties(path: Path) -> dict[str, str]:
    props: dict[str, str] = {}
    if not path.is_file():
        return props
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        props[k.strip()] = v.strip()
    return props
