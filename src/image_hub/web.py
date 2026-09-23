import hashlib
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from image_hub import agent, prompt_optimize
from image_hub.assets import (
    AssetError,
    AssetPermissionError,
    asset_dict,
    can_delete,
    delete_asset,
    get_visible_asset,
    list_visible_assets,
    materialise_asset_reference,
    store_asset,
    uploader_names,
)
from image_hub.auth import (
    ADMIN_ROLES,
    ROLE_ADMIN,
    check_login_rate_limit,
    csrf_token,
    current_user,
    is_admin,
    require_csrf,
    rotate_csrf_token,
    verify_password,
)
from image_hub.config import settings
from image_hub.db import SessionLocal, get_session
from image_hub.models import (
    ASSET_VISIBILITY_SHARED,
    Generation,
    Project,
    ReferenceImage,
    User,
    new_id,
    utcnow,
)
from image_hub.providers import (
    ProviderConfigError,
    freeze_profile_execution,
    get_profile,
    routed_account_ids,
    routed_api_routes,
    routed_model_profiles,
    snapshot_upstream_names,
)
from image_hub.storage import (
    InvalidImage,
    png_download_name,
    resolve_storage_key,
    store_reference,
    transcode_artifact_to_png,
    validate_image_payload,
)
from image_hub.worker import generation_worker

router = APIRouter()
templates = Jinja2Templates(directory=settings.template_dir)
SENTIMENTS = {"satisfied", "adopted", "dissatisfied"}
#: 能进管理台的角色。模板里用来决定「系统管理」入口是否出现。
ADMIN_ROLE_VALUES = tuple(sorted(ADMIN_ROLES))
ROLE_LABELS = {
    "user": "普通账号",
    ROLE_ADMIN: "管理员",
}
templates.env.filters["role_label"] = lambda value: ROLE_LABELS.get(value, value)
templates.env.filters["datetime"] = lambda value: (
    value.strftime("%Y-%m-%d %H:%M") if value else "—"
)


def _user_or_redirect(request: Request, session: Session) -> User | None:
    try:
        return current_user(request, session)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


def _owned_generation(session: Session, user: User, generation_id: str) -> Generation:
    """按 id 取一条生成记录。本人可见，管理员可见全部；不可见与不存在都回 404。"""
    generation = session.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(404, "生成记录不存在")
    if generation.user_id == user.id or is_admin(user):
        return generation
    raise HTTPException(404, "生成记录不存在")


