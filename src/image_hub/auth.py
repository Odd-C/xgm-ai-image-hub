import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from image_hub.models import User

_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_login_lock = threading.Lock()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return "pbkdf2_sha256$310000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt_text), int(rounds)
        )
        return hmac.compare_digest(actual, base64.b64decode(expected_text))
    except (ValueError, TypeError):
        return False


def current_user(
    request: Request, session: Session, *, allow_password_change: bool = False
) -> User:
    """解析当前登录用户；密码未改只作提示，不阻断请求。"""
    user_id = request.session.get("user_id")
    user = session.get(User, user_id) if user_id else None
    session_auth_version = request.session.get("auth_version")
    if (
        user is None
        or not user.is_active
        or user.deleted_at is not None
        or session_auth_version != user.auth_version
    ):
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return user


ROLE_USER = "user"
ROLE_ADMIN = "admin"
ADMIN_ROLES = frozenset({ROLE_ADMIN})


def is_admin(user: User) -> bool:
    return user.role in ADMIN_ROLES


def require_admin(user: User) -> None:
    if user.role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="需要管理员权限")


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def rotate_csrf_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session["csrf_token"] = token
    return token


def require_csrf(request: Request, submitted: str = "") -> None:
    expected = request.session.get("csrf_token", "")
    actual = submitted or request.headers.get("X-CSRF-Token", "")
    if not expected or not actual or not hmac.compare_digest(expected, actual):
        raise HTTPException(status_code=403, detail="请求校验失败，请刷新页面后重试")


def check_login_rate_limit(key: str, *, success: bool = False) -> None:
    now = time.monotonic()
    with _login_lock:
        attempts = _login_attempts[key]
        while attempts and attempts[0] < now - 300:
            attempts.popleft()
        if success:
            _login_attempts.pop(key, None)
            return
        if len(attempts) >= 5:
            raise HTTPException(status_code=429, detail="登录尝试过多，请五分钟后再试")
        attempts.append(now)
