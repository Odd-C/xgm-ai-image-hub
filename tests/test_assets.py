"""账号级素材库的回归测试。

全部走真实 HTTP（TestClient），复用 ``test_app`` 的 ``login`` / ``create_project``
与 ``test_provider_config`` 的 ``_set_dns``，不另造一套。磁盘落盘用 ``tmp_path``
（通过 ``storage_root`` fixture 把 ``settings.storage_root`` 重定向过去），提交路径
monkeypatch 掉 ``_execute_api`` 且不真连外网（DNS 一并打桩）。

覆盖契约：上传持久化 / 内容寻址去重 / 同字节不同人不能串味 / 换会话仍在 / 子账号隔离 /
管理员全租户可见 / 跨租户隔离 / 跨项目复用免重传 / reference_order 顺序保真 / 非法入参 /
旧行为兼容。
"""

import hashlib
import io
import itertools
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from test_app import create_project, login
from test_provider_config import _set_dns

from image_hub.app import app
from image_hub.config import settings
from image_hub.db import SessionLocal
from image_hub.models import (
    ASSET_VISIBILITY_PRIVATE,
    ASSET_VISIBILITY_SHARED,
    Asset,
    Generation,
    ReferenceImage,
    User,
)
from image_hub.providers import ModelProfile

_counter = itertools.count(1)


def uname(prefix: str) -> str:
    return f"{prefix}-{next(_counter)}"


def _key(prefix: str) -> str:
    return f"idem-{prefix}-{next(_counter):04d}-abcd1234"


