"""Gestionnaire de fichiers (confiné au dossier du serveur)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..context import AppContext
from ..deps import Auth, client_ip, get_ctx, require
from ..services.diff import side_by_side
from ..services.file_manager import FileError

router = APIRouter(prefix="/api/files", tags=["files"])
CHUNK = 1024 * 1024


class WriteBody(BaseModel):
    path: str = Field(max_length=1000)
    content: str
    # Transmis sous forme de texte : une date en nanosecondes dépasse la précision des entiers JavaScript.
    expected_mtime_ns: int | str | None = None
    force: bool = False


class CreateBody(BaseModel):
    path: str = Field(default="", max_length=1000)  # dossier parent
    name: str = Field(max_length=200)
    type: str = Field(pattern="^(file|dir)$")


class RenameBody(BaseModel):
    path: str = Field(max_length=1000)
    new_name: str = Field(max_length=200)


class TransferBody(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)
    dest: str = Field(default="", max_length=1000)
    overwrite: bool = False


class DeleteBody(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)
    confirm: bool = False


class ZipBody(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)


class ExtractBody(BaseModel):
    path: str = Field(max_length=1000)
    dest: str = Field(default="", max_length=1000)
    overwrite: bool = False


class DiffTextBody(BaseModel):
    original: str
    modified: str


# ---------------------------------------------------------------- lecture
@router.get("/list")
def list_dir(path: str = "", sort: str = "name", order: str = "asc", auth: Auth = Depends(require("files.read")),
             ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    data = ctx.files.list_dir(path, sort, order)
    data["writable_blocked"] = ctx.file_ops_block_reason()
    return data


@router.get("/read")
def read(path: str, auth: Auth = Depends(require("files.read")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.read_text(path)


@router.get("/search")
def search(q: str, path: str = "", auth: Auth = Depends(require("files.read")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.search(q, path)


@router.get("/download")
def download(path: str, auth: Auth = Depends(require("files.read")), ctx: AppContext = Depends(get_ctx)) -> FileResponse:
    p = ctx.files.open_for_download(path)
    return FileResponse(p, filename=p.name, media_type="application/octet-stream", content_disposition_type="attachment")


@router.post("/download-zip")
def download_zip(body: ZipBody, request: Request, auth: Auth = Depends(require("files.read")), ctx: AppContext = Depends(get_ctx)) -> FileResponse:
    out = ctx.files.make_zip(body.paths)
    name = (body.paths[0].rstrip("/").split("/")[-1] or "serveur") if len(body.paths) == 1 else "selection"
    return FileResponse(out, filename=f"{name}.zip", media_type="application/zip", content_disposition_type="attachment",
                        background=BackgroundTask(lambda: out.unlink(missing_ok=True)))


# ---------------------------------------------------------------- écriture
@router.put("/write")
def write(body: WriteBody, request: Request, auth: Auth = Depends(require("files.write")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    try:
        expected = int(body.expected_mtime_ns) if body.expected_mtime_ns not in (None, "") else None
    except ValueError as exc:
        raise HTTPException(422, "expected_mtime_ns invalide.") from exc
    return ctx.files.write_text(body.path, body.content, expected, body.force, auth.user.username, client_ip(request))


@router.post("/create")
def create(body: CreateBody, request: Request, auth: Auth = Depends(require("files.write")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.create(body.path, body.name, body.type, auth.user.username, client_ip(request))


@router.post("/rename")
def rename(body: RenameBody, request: Request, auth: Auth = Depends(require("files.write")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.rename(body.path, body.new_name, auth.user.username, client_ip(request))


@router.post("/move")
def move(body: TransferBody, request: Request, auth: Auth = Depends(require("files.write")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.move(body.paths, body.dest, body.overwrite, auth.user.username, client_ip(request))


@router.post("/copy")
def copy(body: TransferBody, request: Request, auth: Auth = Depends(require("files.write")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.copy(body.paths, body.dest, body.overwrite, auth.user.username, client_ip(request))


@router.post("/delete")
def delete(body: DeleteBody, request: Request, auth: Auth = Depends(require("files.delete")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.delete(body.paths, body.confirm, auth.user.username, client_ip(request))


@router.post("/upload")
def upload(request: Request, files: list[UploadFile] = File(...), path: str = Form(""), overwrite: bool = Form(False),
           extract: bool = Form(False), auth: Auth = Depends(require("files.write")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Téléverse un ou plusieurs fichiers ; ``extract`` extrait aussitôt les ZIP dans le dossier cible."""
    limit = ctx.settings.max_upload_mb * 1024 * 1024
    ip = client_ip(request)
    results: list[dict[str, Any]] = []
    for up in files:
        name = up.filename or "fichier"
        try:
            target, tmp = ctx.files.prepare_upload(path, name, overwrite)
            size = 0
            try:
                with open(tmp, "wb") as out:
                    while chunk := up.file.read(CHUNK):
                        size += len(chunk)
                        if size > limit:
                            raise FileError(413, f"Fichier trop volumineux (maximum {ctx.settings.max_upload_mb} Mo).")
                        out.write(chunk)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            info = ctx.files.finish_upload(target, tmp, auth.user.username, ip)
            res: dict[str, Any] = {"name": name, "ok": True, **info}
            if extract and info["path"].lower().endswith(".zip"):
                ex = ctx.files.extract_zip(info["path"], path, overwrite, auth.user.username, ip)
                res["extracted"] = ex["extracted"]
            results.append(res)
        except FileError as exc:
            results.append({"name": name, "ok": False, "error": exc.message, "status": exc.status})
    if results and not any(r["ok"] for r in results):
        first = results[0]
        raise HTTPException(first.get("status", 400), first["error"])
    return {"results": results}


