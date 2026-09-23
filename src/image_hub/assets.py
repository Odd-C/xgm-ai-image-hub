"""可见性分两层："""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import func, or_, select, true
from sqlalchemy.orm import Session

from image_hub.auth import ADMIN_ROLES
from image_hub.config import settings
from image_hub.models import (
    ASSET_VISIBILITY_PRIVATE,
    ASSET_VISIBILITY_SHARED,
    Asset,
    User,
    utcnow,
)
from image_hub.storage import (
    StoredImage,
    link_or_copy,
    resolve_storage_key,
    store_asset_payload,
    validate_image_payload,
)

logger = logging.getLogger("image_hub.assets")


class AssetError(ValueError):
    """素材校验失败：尺寸或体积不合规（与「字节不是合法图片」分开）。"""


class AssetPermissionError(AssetError):
    """素材**可见**但当前账号无权操作（例如子账号想删管理员传的共享素材）。"""


def is_manager(user: User) -> bool:
    """管理员是素材库的管理者。"""
    return user.role in ADMIN_ROLES


def visibility_for_uploader(user: User) -> str:
    """管理员上传进共享库，普通账号上传进私有区。"""
    return ASSET_VISIBILITY_SHARED if is_manager(user) else ASSET_VISIBILITY_PRIVATE


def _visibility_filter(user: User):
    """可见性条件。管理员看全部，其余人只看到共享 + 自己的。"""
    if is_manager(user):
        return true()
    return or_(
        Asset.visibility == ASSET_VISIBILITY_SHARED,
        Asset.uploaded_by == user.id,
    )


def list_visible_assets(session: Session, user: User) -> list[Asset]:
    return list(
        session.scalars(
            select(Asset)
            .where(Asset.deleted_at.is_(None), _visibility_filter(user))
            .order_by(Asset.created_at.desc())
        )
    )


def count_live_assets(session: Session) -> int:
    return int(
        session.scalar(select(func.count(Asset.id)).where(Asset.deleted_at.is_(None))) or 0
    )


def get_visible_asset(session: Session, user: User, asset_id: str) -> Asset:
    """取一条可见素材：不可见与不存在都按 404 处理。"""
    asset = session.scalar(
        select(Asset).where(
            Asset.id == asset_id,
            Asset.deleted_at.is_(None),
            _visibility_filter(user),
        )
    )
    if asset is None:
        raise AssetError("素材不存在或无权访问")
    return asset


def can_delete(asset: Asset, user: User) -> bool:
    """注意：普通账号**不能**删管理员传的共享素材 —— 哪怕自己刚用它生成过图。"""
    return is_manager(user) or asset.uploaded_by == user.id


async def store_asset(session: Session, user: User, upload: UploadFile) -> tuple[Asset, bool]:
    """上传一张素材，返回 ``(素材, 是否复用已有记录)``。"""
    payload = await upload.read(settings.max_upload_bytes + 1)
    # 顺序有意为之：校验 → 查重 → 配额 → 才落盘。命中查重就完全不写文件。
    validate_image_payload(payload)
    digest = hashlib.sha256(payload).hexdigest()

    existing = session.scalar(
        select(Asset)
        .where(
            Asset.sha256 == digest,
            Asset.deleted_at.is_(None),
            _visibility_filter(user),
        )
        .order_by(Asset.created_at.asc())
        .limit(1)
    )
    if existing is not None:
        return existing, True

    if count_live_assets(session) >= max(int(settings.asset_max_per_tenant), 1):
        raise AssetError(
            f"素材库已达上限（{settings.asset_max_per_tenant} 张），请先删除不再需要的素材"
        )

    stored = store_asset_payload(payload)
    asset = Asset(
        uploaded_by=user.id,
        visibility=visibility_for_uploader(user),
        original_name=(upload.filename or "素材")[:255],
        storage_key=stored.storage_key,
        sha256=stored.sha256,
        mime_type=stored.mime_type,
        width=stored.width,
        height=stored.height,
        byte_size=stored.byte_size,
    )
    session.add(asset)
    session.flush()
    return asset, False


