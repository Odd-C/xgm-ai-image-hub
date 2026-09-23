"""agent 节点的后置机检：只做**机器判得了**的三类。"""

from __future__ import annotations

from pathlib import Path

#: 采样时把图缩到多大再算。4K 图有 800 万像素，逐像素遍历既慢又没必要。
_SAMPLE_EDGE = 256
#: 判定「纯白」的容差。JPEG 压缩会让 255 掉到 250 上下，卡太严会全是假告警。
_WHITE_MIN = 250


def _parse_ratio(value: str) -> float | None:
    text = (value or "").strip()
    if ":" not in text:
        return None
    left, _, right = text.partition(":")
    try:
        numerator, denominator = float(left), float(right)
    except ValueError:
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    return numerator / denominator


def _tint_ok(mean_channels: tuple[float, float, float]) -> bool:
    return all(channel >= _WHITE_MIN for channel in mean_channels)


def _border_mean(image) -> tuple[float, float, float]:
    """取最外一圈像素的均值 —— 主图的背景就是这一圈。"""
    width, height = image.size
    pixels = image.load()
    total = [0, 0, 0]
    count = 0
    for x in range(width):
        for y in (0, height - 1):
            pixel = pixels[x, y]
            total = [total[i] + pixel[i] for i in range(3)]
            count += 1
    for y in range(height):
        for x in (0, width - 1):
            pixel = pixels[x, y]
            total = [total[i] + pixel[i] for i in range(3)]
            count += 1
    if not count:
        return (0.0, 0.0, 0.0)
    return (total[0] / count, total[1] / count, total[2] / count)


def _non_white_fraction(image) -> float:
    width, height = image.size
    pixels = image.load()
    total = width * height
    if not total:
        return 0.0
    inside = 0
    for x in range(width):
        for y in range(height):
            pixel = pixels[x, y]
            if not all(channel >= _WHITE_MIN for channel in pixel[:3]):
                inside += 1
    return inside / total


def check_artifact(
    path: str | Path,
    *,
    expected_ratio: str = "",
    want_pure_white: bool = False,
    frame_fill_min: float | None = None,
) -> dict:
    """对一张产物做机检。**任何异常都降级成 unknown，不抛** —— 校验不该让任务失败。"""
    result: dict = {
        "status": "unknown",
        "checks": {},
        "warnings": [],
    }
    checks = result["checks"]

    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        checks["aspect_ratio"] = {"ok": None, "reason": "环境缺少 Pillow，无法读图"}
        return result

    try:
        with Image.open(path) as handle:
            handle.load()
            image = handle.convert("RGB")
            width, height = image.size
            sample = image.copy()
            sample.thumbnail((_SAMPLE_EDGE, _SAMPLE_EDGE))
            border = _border_mean(sample)
            white = _tint_ok(border)
            fill = _non_white_fraction(sample) if white else None
    except Exception as exc:  # noqa: BLE001
        checks["aspect_ratio"] = {"ok": None, "reason": f"读图失败：{exc}"}
        return result

    result["size"] = {"width": width, "height": height}

    # --- 比例 -----------------------------------------------------------------
    wanted = _parse_ratio(expected_ratio)
    actual = (width / height) if height else None
    if wanted and actual:
        # 2% 容差：上游按边长取整，1:1 出来常常是 1024×1024 或 2048×2048
        passed = abs(actual - wanted) / wanted <= 0.02
        checks["aspect_ratio"] = {
            "ok": passed,
            "expected": expected_ratio,
            "actual": f"{width}x{height}",
            "reason": "" if passed else f"期望 {expected_ratio}，实际 {width}×{height}",
        }
        if not passed:
            result["warnings"].append(f"比例不符：期望 {expected_ratio}，实际 {width}×{height}")
    else:
        checks["aspect_ratio"] = {"ok": None, "reason": "没有可比对的比例（模型未指定）"}

    # --- 纯白底 ---------------------------------------------------------------
    wants_white = bool(want_pure_white)
    if wants_white:
        checks["background"] = {
            "ok": white,
            "border_mean": [round(channel, 1) for channel in border],
            "reason": "" if white else "边缘像素不是纯白，平台要求 RGB 255,255,255",
        }
        if not white:
            result["warnings"].append("背景不是纯白，不满足该平台主图要求")
    else:
        checks["background"] = {"ok": None, "reason": "该平台未要求纯白底"}

    # --- 填充率 ---------------------------------------------------------------
    minimum = frame_fill_min
    if wants_white and fill is not None and isinstance(minimum, (int, float)):
        passed = fill >= float(minimum)
        checks["frame_fill"] = {
            "ok": passed,
            "actual": round(fill, 3),
            "min": float(minimum),
            "reason": "" if passed else f"主体占比 {fill:.0%}，低于该平台要求的 {float(minimum):.0%}",
        }
        if not passed:
            result["warnings"].append(f"主体占比不足（{fill:.0%} < {float(minimum):.0%}）")
    else:
        checks["frame_fill"] = {"ok": None, "reason": "无法计算（背景非纯白或平台未要求）"}

    # --- 文字 / 水印 ---------------------------------------------------------
    checks["text_watermark"] = {
        "ok": None,
        "reason": "需要 OCR 依赖，当前部署未安装 —— 这一项**没有检查**，请人眼确认",
    }

    judged = [entry.get("ok") for entry in checks.values() if entry.get("ok") is not None]
    if any(entry is False for entry in judged):
        result["status"] = "fail"
    elif judged and all(entry is True for entry in judged):
        result["status"] = "pass"
    else:
        result["status"] = "unknown"
    return result
