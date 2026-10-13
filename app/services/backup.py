"""Sauvegardes et restauration.

* Sauvegarde cohérente : si le serveur tourne, ``save-off`` + ``save-all flush`` puis archivage,
  puis ``save-on`` (toujours rétabli, même en cas d'erreur).
* Archives ZIP avec manifeste, empreinte SHA-256 et vérification d'intégrité (testzip).
* Restauration : vérification, sauvegarde de sécurité de l'état actuel, extraction dans un
  dossier temporaire puis remplacement avec retour arrière en cas d'erreur.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from .. import __version__
from ..models import Backup
from ..utils.timeutil import iso, utcnow
from .audit import audit
from .file_manager import FileError, FileManager
from .minecraft import SAVED_RE, ServerStateError
from .notifications import notify
from .server_config import read_properties
from .settings_store import get_setting

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger("nexcraft.backup")
MANIFEST = "nexcraft-manifest.json"
CONFIG_EXT = {".properties", ".json", ".yml", ".yaml", ".toml", ".txt", ".cfg", ".conf"}
SKIP_NAMES = {"session.lock"}


class BackupError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def serialize(b: Backup) -> dict[str, Any]:
    return {"id": b.id, "filename": b.filename, "kind": b.kind, "status": b.status, "created_at": iso(b.created_at),
            "finished_at": iso(b.finished_at), "size": b.size, "sha256": b.sha256, "includes": b.includes,
            "note": b.note, "error": b.error, "created_by": b.created_by, "verified_at": iso(b.verified_at),
            "verify_ok": b.verify_ok, "consistent": b.consistent}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


class BackupService:
    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self.current: dict[str, Any] | None = None

    # ------------------------------------------------------------------ sélection
    @property
    def root(self) -> Path:
        return self.ctx.settings.server_dir.resolve()

    def default_includes(self) -> list[str]:
        root = self.root
        props = read_properties(root / "server.properties")
        level = props.get("level-name", "world")
        out: list[str] = []
        for name in (level, f"{level}_nether", f"{level}_the_end"):
            if (root / name).is_dir():
                out.append(name)
        for e in sorted(root.iterdir()) if root.is_dir() else []:
            if e.name.startswith(".") or e.name in out or e.is_symlink():
                continue
            if e.is_file() and e.suffix.lower() in CONFIG_EXT:
                out.append(e.name)
            elif e.is_dir() and e.name in {"plugins", "config", "defaultconfigs", "datapacks"}:
                out.append(e.name)
        return out

    def top_level_entries(self) -> list[dict[str, Any]]:
        """Entrées de premier niveau du dossier serveur (pour choisir manuellement)."""
        out = []
        for e in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if e.name.startswith(".") or e.is_symlink():
                continue
            out.append({"name": e.name, "is_dir": e.is_dir()})
        return out

    def _validated_includes(self, includes: list[str]) -> list[str]:
        fm = FileManager(self.ctx)
        out = []
        for inc in includes:
            try:
                p = fm.resolve(inc)
            except FileError as exc:
                raise BackupError(f"Élément de sauvegarde invalide « {inc} » : {exc.message}") from exc
            r = fm.rel(p)
            if r == "":
                raise BackupError("Sélectionnez des éléments précis plutôt que la racine entière.")
            if r not in out:
                out.append(r)
        return out

    # ------------------------------------------------------------------ création
    def create(self, kind: str = "manual", user: str = "système", note: str = "",
               includes: list[str] | None = None, ip: str = "") -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise BackupError("Une sauvegarde ou une restauration est déjà en cours.", 409)
        try:
            return self._create_locked(kind, user, note, includes, ip)
        finally:
            self._lock.release()

    def _create_locked(self, kind: str, user: str, note: str, includes: list[str] | None, ip: str = "") -> dict[str, Any]:
        with self.ctx.db.scope() as db:
            cfg = get_setting(db, "backup")
        chosen = includes or cfg.get("include") or self.default_includes()
        if not chosen:
            raise BackupError("Rien à sauvegarder : le dossier du serveur est vide.")
        chosen = self._validated_includes(chosen)
        now = utcnow()
        self.ctx.settings.backup_dir.mkdir(parents=True, exist_ok=True)
        name = self._unique_name(now, kind)
        out = self.ctx.settings.backup_dir / name
        tmp = out.with_suffix(".zip.part")
        with self.ctx.db.scope() as db:
            row = Backup(filename=name, kind=kind, status="running", includes=chosen, note=note[:300], created_by=user)
            db.add(row)
            db.flush()
            bid = row.id
        self.current = {"id": bid, "kind": kind, "started_at": iso(now)}
        self.ctx.events.publish({"channel": "events", "type": "backup", "state": "started", "id": bid})
        mgr = self.ctx.manager
        save_disabled = False
        consistent = mgr.status == "stopped"
        warnings: list[str] = []
        try:
            if mgr.status == "running" and cfg.get("consistent", True):
                seq = mgr.last_seq
                try:
                    mgr.send_command("save-off")
                    save_disabled = True
                    mgr.send_command("save-all flush")
                    if mgr.wait_for_log(SAVED_RE, seq, 120):
                        consistent = True
                    else:
                        warnings.append("Le serveur n'a pas confirmé la sauvegarde de ses données (délai dépassé).")
                except ServerStateError as exc:
                    warnings.append(str(exc))
            elif mgr.status != "stopped":
                warnings.append("Sauvegarde faite alors que le serveur est en transition ou sans cohérence demandée.")
            count = self._build_archive(tmp, chosen, warnings)
            os.replace(tmp, out)
            digest = sha256_file(out)
            ok, msg = self._test_zip(out)
            if not ok:
                raise BackupError(f"Archive invalide après création : {msg}")
            with self.ctx.db.scope() as db:
                row = db.get(Backup, bid)
                row.status, row.finished_at, row.size, row.sha256 = "ok", utcnow(), out.stat().st_size, digest
                row.verified_at, row.verify_ok, row.consistent = utcnow(), True, consistent
                row.note = (note + (" — " if note and warnings else "") + " ; ".join(warnings))[:300]
                audit(db, user, "backup.create", name, f"{count} fichiers, {row.size} octets", ip)
                result = serialize(row)
            self.apply_retention()
            if kind != "auto":
                notify(self.ctx, "success", "backup", "Sauvegarde terminée", f"{name} ({result['size'] // 1024 // 1024} Mo)")
            self.ctx.events.publish({"channel": "events", "type": "backup", "state": "finished", "id": bid})
            return result
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            msg = exc.message if isinstance(exc, BackupError) else str(exc)
            with self.ctx.db.scope() as db:
                row = db.get(Backup, bid)
                if row:
                    row.status, row.error, row.finished_at = "failed", msg[:2000], utcnow()
                audit(db, user, "backup.failed", name, msg[:500], ip, level="error")
            notify(self.ctx, "error", "backup", "Échec de la sauvegarde", msg[:500])
            self.ctx.events.publish({"channel": "events", "type": "backup", "state": "failed", "id": bid})
            if isinstance(exc, BackupError):
                raise
            log.exception("Échec de la sauvegarde")
            raise BackupError(f"Échec de la sauvegarde : {msg}", 500) from exc
        finally:
            if save_disabled:
                try:
                    mgr.send_command("save-on")
                except ServerStateError:
                    pass
            self.current = None

    def _unique_name(self, now, kind: str) -> str:
        """Nom d'archive unique (deux sauvegardes dans la même seconde ne doivent pas se heurter)."""
        base = f"backup-{now:%Y%m%d-%H%M%S}-{kind}"
        with self.ctx.db.scope() as db:
            taken = set(db.scalars(select(Backup.filename).where(Backup.filename.like(f"{base}%"))).all())
        i = 1
        name = f"{base}.zip"
        while name in taken or (self.ctx.settings.backup_dir / name).exists():
            i += 1
            name = f"{base}-{i}.zip"
        return name

    def _build_archive(self, tmp: Path, includes: list[str], warnings: list[str]) -> int:
        root = self.root
        count = 0
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for inc in includes:
                p = root / inc
                if p.is_symlink() or not p.exists():
                    warnings.append(f"« {inc} » ignoré (absent ou lien symbolique).")
                    continue
                if p.is_file():
                    count += self._add_file(zf, p, inc, warnings)
                    continue
                zf.write(p, inc + "/")
                for dp, dn, fn in os.walk(p, followlinks=False):
                    dn[:] = [d for d in dn if not Path(dp, d).is_symlink()]
                    for d in dn:
                        zf.write(os.path.join(dp, d), Path(dp, d).relative_to(root).as_posix() + "/")
                    for f in fn:
                        fp = Path(dp, f)
                        if f in SKIP_NAMES or fp.is_symlink() or f.endswith((".nexcraft-tmp", ".nexcraft-upload")):
                            continue
                        count += self._add_file(zf, fp, fp.relative_to(root).as_posix(), warnings)
            zf.writestr(MANIFEST, json.dumps({"nexcraft": __version__, "created": iso(utcnow()),
                                              "includes": includes, "files": count}, indent=2))
        return count

    @staticmethod
    def _add_file(zf: zipfile.ZipFile, path: Path, arc: str, warnings: list[str]) -> int:
        try:
            zf.write(path, arc)
            return 1
        except (PermissionError, OSError) as exc:
            if len(warnings) < 20:
                warnings.append(f"Fichier ignoré : {arc} ({exc.strerror or exc})")
            return 0

    # ------------------------------------------------------------------ intégrité
    @staticmethod
    def _test_zip(path: Path) -> tuple[bool, str]:
        try:
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
                if bad:
                    return False, f"entrée corrompue : {bad}"
                if MANIFEST not in zf.namelist():
                    return False, "manifeste NexCraft manquant"
            return True, "ok"
        except (zipfile.BadZipFile, OSError) as exc:
            return False, str(exc)

    def verify(self, backup_id: int) -> dict[str, Any]:
        with self.ctx.db.scope() as db:
            b = db.get(Backup, backup_id)
            if b is None:
                raise BackupError("Sauvegarde introuvable.", 404)
            path, expected = self.ctx.settings.backup_dir / b.filename, b.sha256
        if not path.is_file():
            ok, msg = False, "fichier d'archive introuvable"
        else:
            ok, msg = self._test_zip(path)
            if ok and expected and sha256_file(path) != expected:
                ok, msg = False, "l'empreinte SHA-256 ne correspond plus à celle enregistrée"
        with self.ctx.db.scope() as db:
            b = db.get(Backup, backup_id)
            b.verified_at, b.verify_ok = utcnow(), ok
            result = serialize(b)
        return {"ok": ok, "message": "Archive intègre." if ok else f"Archive invalide : {msg}", "backup": result}

    # ------------------------------------------------------------------ suppression / rétention
    def delete(self, backup_id: int, user: str, ip: str = "") -> None:
        with self.ctx.db.scope() as db:
            b = db.get(Backup, backup_id)
            if b is None:
                raise BackupError("Sauvegarde introuvable.", 404)
            (self.ctx.settings.backup_dir / b.filename).unlink(missing_ok=True)
            audit(db, user, "backup.delete", b.filename, "", ip, level="warning")
            db.delete(b)

    def apply_retention(self) -> int:
        with self.ctx.db.scope() as db:
            cfg = get_setting(db, "backup")
            ok = db.scalars(select(Backup).where(Backup.status == "ok", Backup.kind.in_(("manual", "auto")))
                            .order_by(Backup.created_at.desc())).all()
            safety = db.scalars(select(Backup).where(Backup.status == "ok", Backup.kind == "safety")
                                .order_by(Backup.created_at.desc())).all()
            keep_last = max(1, int(cfg.get("keep_last", 10)))
            max_age = int(cfg.get("max_age_days", 30))
            cutoff = utcnow() - timedelta(days=max_age) if max_age > 0 else None
            doomed = list(ok[keep_last:])
            if cutoff:
                doomed += [b for i, b in enumerate(ok[:keep_last]) if i > 0 and b.created_at < cutoff]
            doomed += [b for i, b in enumerate(safety) if i >= 5 or b.created_at < utcnow() - timedelta(days=14)]
            for b in doomed:
                (self.ctx.settings.backup_dir / b.filename).unlink(missing_ok=True)
                db.delete(b)
            if doomed:
                audit(db, None, "backup.retention", "", f"{len(doomed)} ancienne(s) sauvegarde(s) supprimée(s)")
            return len(doomed)

    def list(self) -> list[dict[str, Any]]:
        with self.ctx.db.scope() as db:
            rows = db.scalars(select(Backup).order_by(Backup.created_at.desc())).all()
            return [serialize(b) for b in rows]

    def archive_path(self, backup_id: int) -> tuple[Path, str]:
        with self.ctx.db.scope() as db:
            b = db.get(Backup, backup_id)
            if b is None or b.status != "ok":
                raise BackupError("Sauvegarde introuvable ou non terminée.", 404)
            p = self.ctx.settings.backup_dir / b.filename
        if not p.is_file():
            raise BackupError("Le fichier d'archive est introuvable sur le disque.", 404)
        return p, b.filename

    # ------------------------------------------------------------------ restauration
    def restore(self, backup_id: int, user: str, stop_server: bool = False, restart: bool = False, ip: str = "") -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise BackupError("Une sauvegarde ou une restauration est déjà en cours.", 409)
        mgr = self.ctx.manager
        self.ctx.operation_lock = "restauration d'une sauvegarde en cours"
        try:
            v = self.verify(backup_id)
            if not v["ok"]:
                raise BackupError(v["message"])
            path, _ = self.archive_path(backup_id)
            if mgr.status != "stopped":
                if not stop_server:
                    raise BackupError("Le serveur doit être arrêté avant une restauration (ou demandez son arrêt propre).", 409)
                mgr.stop()
                if mgr.status != "stopped":
                    raise BackupError("Impossible d'arrêter le serveur : restauration annulée.", 409)
            with zipfile.ZipFile(path) as zf:
                top = sorted({n.split("/")[0] for n in zf.namelist() if n != MANIFEST and n.split("/")[0]})
            for t in top:
                if t in {".", ".."} or "\\" in t or ":" in t:
                    raise BackupError(f"Archive refusée : entrée suspecte « {t} ».")
            existing = [t for t in top if (self.root / t).exists()]
            safety = None
            if existing:
                safety = self._create_locked("safety", user, f"avant restauration #{backup_id}", existing, ip)
            tmp_dir = self.root / f".nexcraft-restore-{backup_id}"
            old_dir = self.root / f".nexcraft-restore-old-{backup_id}"
            for d in (tmp_dir, old_dir):
                shutil.rmtree(d, ignore_errors=True)
            tmp_dir.mkdir()
            keep_old = False
            try:
                self._extract_all(path, tmp_dir)
                old_dir.mkdir()
                swapped: list[str] = []
                try:
                    for name in top:
                        target = self.root / name
                        if os.path.lexists(target):
                            shutil.move(str(target), str(old_dir / name))
                        shutil.move(str(tmp_dir / name), str(target))
                        swapped.append(name)
                except Exception as swap_exc:
                    failed: list[str] = []
                    for name in top:  # retour arrière : on remet l'état d'origine en place
                        t = self.root / name
                        if (old_dir / name).exists():
                            try:
                                if os.path.lexists(t):
                                    shutil.rmtree(t) if t.is_dir() and not t.is_symlink() else t.unlink()
                                shutil.move(str(old_dir / name), str(t))
                            except Exception:  # noqa: BLE001
                                failed.append(name)
                    if failed:
                        keep_old = True  # ne jamais supprimer les données d'origine si on n'a pas pu les remettre
                        raise BackupError(f"Échec de la restauration ET du retour arrière pour : {', '.join(failed)}. "
                                          f"Vos données d'origine sont conservées dans {old_dir}.", 500) from swap_exc
                    raise
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                if not keep_old:
                    shutil.rmtree(old_dir, ignore_errors=True)
            with self.ctx.db.scope() as db:
                audit(db, user, "backup.restore", path.name, f"{len(top)} élément(s) restauré(s)", ip, level="warning")
            notify(self.ctx, "success", "backup", "Restauration terminée", f"Archive {path.name} restaurée.")
            if restart:
                mgr.start(user=user, source="restore")
            return {"restored": top, "safety_backup": safety["filename"] if safety else None}
        except BackupError:
            raise
        except Exception as exc:
            log.exception("Échec de la restauration")
            notify(self.ctx, "error", "backup", "Échec de la restauration", str(exc)[:500])
            raise BackupError(f"Échec de la restauration : {exc}", 500) from exc
        finally:
            self.ctx.operation_lock = None
            self._lock.release()

    def _extract_all(self, archive: Path, dest: Path) -> None:
        limit = self.ctx.settings.max_extract_mb * 1024 * 1024 * 4
        written = 0
        dest_real = dest.resolve()
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if name == MANIFEST or not parts:
                    continue
                if name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name):
                    raise BackupError(f"Archive refusée : chemin malveillant « {name[:80]} ».")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue
                target = (dest_real.joinpath(*parts)).resolve()
                if dest_real not in target.parents and target != dest_real:
                    raise BackupError(f"Archive refusée : chemin hors destination « {name[:80]} ».")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fin, open(target, "wb") as fout:
                    while chunk := fin.read(1024 * 1024):
                        written += len(chunk)
                        if written > limit:
                            raise BackupError("Restauration interrompue : taille décompressée excessive.")
                        fout.write(chunk)
