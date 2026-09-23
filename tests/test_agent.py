"""agent 节点：轴 / spec / 14 字段契约 / 平台规范 / 后置机检。

这些用例**都不联网、不需要 skill 脚本**：它们盯的是纯逻辑 —— 轴的收敛、
计划的宽容解析与硬要求、平台规范的读取，以及机检"宁可不判不可误判"的取向。
"""

import json

import pytest

from image_hub import agent
from image_hub.agent_checks import check_artifact

# ---------------------------------------------------------------------------


def test_unknown_axis_values_fall_back_instead_of_raising():
    """认不出就落默认值，不报错。

    这是给别人兜底的入口：一个拼错的平台名不该让请求 500。真必填由 UI 保证，
    服务端只负责不崩。
    """
    spec = agent.normalize_spec(
        {
            "platform": "amazn",           # 拼错
            "market": "MARS",              # 不存在
            "category": "spaceship",       # 不存在
            "work_type": "???",
            "selling_point": "nope",
        }
    )
    assert spec["platform"] == "general"
    assert spec["market"] == "general"
    assert spec["category"] == agent.CATEGORIES[0][0]
    assert spec["work_type"] == agent.WORK_TYPES[0][0]
    assert spec["selling_point"] == "general"


def test_spec_accepts_json_string_and_garbage():
    assert agent.normalize_spec('{"platform": "amazon"}')["platform"] == "amazon"
    # 坏 JSON / 非字典 / None 都收敛成空 spec，不抛
    assert agent.normalize_spec("{not json")["platform"] == "general"
    assert agent.normalize_spec([1, 2, 3])["platform"] == "general"
    assert agent.normalize_spec(None)["platform"] == "general"


def test_spec_carries_reference_roles_and_extra():
    spec = agent.normalize_spec(
        {
            "platform": "amazon",
            "reference_roles": ["product", "scene", "bogus"],
            "extra": "  背景要暖一点  ",
        }
    )
    # 认不出的职责退回 product（产品外观是默认职责，不是"无"）
    assert spec["reference_roles"] == ["product", "scene", "product"]
    assert spec["extra"] == "背景要暖一点"


def test_blank_reference_roles_stay_blank_and_are_labelled():
    """留空 = 未标注，**不能**被兜成 product（2026-09-21 用户口径：留空由服务端补）。

    把一张场景图当成产品外观，比不标还糟 —— 所以空值必须原样保留，位置也保留
    （``reference_roles`` 与参考图同序），并在参数里给出可读文案。
    """
    spec = agent.normalize_spec({"reference_roles": ["", "scene", "  "]})
    assert spec["reference_roles"] == ["", "scene", ""]

    params = agent.spec_to_script_params(spec)
    assert params["reference_roles"] == [
        {"key": "", "label": agent.UNLABELED_ROLE_LABEL},
        {"key": "scene", "label": "场景参考"},
        {"key": "", "label": agent.UNLABELED_ROLE_LABEL},
    ]

    # 留空不算填上了必选职责 —— 仍然要提示（提示不拦截）。
    assert agent.missing_required_roles(spec) == ["product"]
    # 前端那份文案也来自后端，避免两边漂移。
    assert agent.options_payload()["unlabeled_role_label"] == agent.UNLABELED_ROLE_LABEL


def test_role_notes_carry_a_text_input_instead_of_an_image():
    """「场景用文字描述」这条路：文字按提示词注入，与图片并存。"""
    spec = agent.normalize_spec({"role_notes": {"scene": "  美式后院，午后斜光  ", "bogus": "x"}})
    assert spec["role_notes"] == {"scene": "美式后院，午后斜光"}   # 认不出的角色丢掉

    params = agent.spec_to_script_params(agent.normalize_spec(
        {"reference_roles": ["product"], "role_notes": {"scene": "美式后院"}}
    ))
    assert params["reference_roles"] == [{"key": "product", "label": "产品外观"}]
    assert params["role_notes"] == [{"key": "scene", "label": "场景参考", "text": "美式后院"}]


