import csv
import io
import json
import re
import secrets
import shutil
from datetime import datetime
from math import ceil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from image_hub import api_routing, libtv_mcp, prompt_optimize, provider_pool
from image_hub.assets import purge_expired_assets
from image_hub.auth import (
    ROLE_ADMIN,
    ROLE_USER,
    csrf_token,
    current_user,
    hash_password,
    require_admin,
    require_csrf,
    rotate_csrf_token,
    verify_password,
)
from image_hub.config import settings
from image_hub.db import get_session
from image_hub.models import (
    AdminAuditEvent,
    Asset,
    Generation,
    Project,
    ProviderAccount,
    User,
    utcnow,
)
from image_hub.providers import (
    API_QUALITIES,
    ProviderConfigError,
    account_credentials_ready,
    api_model_id_for_public,
    api_model_rows,
    api_routes_text,
    api_upstream_candidates,
    probe_libtv_connection,
    probe_lovart_connection,
    public_api_config,
    public_api_model_headers,
    public_api_model_notes,
    public_api_model_routes,
    public_api_route_states,
    public_api_routes,
    public_native_credentials,
    recover_generation,
    reorder_api_models,
    routed_model_profiles,
    save_api_config,
    save_lovart_credentials,
    snapshot_upstream_names,
)
from image_hub.web import canvas_home_url

router = APIRouter()
templates = Jinja2Templates(directory=settings.template_dir)
ROLES = {ROLE_USER, ROLE_ADMIN}
USER_STATUSES = {"active", "inactive"}
TASK_STATUSES = {"queued", "running", "recovery_required", "failed", "succeeded"}

ROLE_LABELS = {
    ROLE_USER: "普通账号",
    ROLE_ADMIN: "管理员",
}


def _format_datetime(value: datetime | None) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M")


templates.env.filters["datetime"] = _format_datetime
templates.env.filters["role_label"] = lambda value: ROLE_LABELS.get(value, value)
templates.env.globals["admin_roles"] = (ROLE_ADMIN,)


def _flash(request: Request, kind: str, message: str) -> None:
    request.session["flash"] = {"kind": kind, "message": message}


def _pop_flash(request: Request) -> dict | None:
    value = request.session.pop("flash", None)
    return value if isinstance(value, dict) else None


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def _page_number(value: int) -> int:
    return max(1, value)


def _page_size(value: int) -> int:
    return min(100, max(1, value))


def _parse_date(value: str, *, end: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, "日期筛选格式无效") from exc
    if end and len(value) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _safe_error(generation: Generation) -> str:
    if not generation.error_message:
        return "—"
    if generation.provider == "api":
        return "API 图像生成失败，请检查服务端配置"
    text = re.sub(r"https?://\S+", "[已隐藏地址]", generation.error_message)
    text = re.sub(r"(?i)(bearer|api[_ -]?key|token)\s*[:=]?\s*\S+", r"\1 [已隐藏]", text)
    return text[:180]


def _safe_model_label(generation: Generation) -> str:
    """Return a display label that never exposes server-side API routing."""
    label = generation.model_label or ""
    if generation.provider != "api":
        return label
    try:
        snapshot = json.loads(generation.provider_snapshot_json or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    # 上游名是一个**集合**：展示名解绑之后同一个模型身份可以有好几个叫法
    upstream_names = {generation.model_id, *snapshot_upstream_names(snapshot)}
    if not label or label in upstream_names:
        return "API 模型"
    return label


def _admin_context(
    request: Request, session: Session, user: User, active: str, **extra
) -> dict:
    return {
        "user": user,
        "active_admin_page": active,
        "csrf_token": csrf_token(request),
        "flash": _pop_flash(request),
        "canvas_url": canvas_home_url(request, session, user),
        **extra,
    }


def _admin_page_user(request: Request, session: Session) -> User | RedirectResponse:
    """管理台页面准入：管理员即可进入。"""
    user = current_user(request, session, allow_password_change=True)
    require_admin(user)
    return user


def _locked_admin_and_target(
    request: Request, session: Session, target_user_id: str | None = None
) -> tuple[User, User | None]:
    user_id = request.session.get("user_id")
    auth_version = request.session.get("auth_version")
    if not user_id:
        raise HTTPException(401, "请先登录")
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        actor = session.get(User, user_id)
        target = session.get(User, target_user_id) if target_user_id else None
    else:
        actor = session.scalar(select(User).where(User.id == user_id).with_for_update())
        target = (
            session.scalar(select(User).where(User.id == target_user_id).with_for_update())
            if target_user_id
            else None
        )
    if not actor or not actor.is_active or actor.deleted_at is not None or actor.auth_version != auth_version:
        request.session.clear()
        raise HTTPException(401, "请先登录")
    require_admin(actor)
    if target_user_id and target is None:
        raise HTTPException(404, "账号不存在")
    return actor, target


def _audit_source(request: Request | None) -> tuple[str, str]:
    """从请求里取审计的「来源」两件套：操作者 IP 与 User-Agent。"""
    if request is None:
        return "", ""
    client = request.client
    ip = (client.host if client else "") or ""
    agent = request.headers.get("user-agent", "") or ""
    return ip[:45], agent[:200]


def _audit(
    session: Session,
    actor: User | None,
    action: str,
    target_type: str,
    target_id: str,
    summary: str,
    request: Request | None = None,
) -> None:
    """追加一条审计事件。**不提交事务**：由调用方和业务变更写在同一个事务里。"""
    actor_ip, actor_agent = _audit_source(request)
    session.add(
        AdminAuditEvent(
            actor_user_id=actor.id if actor else None,
            action=action[:80],
            target_type=target_type[:40],
            target_id=target_id[:160],
            summary=summary[:500],
            actor_ip=actor_ip,
            actor_agent=actor_agent,
        )
    )


def _enabled_admin_count(session: Session) -> int:
    """启用的管理员账号数，用来守住「最后一个管理员」。"""
    return int(
        session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == ROLE_ADMIN, User.is_active == 1)
        )
        or 0
    )


@router.get("/account")
def account_page(request: Request, session: Session = Depends(get_session)):
    try:
        user = current_user(request, session, allow_password_change=True)
    except HTTPException as exc:
        if exc.status_code == 401:
            return _redirect("/login")
        raise
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "user": user,
            "csrf_token": csrf_token(request),
            "flash": _pop_flash(request),
            "canvas_url": canvas_home_url(request, session, user),
            "admin_roles": (ROLE_ADMIN,),
        },
    )


