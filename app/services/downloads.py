"""Téléchargement de serveurs depuis des sources officielles (HTTPS, hôtes autorisés, checksums).

Aucun fichier non vérifié n'est installé silencieusement : si la source ne fournit pas de somme
de contrôle (Fabric), l'appelant doit confirmer explicitement (``accept_unverified``).
"""
from __future__ import annotations

import hashlib
import os
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

PROVIDERS = {
    "vanilla": {"label": "Vanilla (Mojang)", "hosts": {"piston-meta.mojang.com", "piston-data.mojang.com", "launcher.mojang.com", "launchermeta.mojang.com"}},
    "paper": {"label": "Paper", "hosts": {"fill.papermc.io", "fill-data.papermc.io"}},
    "purpur": {"label": "Purpur", "hosts": {"api.purpurmc.org"}},
    "fabric": {"label": "Fabric", "hosts": {"meta.fabricmc.net"}},
}
MAX_BYTES = 600 * 1024 * 1024
_TRANSPORT: httpx.BaseTransport | None = None  # injectable dans les tests


class DownloadError(Exception):
    pass


CONTACT = ""  # renseigné au démarrage depuis NEXCRAFT_CONTACT (.env)


def user_agent() -> str:
    contact = CONTACT.strip()
    # En-tête HTTP : ASCII uniquement (PaperMC exige un contact dans le User-Agent).
    contact = contact.encode("ascii", "ignore").decode() or "self-hosted; set NEXCRAFT_CONTACT to a contact URL or email"
    return f"NexCraftPanel/1.0 ({contact})"


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": user_agent()}, timeout=httpx.Timeout(30.0, read=60.0),
                        follow_redirects=False, transport=_TRANSPORT)


def _check_url(url: str, hosts: set[str]) -> None:
    u = urlparse(url)
    if u.scheme != "https":
        raise DownloadError("Source refusée : HTTPS obligatoire.")
    if (u.hostname or "") not in hosts:
        raise DownloadError(f"Source refusée : l'hôte « {u.hostname} » n'est pas autorisé.")


def _get(client: httpx.Client, url: str, hosts: set[str], stream: bool = False) -> httpx.Response:
    for _ in range(4):
        _check_url(url, hosts)
        try:
            r = client.get(url) if not stream else client.send(client.build_request("GET", url), stream=True)
        except httpx.HTTPError as exc:
            raise DownloadError(f"Connexion impossible à {urlparse(url).hostname} : {exc}") from exc
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            url = str(httpx.URL(url).join(r.headers["location"]))
            if stream:
                r.close()
            continue
        if r.status_code != 200:
            if stream:
                r.close()
            raise DownloadError(f"Réponse inattendue ({r.status_code}) de {urlparse(url).hostname}.")
        return r
    raise DownloadError("Trop de redirections.")


def _json(url: str, hosts: set[str]) -> Any:
    with _client() as c:
        return _get(c, url, hosts).json()


# ---------------------------------------------------------------------------- versions
def list_versions(provider: str) -> list[str]:
    if provider not in PROVIDERS:
        raise DownloadError("Type de serveur non pris en charge pour le téléchargement automatique.")
    hosts = PROVIDERS[provider]["hosts"]
    if provider == "vanilla":
        data = _json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json", hosts)
        return [v["id"] for v in data["versions"] if v.get("type") == "release"]
    if provider == "paper":
        data = _json("https://fill.papermc.io/v3/projects/paper", hosts)
        return [v for group in data["versions"].values() for v in group]
    if provider == "purpur":
        return list(reversed(_json("https://api.purpurmc.org/v2/purpur", hosts)["versions"]))
    games = _json("https://meta.fabricmc.net/v2/versions/game", hosts)
    return [g["version"] for g in games if g.get("stable")]