def test_missing_required_roles_says_what_is_absent():
    assert agent.missing_required_roles(agent.normalize_spec({})) == ["product"]
    # 有产品外观图、或只用文字给了产品外观，都算填上了
    assert agent.missing_required_roles(
        agent.normalize_spec({"reference_roles": ["product"]})
    ) == []
    assert agent.missing_required_roles(
        agent.normalize_spec({"role_notes": {"product": "就是这个杯子"}})
    ) == []


def test_submission_warnings_advise_but_never_block():
    """⭐ 用户明确「不要替用户拒绝需求」—— 所以这些是**提示**，不是 422。

    「必选」的含义是：UI 标必填 + 缺了要提示；服务端仍然放行。
    """
    empty = agent.normalize_spec({})
    warnings = agent.submission_warnings(empty, has_reference_images=False)
    assert any("产品外观" in item for item in warnings)

    # 两个条件同时成立时给出两条，而不是互相盖掉
    both = agent.normalize_spec({"platform": "amazon", "work_type": "white_background"})
    assert len(agent.submission_warnings(both, has_reference_images=False)) == 2

    # 图齐了就没有提示
    done = agent.normalize_spec(
        {"platform": "amazon", "work_type": "white_background", "reference_roles": ["product"]}
    )
    assert agent.submission_warnings(done, has_reference_images=True) == []


def test_options_payload_covers_every_axis():
    options = agent.options_payload()
    assert set(options) >= {
        "platforms", "markets", "categories", "work_types",
        "selling_points", "reference_roles", "defaults",
        "required_reference_roles", "work_type_roles",
    }
    # 每根轴至少要有兜底项，否则前端选不出默认值
    assert options["defaults"]["platform"] == "general"
    assert options["defaults"]["market"] == "general"


def test_work_type_roles_say_which_reference_nodes_each_feature_calls():
    """⭐ 用户口径：「根据不同功能调用」—— 每个功能要有明确的**必选/可选节点**。"""
    for work_type in dict(agent.WORK_TYPES):
        roles = agent.roles_for_work_type(work_type)
        assert roles["required"] == ("product",), f"{work_type} 丢了「产品外观」这个必选项"

    # 白底主图**不吃**场景与排版：道具与版式会被带进画面，直接违反主图规则
    white = agent.roles_for_work_type("white_background")
    assert "scene" not in white["optional"]
    assert "layout" not in white["optional"]
    # 但多角度、以及服装类平台要求的真人模特，要留着
    assert "angle" in white["optional"]
    assert "model" in white["optional"]

    # 认不出的工作类型退回「只必选产品外观」，不抛
    assert agent.roles_for_work_type("nope")["required"] == ("product",)


def test_script_params_carry_key_and_label():
    """脚本不该再抄一份分类表 —— 键与标签都从后端带过去。"""
    params = agent.spec_to_script_params(
        agent.normalize_spec({"platform": "amazon", "market": "us", "selling_point": "quiet"})
    )
    assert params["platform"]["key"] == "amazon"
    assert params["platform"]["label"]
    assert params["market"]["key"] == "us"
    assert params["selling_point"]["label"]


# ---------------------------------------------------------------------------


def test_parse_plan_tolerates_field_name_variations():
    """模型可能回 ``Primary request`` 也可能回 ``primary-request``，都得认。"""
    fields, note = agent.parse_plan(
        {"Primary request": "a white mug on a table", "Use Case": "product-mockup",
         "diagnosis": "按类目锁定了外观"}
    )
    assert fields["primary_request"] == "a white mug on a table"
    assert fields["use_case"] == "product-mockup"
    assert note == "按类目锁定了外观"


def test_parse_plan_unwraps_nested_fields():
    fields, _ = agent.parse_plan({"fields": {"primary_request": "x"}})
    assert fields["primary_request"] == "x"


def test_parse_plan_rejects_a_plan_without_the_required_field():
    """缺 primary_request 就等于没有可用的提示词，必须失败而不是继续拿空串去生图。"""
    with pytest.raises(RuntimeError, match="必填字段"):
        agent.parse_plan({"subject": "a mug"})