def png(color, size=(120, 120)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def upload(client, token, name, payload):
    return client.post(
        "/api/assets",
        files={"file": (name, payload, "image/png")},
        headers={"X-CSRF-Token": token})


def asset_ids(client):
    return {a["id"] for a in client.get("/api/assets").json()["assets"]}


def admin_id() -> str:
    with SessionLocal() as session:
        return session.query(User).filter_by(username="admin").one().id


def ensure_member(username, role="user", owner=""):
    with SessionLocal() as session:
        existing = session.query(User).filter_by(username=username).one_or_none()
        if existing is not None:
            return existing
        root = session.query(User).filter_by(username="admin").one()
        user = User(
            username=username,
            display_name=username,
            role=role,
            password_hash=root.password_hash)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def generation_refs(gen_id):
    with SessionLocal() as session:
        gen = session.get(Generation, gen_id)
        refs = list(
            session.scalars(
                select(ReferenceImage)
                .where(ReferenceImage.generation_id == gen_id)
                .order_by(ReferenceImage.position)
            ).all()
        )
        manifest = json.loads(gen.reference_manifest_json)
    return gen, refs, manifest


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    return tmp_path


@pytest.fixture
def stub_profile(monkeypatch):
    profile = ModelProfile(
        id="api:test-assets",
        label="Test Assets",
        provider="api",
        upstream_model="test-assets",
        enabled=True,
        ratios=("1:1"),
        resolutions=("2K"),
        qualities=("standard"),
        max_references=14)
    monkeypatch.setattr("image_hub.web.get_profile", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr("image_hub.providers._execute_api", lambda *a, **k: None)
    return profile


def test_upload_persists_and_shared(storage_root):
    payload = png((20, 60, 200))
    digest = hashlib.sha256(payload).hexdigest()
    with TestClient(app) as c:
        token = login(c)
        resp = upload(c, token, "blue.png", payload)
        assert resp.status_code == 201, resp.text
        asset = resp.json()["asset"]
        # 管理员上传进共享库
        assert asset["shared"] is True
        assert asset["visibility"] == ASSET_VISIBILITY_SHARED
        path = settings.storage_root / f"assets/{digest}.png"
        assert path.is_file()
        assert path.read_bytes() == payload


def test_content_addressing_dedup(storage_root):
    # 用与别处不同的字节，避免复用别的测试遗留的素材行后「不落盘」造成的误判
    payload = png((33, 77, 211))
    digest = hashlib.sha256(payload).hexdigest()
    with TestClient(app) as c:
        token = login(c)
        first = upload(c, token, "dedup.png", payload).json()["asset"]
        second = upload(c, token, "dedup-again.png", payload).json()
        # 同字节再传 → 复用同一行
        assert second["reused"] is True
        assert second["asset"]["id"] == first["id"]
        # 复用的同一行指向的文件确实存在（内容寻址只落一份）
        path = settings.storage_root / f"assets/{digest}.png"
        assert path.is_file()
        assert path.read_bytes() == payload
        # 磁盘上只有一份文件
        files = list((settings.storage_root / "assets").glob("*.png"))
        assert len(files) == 1


def test_same_bytes_private_not_reused_across_users(storage_root):
    """同字节但属别人私有的素材，另一用户上传时必须新建行（不串味）。

    组合：管理员名下先有一条**私有**素材（同字节）；子账号传同字节时，
    ``_visibility_filter``（私有 + 非本人上传 → 不可见）让它找不到可复用行，
    于是新建私有行指向同一份内容寻址文件。断言点：返回 id 与管理员私有行不同、
    仍为私有、文件仍只落一份、且子账号列表里看不到管理员的私有行。
    """
    payload = png((240, 180, 40))
    digest = hashlib.sha256(payload).hexdigest()
    with SessionLocal() as session:
        root = session.query(User).filter_by(username="admin").one()
        private = Asset(
            uploaded_by=root.id,
            visibility=ASSET_VISIBILITY_PRIVATE,
            original_name="admin-private.png",
            storage_key=f"assets/{digest}.png",
            sha256=digest,
            mime_type="image/png",
            width=120,
            height=120,
            byte_size=len(payload))
        session.add(private)
        session.commit()
        private_id = private.id
    member = ensure_member(uname("priv-sub"))
    with TestClient(app) as c:
        token = login(c, member.username)
        resp = upload(c, token, "member-same.png", payload)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        # 不能复用管理员的私有行
        assert data["reused"] is False
        assert data["asset"]["id"] != private_id
        assert data["asset"]["shared"] is False
        assert data["asset"]["uploaded_by"] == member.id
        # 文件仍按内容寻址落一份（与管理员私有行同路径）
        path = settings.storage_root / f"assets/{digest}.png"
        assert path.is_file()
        assert path.read_bytes() == payload
        # 子账号列表里看不到管理员的私有行
        assert private_id not in asset_ids(c)


def test_asset_survives_new_session(storage_root):
    payload = png((10, 90, 160))
    with TestClient(app) as c:
        token = login(c)
        asset = upload(c, token, "persist.png", payload).json()["asset"]
    # 新会话（新 TestClient）下服务端仍持久化
    with TestClient(app) as c:
        login(c)
        assert asset["id"] in asset_ids(c)
        got = c.get(f"/assets/{asset['id']}/content")
        assert got.status_code == 200
        assert got.content == payload


def test_subaccount_sees_shared_asset(storage_root):
    payload = png((20, 200, 60))
    with TestClient(app) as c:
        token = login(c)
        asset = upload(c, token, "green.png", payload).json()["asset"]
    with TestClient(app) as c:
        login(c, ensure_member(uname("shared-sub")).username)
        assert asset["id"] in asset_ids(c)


def test_subaccount_isolation(storage_root):
    payload = png((200, 40, 40))
    with TestClient(app) as c:
        token = login(c, ensure_member(uname("iso-a")).username)
        a_asset = upload(c, token, "a-private.png", payload).json()["asset"]
    with TestClient(app) as c:
        login(c, ensure_member(uname("iso-b")).username)
        # 子账号 B 看不到 A 的私有素材
        assert a_asset["id"] not in asset_ids(c)
        # 直接取 A 私有素材字节 → 404（不泄露存在性）
        assert c.get(f"/assets/{a_asset['id']}/content").status_code == 404


def test_admin_sees_tenant_and_deletes_member_private(storage_root):
    payload = png((240, 120, 20))
    member = ensure_member(uname("del-a"))
    with TestClient(app) as c:
        token = login(c, member.username)
        a_asset = upload(c, token, "a-private.png", payload).json()["asset"]
    with TestClient(app) as c:
        token = login(c)
        # 管理员看得到本租户全部（含子账号私有）
        assert a_asset["id"] in asset_ids(c)
        # 管理员能删子账号的私有素材
        resp = c.post(
            f"/api/assets/{a_asset['id']}/delete",
            headers={"X-CSRF-Token": token})
        assert resp.status_code == 200
        assert a_asset["id"] not in asset_ids(c)


def test_member_cannot_delete_shared_asset(storage_root):
    payload = png((30, 30, 200))
    with TestClient(app) as c:
        token = login(c)
        shared = upload(c, token, "shared.png", payload).json()["asset"]
    with TestClient(app) as c:
        token = login(c, ensure_member(uname("del-sub")).username)
        # 子账号删不掉管理员的共享素材 → 403
        resp = c.post(
            f"/api/assets/{shared['id']}/delete",
            headers={"X-CSRF-Token": token})
        assert resp.status_code == 403
        # 素材仍可见
        assert shared["id"] in asset_ids(c)


def test_cross_project_asset_reuse(storage_root, stub_profile, monkeypatch):
    _set_dns(monkeypatch, "8.8.8.8")
    payload = png((230, 200, 30))
    with TestClient(app) as c:
        token = login(c)
        sku = upload(c, token, "sku.png", payload).json()["asset"]
        pids = [create_project(c, token, uname("proj")) for _ in range(2)]
        keys = []
        for pid in pids:
            resp = c.post(
                f"/api/projects/{pid}/generations",
                data={
                    "prompt": "跨项目复用",
                    "profile_id": "api:test-assets",
                    "ratio": "1:1",
                    "resolution": "2K",
                    "quality": "standard",
                    "idempotency_key": _key("reuse"),
                    "reference_order": json.dumps([f"a:{sku['id']}"]),
                },
                headers={"X-CSRF-Token": token})
            assert resp.status_code == 202, resp.text
            _gen, refs, manifest = generation_refs(resp.json()["id"])
            # 每个项目各 1 条参考图，文件就位且内容与素材一致
            assert len(refs) == 1
            assert (settings.storage_root / refs[0].storage_key).is_file()
            assert (settings.storage_root / refs[0].storage_key).read_bytes() == payload
            # 清单记录了素材来源（可溯源）
            assert manifest[0]["asset_id"] == sku["id"]
            keys.append(refs[0].storage_key)
        # 两次生成各自持有自己的参考图路径（互不覆盖）
        assert len({k for k in keys}) == 2


def test_reference_order_sequence_preserved(storage_root, stub_profile, monkeypatch):
    _set_dns(monkeypatch, "8.8.8.8")
    payload = png((70, 130, 180))
    mid = png((7, 7, 7))
    with TestClient(app) as c:
        token = login(c)
        sku = upload(c, token, "seq.png", payload).json()["asset"]
        pid = create_project(c, token, uname("seqproj"))
        order = [f"a:{sku['id']}", "f:0", f"a:{sku['id']}"]
        resp = c.post(
            f"/api/projects/{pid}/generations",
            data={
                "prompt": "顺序混合",
                "profile_id": "api:test-assets",
                "ratio": "1:1",
                "resolution": "2K",
                "quality": "standard",
                "idempotency_key": _key("seq"),
                "reference_order": json.dumps(order),
            },
            files=[("references", ("mid.png", mid, "image/png"))],
            headers={"X-CSRF-Token": token})
        assert resp.status_code == 202, resp.text
        _, refs, manifest = generation_refs(resp.json()["id"])
        # 位置顺序与请求一致（素材/文件/素材）
        shape = [bool(m.get("asset_id")) for m in manifest]
        assert shape == [True, False, True]
        assert len(refs) == 3


def test_reference_index_out_of_range(storage_root, stub_profile, monkeypatch):
    _set_dns(monkeypatch, "8.8.8.8")
    with TestClient(app) as c:
        token = login(c)
        pid = create_project(c, token, uname("oob"))
        resp = c.post(
            f"/api/projects/{pid}/generations",
            data={
                "prompt": "越界",
                "profile_id": "api:test-assets",
                "ratio": "1:1",
                "resolution": "2K",
                "quality": "standard",
                "idempotency_key": _key("oob"),
                "reference_order": json.dumps(["f:5"]),
            },
            headers={"X-CSRF-Token": token})
        assert resp.status_code == 422


def test_reference_unknown_asset(storage_root, stub_profile, monkeypatch):
    _set_dns(monkeypatch, "8.8.8.8")
    with TestClient(app) as c:
        token = login(c)
        pid = create_project(c, token, uname("unk"))
        resp = c.post(
            f"/api/projects/{pid}/generations",
            data={
                "prompt": "别家素材",
                "profile_id": "api:test-assets",
                "ratio": "1:1",
                "resolution": "2K",
                "quality": "standard",
                "idempotency_key": _key("unk"),
                "reference_order": json.dumps([f"a:{'0' * 32}"]),
            },
            headers={"X-CSRF-Token": token})
        assert resp.status_code == 422


def test_reference_order_not_a_json_array(storage_root, stub_profile, monkeypatch):
    _set_dns(monkeypatch, "8.8.8.8")
    with TestClient(app) as c:
        token = login(c)
        pid = create_project(c, token, uname("notarr"))
        resp = c.post(
            f"/api/projects/{pid}/generations",
            data={
                "prompt": "非数组",
                "profile_id": "api:test-assets",
                "ratio": "1:1",
                "resolution": "2K",
                "quality": "standard",
                "idempotency_key": _key("notarr"),
                "reference_order": "not-json",
            },
            headers={"X-CSRF-Token": token})
        assert resp.status_code == 422


def test_duplicate_file_index_rejected(storage_root, stub_profile, monkeypatch):
    _set_dns(monkeypatch, "8.8.8.8")
    with TestClient(app) as c:
        token = login(c)
        pid = create_project(c, token, uname("dup"))
        resp = c.post(
            f"/api/projects/{pid}/generations",
            data={
                "prompt": "重复下标",
                "profile_id": "api:test-assets",
                "ratio": "1:1",
                "resolution": "2K",
                "quality": "standard",
                "idempotency_key": _key("dup"),
                "reference_order": json.dumps(["f:0", "f:0"]),
            },
            files=[("references", ("a.png", png((1, 2, 3)), "image/png"))],
            headers={"X-CSRF-Token": token})
        assert resp.status_code == 422


def test_no_reference_order_legacy_behavior(storage_root, stub_profile, monkeypatch):
    _set_dns(monkeypatch, "8.8.8.8")
    f1 = png((1, 2, 3))
    f2 = png((4, 5, 6))
    with TestClient(app) as c:
        token = login(c)
        pid = create_project(c, token, uname("legacy"))
        resp = c.post(
            f"/api/projects/{pid}/generations",
            data={
                "prompt": "旧行为",
                "profile_id": "api:test-assets",
                "ratio": "1:1",
                "resolution": "2K",
                "quality": "standard",
                "idempotency_key": _key("legacy"),
            },
            files=[
                ("references", ("a.png", f1, "image/png")),
                ("references", ("b.png", f2, "image/png")),
            ],
            headers={"X-CSRF-Token": token})
        assert resp.status_code == 202, resp.text
        _gen, refs, manifest = generation_refs(resp.json()["id"])
        # 不传 reference_order → 按上传顺序使用全部文件，无素材引
        assert len(refs) == 2
        assert manifest[0]["asset_id"] == ""
        assert manifest[1]["asset_id"] == ""
