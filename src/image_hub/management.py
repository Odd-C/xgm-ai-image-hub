import json
import re
from datetime import datetime
from math import ceil

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from image_hub.auth import (
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
from image_hub.models import AdminAuditEvent, Generation, Project, User, utcnow
from image_hub.providers import (
    ProviderConfigError,
    model_profiles,
    probe_libtv_connection,
    probe_lovart_connection,
    public_api_config,
    public_native_credentials,
    recover_generation,
    save_api_config,
    save_libtv_credentials,
    save_lovart_credentials,
)
from image_hub.web import canvas_home_url

router = APIRouter()
templates = Jinja2Templates(directory=settings.template_dir)
ROLES = {"user", "admin"}
USER_STATUSES = {"active", "inactive"}
TASK_STATUSES = {"queued", "running", "recovery_required", "failed", "succeeded"}


def _format_datetime(value: datetime | None) -> str:
    if not value:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M")


templates.env.filters["datetime"] = _format_datetime


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
    """Return a display label that never exposes server-side API routing.

    Admin tables have to render labels for rows that may predate the labelling
    migration, so a legacy API row can still carry the raw upstream model id in
    ``model_label``. That value is server-only routing material, so it is
    replaced whenever it matches the recorded upstream identity.
    """
    label = generation.model_label or ""
    if generation.provider != "api":
        return label
    try:
        snapshot = json.loads(generation.provider_snapshot_json or "{}")
    except json.JSONDecodeError:
        snapshot = {}
    upstream_model = snapshot.get("upstream_model", "") if isinstance(snapshot, dict) else ""
    if not label or label in {generation.model_id, upstream_model}:
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
    user = current_user(request, session, allow_password_change=True)
    if user.must_change_password:
        return _redirect("/account")
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
    if not actor or not actor.is_active or actor.auth_version != auth_version:
        request.session.clear()
        raise HTTPException(401, "请先登录")
    if actor.must_change_password:
        raise HTTPException(403, "请先修改初始密码")
    require_admin(actor)
    if target_user_id and target is None:
        raise HTTPException(404, "账号不存在")
    return actor, target


def _audit(
    session: Session,
    actor: User | None,
    action: str,
    target_type: str,
    target_id: str,
    summary: str,
) -> None:
    session.add(
        AdminAuditEvent(
            actor_user_id=actor.id if actor else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            summary=summary[:500],
        )
    )


def _enabled_admin_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(User).where(User.role == "admin", User.is_active == 1)
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
    _audit(session, user, "account.password_changed", "user", user.id, "用户自行修改密码")
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
    user_counts = {
        (row[0], int(row[1]))
        for row in session.execute(select(User.is_active, func.count()).group_by(User.is_active))
    }
    enabled_users = next((count for active, count in user_counts if active), 0)
    disabled_users = next((count for active, count in user_counts if not active), 0)
    role_counts = dict(session.execute(select(User.role, func.count()).group_by(User.role)).all())
    task_counts = dict(
        session.execute(select(Generation.status, func.count()).group_by(Generation.status)).all()
    )
    recent_users = session.scalars(select(User).order_by(User.created_at.desc()).limit(6)).all()
    recent_tasks = session.scalars(
        select(Generation)
        .where(Generation.status.in_(("recovery_required", "failed")))
        .order_by(
            case((Generation.status == "recovery_required", 0), else_=1),
            Generation.created_at.desc(),
        )
        .limit(6)
    ).all()
    profiles = model_profiles()
    provider_summary = []
    for provider, label in (("libtv", "LibTV"), ("lovart", "Lovart"), ("api", "API")):
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
            select(func.count()).select_from(Project).where(Project.archived_at.is_(None))
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


@router.post("/admin/users")
def create_user(
    request: Request,
    username: str = Form(..., min_length=2, max_length=80),
    password: str = Form(..., min_length=8, max_length=200),
    display_name: str = Form("", max_length=120),
    department: str = Form("", max_length=120),
    role: str = Form("user"),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    actor, _ = _locked_admin_and_target(request, session)
    clean_username = username.strip()
    if role not in ROLES:
        raise HTTPException(422, "角色无效")
    if session.scalar(select(User.id).where(User.username == clean_username)):
        raise HTTPException(409, "用户名已存在")
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
    _audit(session, actor, "user.created", "user", target.id, f"新建账号 {clean_username}")
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
    _audit(session, actor, "user.profile_updated", "user", target.id, "编辑账号资料")
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
    if target.id == actor.id and role != "admin":
        raise HTTPException(409, "不能降级当前登录管理员")
    if (
        target.role == "admin"
        and target.is_active
        and role != "admin"
        and _enabled_admin_count(session) <= 1
    ):
        raise HTTPException(409, "不能降级系统最后一个启用管理员")
    previous = target.role
    target.role = role
    if previous != role:
        target.auth_version += 1
    target.updated_at = utcnow()
    _audit(session, actor, "user.role_changed", "user", target.id, f"角色 {previous} → {role}")
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
    _audit(session, actor, "user.password_reset", "user", target.id, "管理员重置密码")
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
    if disabling and target.role == "admin" and _enabled_admin_count(session) <= 1:
        raise HTTPException(409, "不能停用系统最后一个启用管理员")
    target.is_active = 0 if disabling else 1
    if disabling:
        target.auth_version += 1
    target.updated_at = utcnow()
    action = "user.disabled" if disabling else "user.enabled"
    summary = "停用账号并撤销旧会话" if disabling else "启用账号"
    _audit(session, actor, action, "user", target.id, summary)
    session.commit()
    _flash(request, "success", "账号已停用，数据仍完整保留" if disabling else "账号已启用")
    return _redirect("/admin/users")


@router.get("/admin/providers")
def admin_providers(request: Request, session: Session = Depends(get_session)):
    user = _admin_page_user(request, session)
    if isinstance(user, RedirectResponse):
        return user
    profiles = model_profiles()
    provider_cards = []
    for provider, label in (("libtv", "LibTV"), ("lovart", "Lovart"), ("api", "API")):
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
    api_profiles = [item.public_dict() for item in profiles if item.provider == "api"]
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
            api_config=public_api_config(),
            native_credentials=public_native_credentials(),
        ),
    )