@router.post("/extract")
def extract(body: ExtractBody, request: Request, auth: Auth = Depends(require("files.write")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.extract_zip(body.path, body.dest, body.overwrite, auth.user.username, client_ip(request))


# ---------------------------------------------------------------- corbeille
@router.get("/trash")
def trash(auth: Auth = Depends(require("files.delete")), ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    return ctx.files.trash_list()


@router.post("/trash/{item_id}/restore")
def trash_restore(item_id: int, request: Request, auth: Auth = Depends(require("files.delete")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.trash_restore(item_id, auth.user.username, client_ip(request))


@router.delete("/trash/{item_id}")
def trash_delete(item_id: int, request: Request, auth: Auth = Depends(require("files.delete")), ctx: AppContext = Depends(get_ctx)) -> dict[str, int]:
    return {"purged": ctx.files.trash_purge(item_id, auth.user.username, client_ip(request))}


@router.delete("/trash")
def trash_empty(request: Request, auth: Auth = Depends(require("files.delete")), ctx: AppContext = Depends(get_ctx)) -> dict[str, int]:
    return {"purged": ctx.files.trash_purge(None, auth.user.username, client_ip(request))}


# ---------------------------------------------------------------- versions et comparaison
@router.get("/versions")
def versions(path: str, auth: Auth = Depends(require("files.read")), ctx: AppContext = Depends(get_ctx)) -> list[dict[str, Any]]:
    return ctx.files.versions(path)


@router.get("/versions/{version_id}")
def version_content(version_id: int, auth: Auth = Depends(require("files.read")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    info, text = ctx.files.version_text(version_id)
    return {**info, "content": text}


@router.post("/versions/{version_id}/restore")
def version_restore(version_id: int, request: Request, auth: Auth = Depends(require("files.write")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    return ctx.files.restore_version(version_id, auth.user.username, client_ip(request))


def _diff(a: str, b: str) -> dict[str, Any]:
    try:
        return side_by_side(a, b)
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc


@router.get("/diff")
def diff_versions(path: str, a: str = Query(..., description="id de version ou « current »"),
                  b: str = Query("current"), auth: Auth = Depends(require("files.read")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    def load(ref: str) -> tuple[str, str]:
        if ref == "current":
            return "Version actuelle", ctx.files.read_text(path)["content"]
        if not ref.isdigit():
            raise HTTPException(400, "Référence de version invalide.")
        info, text = ctx.files.version_text(int(ref))
        if info["path"] != ctx.files.rel(ctx.files.resolve(path, must_exist=False)):
            raise HTTPException(400, "Cette version n'appartient pas à ce fichier.")
        return f"Version #{ref} ({info['created_at']})", text

    la, ta = load(a)
    lb, tb = load(b)
    return {"a_label": la, "b_label": lb, **_diff(ta, tb)}


@router.get("/diff-files")
def diff_files(a: str, b: str, auth: Auth = Depends(require("files.read")), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    fa, fb = ctx.files.read_text(a), ctx.files.read_text(b)
    return {"a_label": fa["path"], "b_label": fb["path"], **_diff(fa["content"], fb["content"])}


@router.post("/diff-text")
def diff_text(body: DiffTextBody, auth: Auth = Depends(require("files.read"))) -> dict[str, Any]:
    return {"a_label": "Enregistré", "b_label": "Modifié", **_diff(body.original, body.modified)}