def _owned_project(session: Session, user: User, project_id: str) -> Project:
    project = session.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.user_id == user.id,
            Project.archived_at.is_(None),
            Project.deleted_at.is_(None),
        )
    )
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def canvas_home_url(request: Request, session: Session, user: User) -> str:
    """Resolve where the shared 画布 entry sends the user."""
    remembered = request.session.get("last_project_id")
    if not isinstance(remembered, str) or not remembered:
        return "/projects"
    owned_id = session.scalar(
        select(Project.id).where(
            Project.id == remembered,
            Project.user_id == user.id,
            Project.archived_at.is_(None),
            Project.deleted_at.is_(None),
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
    # 旧行的 ``model_label`` 可能仍然是当年写进去的裸上游模型名
    upstream_names = {generation.model_id, *snapshot_upstream_names(snapshot)}
    model_label = generation.model_label
    if generation.provider == "api" and model_label in upstream_names:
        model_label = "API 模型"
    return {
        "id": generation.id,
        # ``prompt`` 始终是**用户写的那一句**。
        "prompt": generation.original_prompt,
        "optimized_prompt": generation.optimized_prompt or "",
        "prompt_optimize_state": generation.prompt_optimize_state
        or prompt_optimize.STATE_NONE,
        "prompt_optimize_mode": generation.prompt_optimize_mode or "",
        # Agent 模式的诊断结论（"为什么会像 AI / 结构为什么漂移"）。
        "prompt_optimize_note": generation.prompt_optimize_note or "",
        "prompt_optimize_error": generation.prompt_optimize_error or "",
        "effective_prompt": generation.optimized_prompt or generation.original_prompt,
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
    """Canvas entries that may carry a generation id."""
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


def _require_active_session_user(request: Request, user: User | None) -> User:
    """会话与账号仍然有效，否则清掉会话并 401。"""
    if (
        user is None
        or not user.is_active
        or user.deleted_at is not None
        or request.session.get("auth_version") != user.auth_version
    ):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return user


def _unlocked_generation_user(request: Request, session: Session) -> User:
    """**不加锁**地取当前提交用户（供锁外预演使用）。"""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return _require_active_session_user(request, session.get(User, user_id))


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
    return _require_active_session_user(request, user)


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
        or user.deleted_at is not None
        or request.session.get("auth_version") != user.auth_version
    ):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
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
    if (
        user is None
        or not user.is_active
        or user.deleted_at is not None
        or not verify_password(password, user.password_hash)
    ):
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
    # 「必须先改初始密码」不再是一道硬门槛：登录后直接进项目列表
    return RedirectResponse("/projects", status_code=status.HTTP_303_SEE_OTHER)


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
        .where(
            Project.user_id == user.id,
            Project.archived_at.is_(None),
            Project.deleted_at.is_(None),
        )
        .order_by(Project.updated_at.desc())
    ).all()
    if not projects:
        # 新账号不该先看到一个空列表：直接建一个未命名项目并送进画布
        project = Project(user_id=user.id, name=_default_project_name(session, user.id))
        session.add(project)
        session.commit()
        return RedirectResponse(f"/projects/{project.id}", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            "user": user,
            "projects": projects,
            "csrf_token": csrf_token(request),
            "canvas_url": canvas_home_url(request, session, user),
            "admin_roles": ADMIN_ROLE_VALUES,
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
        .where(
            Project.user_id == user.id,
            Project.archived_at.is_(None),
            Project.deleted_at.is_(None),
        )
        .order_by(Project.updated_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "user": user,
            "project": project,
            "projects": projects,
            "profiles": [
                profile.public_dict() for profile in routed_model_profiles(session)
            ],
            "csrf_token": csrf_token(request),
            "admin_roles": ADMIN_ROLE_VALUES,
            # 类目/工作类型的可选项：**唯一来源在后端**，前端从页面里读这份 JSON
            "prompt_optimize_options": prompt_optimize.options_payload(),
            # agent 节点的轴可选项（平台/市场/卖点/参考图职责 + 类目/工作类型）。
            "agent_options": agent.options_payload(),
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
        .where(
            Project.user_id == user.id,
            Project.archived_at.is_(None),
            Project.deleted_at.is_(None),
        )
        .order_by(Project.updated_at.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": user,
            "project": project,
            "projects": projects,
            "profiles": [
                profile.public_dict() for profile in routed_model_profiles(session)
            ],
            "canvas_url": canvas_home_url(request, session, user),
            "admin_roles": ADMIN_ROLE_VALUES,
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


@router.post("/projects/{project_id}/delete")
def delete_project(
    project_id: str,
    request: Request,
    csrf: str = Form(...),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
):
    """软删项目：从列表里彻底移除，但任务记录与图片一张都不删。"""
    require_csrf(request, csrf)
    user = current_user(request, session)
    project = _owned_project(session, user, project_id)
    if confirm != "delete":
        raise HTTPException(422, "删除项目需要二次确认")
    project.deleted_at = utcnow()
    project.updated_at = utcnow()
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


def _reference_plan(raw: str, file_count: int) -> list[tuple[str, str | int]]:
    """把前端的参考图顺序解析成执行计划。"""
    if not raw.strip():
        return [("file", index) for index in range(file_count)]
    try:
        tokens = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "参考图顺序格式无效") from exc
    if not isinstance(tokens, list):
        raise HTTPException(422, "参考图顺序格式无效")
    plan: list[tuple[str, str | int]] = []
    consumed: set[int] = set()
    for token in tokens:
        if not isinstance(token, str) or len(token) < 3 or token[1] != ":":
            raise HTTPException(422, "参考图顺序格式无效")
        kind, value = token[0], token[2:]
        if kind == "a":
            plan.append(("asset", value))
        elif kind == "f" and value.isdigit():
            index = int(value)
            if index >= file_count or index in consumed:
                raise HTTPException(422, "参考图顺序与上传文件不匹配")
            consumed.add(index)
            plan.append(("file", index))
        else:
            raise HTTPException(422, "参考图顺序格式无效")
    return plan


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
    # 参考图的**顺序**：JSON 数组，元素形如 "a:<素材id>" 或 "f:<文件下标>"。
    reference_order: str = Form(""),
    # 「优化提示词」开关（前端提交时带）。
    optimize_prompt: str = Form(""),
    # 用哪种优化模式：``optimize``（纯文本）或 ``agent``（带参考图的多模态诊断+改写）。
    prompt_optimize_mode: str = Form(""),
    # agent 节点的**轴选择**（JSON 字符串）。
    agent_spec: str = Form(""),
    session: Session = Depends(get_session),
):
    require_csrf(request)
    # ── 阶段 A：纯内存解析（不碰库、不需要锁）────────────────────────────
    uploaded = [item for item in references if item.filename]
    plan = _reference_plan(reference_order, len(uploaded))
    clean_prompt = prompt.strip()
    # 参考图按**任务 id** 落盘，所以 id 必须在写盘之前就定下来。
    generation_id = new_id()

    # ── 阶段 B：**锁外预演**（独立只读会话）──────────────────────────────
    with SessionLocal() as probe:
        probe_user = _unlocked_generation_user(request, probe)
        probe_project = _owned_project(probe, probe_user, project_id)
        probe_existing = probe.scalar(
            select(Generation).where(
                Generation.project_id == probe_project.id,
                Generation.idempotency_key == idempotency_key,
            )
        )
        if probe_existing:
            # 幂等短路：**一个字节都不写盘**（重复提交不该有落盘副作用）。
            return {"id": probe_existing.id, "status": probe_existing.status}
        # 账号级未完成任务上限：权威判定在锁内
        probe_active = int(
            probe.scalar(
                select(func.count())
                .select_from(Generation)
                .where(
                    Generation.user_id == probe_user.id,
                    Generation.status.in_(("queued", "running")),
                )
            )
            or 0
        )
        if probe_active >= settings.max_active_tasks_per_user:
            raise HTTPException(429, "当前进行中的任务已达到上限，请稍后再试")
        # 账号池选号：**只读**
        routed = routed_account_ids(probe)
        # API 通道的选路同理由：**只读**
        routed_routes = routed_api_routes(probe)
        # ⚠️ get_profile 在 LibTV 目录**冷缓存**时会打上游（约 6.6s）。
        profile = get_profile(profile_id, accounts=routed, api_routes=routed_routes)
        if profile is None or not profile.enabled:
            raise HTTPException(503, "平台或模型尚未启用")
        # agent 节点：结构化轴驱动的生成。
        agent_payload = agent.normalize_spec(agent_spec) if agent_spec.strip() else None
        if agent_payload is not None:
            if profile.provider != "api":
                raise HTTPException(422, "agent 节点只支持 API 渠道的模型")
            if not prompt_optimize.is_configured(mode=prompt_optimize.MODE_PLAN):
                raise HTTPException(503, "服务端尚未配置提示词计划 skill，agent 节点暂不可用")
            allowed_ratios = agent.platform_aspect_ratios(agent_payload["platform"])
            if allowed_ratios and ratio not in allowed_ratios:
                ratio = allowed_ratios[0]
        if (
            ratio not in profile.ratios
            or resolution not in profile.resolutions
            # 有的模型没有质量维度（内置能力表里 qualities 为空，例如 nano-banana 系列）
            or (profile.qualities and quality not in profile.qualities)
        ):
            raise HTTPException(422, "模型参数无效")
        if len(plan) > profile.max_references:
            raise HTTPException(422, f"该模型参考图最多 {profile.max_references} 张")
        try:
            # 选出的号进入冻结配置 ⇒ 执行侧按 ``provider_account_id`` 取对应凭据。
            profile = freeze_profile_execution(
                profile, account_id=routed.get(profile.provider, "")
            )
        except ProviderConfigError as exc:
            raise HTTPException(503, f"API 路由安全校验失败：{exc}") from exc
        # agent 节点与提示词优化**互斥**：一个是「另一类节点」，一个是「生成前的改写」。
        optimize_requested = bool(optimize_prompt.strip()) and agent_payload is None
        optimize_mode = prompt_optimize.normalize_mode(prompt_optimize_mode)
        optimize_active = optimize_requested and prompt_optimize.is_configured(mode=optimize_mode)

    # ── 阶段 C：锁外**落盘**（本次改动的重点）──────────────────────────────
    prepared: list[dict] = []
    total_upload_bytes = 0
    try:
        for position, (kind, value) in enumerate(plan, 1):
            reference_id = new_id()
            if kind != "file":
                prepared.append(
                    {"kind": "asset", "value": str(value), "reference_id": reference_id}
                )
                continue
            upload = uploaded[int(value)]
            stored = await store_reference(generation_id, reference_id, upload)
            total_upload_bytes += stored.byte_size
            if total_upload_bytes > settings.max_request_upload_bytes:
                raise InvalidImage("本次参考图总大小超过限制")
            prepared.append(
                {
                    "kind": "file",
                    "reference_id": reference_id,
                    "original_name": upload.filename or f"reference-{position}",
                    "stored": stored,
                }
            )
    except InvalidImage as exc:
        shutil.rmtree(settings.storage_root / f"generations/{generation_id}", ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc

    # ── 阶段 D：锁内只做纯数据库工作 ──────────────────────────────────────
    user = _locked_generation_user(request, session)
    project = _owned_project(session, user, project_id)
    # 权威幂等复检：预演到此刻之间可能有并发的同名提交落进来。
    existing = session.scalar(
        select(Generation).where(
            Generation.project_id == project.id,
            Generation.idempotency_key == idempotency_key,
        )
    )
    if existing:
        # 阶段 C 写的文件这次用不上 —— 必须清掉，否则留下孤儿目录。
        shutil.rmtree(settings.storage_root / f"generations/{generation_id}", ignore_errors=True)
        return {"id": existing.id, "status": existing.status}
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
    generation = Generation(
        # id 在阶段 A 就定了 —— 参考图按它落盘，两边必须同一个
        id=generation_id,
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
        # agent 节点：记下节点类型与**收敛后**的轴选择。``mode`` 为空即普通生图。
        mode=agent.MODE_AGENT if agent_payload is not None else "",
        agent_spec_json=(
            json.dumps(agent_payload, ensure_ascii=False) if agent_payload else "{}"
        ),
        # 记下**实际生效的模式**（没生效则为空）：历史里要能说清这次走的哪条路。
        prompt_optimize_mode=optimize_mode if optimize_active else "",
        # 用户开了开关但服务端没配 skill：**记成 skipped 而不是 none**。
        prompt_optimize_state=(
            prompt_optimize.STATE_REQUESTED
            if optimize_active
            else prompt_optimize.STATE_SKIPPED
            if optimize_requested
            else prompt_optimize.STATE_NONE
        ),
    )
    session.add(generation)
    session.flush()
    # 上传的字节**已经在阶段 C 落盘**（那时没有持锁）
    manifest = []
    try:
        for position, item in enumerate(prepared, 1):
            reference_id = item["reference_id"]
            if item["kind"] == "asset":
                # 素材库里的图：服务端直接把它挂进本次生成，**客户端不重传字节**。
                asset = get_visible_asset(session, user, item["value"])
                stored = materialise_asset_reference(asset, generation.id, reference_id)
                original_name = asset.original_name or f"reference-{position}"
            else:
                stored = item["stored"]
                original_name = item["original_name"]
            reference = ReferenceImage(
                id=reference_id,
                generation_id=generation.id,
                position=position,
                original_name=original_name,
                storage_key=stored.storage_key,
                sha256=stored.sha256,
                mime_type=stored.mime_type,
                width=stored.width,
                height=stored.height,
            )
            session.add(reference)
            manifest.append(
                {
                    "id": reference_id,
                    "position": position,
                    "name": reference.original_name,
                    "sha256": stored.sha256,
                    "width": stored.width,
                    "height": stored.height,
                    "byte_size": stored.byte_size,
                    # 可溯源：这一格取自素材库的哪张素材（本次上传的文件则为空）。
                    "asset_id": item["value"] if item["kind"] == "asset" else "",
                }
            )
    except AssetError as exc:
        session.rollback()
        shutil.rmtree(settings.storage_root / f"generations/{generation.id}", ignore_errors=True)
        raise HTTPException(422, f"参考图不可用：{exc}") from exc
    except InvalidImage as exc:
        session.rollback()
        shutil.rmtree(settings.storage_root / f"generations/{generation.id}", ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc
    generation.reference_manifest_json = json.dumps(manifest, ensure_ascii=False)
    project.updated_at = utcnow()
    session.commit()
    generation_worker.wake()
    # 提示**一律不阻断**
    warnings: list[str] = []
    if agent_payload is not None:
        warnings = agent.submission_warnings(agent_payload, has_reference_images=bool(plan))
    return {
        "id": generation.id,
        "status": generation.status,
        "warnings": warnings,
        "warning": "\n".join(warnings),
    }


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
    user = _locked_generation_user(request, session)
    source = _owned_project_generation(session, user, project_id, generation_id)
    # 守卫必须与 _generation_dict() 交给前端的 can_retry 完全一致
    if (
        source.status not in {"failed", "recovery_required"}
        or source.external_project_id
        or source.external_task_id
    ):
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
    """Serve a stored artifact."""
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


# ---------------------------------------------------------------------------


def _optimize_images(session: Session, user: User, asset_ids: str, files: list[UploadFile],
                     staging: Path) -> list[str]:
    """把这次要交给模型的参考图落到临时目录，返回路径列表。"""
    paths: list[str] = []
    raw_ids = (asset_ids or "").strip()
    if raw_ids:
        try:
            parsed = json.loads(raw_ids)
        except json.JSONDecodeError as exc:
            raise HTTPException(422, "参考素材格式无效") from exc
        if not isinstance(parsed, list):
            raise HTTPException(422, "参考素材格式无效")
        for asset_id in parsed:
            if not isinstance(asset_id, str) or not asset_id:
                continue
            # 可见性校验走素材域，拿不到就 404（与素材库接口一致，不泄露存在性）。
            asset = get_visible_asset(session, user, asset_id)
            source = resolve_storage_key(asset.storage_key)
            if source.is_file():
                paths.append(str(source))

    for index, upload in enumerate([item for item in files if item.filename]):
        payload = upload.file.read(settings.max_upload_bytes + 1)
        if not payload or len(payload) > settings.max_upload_bytes:
            raise HTTPException(422, "图片为空或超过 30 MB")
        try:
            validate_image_payload(payload)
        except InvalidImage as exc:
            raise HTTPException(422, str(exc)) from exc
        suffix = Path(upload.filename or "").suffix or ".png"
        target = staging / f"reference-{index}{suffix}"
        target.write_bytes(payload)
        paths.append(str(target))
    return paths


def _reverse_input_paths(session: Session, user: User, asset_id: str, generation_id: str,
                         files: list[UploadFile], staging: Path) -> list[str]:
    """反推的输入图：**素材库**里的资产、**已生成**的结果、或刚传上来的文件。"""
    paths: list[str] = []
    if asset_id.strip():
        # 可见性校验走素材域，拿不到就 404（与素材库接口同一口径，不泄露存在性）。
        asset = get_visible_asset(session, user, asset_id.strip())
        source = resolve_storage_key(asset.storage_key)
        if source.is_file():
            paths.append(str(source))
    if generation_id.strip():
        generation = session.get(Generation, generation_id.strip())
        # 归属校验与其它接口一致：别人的生成结果一律当作不存在。
        if (
            generation is not None
            and generation.user_id == user.id
            and generation.artifact_storage_key
        ):
            artifact = resolve_storage_key(generation.artifact_storage_key)
            if artifact.is_file():
                paths.append(str(artifact))
    for index, upload in enumerate(files):
        payload = upload.file.read(settings.max_upload_bytes + 1)
        if not payload or len(payload) > settings.max_upload_bytes:
            raise HTTPException(422, "图片为空或超过 30 MB")
        try:
            validate_image_payload(payload)
        except InvalidImage as exc:
            raise HTTPException(422, str(exc)) from exc
        suffix = Path(upload.filename or "").suffix or ".png"
        target = staging / f"reverse-{index}{suffix}"
        target.write_bytes(payload)
        paths.append(str(target))
    return paths


@router.post("/api/prompt-reverse")
def reverse_prompt_endpoint(
    request: Request,
    asset_id: str = Form(""),
    generation_id: str = Form(""),
    file: UploadFile | None = File(default=None),
    session: Session = Depends(get_session),
):
    """反推提示词接口（同步阻塞，所以写成 def）。"""
    require_csrf(request)
    user = current_user(request, session)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
    if not prompt_optimize.is_configured(mode=prompt_optimize.MODE_REVERSE):
        raise HTTPException(503, "服务端尚未配置反推提示词所用的视觉模型，请联系管理员")

    staging = settings.storage_root / f"prompt-reverse-staging/{new_id()}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        try:
            paths = _reverse_input_paths(
                session, user, asset_id, generation_id,
                [item for item in [file] if item is not None and item.filename], staging,
            )
        except AssetError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not paths:
            raise HTTPException(422, "反推提示词需要一张图片作输入")
        try:
            reversed_prompt, _usage = prompt_optimize.reverse_prompt(image_paths=paths)
        except RuntimeError as exc:
            # 脚本失败/未找到/超时都归到这里，让用户看到原因。
            raise HTTPException(422, f"反推提示词失败：{exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {"prompt": reversed_prompt}


@router.post("/api/prompt-optimize")
def optimize_prompt_endpoint(
    request: Request,
    prompt: str = Form(..., min_length=1, max_length=12000),
    mode: str = Form(""),
    category: str = Form(""),
    work_type: str = Form(""),
    reference_asset_ids: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    """把用户写好的提示词优化/改写，**同步**返回结果（前端填回输入框）。"""
    require_csrf(request)
    user = current_user(request, session)
    resolved_mode = prompt_optimize.normalize_mode(mode)
    if not prompt_optimize.is_configured(mode=resolved_mode):
        raise HTTPException(
            503, f"提示词优化（{resolved_mode} 模式）尚未配置或未启用，请联系管理员"
        )
    if resolved_mode == prompt_optimize.MODE_AGENT and not (reference_asset_ids.strip() or files):
        # 与脚本侧的守卫一致：这个模式要读图，没有图就不该走。
        raise HTTPException(422, "Agent 模式需要参考图，请先在该节点接入参考图片")

    staging = settings.storage_root / f"prompt-optimize-staging/{new_id()}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        try:
            image_paths = _optimize_images(session, user, reference_asset_ids, files, staging)
        except AssetError as exc:
            # 素材不存在/不可见 → 404；与素材库接口同一套口径。
            raise HTTPException(404, str(exc)) from exc
        try:
            optimized, note = prompt_optimize.optimize_prompt(
                prompt.strip(),
                mode=resolved_mode,
                image_paths=image_paths,
                category=category,
                work_type=work_type,
            )
        except RuntimeError as exc:
            # 脚本失败/未找到/超时都归到这里，让用户看到原因。
            raise HTTPException(422, f"提示词优化失败：{exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "prompt": optimized,
        "diagnosis": note,
        "mode": resolved_mode,
        "category": prompt_optimize.normalize_category(category),
        "work_type": prompt_optimize.normalize_work_type(work_type),
    }


# ---------------------------------------------------------------------------


@router.get("/assets")
def assets_page(request: Request, session: Session = Depends(get_session)):
    user = _user_or_redirect(request, session)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        assets = list_visible_assets(session, user)
        names = uploader_names(session, assets)
    except AssetError as exc:
        raise HTTPException(403, str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "assets.html",
        {
            "user": user,
            "assets": assets,
            "uploader_names": names,
            "can_delete": lambda asset: can_delete(asset, user),
            "is_manager": is_admin(user),
            "shared_visibility": ASSET_VISIBILITY_SHARED,
            # 页面上要写清保留期：素材到期会被清掉，用户有权知道。
            "asset_retention_days": settings.asset_retention_days,
            "canvas_url": canvas_home_url(request, session, user),
            "csrf_token": csrf_token(request),
        },
    )


@router.get("/api/assets")
def list_assets(request: Request, session: Session = Depends(get_session)):
    user = current_user(request, session)
    try:
        assets = list_visible_assets(session, user)
        names = uploader_names(session, assets)
    except AssetError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {
        "assets": [
            asset_dict(
                asset,
                uploader_name=names.get(asset.uploaded_by, ""),
                can_remove=can_delete(asset, user),
            )
            for asset in assets
        ]
    }


@router.post("/api/assets", status_code=201)
async def upload_asset(
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    """上传即持久化。画布上的图片走这里落地，之后切换项目/刷新都不用重传。"""
    require_csrf(request)
    user = current_user(request, session)
    if not file.filename:
        raise HTTPException(422, "没有收到文件")
    try:
        asset, reused = await store_asset(session, user, file)
    except InvalidImage as exc:
        session.rollback()
        raise HTTPException(422, str(exc)) from exc
    except AssetError as exc:
        session.rollback()
        raise HTTPException(403, str(exc)) from exc
    session.commit()
    name = user.display_name or user.username
    return {
        "asset": asset_dict(asset, uploader_name=name, can_remove=can_delete(asset, user)),
        "reused": reused,
    }


@router.post("/api/assets/{asset_id}/delete")
def remove_asset(
    asset_id: str, request: Request, session: Session = Depends(get_session)
):
    require_csrf(request)
    user = current_user(request, session)
    try:
        delete_asset(session, user, asset_id)
    except AssetPermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except AssetError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@router.get("/assets/{asset_id}/content")
def asset_content(
    asset_id: str, request: Request, session: Session = Depends(get_session)
):
    user = current_user(request, session)
    try:
        asset = get_visible_asset(session, user, asset_id)
    except AssetError as exc:
        raise HTTPException(404, str(exc)) from exc
    path = resolve_storage_key(asset.storage_key)
    if not path.is_file():
        raise HTTPException(404, "素材文件不存在")
    return FileResponse(path)


@router.get("/health")
def health(session: Session = Depends(get_session)):
    queued = session.scalar(select(Generation).where(or_(Generation.status == "queued", Generation.status == "running")).limit(1))
    return {"status": "ok", "service": "image-hub", "worker_backlog": bool(queued)}