def test_parse_plan_joins_list_values():
    fields, _ = agent.parse_plan({"primary_request": ["a mug", "on a table"]})
    assert fields["primary_request"] == "a mug; on a table"


def test_render_prompt_drops_metadata_fields_but_keeps_the_rest():
    fields = dict.fromkeys(agent.PLAN_FIELDS, "")
    fields.update(
        {
            "use_case": "product-mockup",
            "asset_type": "main-image",
            "input_images": "1 product photo",
            "primary_request": "a matte white mug",
            "constraints": "keep the exact shape",
        }
    )
    rendered = agent.render_prompt(fields)
    assert "a matte white mug" in rendered
    assert "keep the exact shape" in rendered
    # 元信息不进提示词：它们是给系统看的，不是画面描述
    assert "product-mockup" not in rendered
    assert "main-image" not in rendered


# ---------------------------------------------------------------------------


def test_platform_record_comes_from_the_research_dictionary():
    """⭐ 平台规范**只有一份数据源**：调研产出的机读字典。

    它是**引用式**的（profile → constraint_keys → blocks），所以同一句约束被多个
    平台复用时只维护一份 —— 这也是它比我手搓的 YAML 好的地方。
    """
    record = agent.platform_record("amazon")
    assert record["canvas"]["ratio"] == "1:1"
    assert record["fill_pct"]["main_min"] == 85
    assert record["source_tier"] == "official"


def test_platform_lookup_is_a_soft_failure_not_a_crash():
    """少一份资料不该让生图崩 —— 认不出就返回空，绝不抛。"""
    assert agent.platform_record("does-not-exist") == {}
    assert agent.platform_prompt_constraints("does-not-exist") == []
    assert agent.render_platform_norms("does-not-exist") == ""


def test_platform_aspect_ratios_drive_the_ui():
    assert agent.platform_aspect_ratios("amazon") == ["1:1"]
    # general 不限比例 → 空列表，让前端回落到模型自己的比例表
    assert agent.platform_aspect_ratios("general") == []


def test_general_platform_renders_nothing():
    """「通用」= **没有平台约束**，不是一条叫「通用」的约束 —— 别塞废话进上下文。"""
    assert agent.render_platform_norms("general") == ""


def test_prompt_constraints_are_the_english_block_text():
    """进 prompt 的是约束块的**英文原文**，与研究字典的取用方式一致。"""
    text = agent.render_platform_norms("amazon", "white_background")
    assert "pure white" in text.lower()
    # 平台 AI 政策要写清「不由你拦」，免得模型自作主张改写需求
    assert "不由你拦" in text


def test_output_specs_never_leak_into_the_prompt():
    """⭐ 像素 / 命名是**后处理**，绝不能出现在提示词里。

    调研结论：只有 6 个维度该进 prompt。模型管不了像素与文件名 —— 写进 prompt
    只会挤占有效信息，还会让它去「保证」一件它根本保证不了的事。
    """
    text = agent.render_platform_norms("amazon", "white_background")
    assert "2000" not in text, "推荐像素漏进提示词了"
    assert "ASIN.MAIN.jpg" not in text, "命名漏进提示词了"
    assert ".jpg" not in text
    # 但比例仍要读得到（它驱动 UI 与生图参数），只是不走提示词
    assert agent.platform_aspect_ratios("amazon") == ["1:1"]


def test_machine_check_spec_exposes_only_what_a_machine_can_judge():
    """机检入参刻意只有两项 —— 文字要 OCR（没装），审美与卖点判不了。"""
    spec = agent.machine_check_spec("amazon")
    assert spec["pure_white"] is True
    assert spec["frame_fill_min"] == 0.85


# ---------------------------------------------------------------------------