def delete_asset(session: Session, user: User, asset_id: str) -> None:
    """但文件本身留着就不会出现「历史任务图片打不开」这类悬空引用。"""
    asset = get_visible_asset(session, user, asset_id)
    if not can_delete(asset, user):
        raise AssetPermissionError("只能删除自己上传的素材")
    asset.deleted_at = utcnow()
    session.commit()


def purge_expired_assets(session: Session) -> tuple[int, int]:
    """清理到期素材：行与文件一并删除（与「用户主动删除」的软删语义不同）。"""
    limit = utcnow() - timedelta(days=max(int(settings.asset_retention_days), 1))
    expired = list(session.scalars(select(Asset).where(Asset.created_at < limit)))
    if not expired:
        return 0, 0
    expired_keys = {asset.storage_key for asset in expired}
    expired_ids = {asset.id for asset in expired}
    # 同一份文件可能被多行指向（内容寻址），只有「没有任何未过期行还指着它」才能删文件。
    still_needed = set(
        session.scalars(
            select(Asset.storage_key).where(
                Asset.storage_key.in_(expired_keys),
                Asset.id.not_in(expired_ids),
            )
        )
    )
    removed_files = 0
    for storage_key in sorted(expired_keys - still_needed):
        path = settings.storage_root / storage_key
        try:
            if path.is_file():
                path.unlink()
                removed_files += 1
        except OSError as exc:  # 文件删不掉不该让整轮清理失败
            logger.warning("素材文件删除失败，将在下一轮重试：%s（%s）", storage_key, exc)
    for asset in expired:
        session.delete(asset)
    session.commit()
    logger.info(
        "素材到期清理：删除 %d 条记录、%d 个文件（保留 %d 天）",
        len(expired),
        removed_files,
        settings.asset_retention_days,
    )
    return len(expired), removed_files


def materialise_asset_reference(
    asset: Asset, generation_id: str, reference_id: str
) -> StoredImage:
    """把素材挂进某次生成的参考图目录，供执行器当普通参考图读取。"""
    source = resolve_storage_key(asset.storage_key)
    if not source.is_file():
        raise AssetError("素材文件缺失，无法用作参考图")
    extension = Path(asset.storage_key).suffix or ".png"
    storage_key = f"generations/{generation_id}/references/{reference_id}{extension}"
    link_or_copy(source, settings.storage_root / storage_key)
    return StoredImage(
        storage_key=storage_key,
        sha256=asset.sha256,
        mime_type=asset.mime_type,
        width=asset.width,
        height=asset.height,
        byte_size=asset.byte_size,
    )


def uploader_names(session: Session, assets: list[Asset]) -> dict[str, str]:
    """素材页要显示「谁传的」，一次查回所有上传者的显示名，别在循环里逐条查。"""
    ids = {asset.uploaded_by for asset in assets if asset.uploaded_by}
    if not ids:
        return {}
    rows = session.execute(
        select(User.id, User.display_name, User.username).where(User.id.in_(ids))
    ).all()
    return {row[0]: (row[1] or row[2] or "") for row in rows}


def asset_dict(asset: Asset, *, uploader_name: str = "", can_remove: bool = False) -> dict:
    return {
        "id": asset.id,
        "name": asset.original_name,
        "url": f"/assets/{asset.id}/content",
        "width": asset.width,
        "height": asset.height,
        "byte_size": asset.byte_size,
        "mime_type": asset.mime_type,
        "visibility": asset.visibility,
        "shared": asset.visibility == ASSET_VISIBILITY_SHARED,
        "uploaded_by": asset.uploaded_by,
        "uploaded_by_name": uploader_name,
        "can_delete": can_remove,
        "created_at": asset.created_at.isoformat(),
    }