def resolve_download(provider: str, version: str) -> dict[str, Any]:
    """Retourne {url, filename, algo, checksum|None, size|None}."""
    hosts = PROVIDERS[provider]["hosts"]
    if provider == "vanilla":
        manifest = _json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json", hosts)
        entry = next((v for v in manifest["versions"] if v["id"] == version), None)
        if not entry:
            raise DownloadError(f"Version {version} introuvable.")
        server = _json(entry["url"], hosts).get("downloads", {}).get("server")
        if not server:
            raise DownloadError(f"La version {version} n'a pas de serveur téléchargeable.")
        return {"url": server["url"], "filename": f"minecraft_server.{version}.jar", "algo": "sha1",
                "checksum": server.get("sha1"), "size": server.get("size")}
    if provider == "paper":
        builds = _json(f"https://fill.papermc.io/v3/projects/paper/versions/{version}/builds", hosts)
        if isinstance(builds, dict) and builds.get("ok") is False:
            raise DownloadError(builds.get("message", "Version Paper introuvable."))
        stable = next((b for b in builds if b.get("channel") == "STABLE"), None)
        if not stable:
            raise DownloadError(f"Aucune build stable de Paper pour {version}.")
        d = stable["downloads"]["server:default"]
        return {"url": d["url"], "filename": d.get("name", f"paper-{version}.jar"), "algo": "sha256",
                "checksum": (d.get("checksums") or {}).get("sha256"), "size": d.get("size")}
    if provider == "purpur":
        info = _json(f"https://api.purpurmc.org/v2/purpur/{version}", hosts)
        build = info["builds"]["latest"]
        meta = _json(f"https://api.purpurmc.org/v2/purpur/{version}/{build}", hosts)
        return {"url": f"https://api.purpurmc.org/v2/purpur/{version}/{build}/download",
                "filename": f"purpur-{version}-{build}.jar", "algo": "md5", "checksum": meta.get("md5"), "size": None}
    loaders = [l for l in _json("https://meta.fabricmc.net/v2/versions/loader", hosts) if l.get("stable")]
    installers = [i for i in _json("https://meta.fabricmc.net/v2/versions/installer", hosts) if i.get("stable")]
    if not loaders or not installers:
        raise DownloadError("Impossible de déterminer les versions de Fabric.")
    return {"url": f"https://meta.fabricmc.net/v2/versions/loader/{version}/{loaders[0]['version']}/{installers[0]['version']}/server/jar",
            "filename": f"fabric-server-{version}.jar", "algo": None, "checksum": None, "size": None}


# ---------------------------------------------------------------------------- téléchargement
def download_server(provider: str, version: str, tmp_dir: Path, accept_unverified: bool = False,
                    progress=None) -> dict[str, Any]:
    """Télécharge et vérifie le JAR dans ``tmp_dir`` ; retourne {path, filename, sha256, verified}."""
    if provider not in PROVIDERS:
        raise DownloadError("Type de serveur non pris en charge pour le téléchargement automatique.")
    info = resolve_download(provider, version)
    if not info["checksum"] and not accept_unverified:
        raise DownloadError("UNVERIFIED: cette source ne fournit pas de somme de contrôle. Confirmez explicitement pour continuer.")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    part = tmp_dir / f"dl-{int(time.time())}-{os.getpid()}.part"
    hosts = PROVIDERS[provider]["hosts"]
    algo = info["algo"]
    h = hashlib.new(algo) if algo else None
    h256 = hashlib.sha256()
    total = 0
    try:
        with _client() as c:
            r = _get(c, info["url"], hosts, stream=True)
            try:
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes(1024 * 256):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise DownloadError("Fichier trop volumineux : téléchargement interrompu.")
                        f.write(chunk)
                        h256.update(chunk)
                        if h:
                            h.update(chunk)
                        if progress:
                            progress(total, info.get("size"))
            finally:
                r.close()
        if h and info["checksum"] and h.hexdigest().lower() != info["checksum"].lower():
            raise DownloadError(f"Somme de contrôle {algo} invalide : fichier rejeté (téléchargement corrompu ou altéré).")
        if not zipfile.is_zipfile(part):
            raise DownloadError("Le fichier téléchargé n'est pas un JAR valide.")
        return {"path": part, "filename": info["filename"], "sha256": h256.hexdigest(), "verified": bool(h and info["checksum"]),
                "algo": algo, "size": total}
    except Exception:
        part.unlink(missing_ok=True)
        raise