@router.post("/admin/providers/libtv/credentials")
def configure_libtv_credentials(
    request: Request,
    libtv_token: str = Form(""),
    libtv_scripts_dir: str = Form(""),
    clear_credentials: str = Form(""),
    confirm_clear: str = Form(""),
    action: str = Form(""),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    clearing = clear_credentials == "on"
    if clearing and confirm_clear != "clear-libtv":
        _flash(request, "error", "清除 LibTV 配置需要再次明确确认")
        return _redirect("/admin/providers")
    try:
        result = save_libtv_credentials(
            libtv_token, libtv_scripts_dir, clear=clearing
        )
    except ProviderConfigError as exc:
        _flash(request, "error", str(exc))
        return _redirect("/admin/providers")
    if action == "check":
        ok, message = probe_libtv_connection()
        _audit(session, admin, "provider.libtv_checked", "provider", "libtv", message)
        session.commit()
        _flash(request, "success" if ok else "error", message)
        return _redirect("/admin/providers")
    summary = f"LibTV 配置{'已清除' if result == 'cleared' else '已更新' if result == 'saved' else '保持不变'}"
    _audit(
        session,
        admin,
        "provider.libtv_credentials_cleared"
        if result == "cleared"
        else "provider.libtv_credentials_saved",
        "provider",
        "libtv",
        summary,
    )
    session.commit()
    _flash(
        request,
        "success",
        # 表单里所有字段都会被提交，密码框留空是"保持不变"。此时必须让用户
        # 明确知道什么都没存，否则他会以为保存失败而反复点击。
        "LibTV Access Key 未变化（输入框留空即保持不变）"
        if result == "kept"
        else summary,
    )
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
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
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
        )
    except ProviderConfigError as exc:
        _flash(request, "error", str(exc))
        return _redirect("/admin/providers")
    if action == "check":
        ok, message = probe_lovart_connection()
        _audit(session, admin, "provider.lovart_checked", "provider", "lovart", message)
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


@router.post("/admin/providers/api")
def configure_api_provider(
    request: Request,
    base_url: str = Form(...),
    api_key: str = Form(""),
    models: str = Form(...),
    csrf: str = Form(...),
    session: Session = Depends(get_session),
):
    require_csrf(request, csrf)
    admin = current_user(request, session)
    require_admin(admin)
    if not 8 <= len(base_url) <= 500:
        raise HTTPException(422, "API Base URL 长度无效")
    if len(api_key) > 1000:
        raise HTTPException(422, "API Key 长度超过限制")
    if not 1 <= len(models) <= 12000:
        raise HTTPException(422, "API 模型配置长度无效")
    try:
        save_api_config(base_url, api_key, models)
    except ProviderConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    model_count = len([item for item in models.split(",") if item.strip()])
    _audit(
        session,
        admin,
        "provider.api_saved",
        "provider",
        "api",
        f"保存 API 中转配置；模型 {model_count} 项；密钥{'已更新' if api_key else '保持'}",
    )
    session.commit()
    _flash(request, "success", "API 中转配置已安全保存")
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
    admin = current_user(request, session)
    require_admin(admin)
    generation = session.get(Generation, generation_id)
    if not generation or generation.status != "recovery_required":
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
    )
    session.commit()
    _flash(request, "success", "任务已收敛为失败")
    return _redirect("/admin/tasks")


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
    conditions = []
    if action.strip():
        conditions.append(AdminAuditEvent.action.contains(action.strip()))
    if actor.strip():
        conditions.append(
            or_(User.username.contains(actor.strip()), User.display_name.contains(actor.strip()))
        )
    start, end = _parse_date(date_from), _parse_date(date_to, end=True)
    if start:
        conditions.append(AdminAuditEvent.created_at >= start)
    if end:
        conditions.append(AdminAuditEvent.created_at <= end)
    total = int(
        session.scalar(
            select(func.count())
            .select_from(AdminAuditEvent)
            .outerjoin(User, AdminAuditEvent.actor_user_id == User.id)
            .where(*conditions)
        )
        or 0
    )
    events = session.scalars(
        select(AdminAuditEvent)
        .outerjoin(User, AdminAuditEvent.actor_user_id == User.id)
        .where(*conditions)
        .order_by(AdminAuditEvent.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
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
        ),
    )
