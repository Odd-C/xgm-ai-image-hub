import hashlib
import json
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from image_hub.auth import (
    check_login_rate_limit,
    csrf_token,
    current_user,
    require_csrf,
    rotate_csrf_token,
    verify_password,
)
from image_hub.config import settings
from image_hub.db import get_session
from image_hub.models import Generation, Project, ReferenceImage, User, new_id, utcnow
from image_hub.providers import (
    ProviderConfigError,
    freeze_profile_execution,
    get_profile,
    model_profiles,
)
from image_hub.storage import (
    InvalidImage,
    png_download_name,
    resolve_storage_key,
    store_reference,
    transcode_artifact_to_png,
)
from image_hub.worker import generation_worker

router = APIRouter()
templates = Jinja2Templates(directory=settings.template_dir)
SENTIMENTS = {"satisfied", "adopted", "dissatisfied"}


def _user_or_redirect(request: Request, session: Session) -> User | None:
    try:
        return current_user(request, session)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


def _owned_generation(session: Session, user: User, generation_id: str) -> Generation:
    generation = session.get(Generation, generation_id)
    if generation is None or (user.role != "admin" and generation.user_id != user.id):
        raise HTTPException(404, "生成记录不存在")
    return generation


def _owned_project(session: Session, user: User, project_id: str) -> Project:
    project = session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user.id,
            Project.archived_at.is_(None),
        )
    )
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def canvas_home_url(request: Request, session: Session, user: User) -> str:
    """Resolve where the shared 画布 entry sends the user.

    The session remembers the last project canvas that actually opened, but only
    a project that still exists, belongs to this user and is not archived may be
    reused. Anything else falls back to the project list, so an archived or
    deleted project can neither raise nor point at somebody else's canvas.
    """
    remembered = request.session.get("last_project_id")
    if not isinstance(remembered, str) or not remembered:
        return "/projects"
    owned_id = session.scalar(
        select(Project.id).where(
            Project.id == remembered,
            Project.user_id == user.id,
            Project.archived_at.is_(None),
        )
    )
    return f"/projects/{owned_id}" if owned_id else "/projects"


def _remember_last_project(request: Request, project: Project) -> None:
    """Record the canvas the user is on, so sub-pages can return to it."""
    request.session["last_project_id"] = project.id


def _owned_project_generation(
    session: Session, user: User, project_id: str, generation_id: str
) -> Generation:
    project = _owned_project(session, user, project_id)
    generation = session.scalar(
        select(Generation).where(
            Generation.id == generation_id,
            Generation.project_id == project.id,
        )
    )
    if generation is None:
        raise HTTPException(404, "生成记录不存在")
    return generation