@router.post("/account/profile")
def update_account_profile(
    request: Request,
    display_name: str = Form("", max_length=120),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    user = current_user(request, session, allow_password_change=True)
    user.display_name = display_name.strip()
    user.updated_at = utcnow()
    session.commit()
    _flash(request, "success", "显示名称已更新")
    return _redirect("/account")


@router.post("/account/password")
def change_account_password(
    request: Request,
    current_password: str = Form(..., max_length=200),
    new_password: str = Form(..., max_length=200),
    confirm_password: str = Form(..., max_length=200),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    user = current_user(request, session, allow_password_change=True)
    if len(new_password) < 8:
        _flash(request, "error", "新密码至少需要 8 个字符")
        return _redirect("/account")
    if new_password != confirm_password:
        _flash(request, "error", "两次输入的新密码不一致")
        return _redirect("/account")
    if not verify_password(current_password, user.password_hash):
        _flash(request, "error", "当前密码不正确")
        return _redirect("/account")
    user.password_hash = hash_password(new_password)
    user.auth_version += 1
    user.must_change_password = 0
    user.updated_at = utcnow()
    _audit(
        session,
        user,
        "account.password_changed",
        "user",
        user.id,
        "用户自行修改密码",
        request=request,
    )
    session.commit()
    request.session["auth_version"] = user.auth_version
    rotate_csrf_token(request)
    _flash(request, "success", "密码已修改，其他旧会话已退出")
    return _redirect("/account")


@router.get("/admin")
def admin_overview(request: Request, session: Session = Depends(get_session)):
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    user_scope: list = []
    task_scope: list = []
    project_scope: list = []

    user_counts = {
        (row[0], int(row[1]))
        for row in session.execute(
            select(User.is_active, func.count()).where(*user_scope).group_by(User.is_active)
        )
    }
    enabled_users = next((count for active, count in user_counts if active), 0)
    disabled_users = next((count for active, count in user_counts if not active), 0)
    role_counts = dict(
        session.execute(
            select(User.role, func.count()).where(*user_scope).group_by(User.role)
        ).all()
    )
    task_counts = dict(
        session.execute(
            select(Generation.status, func.count())
            .where(*task_scope)
            .group_by(Generation.status)
        ).all()
    )
    recent_users = session.scalars(
        select(User).where(*user_scope).order_by(User.created_at.desc()).limit(6)
    ).all()
    recent_tasks = session.scalars(
        select(Generation)
        .where(Generation.status.in_(("recovery_required", "failed")), *task_scope)
        .order_by(
            case((Generation.status == "recovery_required", 0), else_=1),
            Generation.created_at.desc(),
        )
        .limit(6)
    ).all()
    profiles = routed_model_profiles(session)
    provider_rows = [("libtv", "LibTV"), ("lovart", "Lovart"), ("api", "API")]
    provider_summary = []
    for provider, label in provider_rows:
        items = [profile for profile in profiles if profile.provider == provider]
        provider_summary.append(
            {
                "id": provider,
                "label": label,
                "available": any(item.enabled for item in items),
                "model_count": len(items),
                "enabled_count": sum(1 for item in items if item.enabled),
            }
        )
    active_projects = int(
        session.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.archived_at.is_(None), *project_scope)
        )
        or 0
    )
    return templates.TemplateResponse(
        request,
        "admin_overview.html",
        _admin_context(
            request,
            session,
            user,
            "overview",
            enabled_users=enabled_users,
            disabled_users=disabled_users,
            role_counts=role_counts,
            task_counts=task_counts,
            active_projects=active_projects,
            recent_users=recent_users,
            recent_tasks=recent_tasks,
            provider_summary=provider_summary,
            safe_error=_safe_error,
            safe_label=_safe_model_label,
        ),
    )


