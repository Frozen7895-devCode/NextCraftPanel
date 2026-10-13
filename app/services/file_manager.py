"""Gestionnaire de fichiers confiné au dossier du serveur Minecraft.

Garanties :
* tout chemin est résolu (liens symboliques inclus) et doit rester dans la racine configurée ;
* ``..``, chemins absolus, lettres de lecteur et octets nuls sont refusés ;
* les suppressions passent par une corbeille restaurable (durée configurable) ;
* chaque écriture sur un fichier existant conserve la version précédente (historique) ;
* l'extraction ZIP refuse les chemins malveillants, les liens symboliques et les « zip bombs » ;
* les modifications concurrentes sont détectées (mtime attendu).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat as statmod
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from sqlalchemy import select

from ..models import FileVersion, TrashItem
from ..utils.timeutil import iso, utcnow
from .audit import audit
from .settings_store import get_setting

if TYPE_CHECKING:
    from ..context import AppContext

BINARY_EXT = {
    ".jar", ".zip", ".gz", ".tgz", ".7z", ".rar", ".xz", ".bz2", ".dat", ".dat_old", ".mca", ".mcr", ".nbt", ".png", ".jpg",
    ".jpeg", ".gif", ".ico", ".webp", ".exe", ".dll", ".so", ".dylib", ".class", ".bin", ".db", ".sqlite", ".lock",
    ".ogg", ".mp3", ".wav", ".mp4", ".pdf", ".ttf", ".otf", ".woff", ".woff2", ".schematic", ".litematic", ".mcfunction_bin",
    ".pack", ".idx", ".rcon", ".cache",
}
LANGUAGES = {
    ".json": "json", ".yml": "yaml", ".yaml": "yaml", ".properties": "ini", ".toml": "ini", ".cfg": "ini", ".conf": "ini",
    ".ini": "ini", ".txt": "plaintext", ".log": "plaintext", ".md": "markdown", ".xml": "xml", ".sh": "shell",
    ".bat": "bat", ".cmd": "bat", ".ps1": "powershell", ".js": "javascript", ".py": "python", ".java": "java",
    ".sql": "sql", ".mcmeta": "json", ".json5": "json", ".csv": "plaintext", ".mcfunction": "plaintext",
}
VERSION_MAX_BYTES = 5 * 1024 * 1024
MAX_LIST = 5000
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class FileError(Exception):
    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in {".", ".."} or len(name) > 200:
        raise FileError(400, "Nom invalide.")
    if INVALID_NAME.search(name) or name.endswith((" ", ".")):
        raise FileError(400, "Le nom contient des caractères interdits (< > : \" / \\ | ? *) ou finit par un point ou un espace.")
    return name


class FileManager:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ chemins
    @property
    def root(self) -> Path:
        return self.ctx.settings.server_dir.resolve()

    def resolve(self, rel: str, must_exist: bool = True) -> Path:
        """Chemin absolu validé pour ``rel`` (relatif à la racine). Lève FileError sinon."""
        rel = (rel or "").replace("\\", "/")
        if "\x00" in rel:
            raise FileError(400, "Chemin invalide.")
        if re.match(r"^[A-Za-z]:", rel):
            raise FileError(400, "Les chemins absolus ne sont pas autorisés.")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise FileError(400, "Chemin invalide : « .. » est interdit.")
        target = self.root.joinpath(*parts)
        real = Path(os.path.realpath(target))
        if real != self.root and self.root not in real.parents:
            raise FileError(403, "Accès refusé : ce chemin sort du dossier du serveur.")
        if must_exist and not os.path.lexists(target):
            raise FileError(404, "Fichier ou dossier introuvable.")
        return target

    def rel(self, path: Path) -> str:
        r = path.relative_to(self.root).as_posix()
        return "" if r == "." else r

    def _general(self) -> dict[str, Any]:
        with self.ctx.db.scope() as db:
            return get_setting(db, "general")

    def check_writable(self) -> None:
        reason = self.ctx.file_ops_block_reason()
        if reason:
            raise FileError(423, f"Opérations de fichiers bloquées : {reason}")

    # ------------------------------------------------------------------ lecture
    def _is_text(self, path: Path, size: int) -> tuple[bool, str]:
        if path.suffix.lower() in BINARY_EXT:
            return False, "Fichier binaire : ouverture comme texte impossible."
        if size > self.ctx.settings.max_edit_kb * 1024:
            return False, f"Fichier trop volumineux pour l'éditeur (> {self.ctx.settings.max_edit_kb} Ko)."
        try:
            with open(path, "rb") as f:
                head = f.read(8192)
        except OSError as exc:
            return False, f"Lecture impossible : {exc.strerror}"
        if b"\x00" in head:
            return False, "Fichier binaire : ouverture comme texte impossible."
        return True, ""

    def list_dir(self, rel: str, sort: str = "name", order: str = "asc") -> dict[str, Any]:
        d = self.resolve(rel)
        if not d.is_dir():
            raise FileError(400, "Ce chemin n'est pas un dossier.")
        entries: list[dict[str, Any]] = []
        truncated = False
        with os.scandir(d) as it:
            for i, e in enumerate(it):
                if i >= MAX_LIST:
                    truncated = True
                    break
                try:
                    st = e.stat(follow_symlinks=False)
                    is_link = e.is_symlink()
                    is_dir = e.is_dir(follow_symlinks=True) if is_link else statmod.S_ISDIR(st.st_mode)
                    if is_link:
                        st_t = e.stat(follow_symlinks=True)
                    else:
                        st_t = st
                except OSError:
                    continue
                ext = Path(e.name).suffix.lower()
                entries.append({
                    "name": e.name, "path": self.rel(d / e.name), "is_dir": is_dir, "is_symlink": is_link,
                    "size": None if is_dir else st_t.st_size, "mtime": iso(datetime.fromtimestamp(st_t.st_mtime, timezone.utc).replace(tzinfo=None)),
                    "ext": ext, "editable": (not is_dir) and ext not in BINARY_EXT and st_t.st_size <= self.ctx.settings.max_edit_kb * 1024,
                })
        keyf = {"name": lambda x: x["name"].lower(), "size": lambda x: x["size"] or 0, "date": lambda x: x["mtime"]}.get(sort, lambda x: x["name"].lower())
        entries.sort(key=keyf, reverse=(order == "desc"))
        entries.sort(key=lambda x: not x["is_dir"])  # dossiers d'abord (tri stable)
        return {"path": self.rel(d), "parent": None if d == self.root else self.rel(d.parent), "entries": entries,
                "truncated": truncated, "root_name": self.root.name}

    def read_text(self, rel: str) -> dict[str, Any]:
        p = self.resolve(rel)
        if not p.is_file():
            raise FileError(400, "Ce chemin n'est pas un fichier.")
        st = p.stat()
        ok, reason = self._is_text(p, st.st_size)
        if not ok:
            raise FileError(415, reason)
        data = p.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise FileError(415, "Encodage non pris en charge (UTF-8 attendu) : ouverture refusée pour ne pas corrompre le fichier.")
        return {"path": self.rel(p), "name": p.name, "content": text, "size": st.st_size, "mtime_ns": str(st.st_mtime_ns),
                "sha256": sha256_bytes(data), "language": LANGUAGES.get(p.suffix.lower(), "plaintext")}

    def open_for_download(self, rel: str) -> Path:
        p = self.resolve(rel)
        if not p.is_file():
            raise FileError(400, "Seuls les fichiers peuvent être téléchargés directement.")
        return p

    # ------------------------------------------------------------------ historique de versions
    def snapshot(self, path: Path, action: str, user: str) -> None:
        """Conserve la version actuelle d'un fichier avant modification/suppression."""
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > VERSION_MAX_BYTES:
                return
            data = path.read_bytes()
        except OSError:
            return
        digest = sha256_bytes(data)
        rel = self.rel(path)
        hist = self.ctx.settings.history_dir
        hist.mkdir(parents=True, exist_ok=True)
        with self.ctx.db.scope() as db:
            last = db.scalar(select(FileVersion).where(FileVersion.path == rel).order_by(FileVersion.id.desc()))
            if last and last.sha256 == digest:
                return
            name = uuid.uuid4().hex
            (hist / name).write_bytes(data)
            db.add(FileVersion(path=rel, stored_name=name, sha256=digest, size=len(data), action=action, username=user))
            db.flush()
            keep = int(get_setting(db, "general").get("file_versions_keep", 20))
            old = db.scalars(select(FileVersion).where(FileVersion.path == rel).order_by(FileVersion.id.desc()).offset(keep)).all()
            for v in old:
                (hist / v.stored_name).unlink(missing_ok=True)
                db.delete(v)

    def versions(self, rel: str) -> list[dict[str, Any]]:
        rel = self.rel(self.resolve(rel, must_exist=False))
        with self.ctx.db.scope() as db:
            rows = db.scalars(select(FileVersion).where(FileVersion.path == rel).order_by(FileVersion.id.desc())).all()
            return [{"id": v.id, "created_at": iso(v.created_at), "size": v.size, "sha256": v.sha256,
                     "username": v.username, "action": v.action} for v in rows]

    def version_text(self, version_id: int) -> tuple[dict[str, Any], str]:
        with self.ctx.db.scope() as db:
            v = db.get(FileVersion, version_id)
            if v is None:
                raise FileError(404, "Version introuvable.")
            info = {"id": v.id, "path": v.path, "created_at": iso(v.created_at), "username": v.username, "action": v.action}
            stored = v.stored_name
        f = self.ctx.settings.history_dir / stored
        if not f.is_file():
            raise FileError(404, "Le contenu de cette version n'est plus disponible.")
        data = f.read_bytes()
        try:
            return info, data.decode("utf-8")
        except UnicodeDecodeError:
            raise FileError(415, "Cette version n'est pas un fichier texte UTF-8.")

    def restore_version(self, version_id: int, user: str, ip: str = "") -> dict[str, Any]:
        self.check_writable()
        info, text = self.version_text(version_id)
        target = self.resolve(info["path"], must_exist=False)
        if target.parent.exists() is False:
            target.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot(target, "restore", user)
        self._atomic_write(target, text.encode("utf-8"))
        with self.ctx.db.scope() as db:
            audit(db, user, "file.restore_version", info["path"], f"version #{version_id}", ip)
        return {"path": info["path"]}

    # ------------------------------------------------------------------ écriture
    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        tmp = path.with_name(f".{path.name}.nexcraft-tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except PermissionError as exc:
            tmp.unlink(missing_ok=True)
            raise FileError(423, "Fichier verrouillé ou accès refusé (le serveur l'utilise peut-être).") from exc
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise FileError(500, f"Écriture impossible : {exc.strerror or exc}") from exc

    def write_text(self, rel: str, content: str, expected_mtime_ns: int | None, force: bool, user: str, ip: str = "") -> dict[str, Any]:
        self.check_writable()
        p = self.resolve(rel, must_exist=False)
        data = content.encode("utf-8")
        if len(data) > self.ctx.settings.max_edit_kb * 1024:
            raise FileError(413, f"Contenu trop volumineux (maximum {self.ctx.settings.max_edit_kb} Ko).")
        existed = p.exists()
        if existed:
            if not p.is_file():
                raise FileError(400, "Ce chemin est un dossier.")
            if p.suffix.lower() in BINARY_EXT:
                raise FileError(415, "Ce type de fichier ne peut pas être enregistré comme texte.")
            st = p.stat()
            if not force and (expected_mtime_ns is None or st.st_mtime_ns != expected_mtime_ns):
                raise FileError(409, "Le fichier a été modifié depuis son ouverture. Rechargez-le ou forcez l'écriture.",
                                conflict=True, current_mtime_ns=str(st.st_mtime_ns))
            self.snapshot(p, "edit", user)
        else:
            if not p.parent.is_dir():
                raise FileError(404, "Le dossier parent n'existe pas.")
            validate_name(p.name)
        self._atomic_write(p, data)
        st = p.stat()
        with self.ctx.db.scope() as db:
            audit(db, user, "file.edit" if existed else "file.create", self.rel(p), f"{len(data)} octets", ip)
        return {"path": self.rel(p), "size": st.st_size, "mtime_ns": str(st.st_mtime_ns), "sha256": sha256_bytes(data)}

    def create(self, rel_dir: str, name: str, kind: str, user: str, ip: str = "") -> dict[str, Any]:
        self.check_writable()
        d = self.resolve(rel_dir)
        if not d.is_dir():
            raise FileError(400, "Le dossier de destination est invalide.")
        name = validate_name(name)
        target = self.resolve(self.rel(d / name) if d != self.root else name, must_exist=False)
        if os.path.lexists(target):
            raise FileError(409, f"« {name} » existe déjà.")
        if kind == "dir":
            target.mkdir()
        elif kind == "file":
            target.touch()
        else:
            raise FileError(400, "Type inconnu (file ou dir).")
        with self.ctx.db.scope() as db:
            audit(db, user, "file.create", self.rel(target), kind, ip)
        return {"path": self.rel(target)}

    def rename(self, rel: str, new_name: str, user: str, ip: str = "") -> dict[str, Any]:
        self.check_writable()
        src = self.resolve(rel)
        if src == self.root:
            raise FileError(400, "La racine ne peut pas être renommée.")
        new_name = validate_name(new_name)
        dst = self.resolve(self.rel(src.parent / new_name) if src.parent != self.root else new_name, must_exist=False)
        if os.path.lexists(dst):
            raise FileError(409, f"« {new_name} » existe déjà.")
        self.snapshot(src, "rename", user)
        os.rename(src, dst)
        with self.ctx.db.scope() as db:
            audit(db, user, "file.rename", self.rel(src), f"→ {self.rel(dst)}", ip)
        return {"path": self.rel(dst)}

    def _transfer(self, rels: Iterable[str], dest_rel: str, move: bool, overwrite: bool, user: str, ip: str) -> dict[str, Any]:
        self.check_writable()
        dest = self.resolve(dest_rel)
        if not dest.is_dir():
            raise FileError(400, "Le dossier de destination est invalide.")
        plan: list[tuple[Path, Path]] = []
        conflicts: list[str] = []
        for rel in rels:
            src = self.resolve(rel)
            if src == self.root:
                raise FileError(400, "La racine ne peut pas être déplacée ou copiée.")
            target = dest / src.name
            real_src, real_dest = Path(os.path.realpath(src)), Path(os.path.realpath(dest))
            if src.is_dir() and not src.is_symlink() and (real_dest == real_src or real_src in real_dest.parents):
                raise FileError(400, f"Impossible de placer « {src.name} » dans lui-même.")
            if os.path.lexists(target):
                if target == src:
                    raise FileError(400, f"« {src.name} » est déjà dans ce dossier.")
                conflicts.append(src.name)
            plan.append((src, target))
        if conflicts and not overwrite:
            raise FileError(409, "Éléments déjà présents : " + ", ".join(conflicts[:10]), conflict=True, names=conflicts)
        done = []
        for src, target in plan:
            if os.path.lexists(target):
                if target.is_dir() and not target.is_symlink():
                    raise FileError(409, f"« {target.name} » est un dossier existant : supprimez-le d'abord ou renommez.")
                self.snapshot(target, "overwrite", user)
                target.unlink()
            if move:
                self.snapshot(src, "move", user)
                shutil.move(str(src), str(target))
            elif src.is_dir() and not src.is_symlink():
                shutil.copytree(src, target, symlinks=True)
            else:
                shutil.copy2(src, target, follow_symlinks=False)
            done.append(self.rel(target))
        with self.ctx.db.scope() as db:
            audit(db, user, "file.move" if move else "file.copy", ", ".join(self.rel(s) for s, _ in plan)[:400],
                  f"→ {self.rel(dest) or '/'}", ip)
        return {"done": done}

    def move(self, rels: list[str], dest: str, overwrite: bool, user: str, ip: str = "") -> dict[str, Any]:
        return self._transfer(rels, dest, True, overwrite, user, ip)

    def copy(self, rels: list[str], dest: str, overwrite: bool, user: str, ip: str = "") -> dict[str, Any]:
        return self._transfer(rels, dest, False, overwrite, user, ip)

    # ------------------------------------------------------------------ suppression / corbeille
    def _dir_size(self, p: Path, cap: int = 200_000) -> int:
        total = n = 0
        for dp, _dn, fn in os.walk(p, followlinks=False):
            for f in fn:
                n += 1
                if n > cap:
                    return total
                try:
                    total += os.lstat(os.path.join(dp, f)).st_size
                except OSError:
                    pass
        return total

    def delete(self, rels: list[str], confirm: bool, user: str, ip: str = "") -> dict[str, Any]:
        self.check_writable()
        paths = [self.resolve(r) for r in rels]
        if any(p == self.root for p in paths):
            raise FileError(400, "La racine du serveur ne peut pas être supprimée.")
        if not confirm and (len(paths) > 1 or any(p.is_dir() and not p.is_symlink() for p in paths)):
            raise FileError(409, "Confirmation requise pour supprimer des dossiers ou plusieurs éléments.", confirm_required=True)
        gen = self._general()
        retention = int(gen.get("trash_retention_days", 7))
        trash = self.ctx.settings.trash_dir
        trash.mkdir(parents=True, exist_ok=True)
        removed = []
        for p in paths:
            rel = self.rel(p)
            if p.is_symlink():  # on supprime le lien, jamais sa cible
                p.unlink()
                removed.append(rel)
                continue
            is_dir = p.is_dir()
            size = self._dir_size(p) if is_dir else p.stat().st_size
            stored = uuid.uuid4().hex
            try:
                shutil.move(str(p), str(trash / stored))
            except PermissionError as exc:
                raise FileError(423, f"Suppression impossible (fichier verrouillé) : {p.name}") from exc
            with self.ctx.db.scope() as db:
                db.add(TrashItem(original_path=rel, stored_name=stored, is_dir=is_dir, size=size, deleted_by=user,
                                 expires_at=utcnow() + timedelta(days=retention)))
            removed.append(rel)
        with self.ctx.db.scope() as db:
            audit(db, user, "file.delete", ", ".join(removed)[:450], f"corbeille ({retention} j)", ip, level="warning")
        return {"removed": removed, "retention_days": retention}

    def trash_list(self) -> list[dict[str, Any]]:
        with self.ctx.db.scope() as db:
            rows = db.scalars(select(TrashItem).order_by(TrashItem.deleted_at.desc())).all()
            return [{"id": t.id, "original_path": t.original_path, "is_dir": t.is_dir, "size": t.size,
                     "deleted_at": iso(t.deleted_at), "expires_at": iso(t.expires_at), "deleted_by": t.deleted_by} for t in rows]

    def trash_restore(self, item_id: int, user: str, ip: str = "") -> dict[str, Any]:
        self.check_writable()
        with self.ctx.db.scope() as db:
            t = db.get(TrashItem, item_id)
            if t is None:
                raise FileError(404, "Élément introuvable dans la corbeille.")
            src = self.ctx.settings.trash_dir / t.stored_name
            original = t.original_path
        if not src.exists():
            raise FileError(404, "Le contenu a déjà été purgé.")
        target = self.resolve(original, must_exist=False)
        if not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(target):
            i = 1
            while os.path.lexists(target.with_name(f"{target.stem} (restauré {i}){target.suffix}")):
                i += 1
            target = target.with_name(f"{target.stem} (restauré {i}){target.suffix}")
        shutil.move(str(src), str(target))
        with self.ctx.db.scope() as db:
            t = db.get(TrashItem, item_id)
            if t:
                db.delete(t)
            audit(db, user, "file.trash_restore", self.rel(target), "", ip)
        return {"path": self.rel(target)}

    def trash_purge(self, item_id: int | None, user: str, ip: str = "", expired_only: bool = False) -> int:
        n = 0
        with self.ctx.db.scope() as db:
            q = select(TrashItem)
            if item_id is not None:
                q = q.where(TrashItem.id == item_id)
            elif expired_only:
                q = q.where(TrashItem.expires_at <= utcnow())
            for t in db.scalars(q).all():
                f = self.ctx.settings.trash_dir / t.stored_name
                if f.is_dir() and not f.is_symlink():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink(missing_ok=True)
                db.delete(t)
                n += 1
            if n and not expired_only:
                audit(db, user, "file.trash_purge", str(item_id or "tout"), f"{n} élément(s)", ip, level="warning")
        return n

    # ------------------------------------------------------------------ téléversement / archives
    def prepare_upload(self, rel_dir: str, filename: str, overwrite: bool) -> tuple[Path, Path]:
        """Retourne (cible finale, fichier temporaire) après validations."""
        self.check_writable()
        d = self.resolve(rel_dir)
        if not d.is_dir():
            raise FileError(400, "Le dossier de destination est invalide.")
        name = validate_name(os.path.basename((filename or "").replace("\\", "/")))
        target = d / name
        self.resolve(self.rel(target), must_exist=False)
        if os.path.lexists(target) and not overwrite:
            raise FileError(409, f"« {name} » existe déjà.", conflict=True)
        if target.is_dir():
            raise FileError(409, f"« {name} » est un dossier existant.")
        return target, d / f".{name}.nexcraft-upload"

    def finish_upload(self, target: Path, tmp: Path, user: str, ip: str = "") -> dict[str, Any]:
        existed = target.exists()
        if existed:
            self.snapshot(target, "overwrite", user)
        try:
            os.replace(tmp, target)
        except PermissionError as exc:
            tmp.unlink(missing_ok=True)
            raise FileError(423, "Fichier verrouillé : remplacement impossible.") from exc
        size = target.stat().st_size
        with self.ctx.db.scope() as db:
            audit(db, user, "file.upload", self.rel(target), f"{size} octets" + (" (remplacé)" if existed else ""), ip)
        return {"path": self.rel(target), "size": size}

    def extract_zip(self, rel: str, dest_rel: str, overwrite: bool, user: str, ip: str = "") -> dict[str, Any]:
        self.check_writable()
        src = self.resolve(rel)
        dest = self.resolve(dest_rel, must_exist=False)
        if not src.is_file() or not zipfile.is_zipfile(src):
            raise FileError(400, "Ce fichier n'est pas une archive ZIP valide.")
        if dest.exists() and not dest.is_dir():
            raise FileError(400, "La destination n'est pas un dossier.")
        max_total = self.ctx.settings.max_extract_mb * 1024 * 1024
        plan: list[tuple[zipfile.ZipInfo, Path]] = []
        conflicts: list[str] = []
        total = 0
        skipped_links = 0
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
            if len(infos) > 100_000:
                raise FileError(400, "Archive refusée : trop d'entrées.")
            for info in infos:
                name = info.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts or "\x00" in name:
                    raise FileError(400, f"Archive refusée : chemin malveillant « {name[:80]} ».")
                if not parts:
                    continue
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    skipped_links += 1
                    continue
                total += info.file_size
                if total > max_total:
                    raise FileError(413, f"Archive refusée : contenu décompressé supérieur à {self.ctx.settings.max_extract_mb} Mo.")
                if info.file_size > 50 * 2**20 and info.compress_size and info.file_size / info.compress_size > 500:
                    raise FileError(400, "Archive refusée : taux de compression suspect (zip bomb).")
                target = self.resolve(self.rel(dest) + "/" + "/".join(parts) if self.rel(dest) else "/".join(parts), must_exist=False)
                if info.is_dir():
                    plan.append((info, target))
                    continue
                if os.path.lexists(target):
                    if target.is_dir():
                        raise FileError(409, f"Conflit : « {'/'.join(parts)} » est un dossier existant.")
                    conflicts.append("/".join(parts))
                plan.append((info, target))
            if conflicts and not overwrite:
                raise FileError(409, f"{len(conflicts)} fichier(s) existent déjà : " + ", ".join(conflicts[:8]), conflict=True)
            dest.mkdir(parents=True, exist_ok=True)
            written = count = 0
            for info, target in plan:
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    self.snapshot(target, "overwrite", user)
                tmp = target.with_name(f".{target.name}.nexcraft-tmp")
                try:
                    with zf.open(info) as fin, open(tmp, "wb") as fout:
                        while chunk := fin.read(1024 * 1024):
                            written += len(chunk)
                            if written > max_total:
                                raise FileError(413, "Extraction interrompue : taille décompressée excessive.")
                            fout.write(chunk)
                    os.replace(tmp, target)
                    count += 1
                except FileError:
                    tmp.unlink(missing_ok=True)
                    raise
                except OSError as exc:
                    tmp.unlink(missing_ok=True)
                    raise FileError(500, f"Extraction impossible : {exc.strerror or exc}") from exc
        with self.ctx.db.scope() as db:
            audit(db, user, "file.extract", self.rel(src), f"→ {self.rel(dest) or '/'} ({count} fichiers)", ip)
        return {"extracted": count, "skipped_links": skipped_links, "dest": self.rel(dest)}

    def make_zip(self, rels: list[str]) -> Path:
        """Crée une archive temporaire des éléments sélectionnés (liens symboliques ignorés)."""
        if not rels:
            raise FileError(400, "Aucun élément sélectionné.")
        self.ctx.settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        out = self.ctx.settings.tmp_dir / f"download-{uuid.uuid4().hex}.zip"
        limit = self.ctx.settings.max_extract_mb * 1024 * 1024
        total = 0
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for rel in rels:
                    p = self.resolve(rel)
                    if p.is_symlink():
                        continue
                    base = p.parent
                    if p.is_file():
                        total += p.stat().st_size
                        zf.write(p, p.relative_to(base).as_posix())
                    else:
                        zf.write(p, p.relative_to(base).as_posix() + "/")
                        for dp, dn, fn in os.walk(p, followlinks=False):
                            for d in dn:
                                zf.write(os.path.join(dp, d), (Path(dp, d).relative_to(base).as_posix()) + "/")
                            for f in fn:
                                fp = Path(dp, f)
                                if fp.is_symlink() or fp.name.endswith((".nexcraft-tmp", ".nexcraft-upload")):
                                    continue
                                try:
                                    total += fp.stat().st_size
                                    if total > limit:
                                        raise FileError(413, "Sélection trop volumineuse pour une archive.")
                                    zf.write(fp, fp.relative_to(base).as_posix())
                                except PermissionError:
                                    continue
        except Exception:
            out.unlink(missing_ok=True)
            raise
        return out

    def search(self, query: str, rel_dir: str = "", limit: int = 200) -> dict[str, Any]:
        q = (query or "").strip().lower()
        if len(q) < 2:
            raise FileError(400, "Saisissez au moins 2 caractères.")
        base = self.resolve(rel_dir)
        results: list[dict[str, Any]] = []
        visited = 0
        for dp, dn, fn in os.walk(base, followlinks=False):
            dn[:] = [d for d in dn if not d.startswith(".nexcraft")]
            for name in dn + fn:
                visited += 1
                if q in name.lower():
                    full = Path(dp, name)
                    try:
                        st = full.lstat()
                    except OSError:
                        continue
                    results.append({"name": name, "path": self.rel(full), "is_dir": full.is_dir() and not full.is_symlink(),
                                    "size": None if full.is_dir() else st.st_size})
                    if len(results) >= limit:
                        return {"results": results, "truncated": True}
            if visited > 300_000:
                return {"results": results, "truncated": True}
        return {"results": results, "truncated": False}

    # ------------------------------------------------------------------ utilitaires système
    def update_properties(self, updates: dict[str, str], user: str, ip: str = "") -> None:
        """Modifie des clés de server.properties en conservant le reste du fichier."""
        p = self.root / "server.properties"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.exists() else []
        remaining = dict(updates)
        out = []
        for line in lines:
            k = line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#") and k in remaining:
                out.append(f"{k}={remaining.pop(k)}")
            else:
                out.append(line)
        out += [f"{k}={v}" for k, v in remaining.items()]
        self.snapshot(p, "edit", user)
        self._atomic_write(p, ("\n".join(out) + "\n").encode("utf-8"))
        with self.ctx.db.scope() as db:
            audit(db, user, "file.edit", "server.properties", ", ".join(updates), ip)

    def accept_eula(self, cwd_rel: str, user: str, ip: str = "") -> None:
        d = self.resolve(cwd_rel)
        p = d / "eula.txt"
        self.snapshot(p, "edit", user)
        self._atomic_write(p, b"# Acceptation du CLUF Minecraft (https://aka.ms/MinecraftEULA) via NexCraft Panel\neula=true\n")
        with self.ctx.db.scope() as db:
            audit(db, user, "server.eula", "eula.txt", "CLUF accepté par l'utilisateur", ip, level="warning")