def _generation_profile_id(generation: Generation) -> tuple[str, bool]:
    requested = f"{generation.provider}:{generation.model_id}"
    try:
        snapshot = json.loads(generation.provider_snapshot_json or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    snapshot_id = snapshot.get("id") if isinstance(snapshot, dict) else ""
    profile = get_profile(str(snapshot_id or requested))
    if profile:
        return profile.id, True
    if generation.provider == "api":
        upstream_model = (
            snapshot.get("upstream_model", generation.model_id)
            if isinstance(snapshot, dict)
            else generation.model_id
        )
        opaque_id = hashlib.sha256(str(upstream_model).encode()).hexdigest()[:16]
        return f"api:{opaque_id}", False
    return str(snapshot_id or requested), False


def _generation_dict(generation: Generation) -> dict:
    profile_id, profile_available = _generation_profile_id(generation)
    try:
        snapshot = json.loads(generation.provider_snapshot_json or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    upstream_model = snapshot.get("upstream_model", "") if isinstance(snapshot, dict) else ""
    model_label = generation.model_label
    if generation.provider == "api" and model_label in {generation.model_id, upstream_model}:
        model_label = "API 模型"
    return {
        "id": generation.id,
        "prompt": generation.original_prompt,
        "provider": generation.provider,
        "profile_id": profile_id,
        "profile_available": profile_available,
        "model_label": model_label,
        "parameters": json.loads(generation.parameters_json or "{}"),
        "references": json.loads(generation.reference_manifest_json or "[]"),
        "parent_generation_id": generation.parent_generation_id,
        "status": generation.status,
        "sentiment": generation.sentiment,
        "error": (
            "API 图像生成失败，请联系管理员检查服务端配置"
            if generation.provider == "api" and generation.error_message
            else generation.error_message
        ),
        "created_at": generation.created_at.isoformat(),
        "artifact_url": (
            f"/generations/{generation.id}/artifact" if generation.artifact_storage_key else ""
        ),
        "can_retry": generation.status in {"failed", "recovery_required"}
        and not generation.external_project_id
        and not generation.external_task_id,
    }


def _canvas_generation_targets(node: dict) -> list[dict]:
    """Canvas entries that may carry a generation id.

    The unified generation node keeps its results inside the active batch and an
    in-flight attempt, so the profile check has to look one level deeper than the
    flat ``generationId`` field of the legacy presentation nodes.
    """
    targets = [node]
    for key in ("activeBatch", "attempt"):
        batch = node.get(key)
        if isinstance(batch, dict) and isinstance(batch.get("results"), list):
            targets.extend(result for result in batch["results"] if isinstance(result, dict))
    return targets


def _migrate_canvas_profile_ids(state: dict, session: Session, project: Project) -> dict:
    draft = state.get("draft")
    if isinstance(draft, dict) and draft.get("profile"):
        draft_profile = get_profile(str(draft["profile"]))
        if draft_profile:
            draft["profile"] = draft_profile.id
            draft.pop("profileUnavailable", None)
        else:
            draft["profileUnavailable"] = True
    nodes = state.get("nodes")
    if not isinstance(nodes, list):
        return state
    generation_ids = {
        str(target.get("generationId"))
        for node in nodes
        if isinstance(node, dict)
        for target in _canvas_generation_targets(node)
        if target.get("generationId")
    }
    generations = {
        generation.id: generation
        for generation in session.scalars(
            select(Generation).where(
                Generation.project_id == project.id,
                Generation.id.in_(generation_ids),
            )
        ).all()
    } if generation_ids else {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for target in _canvas_generation_targets(node):
            generation = generations.get(str(target.get("generationId", "")))
            raw_profile_id = (
                f"{generation.provider}:{generation.model_id}"
                if generation
                else str(target.get("profileId", ""))
            )
            profile = get_profile(raw_profile_id) if raw_profile_id else None
            if profile:
                target["profileId"] = profile.id
                target.pop("profileUnavailable", None)
            elif raw_profile_id:
                target["profileId"] = raw_profile_id
                target["profileUnavailable"] = True
    return state


def _locked_generation_user(request: Request, session: Session) -> User:
    """Serialize one user's quota and idempotency checks before creating a task."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user = session.get(User, user_id)
    else:
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
    if (
        user is None
        or not user.is_active
        or request.session.get("auth_version") != user.auth_version
    ):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if user.must_change_password:
        raise HTTPException(status_code=403, detail="请先修改初始密码")
    return user


def _locked_project_user(request: Request, session: Session) -> User:
    """Serialize a user's default project names before inserting a project."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        user = session.get(User, user_id)
    else:
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
    if (
        user is None
        or not user.is_active
        or request.session.get("auth_version") != user.auth_version
    ):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if user.must_change_password:
        raise HTTPException(status_code=403, detail="请先修改初始密码")
    return user


def _default_project_name(session: Session, user_id: str) -> str:
    names = set(
        session.scalars(
            select(Project.name).where(Project.user_id == user_id)
        ).all()
    )
    base = "未命名项目"
    if base not in names:
        return base
    suffix = 2
    while f"{base} {suffix}" in names:
        suffix += 1
    return f"{base} {suffix}"


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(
        request, "login.html", {"error": "", "csrf_token": csrf_token(request)}
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    rate_key = f"{request.client.host if request.client else 'unknown'}:{username.strip()}"
    check_login_rate_limit(rate_key)
    user = session.scalar(select(User).where(User.username == username.strip()))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "账号或密码不正确", "csrf_token": csrf_token(request)},
            status_code=401,
        )
    check_login_rate_limit(rate_key, success=True)
    user.last_login_at = utcnow()
    user.updated_at = utcnow()
    session.commit()
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["auth_version"] = user.auth_version
    rotate_csrf_token(request)
    destination = "/account" if user.must_change_password else "/projects"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request, csrf: str = Form(...)):
    require_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/")
def home(request: Request, session: Session = Depends(get_session)):
    user = _user_or_redirect(request, session)
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/projects")
def projects_page(request: Request, session: Session = Depends(get_session)):
    user = _user_or_redirect(request, session)
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    projects = session.scalars(
        select(Project)
        .where(Project.user_id == user.id, Project.archived_at.is_(None))
        .order_by(Project.updated_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            "user": user,
            "projects": projects,
            "csrf_token": csrf_token(request),
            "canvas_url": canvas_home_url(request, session, user),
        },
    )


@router.post("/projects")
def create_project(
    request: Request,
    name: str = Form("", max_length=160),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    user = _locked_project_user(request, session)
    clean_name = name.strip()
    if not clean_name:
        clean_name = _default_project_name(session, user.id)
    if session.scalar(
        select(Project.id).where(Project.user_id == user.id, Project.name == clean_name)
    ):
        raise HTTPException(409, "项目名称已存在")
    project = Project(user_id=user.id, name=clean_name)
    session.add(project)
    session.commit()
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


@router.get("/projects/{project_id}")
def workspace(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    user = _user_or_redirect(request, session)
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    project = _owned_project(session, user, project_id)
    _remember_last_project(request, project)
    projects = session.scalars(
        select(Project)
        .where(Project.user_id == user.id, Project.archived_at.is_(None))
        .order_by(Project.updated_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "user": user,
            "project": project,
            "projects": projects,
            "profiles": [profile.public_dict() for profile in model_profiles()],
            "csrf_token": csrf_token(request),
        },
    )


@router.get("/projects/{project_id}/history")
def history_page(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    user = _user_or_redirect(request, session)
    if not user:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    project = _owned_project(session, user, project_id)
    _remember_last_project(request, project)
    projects = session.scalars(
        select(Project)
        .where(Project.user_id == user.id, Project.archived_at.is_(None))
        .order_by(Project.updated_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": user,
            "project": project,
            "projects": projects,
            "profiles": [profile.public_dict() for profile in model_profiles()],
            "canvas_url": canvas_home_url(request, session, user),
        },
    )


@router.post("/projects/{project_id}/rename")
def rename_project(
    project_id: str,
    request: Request,
    name: str = Form(..., min_length=1, max_length=160),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    user = current_user(request, session)
    project = _owned_project(session, user, project_id)
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(422, "项目名称不能为空")
    duplicate = session.scalar(
        select(Project.id).where(
            Project.user_id == user.id,
            Project.name == clean_name,
            Project.id != project.id,
        )
    )
    if duplicate:
        raise HTTPException(409, "项目名称已存在")
    project.name = clean_name
    project.updated_at = utcnow()
    session.commit()
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


@router.post("/projects/{project_id}/archive")
def archive_project(
    project_id: str,
    request: Request,
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    user = current_user(request, session)
    project = _owned_project(session, user, project_id)
    project.archived_at = utcnow()
    session.commit()
    return RedirectResponse("/projects", status_code=303)


@router.get("/api/projects/{project_id}/canvas")
def get_canvas_state(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    user = current_user(request, session)
    project = _owned_project(session, user, project_id)
    state = json.loads(project.canvas_state_json or "{}")
    return {"project_id": project.id, "state": _migrate_canvas_profile_ids(state, session, project)}


@router.put("/api/projects/{project_id}/canvas")
async def save_canvas_state(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    require_csrf(request)
    user = current_user(request, session)
    project = _owned_project(session, user, project_id)
    state = await request.json()
    if not isinstance(state, dict):
        raise HTTPException(422, "画布状态格式无效")
    existing_state = json.loads(project.canvas_state_json or "{}")
    merged_state = {**existing_state, **state}
    encoded = json.dumps(merged_state, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 2 * 1024 * 1024:
        raise HTTPException(413, "画布状态超过 2 MB")
    project.canvas_state_json = encoded
    project.updated_at = utcnow()
    session.commit()
    return {"project_id": project.id, "saved_at": project.updated_at.isoformat()}


@router.post("/api/projects/{project_id}/generations", status_code=202)
async def create_generation(
    project_id: str,
    request: Request,
    prompt: str = Form(..., min_length=1, max_length=12000),
    profile_id: str = Form(...),
    ratio: str = Form("1:1"),
    resolution: str = Form("2K"),
    quality: str = Form("standard"),
    parent_generation_id: str = Form(""),
    idempotency_key: str = Form(..., min_length=16, max_length=64),
    references: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    require_csrf(request)
    user = _locked_generation_user(request, session)
    project = _owned_project(session, user, project_id)
    existing = session.scalar(
        select(Generation).where(
            Generation.project_id == project.id,
            Generation.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return {"id": existing.id, "status": existing.status}
    profile = get_profile(profile_id)
    if profile is None or not profile.enabled:
        raise HTTPException(503, "平台或模型尚未启用")
    if (
        ratio not in profile.ratios
        or resolution not in profile.resolutions
        or quality not in profile.qualities
    ):
        raise HTTPException(422, "模型参数无效")
    if len(references) > profile.max_references:
        raise HTTPException(422, f"该模型参考图最多 {profile.max_references} 张")
    try:
        profile = freeze_profile_execution(profile)
    except ProviderConfigError as exc:
        raise HTTPException(503, f"API 路由安全校验失败：{exc}") from exc
    active_count = session.scalar(
        select(func.count()).select_from(Generation).where(
            Generation.user_id == user.id,
            Generation.status.in_(("queued", "running")),
        )
    )
    if (active_count or 0) >= settings.max_active_tasks_per_user:
        raise HTTPException(429, "当前进行中的任务已达到上限，请稍后再试")
    if parent_generation_id:
        parent = session.scalar(
            select(Generation.id).where(
                Generation.id == parent_generation_id,
                Generation.project_id == project.id,
            )
        )
        if parent is None:
            raise HTTPException(422, "来源历史记录无效")
    clean_prompt = prompt.strip()
    generation = Generation(
        user_id=user.id,
        project_id=project.id,
        idempotency_key=idempotency_key,
        original_prompt=clean_prompt,
        provider=profile.provider,
        model_id=profile.id.split(":", 1)[1],
        model_label=profile.label,
        provider_snapshot_json=json.dumps(profile.snapshot_dict(), ensure_ascii=False),
        parameters_json=json.dumps(
            {"ratio": ratio, "resolution": resolution, "quality": quality},
            ensure_ascii=False,
        ),
        parent_generation_id=parent_generation_id,
        status="queued",
    )
    session.add(generation)
    session.flush()
    manifest = []
    total_upload_bytes = 0
    try:
        for position, upload in enumerate([item for item in references if item.filename], 1):
            reference_id = new_id()
            stored = await store_reference(generation.id, reference_id, upload)
            total_upload_bytes += stored.byte_size
            if total_upload_bytes > settings.max_request_upload_bytes:
                raise InvalidImage("本次参考图总大小超过限制")
            reference = ReferenceImage(
                id=reference_id,
                generation_id=generation.id,
                position=position,
                original_name=upload.filename or f"reference-{position}",
                storage_key=stored.storage_key,
                sha256=stored.sha256,
                mime_type=stored.mime_type,
                width=stored.width,
                height=stored.height,
            )
            session.add(reference)
            manifest.append(
                {"id": reference_id, "position": position, "name": reference.original_name,
                 "sha256": stored.sha256, "width": stored.width, "height": stored.height,
                 "byte_size": stored.byte_size}
            )
    except InvalidImage as exc:
        session.rollback()
        shutil.rmtree(settings.storage_root / f"generations/{generation.id}", ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc
    generation.reference_manifest_json = json.dumps(manifest, ensure_ascii=False)
    project.updated_at = utcnow()
    session.commit()
    generation_worker.wake()
    return {"id": generation.id, "status": generation.status}


@router.get("/api/projects/{project_id}/generations")
def list_generations(
    project_id: str,
    request: Request,
    q: str = "",
    provider: str = "",
    model: str = "",
    status_filter: str = "",
    sentiment: str = "",
    limit: int = 60,
    session: Session = Depends(get_session),
):
    user = current_user(request, session)
    project = _owned_project(session, user, project_id)
    query = select(Generation).where(Generation.project_id == project.id)
    if q.strip():
        query = query.where(Generation.original_prompt.contains(q.strip()))
    if provider:
        query = query.where(Generation.provider == provider)
    if model:
        query = query.where(Generation.model_id == model)
    if status_filter:
        query = query.where(Generation.status == status_filter)
    if sentiment:
        query = query.where(Generation.sentiment == sentiment)
    generations = session.scalars(query.order_by(Generation.created_at.desc()).limit(min(limit, 100))).all()
    return {"items": [_generation_dict(item) for item in generations]}


@router.get("/api/projects/{project_id}/generations/{generation_id}")
def generation_detail(
    project_id: str,
    generation_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    user = current_user(request, session)
    generation = _owned_project_generation(session, user, project_id, generation_id)
    return _generation_dict(generation)


@router.post("/api/projects/{project_id}/generations/{generation_id}/sentiment")
async def mark_sentiment(
    project_id: str,
    generation_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    require_csrf(request)
    user = current_user(request, session)
    payload = await request.json()
    sentiment = str(payload.get("sentiment", ""))
    if sentiment not in SENTIMENTS:
        raise HTTPException(422, "标记必须是满意、采用或不满意")
    generation = _owned_project_generation(session, user, project_id, generation_id)
    if generation.status != "succeeded":
        raise HTTPException(409, "只有成功结果可以标记")
    generation.sentiment = sentiment
    session.commit()
    return {"id": generation.id, "sentiment": sentiment}


@router.post("/api/projects/{project_id}/generations/{generation_id}/retry", status_code=202)
def retry_generation(
    project_id: str,
    generation_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    require_csrf(request)
    user = current_user(request, session)
    source = _owned_project_generation(session, user, project_id, generation_id)
    if source.status not in {"failed", "recovery_required"} or source.external_task_id:
        raise HTTPException(409, "该任务不能安全重试")
    source.status = "queued"
    source.error_message = ""
    source.started_at = None
    source.finished_at = None
    source.lease_owner = ""
    source.lease_expires_at = None
    session.commit()
    generation_worker.wake()
    return {"id": source.id, "status": source.status}


@router.get("/generations/{generation_id}/artifact")
def artifact(
    generation_id: str,
    request: Request,
    download: bool = False,
    session: Session = Depends(get_session),
):
    """Serve a stored artifact.

    Inline previews stream the stored bytes untouched so opening a canvas never
    pays for a decode. A download is always re-encoded to PNG, because a
    provider may return JPEG or WebP and the download contract is "PNG".
    """
    user = current_user(request, session)
    generation = _owned_generation(session, user, generation_id)
    if not generation.artifact_storage_key:
        raise HTTPException(404, "图片不存在")
    path = resolve_storage_key(generation.artifact_storage_key)
    if not path.is_file():
        raise HTTPException(404, "图片不存在")
    if not download:
        return FileResponse(path)
    try:
        payload = transcode_artifact_to_png(path)
    except InvalidImage as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    filename = png_download_name(path.name, generation.id)
    return Response(
        content=payload,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/generations/{generation_id}/references/{reference_id}")
def reference_file(
    generation_id: str,
    reference_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    user = current_user(request, session)
    generation = _owned_generation(session, user, generation_id)
    reference = session.get(ReferenceImage, reference_id)
    if not reference or reference.generation_id != generation.id:
        raise HTTPException(404, "参考图不存在")
    return FileResponse(resolve_storage_key(reference.storage_key))


@router.get("/health")
def health(session: Session = Depends(get_session)):
    queued = session.scalar(select(Generation).where(or_(Generation.status == "queued", Generation.status == "running")).limit(1))
    return {"status": "ok", "service": "xgm-ai-image-hub", "worker_backlog": bool(queued)}