@router.get("/admin/users")
def admin_users(
    request: Request,
    q: str = "",
    status_filter: str = "",
    role: str = "",
    page: int = 1,
    per_page: int = 30,
    session: Session = Depends(get_session),
):
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    page, per_page = _page_number(page), _page_size(per_page)
    conditions = []
    # 已删除的账号从列表里消失（数据仍在库里，任务与存储全都保留）。
    conditions.append(User.deleted_at.is_(None))
    search = q.strip()
    if search:
        conditions.append(
            or_(
                User.username.contains(search),
                User.display_name.contains(search),
                User.department.contains(search),
            )
        )
    if status_filter in USER_STATUSES:
        conditions.append(User.is_active == (1 if status_filter == "active" else 0))
    if role in ROLES:
        conditions.append(User.role == role)
    total = int(
        session.scalar(select(func.count()).select_from(User).where(*conditions)) or 0
    )
    users = session.scalars(
        select(User)
        .where(*conditions)
        .order_by(User.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    ids = [item.id for item in users]
    project_counts = dict(
        session.execute(
            select(Project.user_id, func.count())
            .where(Project.user_id.in_(ids))
            .group_by(Project.user_id)
        ).all()
    ) if ids else {}
    generation_counts = dict(
        session.execute(
            select(Generation.user_id, func.count())
            .where(Generation.user_id.in_(ids))
            .group_by(Generation.user_id)
        ).all()
    ) if ids else {}
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        _admin_context(
            request,
            session,
            user,
            "users",
            users=users,
            project_counts=project_counts,
            generation_counts=generation_counts,
            q=search,
            status_filter=status_filter,
            role_filter=role,
            page=page,
            per_page=per_page,
            pages=max(1, ceil(total / per_page)),
            total=total,
        ),
    )


def _release_username(session: Session, target: User) -> str:
    """把注销账号占着的用户名释放出来，同时保住这行记录的可追溯性。"""
    stamp = f"#deleted#{target.id[:8]}"
    base = target.username.split("#deleted#")[0][: 80 - len(stamp)]
    candidate = f"{base}{stamp}"
    suffix = 1
    while session.scalar(select(User.id).where(User.username == candidate)):
        candidate = f"{base}#{suffix}{stamp}"[:80]
        suffix += 1
    previous = target.username
    target.username = candidate
    return previous


def _find_username_holder(session: Session, username: str) -> User | None:
    return session.scalar(select(User).where(User.username == username))


@router.post("/admin/users")
def create_user(
    request: Request,
    username: str = Form(..., min_length=2, max_length=80),
    password: str = Form(..., min_length=8, max_length=200),
    display_name: str = Form("", max_length=120),
    department: str = Form("", max_length=120),
    role: str = Form(ROLE_USER),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    actor, _ = _locked_admin_and_target(request, session)
    clean_username = username.strip()
    if role not in ROLES:
        raise HTTPException(422, "角色无效")
    holder = _find_username_holder(session, clean_username)
    if holder is not None:
        if holder.deleted_at is None:
            raise HTTPException(409, "用户名已存在")
        # 历史遗留：这条用户名被一个已注销的账号占着（注销释放是后加的）
        _release_username(session, holder)
        session.flush()
    target = User(
        username=clean_username,
        display_name=display_name.strip(),
        department=department.strip(),
        role=role,
        password_hash=hash_password(password),
        must_change_password=1,
    )
    session.add(target)
    session.flush()
    _audit(
        session,
        actor,
        "user.created",
        "user",
        target.id,
        f"新建账号 {clean_username}（{ROLE_LABELS.get(role, role)}）",
        request=request,
    )
    session.commit()
    _flash(request, "success", "账号已创建，首次登录需要修改初始密码")
    return _redirect("/admin/users")


@router.post("/admin/users/{user_id}/profile")
def edit_user_profile(
    user_id: str,
    request: Request,
    display_name: str = Form("", max_length=120),
    department: str = Form("", max_length=120),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    actor, target = _locked_admin_and_target(request, session, user_id)
    assert target is not None
    target.display_name = display_name.strip()
    target.department = department.strip()
    target.updated_at = utcnow()
    _audit(
        session,
        actor,
        "user.profile_updated",
        "user",
        target.id,
        "编辑账号资料",
        request=request,
    )
    session.commit()
    _flash(request, "success", "账号资料已更新")
    return _redirect("/admin/users")


@router.post("/admin/users/{user_id}/role")
def change_user_role(
    user_id: str,
    request: Request,
    role: str = Form(...),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    actor, target = _locked_admin_and_target(request, session, user_id)
    assert target is not None
    if role not in ROLES:
        raise HTTPException(422, "角色无效")
    if target.id == actor.id:
        raise HTTPException(409, "不能修改当前登录账号的角色")
    if target.role == role:
        _flash(request, "success", "账号角色未变化")
        return _redirect("/admin/users")
    if target.role == ROLE_ADMIN and role == ROLE_USER and _enabled_admin_count(session) <= 1:
        raise HTTPException(409, "不能降级系统最后一个启用管理员")
    previous = target.role
    target.role = role
    target.auth_version += 1
    target.updated_at = utcnow()
    _audit(
        session,
        actor,
        "user.role_changed",
        "user",
        target.id,
        f"角色 {ROLE_LABELS.get(previous, previous)} → {ROLE_LABELS.get(role, role)}",
        request=request,
    )
    session.commit()
    _flash(request, "success", "账号角色已更新")
    return _redirect("/admin/users")


@router.post("/admin/users/{user_id}/reset-password")
def reset_user_password(
    user_id: str,
    request: Request,
    temporary_password: str = Form(..., max_length=200),
    confirm_password: str = Form(..., max_length=200),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    actor, target = _locked_admin_and_target(request, session, user_id)
    assert target is not None
    if target.id == actor.id:
        raise HTTPException(409, "请在账号设置中修改自己的密码")
    if len(temporary_password) < 8:
        raise HTTPException(422, "临时密码至少需要 8 个字符")
    if temporary_password != confirm_password:
        raise HTTPException(422, "两次输入的临时密码不一致")
    target.password_hash = hash_password(temporary_password)
    target.must_change_password = 1
    target.auth_version += 1
    target.updated_at = utcnow()
    _audit(
        session,
        actor,
        "user.password_reset",
        "user",
        target.id,
        "管理员重置密码",
        request=request,
    )
    session.commit()
    _flash(request, "success", "临时密码已重置，旧会话已失效")
    return _redirect("/admin/users")


@router.post("/admin/users/{user_id}/toggle")
def toggle_user(
    user_id: str,
    request: Request,
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    actor, target = _locked_admin_and_target(request, session, user_id)
    assert target is not None
    if target.id == actor.id:
        raise HTTPException(409, "不能停用当前登录管理员")
    disabling = bool(target.is_active)
    if disabling and target.role == ROLE_ADMIN and _enabled_admin_count(session) <= 1:
        raise HTTPException(409, "不能停用系统最后一个启用管理员")
    target.is_active = 0 if disabling else 1
    if disabling:
        target.auth_version += 1
    target.updated_at = utcnow()
    action = "user.disabled" if disabling else "user.enabled"
    summary = "停用账号并撤销旧会话" if disabling else "启用账号"
    _audit(
        session,
        actor,
        action,
        "user",
        target.id,
        summary,
        request=request,
    )
    session.commit()
    _flash(request, "success", "账号已停用，数据仍完整保留" if disabling else "账号已启用")
    return _redirect("/admin/users")


@router.post("/admin/users/{user_id}/delete")
def delete_user(
    user_id: str,
    request: Request,
    csrf: str = Form(...),
    confirm: str = Form(""),
    session: Session = Depends(get_session),
):
    """两条硬约束："""
    require_csrf(request, csrf)
    actor, target = _locked_admin_and_target(request, session, user_id)
    assert target is not None
    if target.id == actor.id:
        raise HTTPException(409, "不能删除当前登录账号")
    if confirm != "delete":
        raise HTTPException(422, "删除账号需要二次确认")
    if target.is_active and target.role == ROLE_ADMIN and _enabled_admin_count(session) <= 1:
        raise HTTPException(409, "不能删除系统最后一个启用管理员")
    target.deleted_at = utcnow()
    # 双保险：deleted_at 负责从列表消失，is_active=0 负责连登录都进不来。
    target.is_active = 0
    target.auth_version += 1
    target.updated_at = utcnow()
    _release_username(session, target)
    _audit(
        session,
        actor,
        "user.deleted",
        "user",
        target.id,
        f"软删账号 {target.username}；数据保留",
        request=request,
    )
    session.commit()
    _flash(request, "success", "账号已删除；项目与任务均保留")
    return _redirect("/admin/users")


def _owned_provider_account(session: Session, account_id: str) -> ProviderAccount | None:
    """按 id 取池里的一个号。"""
    return session.scalar(select(ProviderAccount).where(ProviderAccount.id == account_id))


def _resolve_account_form_value(session: Session, provider: str, raw: str) -> str | None:
    """校验表单里的平台账号：非法或不归属当前渠道时返回 None。"""
    if not raw:
        return ""
    account = _owned_provider_account(session, raw)
    if account is None or account.provider != provider:
        return None
    return account.id


def _provider_pool_view(session: Session, provider: str, label: str) -> dict:
    """管理页用的账号池视图。**不含任何凭据**，只有状态与顺序。"""
    accounts = provider_pool.pool_accounts(session, provider)
    # 「当前走哪个号」按**真实路由口径**算：带凭据就绪性。
    current = provider_pool.select_account(
        session, provider, ready=lambda account: account_credentials_ready(provider, account.id)
    )
    return {
        "id": provider,
        "label": label,
        "accounts": [
            {
                "id": account.id,
                "label": account.label,
                "enabled": bool(account.enabled),
                "exhausted": account.exhausted_at is not None,
                # 上游错误原文要展示给管理员：识别词表未经真实校准
                "reason": account.exhausted_reason or account.last_error_message,
                "credentials_ready": account_credentials_ready(provider, account.id),
                "is_current": bool(current and current.id == account.id),
            }
            for account in accounts
        ],
    }


@router.get("/admin/providers")
def admin_providers(request: Request, session: Session = Depends(get_session)):
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    profiles = routed_model_profiles(session)
    provider_rows = [("libtv", "LibTV"), ("lovart", "Lovart"), ("api", "API")]
    provider_cards = []
    for provider, label in provider_rows:
        items = [item for item in profiles if item.provider == provider]
        ratios = sorted({ratio for item in items for ratio in item.ratios})
        resolutions = sorted({value for item in items for value in item.resolutions})
        provider_cards.append(
            {
                "id": provider,
                "label": label,
                "available": any(item.enabled for item in items),
                "enabled_count": sum(1 for item in items if item.enabled),
                "model_count": len(items),
                "ratios": ratios,
                "resolutions": resolutions,
                "max_references": max((item.max_references for item in items), default=0),
            }
        )
    # 账号池视图：只覆盖平台渠道（``api`` 通道没有账号概念）。
    account_pools = {
        provider: _provider_pool_view(session, provider, label)
        for provider, label in provider_rows
        if provider_pool.is_pool_provider(provider)
    }
    api_profiles = [item.public_dict() for item in profiles if item.provider == "api"]
    # 请求头要回显，否则管理员改一个别的字段再保存就会把它们静默清空
    model_headers = public_api_model_headers()
    # 路由、模型介绍同理 —— 不回显，管理员改个别的字段再保存就把它清空了。
    model_routes = public_api_model_routes()
    model_notes = public_api_model_notes()
    route_states = public_api_route_states(session)
    return templates.TemplateResponse(
        request,
        "admin_providers.html",
        _admin_context(
            request,
            session,
            user,
            "providers",
            provider_cards=provider_cards,
            api_profiles=api_profiles,
            # 只有**真正落过盘**的模型行才带序号（模板据此决定哪几行参与拖动换序）。
            api_stored_model_rows=len(api_model_rows()),
            model_headers=model_headers,
            model_routes=model_routes,
            model_notes=model_notes,
            route_states=route_states,
            api_config=public_api_config(),
            api_routes_text=api_routes_text(),
            api_routes=public_api_routes(),
            api_quality_options=sorted(API_QUALITIES),
            account_pools=account_pools,
            # 「单账号配置」= 池为空时的回落路径
            native_credentials=public_native_credentials(),
        ),
    )


@router.post("/admin/providers/libtv/connect")
def connect_libtv(
    request: Request,
    csrf: str = Form(...),
    account_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """发起 LibTV OAuth 授权：生成绑定当前管理员的授权链接。"""
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    # 池里的哪个号：空串 = 单账号配置（池为空时的回落路径）。
    account_id = _resolve_account_form_value(session, "libtv", account_id)
    if account_id is None:
        _flash(request, "error", "该平台账号不存在或不属于当前管理员")
        return _redirect("/admin/providers")
    try:
        authorize_url = libtv_mcp.build_authorization_url(account_id)
    except RuntimeError as exc:
        _flash(request, "error", str(exc))
        return _redirect("/admin/providers")
    _audit(
        session,
        admin,
        "provider.libtv_connect_started",
        "provider",
        "libtv",
        "发起授权",
        request=request,
    )
    session.commit()
    return templates.TemplateResponse(
        request,
        "admin_libtv_connect.html",
        _admin_context(
            request, session, admin, "providers",
            authorize_url=authorize_url,
            redirect_uri=settings.libtv_oauth_redirect_uri,
        ),
    )


@router.get("/admin/providers/libtv/callback")
def libtv_oauth_callback(
    request: Request,
    session: Session = Depends(get_session),
):
    """OAuth 自动回跳：本机浏览管理页时，授权完成会直接落回这里。"""
    admin = current_user(request, session)
    require_admin(admin)
    source = str(request.url)
    try:
        message = libtv_mcp.complete_authorization(source)
    except RuntimeError as exc:
        _flash(request, "error", str(exc))
        return _redirect("/admin/providers")
    _audit(
        session,
        admin,
        "provider.libtv_authorized",
        "provider",
        "libtv",
        message,
        request=request,
    )
    session.commit()
    _flash(request, "success", message)
    return _redirect("/admin/providers")


@router.post("/admin/providers/libtv/paste")
def paste_libtv_callback(
    request: Request,
    callback_url: str = Form(""),
    csrf: str = Form(...),
    account_id: str = Form(""),
    session: Session = Depends(get_session),
):
    """远程浏览场景：管理员把授权后浏览器地址栏的完整 URL 粘贴回来。"""
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    account_id = _resolve_account_form_value(session, "libtv", account_id)
    if account_id is None:
        _flash(request, "error", "该平台账号不存在或不属于当前管理员")
        return _redirect("/admin/providers")
    try:
        # account_id 为空时由授权会话（state → pending）决定挂到哪个账号槽上。
        message = libtv_mcp.complete_authorization(callback_url, account_id)
    except RuntimeError as exc:
        _flash(request, "error", str(exc))
        return _redirect("/admin/providers")
    _audit(
        session,
        admin,
        "provider.libtv_authorized",
        "provider",
        "libtv",
        message,
        request=request,
    )
    session.commit()
    _flash(request, "success", message)
    return _redirect("/admin/providers")


@router.post("/admin/providers/libtv/disconnect")
def disconnect_libtv(
    request: Request,
    confirm_clear: str = Form(""),
    csrf: str = Form(...),
    account_id: str = Form(""),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    if confirm_clear != "disconnect-libtv":
        _flash(request, "error", "断开 LibTV 授权需要再次明确确认")
        return _redirect("/admin/providers")
    account_id = _resolve_account_form_value(session, "libtv", account_id)
    if account_id is None:
        _flash(request, "error", "该平台账号不存在或不属于当前管理员")
        return _redirect("/admin/providers")
    libtv_mcp.disconnect(account_id)
    _audit(
        session,
        admin,
        "provider.libtv_disconnected",
        "provider",
        "libtv",
        "已断开授权",
        request=request,
    )
    session.commit()
    _flash(request, "success", "LibTV 授权已断开")
    return _redirect("/admin/providers")


@router.post("/admin/providers/libtv/models/refresh")
def refresh_libtv_models(
    request: Request,
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    """显式刷新 LibTV 模型目录（活的：search_models + 逐模型 get_model）。"""
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    if not libtv_mcp.authorized():
        _flash(request, "error", "LibTV 尚未授权连接")
        return _redirect("/admin/providers")
    try:
        models = libtv_mcp.fetch_model_catalog()
    except RuntimeError as exc:
        _flash(request, "error", f"模型目录刷新失败：{exc}")
        return _redirect("/admin/providers")
    message = f"LibTV 模型目录已刷新（{len(models)} 个图像模型）"
    _audit(
        session,
        admin,
        "provider.libtv_models_refreshed",
        "provider",
        "libtv",
        message,
        request=request,
    )
    session.commit()
    _flash(request, "success", message)
    return _redirect("/admin/providers")


@router.post("/admin/providers/libtv/check")
def check_libtv(
    request: Request,
    csrf: str = Form(...),
    account_id: str = Form(""),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    account_id = _resolve_account_form_value(session, "libtv", account_id)
    if account_id is None:
        _flash(request, "error", "该平台账号不存在或不属于当前管理员")
        return _redirect("/admin/providers")
    ok, message = probe_libtv_connection(account_id)
    _audit(
        session,
        admin,
        "provider.libtv_checked",
        "provider",
        "libtv",
        message,
        request=request,
    )
    session.commit()
    _flash(request, "success" if ok else "error", message)
    return _redirect("/admin/providers")


@router.post("/admin/providers/lovart/credentials")
def configure_lovart_credentials(
    request: Request,
    lovart_access_key: str = Form(""),
    lovart_secret_key: str = Form(""),
    lovart_skill_script: str = Form(""),
    clear_credentials: str = Form(""),
    confirm_clear: str = Form(""),
    action: str = Form(""),
    csrf: str = Form(...),
    account_id: str = Form(""),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    # 池里的哪个号：空串 = 单账号配置（池为空时的回落路径）。每个号各存一对 AK/SK。
    account_id = _resolve_account_form_value(session, "lovart", account_id)
    if account_id is None:
        _flash(request, "error", "该平台账号不存在或不属于当前管理员")
        return _redirect("/admin/providers")
    clearing = clear_credentials == "on"
    if clearing and confirm_clear != "clear-lovart":
        _flash(request, "error", "清除 Lovart 配置需要再次明确确认")
        return _redirect("/admin/providers")
    try:
        result = save_lovart_credentials(
            lovart_access_key,
            lovart_secret_key,
            lovart_skill_script,
            clear=clearing,
            account_id=account_id,
        )
    except ProviderConfigError as exc:
        _flash(request, "error", str(exc))
        return _redirect("/admin/providers")
    if action == "check":
        ok, message = probe_lovart_connection(account_id)
        _audit(
            session,
            admin,
            "provider.lovart_checked",
            "provider",
            "lovart",
            message,
            request=request,
        )
        session.commit()
        _flash(request, "success" if ok else "error", message)
        return _redirect("/admin/providers")
    summary = f"Lovart 配置{'已清除' if result == 'cleared' else '已更新' if result == 'saved' else '保持不变'}"
    _audit(
        session,
        admin,
        "provider.lovart_credentials_cleared"
        if result == "cleared"
        else "provider.lovart_credentials_saved",
        "provider",
        "lovart",
        summary,
        request=request,
    )
    session.commit()
    _flash(
        request,
        "success",
        "Lovart 凭据未变化（输入框留空即保持不变）"
        if result == "kept"
        else summary,
    )
    return _redirect("/admin/providers")


# ── 平台账号池：可配置多个 LibTV / Lovart 账号 ────────────────────────────────


@router.post("/admin/providers/accounts/add")
def add_provider_account(
    request: Request,
    provider: str = Form(...),
    label: str = Form(""),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    if not provider_pool.is_pool_provider(provider):
        _flash(request, "error", "该渠道不支持账号池")
        return _redirect("/admin/providers")
    accounts = provider_pool.pool_accounts(session, provider)
    account = ProviderAccount(
        provider=provider,
        label=label.strip()[:120],
        # 排在最后：新加的号默认是备用，不会把当前正在用的号挤掉。
        position=max((item.position for item in accounts), default=-1) + 1,
    )
    session.add(account)
    # 先 flush 才拿得到 id（主键是 Python 侧默认值，落 INSERT 时才赋值），审计要记它。
    session.flush()
    _audit(
        session,
        admin,
        "provider.account_added",
        "provider_account",
        account.id,
        f"{provider} 账号已加入账号池（第 {account.position + 1} 位）",
        request=request,
    )
    session.commit()
    _flash(request, "success", "账号已加入池；请为它单独完成授权/凭据配置")
    return _redirect("/admin/providers")


@router.post("/admin/providers/accounts/update")
def update_provider_account(
    request: Request,
    account_id: str = Form(...),
    action: str = Form(...),
    label: str = Form(""),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    """改名 / 启用 / 停用。"""
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    if action not in {"rename", "enable", "disable"}:
        _flash(request, "error", "不支持的账号操作")
        return _redirect("/admin/providers")
    account = _owned_provider_account(session, account_id)
    if account is None:
        _flash(request, "error", "该平台账号不存在或不属于当前管理员")
        return _redirect("/admin/providers")
    if action == "rename":
        account.label = label.strip()[:120]
        summary = f"{account.provider} 账号已改名：{account.label or '（未命名）'}"
    else:
        account.enabled = 1 if action == "enable" else 0
        summary = f"{account.provider} 账号已{'启用' if action == 'enable' else '停用'}"
    _audit(
        session,
        admin,
        f"provider.account_{action}d" if action != "rename" else "provider.account_renamed",
        "provider_account",
        account.id,
        summary,
        request=request,
    )
    session.commit()
    _flash(request, "success", summary)
    return _redirect("/admin/providers")


@router.post("/admin/providers/accounts/move")
def move_provider_account(
    request: Request,
    account_id: str = Form(...),
    direction: str = Form(...),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    """固定顺序里上移 / 下移一格（交换两行的 position）。"""
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    if direction not in {"up", "down"}:
        _flash(request, "error", "不支持的排序操作")
        return _redirect("/admin/providers")
    account = _owned_provider_account(session, account_id)
    if account is None:
        _flash(request, "error", "该平台账号不存在或不属于当前管理员")
        return _redirect("/admin/providers")
    ordered = provider_pool.pool_accounts(session, account.provider)
    index = next(i for i, item in enumerate(ordered) if item.id == account.id)
    neighbour_index = index - 1 if direction == "up" else index + 1
    if not 0 <= neighbour_index < len(ordered):
        _flash(request, "error", "已经在顺序的尽头了")
        return _redirect("/admin/providers")
    neighbour = ordered[neighbour_index]
    account_position, neighbour_position = account.position, neighbour.position
    spare = max(item.position for item in ordered) + 1
    account.position = spare
    session.flush()
    neighbour.position = account_position
    session.flush()
    account.position = neighbour_position
    session.flush()
    summary = f"{account.provider} 账号顺序已{'上移' if direction == 'up' else '下移'}"
    _audit(
        session,
        admin,
        "provider.account_reordered",
        "provider_account",
        account.id,
        summary,
        request=request,
    )
    session.commit()
    _flash(request, "success", summary)
    return _redirect("/admin/providers")


@router.post("/admin/providers/accounts/clear_exhausted")
def clear_provider_account_exhausted(
    request: Request,
    account_id: str = Form(...),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    """手工清除「额度耗尽」标记。"""
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    account = _owned_provider_account(session, account_id)
    if account is None:
        _flash(request, "error", "该平台账号不存在或不属于当前管理员")
        return _redirect("/admin/providers")
    if not provider_pool.clear_exhausted(session, account.id):
        session.commit()
        _flash(request, "error", "该账号当前没有额度耗尽标记")
        return _redirect("/admin/providers")
    summary = f"{account.provider} 账号的额度耗尽标记已清除，重新参与路由"
    _audit(
        session,
        admin,
        "provider.account_quota_cleared",
        "provider_account",
        account.id,
        summary,
        request=request,
    )
    session.commit()
    _flash(request, "success", summary)
    return _redirect("/admin/providers")


@router.post("/admin/providers/accounts/delete")
def delete_provider_account(
    request: Request,
    account_id: str = Form(...),
    confirm_clear: str = Form(""),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    """把号移出账号池，并顺手清掉它的服务端凭据。"""
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    if confirm_clear != "delete-provider-account":
        _flash(request, "error", "移出账号池需要再次明确确认")
        return _redirect("/admin/providers")
    account = _owned_provider_account(session, account_id)
    if account is None:
        _flash(request, "error", "该平台账号不存在或不属于当前管理员")
        return _redirect("/admin/providers")
    provider = account.provider
    cleanup_failed = False
    try:
        if provider == "libtv":
            libtv_mcp.disconnect(account.id)
        else:
            save_lovart_credentials("", "", clear=True, account_id=account.id)
    except (RuntimeError, ProviderConfigError):
        cleanup_failed = True
    session.delete(account)
    _audit(
        session,
        admin,
        "provider.account_removed",
        "provider_account",
        account_id,
        f"{provider} 账号已从账号池移除"
        + ("（服务端凭据清理失败，需手工处理）" if cleanup_failed else "，服务端凭据已清理"),
        request=request,
    )
    session.commit()
    _flash(
        request,
        "error" if cleanup_failed else "success",
        "账号已移除，但服务端凭据清理失败，请手工删除该账号的凭据文件"
        if cleanup_failed
        else "账号已移除，服务端凭据已清理",
    )
    return _redirect("/admin/providers")


@router.post("/admin/providers/api")
def configure_api_provider(
    request: Request,
    base_url: str = Form(...),
    api_key: str = Form(""),
    models: str = Form(...),
    api_routes: str | None = Form(None),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    admin = current_user(request, session)
    if not 8 <= len(base_url) <= 500:
        raise HTTPException(422, "API Base URL 长度无效")
    if len(api_key) > 1000:
        raise HTTPException(422, "API Key 长度超过限制")
    if not 1 <= len(models) <= 12000:
        raise HTTPException(422, "API 模型配置长度无效")
    # ``api_routes`` 为 ``None`` = 这个表单字段**根本不在**请求里（旧页面/程序化调用）
    if api_routes is not None and len(api_routes) > 12000:
        raise HTTPException(422, "API 路由配置长度无效")
    try:
        save_api_config(base_url, api_key, models, api_routes)
    except ProviderConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    model_count = len([item for item in models.split(",") if item.strip()])
    route_count = len([item for item in (api_routes or "").splitlines() if item.strip()])
    _audit(
        session,
        admin,
        "provider.api_saved",
        "provider",
        "api",
        f"保存 API 中转配置；模型 {model_count} 项；"
        f"路由{'保持' if api_routes is None else f' {route_count} 条'}；"
        f"密钥{'已更新' if api_key else '保持'}",
        request=request,
    )
    session.commit()
    _flash(request, "success", "API 中转配置已安全保存")
    return _redirect("/admin/providers")


@router.post("/admin/providers/api/reorder")
def reorder_api_provider_models(
    request: Request,
    order: str = Form(...),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    """只调整 API 模型行在清单里的先后顺序 —— 行的内容一个字节都不动。"""
    require_csrf(request, csrf)
    admin = current_user(request, session)
    try:
        count = reorder_api_models(order)
    except ProviderConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    _audit(
        session,
        admin,
        "provider.api_models_reordered",
        "provider_config",
        "api",
        f"API 模型顺序已调整（{count} 行）",
        request=request,
    )
    session.commit()
    _flash(request, "success", "模型顺序已保存")
    return _redirect("/admin/providers")


@router.post("/admin/providers/api/routes/clear_exhausted")
def clear_api_route_exhausted(
    request: Request,
    model: str = Form(...),
    route: str = Form(""),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    """手工清除某条 API 路由的「额度耗尽」标记（``route`` 留空 = 该模型全部路由）。"""
    require_csrf(request, csrf)
    admin = current_user(request, session)
    model_id = api_model_id_for_public(model)
    if not model_id:
        _flash(request, "error", "该 API 模型不存在或已被移除")
        return _redirect("/admin/providers")
    route_name = route.strip()
    declared = [item["route"] for item in public_api_route_states(session).get(model, ())]
    if route_name and route_name not in declared:
        # 路由名由表单回传，陈旧或越权的值都落在这里：宁可如实报错
        _flash(request, "error", "该路由不属于这个模型")
        return _redirect("/admin/providers")
    # 一个模型在几家上游都有名字
    cleared = sum(
        api_routing.clear_exhausted(session, name, route_name)
        for name in api_upstream_candidates(model_id)
    )
    if not cleared:
        _flash(request, "error", "该路由当前没有额度耗尽标记")
        return _redirect("/admin/providers")
    summary = (
        f"API 路由 {route_name} 的额度耗尽标记已清除"
        if route_name
        else "该 API 模型全部路由的额度耗尽标记已清除"
    )
    _audit(
        session,
        admin,
        "provider.api_route_quota_cleared",
        "provider",
        "api",
        summary,
        request=request,
    )
    session.commit()
    _flash(request, "success", summary)
    return _redirect("/admin/providers")


@router.get("/admin/tasks")
def admin_tasks(
    request: Request,
    q: str = "",
    status_filter: str = "",
    provider: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    per_page: int = 30,
    session: Session = Depends(get_session),
):
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    page, per_page = _page_number(page), _page_size(per_page)
    conditions = []
    search = q.strip()
    if search:
        conditions.append(
            or_(
                Generation.id.contains(search),
                User.username.contains(search),
                User.display_name.contains(search),
                Project.name.contains(search),
            )
        )
    if status_filter in TASK_STATUSES:
        conditions.append(Generation.status == status_filter)
    if provider in {"libtv", "lovart", "api"}:
        conditions.append(Generation.provider == provider)
    start, end = _parse_date(date_from), _parse_date(date_to, end=True)
    if start:
        conditions.append(Generation.created_at >= start)
    if end:
        conditions.append(Generation.created_at <= end)
    joined = select(Generation).join(User, Generation.user_id == User.id).join(
        Project, Generation.project_id == Project.id
    )
    total = int(
        session.scalar(
            select(func.count())
            .select_from(Generation)
            .join(User, Generation.user_id == User.id)
            .join(Project, Generation.project_id == Project.id)
            .where(*conditions)
        )
        or 0
    )
    tasks = session.scalars(
        joined.where(*conditions)
        .order_by(
            case((Generation.status == "recovery_required", 0), else_=1),
            Generation.created_at.desc(),
        )
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_tasks.html",
        _admin_context(
            request,
            session,
            user,
            "tasks",
            tasks=tasks,
            q=search,
            status_filter=status_filter,
            provider_filter=provider,
            date_from=date_from,
            date_to=date_to,
            page=page,
            per_page=per_page,
            pages=max(1, ceil(total / per_page)),
            total=total,
            safe_error=_safe_error,
            safe_label=_safe_model_label,
        ),
    )


@router.post("/admin/generations/{generation_id}/recover")
def recover_generation_admin(
    generation_id: str,
    request: Request,
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    generation = session.get(Generation, generation_id)
    if not generation:
        raise HTTPException(404, "任务不存在")
    # 刻意**不**在这里取写锁：``recover_generation`` 用的是自己的 session
    if generation.provider not in {"libtv", "lovart"} or not (
        generation.external_project_id or generation.external_task_id
    ):
        raise HTTPException(409, "该任务不支持安全查询恢复")
    try:
        recovered, message = recover_generation(generation.id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    _audit(
        session,
        admin,
        "task.recovered" if recovered else "task.recovery_queried",
        "generation",
        generation.id,
        "查询上游并恢复结果" if recovered else "查询上游；暂未恢复结果",
        request=request,
    )
    session.commit()
    _flash(request, "success", message)
    return _redirect("/admin/tasks")


@router.post("/admin/generations/{generation_id}/resolve-failed")
def resolve_generation_failed(
    generation_id: str,
    request: Request,
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    # 取写锁：状态复查 → 改终态必须在**同一个临界区**里完成。
    admin, _ = _locked_admin_and_target(request, session)
    generation = session.get(Generation, generation_id)
    if generation is None:
        raise HTTPException(404, "任务不存在")
    if generation.status != "recovery_required":
        raise HTTPException(409, "任务不处于待恢复状态")
    generation.status = "failed"
    generation.error_message = "管理员已核对上游并将任务收敛为失败。"
    generation.finished_at = utcnow()
    _audit(
        session,
        admin,
        "task.resolved_failed",
        "generation",
        generation.id,
        "管理员确认后将待恢复任务收敛为失败",
        request=request,
    )
    session.commit()
    _flash(request, "success", "任务已收敛为失败")
    return _redirect("/admin/tasks")


#: 单次导出的行数上限。审计表只增不减，无上限导出迟早变成一次内存事故
AUDIT_EXPORT_MAX_ROWS = 5000


def _like_needle(value: str) -> str:
    """把用户输入转成 LIKE 的**字面**匹配串（转义 ``%`` / ``_`` / ``\\``）。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _audit_filters(
    action: str, actor: str, date_from: str, date_to: str
) -> tuple[list, bool]:
    """把四个筛选条件翻译成 WHERE 子句，外加「是否需要 join users」。"""
    conditions: list = []
    # 整页只对管理员开放（见 _admin_page_user），因此不需要再按可见范围收窄。
    if action.strip():
        conditions.append(AdminAuditEvent.action == action.strip())
    needs_actor = bool(actor.strip())
    if needs_actor:
        needle = _like_needle(actor.strip())
        conditions.append(
            or_(
                User.username.contains(needle, escape="\\"),
                User.display_name.contains(needle, escape="\\"),
            )
        )
    start, end = _parse_date(date_from), _parse_date(date_to, end=True)
    if start:
        conditions.append(AdminAuditEvent.created_at >= start)
    if end:
        conditions.append(AdminAuditEvent.created_at <= end)
    return conditions, needs_actor


def _audit_count(session: Session, conditions: list, needs_actor: bool) -> int:
    statement = select(func.count()).select_from(AdminAuditEvent)
    if needs_actor:
        statement = statement.outerjoin(User, AdminAuditEvent.actor_user_id == User.id)
    return int(session.scalar(statement.where(*conditions)) or 0)


def _audit_rows(
    session: Session, conditions: list, needs_actor: bool, *, offset: int, limit: int
) -> list[AdminAuditEvent]:
    """取一页审计记录。"""
    statement = select(AdminAuditEvent).options(joinedload(AdminAuditEvent.actor))
    if needs_actor:
        statement = statement.outerjoin(User, AdminAuditEvent.actor_user_id == User.id)
    return list(
        session.scalars(
            statement.where(*conditions)
            # 秒级时间戳会撞车；用 id 兜底排序，否则翻页时同一秒内的行会来回跳。
            .order_by(AdminAuditEvent.created_at.desc(), AdminAuditEvent.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )


def _audit_actor_label(event: AdminAuditEvent) -> str:
    if event.actor is None:
        return ""
    return event.actor.display_name or event.actor.username


def _csv_cell(value: str) -> str:
    """中和 CSV 公式注入。"""
    text = value or ""
    return f"'{text}" if text[:1] in ("=", "+", "-", "@") else text


@router.get("/admin/audit")
def admin_audit(
    request: Request,
    action: str = "",
    actor: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    per_page: int = 30,
    session: Session = Depends(get_session),
):
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    page, per_page = _page_number(page), _page_size(per_page)
    conditions, needs_actor = _audit_filters(action, actor, date_from, date_to)
    total = _audit_count(session, conditions, needs_actor)
    events = _audit_rows(
        session, conditions, needs_actor, offset=(page - 1) * per_page, limit=per_page
    )
    actions = session.scalars(
        select(AdminAuditEvent.action).distinct().order_by(AdminAuditEvent.action)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        _admin_context(
            request,
            session,
            user,
            "audit",
            events=events,
            actions=actions,
            action_filter=action,
            actor_filter=actor,
            date_from=date_from,
            date_to=date_to,
            page=page,
            per_page=per_page,
            pages=max(1, ceil(total / per_page)),
            total=total,
            export_max_rows=AUDIT_EXPORT_MAX_ROWS,
        ),
    )


@router.get("/admin/audit.csv")
def export_audit(
    request: Request,
    action: str = "",
    actor: str = "",
    date_from: str = "",
    date_to: str = "",
    session: Session = Depends(get_session),
):
    """把**当前筛选结果**导出成 CSV（仅管理员，只读 GET 所以不需要 CSRF）。"""
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    conditions, needs_actor = _audit_filters(action, actor, date_from, date_to)
    rows = _audit_rows(session, conditions, needs_actor, offset=0, limit=AUDIT_EXPORT_MAX_ROWS)

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        ["时间", "操作者", "Action", "目标类型", "目标", "来源 IP", "User-Agent", "安全摘要"]
    )
    for event in rows:
        writer.writerow(
            [
                _csv_cell(event.created_at.isoformat(sep=" ", timespec="seconds"))
                if event.created_at
                else "",
                _csv_cell(_audit_actor_label(event)),
                _csv_cell(event.action),
                _csv_cell(event.target_type),
                _csv_cell(event.target_id),
                _csv_cell(event.actor_ip),
                _csv_cell(event.actor_agent),
                _csv_cell(event.summary),
            ]
        )

    _audit(
        session,
        user,
        "audit.exported",
        "audit",
        "audit",
        f"导出 {len(rows)} 条审计记录（上限 {AUDIT_EXPORT_MAX_ROWS}）",
        request=request,
    )
    session.commit()

    # Excel 只有看到 BOM 才会把 UTF-8 认成 UTF-8，否则中文列名与摘要全是乱码。
    body = "\ufeff" + buffer.getvalue()
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit-{stamp}.csv"'},
    )


# ---------------------------------------------------------------------------


@router.get("/admin/prompt-optimize")
def admin_prompt_optimize(request: Request, session: Session = Depends(get_session)):
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    live_assets = int(
        session.scalar(select(func.count(Asset.id)).where(Asset.deleted_at.is_(None))) or 0
    )
    return templates.TemplateResponse(
        request,
        "admin_prompt_optimize.html",
        _admin_context(
            request,
            session,
            user,
            "prompt_optimize",
            config=prompt_optimize.public_config(),
            asset_retention_days=settings.asset_retention_days,
            asset_purge_interval_minutes=settings.asset_purge_interval_minutes,
            live_assets=live_assets,
        ),
    )


@router.post("/admin/prompt-optimize")
def save_prompt_optimize(
    request: Request,
    csrf: str = Form(...),
    enabled: str = Form(""),
    script: str = Form(""),
    args: str = Form(""),
    api_base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(""),
    timeout: int = Form(120),
    agent_enabled: str = Form(""),
    agent_model: str = Form(""),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    current = prompt_optimize.load_config()
    # 空 key = 保持原值。
    effective_key = api_key.strip() or str(current.get("api_key") or "")
    prompt_optimize.save_config(
        enabled=bool(enabled),
        script=script,
        args=args,
        api_base_url=api_base_url,
        api_key=effective_key,
        model=model,
        timeout=timeout,
        agent_enabled=bool(agent_enabled),
        agent_model=agent_model,
    )
    _audit(
        session,
        user,
        "prompt_optimize.saved",
        "setting",
        "prompt_optimize",
        f"提示词优化配置：纯文本{'启用' if enabled else '停用'}、"
        f"Agent 模式{'启用' if agent_enabled else '停用'}，"
        f"脚本 {script.strip() or '（未设置）'}",
        request=request,
    )
    session.commit()
    _flash(request, "success", "提示词优化配置已保存")
    return _redirect("/admin/prompt-optimize")


@router.post("/admin/prompt-optimize/test")
def test_prompt_optimize(
    request: Request,
    csrf: str = Form(...),
    sample: str = Form(""),
    mode: str = Form(""),
    sample_image: UploadFile | None = File(default=None),
    session: Session = Depends(get_session),
):
    """用一句样例（可带一张参考图）跑一遍 skill。"""
    require_csrf(request, csrf)
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    text = (sample or "").strip() or "白色陶瓷马克杯，跨境主图，干净背景"
    resolved_mode = prompt_optimize.normalize_mode(
        mode or (prompt_optimize.MODE_AGENT if sample_image is not None else "")
    )
    if not prompt_optimize.is_configured(mode=resolved_mode):
        _flash(
            request, "error",
            f"还没配置或未启用 skill（{resolved_mode} 模式），先保存一次配置再自检",
        )
        return _redirect("/admin/prompt-optimize")

    staged: Path | None = None
    try:
        if sample_image is not None and sample_image.filename:
            staged = settings.storage_root / f"selfcheck-{secrets.token_hex(8)}"
            staged.mkdir(parents=True, exist_ok=True)
            target = staged / Path(sample_image.filename).name
            # 同步路由不能 await
            target.write_bytes(sample_image.file.read())
            image_paths = [str(target)]
        else:
            image_paths = []
        result, note = prompt_optimize.optimize_prompt(
            text, mode=resolved_mode, image_paths=image_paths
        )
    except Exception as exc:  # noqa: BLE001 —— 这里就是要把它展示给管理员
        _audit(
            session, user, "prompt_optimize.tested", "setting", "prompt_optimize",
            f"自检失败（{resolved_mode}）：{exc}",
            request=request,
        )
        session.commit()
        # ⚠️ 截断：flash 存在签名 Cookie 里，长文本会把 Cookie 撑爆。
        _flash(request, "error", f"[{resolved_mode}] 优化失败：{str(exc)[:300]}")
        return _redirect("/admin/prompt-optimize")
    finally:
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)

    _audit(
        session, user, "prompt_optimize.tested", "setting", "prompt_optimize",
        f"自检通过（{resolved_mode}，参考图 {len(image_paths)} 张）",
        request=request,
    )
    session.commit()
    summary = f"[{resolved_mode}] 优化结果（{len(result)} 字）：{result[:240]}"
    if note:
        summary += f"　诊断：{note[:160]}"
    _flash(request, "success", summary)
    return _redirect("/admin/prompt-optimize")


@router.post("/admin/assets/purge")
def purge_assets_now(
    request: Request, csrf: str = Form(...), session: Session = Depends(get_session)
):
    """手动跑一次素材到期清理。"""
    require_csrf(request, csrf)
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    rows, files = purge_expired_assets(session)
    _audit(
        session, user, "assets.purged", "asset", "",
        f"手动清理过期素材：{rows} 条记录、{files} 个文件",
        request=request,
    )
    session.commit()
    _flash(request, "success", f"已清理 {rows} 条过期素材（{files} 个文件）")
    return _redirect("/admin/prompt-optimize")