def test_main_image_warning_fires_only_without_a_real_product_photo():
    spec = agent.normalize_spec({"platform": "amazon", "work_type": "white_background"})
    # 没有实物参考图 → 模型会凭空编产品，这一次要提示
    assert agent.main_image_warning(spec, has_reference_images=False)
    # 有实物图 → 属于换背景/修光影，是合规路径，不必打扰用户
    assert agent.main_image_warning(spec, has_reference_images=True) == ""


def test_main_image_warning_is_scoped_to_the_work_type_and_platform():
    other_work = agent.normalize_spec({"platform": "amazon", "work_type": "replace"})
    assert agent.main_image_warning(other_work, has_reference_images=False) == ""
    other_platform = agent.normalize_spec(
        {"platform": "shopify", "work_type": "white_background"}
    )
    assert agent.main_image_warning(other_platform, has_reference_images=False) == ""


def test_main_image_warning_states_the_consequence_not_a_refusal():
    spec = agent.normalize_spec({"platform": "amazon", "work_type": "white_background"})
    text = agent.main_image_warning(spec, has_reference_images=False)
    # 用户明确：不要替用户拒绝需求 —— 提示里必须有"风险自担"，不能是"我们不给你做"
    assert "风险由你自行承担" in text


# ---------------------------------------------------------------------------


def _write_png(path, size, color):
    from PIL import Image

    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def test_check_artifact_passes_a_white_square_when_the_platform_demands_it(tmp_path):
    """纯白 + 比例正确 + 主体占比够 —— 三项都过。"""
    # 居中一个小方块，四周留白：边缘是纯白，主体占比远小于 85% → 只该在填充率上提醒
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1024, 1024), (255, 255, 255))
    ImageDraw.Draw(image).rectangle([420, 420, 604, 604], fill=(30, 30, 30))
    path = tmp_path / "white.png"
    image.save(path, format="PNG")

    result = check_artifact(path, expected_ratio="1:1", want_pure_white=True, frame_fill_min=0.85)
    assert result["checks"]["aspect_ratio"]["ok"] is True
    assert result["checks"]["background"]["ok"] is True
    # 小块只占约 3% → 填充率不达标必须被指出来
    assert result["checks"]["frame_fill"]["ok"] is False
    assert result["status"] == "fail"


def test_check_artifact_flags_a_wrong_aspect_ratio(tmp_path):
    path = _write_png(tmp_path / "tall.png", (768, 1024), (255, 255, 255))
    result = check_artifact(path, expected_ratio="1:1")
    assert result["checks"]["aspect_ratio"]["ok"] is False
    assert any("比例不符" in item for item in result["warnings"])


def test_check_artifact_flags_a_tinted_background(tmp_path):
    path = _write_png(tmp_path / "grey.png", (1024, 1024), (200, 200, 200))
    result = check_artifact(path, expected_ratio="1:1", want_pure_white=True)
    assert result["checks"]["background"]["ok"] is False


def test_text_check_reports_unknown_rather_than_guessing(tmp_path):
    """⭐ 没有 OCR 就该说「没检查」，绝不能返回一个看起来像结论的猜测。"""
    path = _write_png(tmp_path / "plain.png", (1024, 1024), (255, 255, 255))
    result = check_artifact(path, expected_ratio="1:1")
    assert result["checks"]["text_watermark"]["ok"] is None
    assert "OCR" in result["checks"]["text_watermark"]["reason"]


def test_check_artifact_degrades_to_unknown_on_an_unreadable_file(tmp_path):
    bad = tmp_path / "not-an-image.png"
    bad.write_bytes(b"definitely not a png")
    result = check_artifact(bad, expected_ratio="1:1")
    assert result["status"] == "unknown"
    assert result["checks"]["aspect_ratio"]["ok"] is None


def test_plan_fields_contract_is_stable():
    """14 字段是**对外契约**，顺序也有意义（渲染时按它拼）。改动必须是有意的。"""
    assert len(agent.PLAN_FIELDS) == 14
    assert agent.PLAN_FIELDS[0] == "use_case"
    assert "primary_request" in agent.REQUIRED_PLAN_FIELDS
    assert json.dumps(list(agent.PLAN_FIELDS))  # 可序列化，前端/历史能直接用
