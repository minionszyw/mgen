"""Responses 图片工作流的请求构建和独立预检。"""

import base64
import binascii
import copy
import re
import tempfile
from pathlib import Path

from PIL import Image

from ..common import CliError, http_url, log, prepare_request, require, safe_id
from ..files import fetch_media, temporary_path

MB = 1_000_000
MAX_PAYLOAD = 512 * MB
MAX_IMAGES = 1500
TOOL_MODELS = {
    name + suffix
    for name in ("gpt-image-2.5-sunburst", "gpt-image-2.5-flare")
    for suffix in ("", "-2026-09-08")
}
MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
TOOL_FIELDS = {
    "type",
    "model",
    "action",
    "size",
    "quality",
    "output_format",
    "output_compression",
    "background",
    "input_image_mask",
}


def inspect_image(path):
    try:
        with Image.open(path) as image:
            require(image.format in MIME_TYPES, "输入图片只支持 PNG、JPEG、WEBP 和非动画 GIF")
            require(not getattr(image, "is_animated", False), "不支持动画图片")
            info = {
                "format": image.format,
                "size": image.size,
                "alpha": "A" in image.getbands(),
                "bytes": Path(path).stat().st_size,
            }
            image.verify()
        with Image.open(path) as image:
            image.load()
        return info
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise CliError("图片内容无效或无法解码") from exc


def prepare_image(item, tmp, timeout, limit=MAX_PAYLOAD, mask=False):
    """返回规范化引用和可读媒体信息；file_id 无法在本地探测。"""
    require(isinstance(item, dict), "图片引用必须是对象")
    fields = set(item) & {"image_url", "file_id"}
    require(len(fields) == 1, "图片必须且只能提供 image_url 或 file_id")
    if "file_id" in fields:
        value = item["file_id"]
        require(
            isinstance(value, str) and re.fullmatch(r"file[-_][A-Za-z0-9_-]+", value),
            "file_id 格式无效",
        )
        log(f"文件 {value} 的图片属性由服务端校验")
        return item, None
    value = item["image_url"]
    require(isinstance(value, str) and bool(value.strip()), "图片地址必须非空")
    path = temporary_path(tmp)
    remote = http_url(value)
    mime = None
    if remote:
        require(not mask, "蒙版请使用本地文件、data URI 或 file_id")
        fetch_media(value, path, limit, timeout)
    elif value.startswith("data:"):
        match = re.fullmatch(r"data:(image/[a-z]+);base64,([A-Za-z0-9+/=\r\n]+)", value)
        require(match is not None, "图片 data URI 格式无效")
        mime, encoded = match.groups()
        require(mime in MIME_TYPES.values(), "图片 data URI MIME 不受支持")
        encoded = encoded.replace("\r", "").replace("\n", "")
        require(len(encoded) <= ((limit + 2) // 3) * 4, "图片 Base64 大小超过限制")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CliError("图片 Base64 内容无效") from exc
        require(0 < len(raw) <= limit, "图片为空或超过大小限制")
        path.write_bytes(raw)
    else:
        require("://" not in value, "图片只支持 HTTP(S)、data URI 或本地路径")
        path = Path(value).expanduser()
        require(path.is_file(), "本地图片文件不存在")
    size = path.stat().st_size
    require(0 < size <= limit, "图片为空或超过大小限制")
    info = inspect_image(path)
    actual_mime = MIME_TYPES[info["format"]]
    require(mime is None or mime == actual_mime, "data URI MIME 与实际图片格式不匹配")
    if not remote:
        item["image_url"] = f"data:{actual_mime};base64," + base64.b64encode(
            path.read_bytes()
        ).decode("ascii")
    return item, info


def enum(value, choices, label):
    require(
        isinstance(value, str) and value in choices, f"{label} 必须是：{', '.join(sorted(choices))}"
    )


def validate_tool(tool):
    require(
        isinstance(tool, dict) and not set(tool) - TOOL_FIELDS,
        "image_generation 工具包含不支持的字段",
    )
    require(tool.get("type") == "image_generation", "仅支持 image_generation 工具")
    enum(tool.get("model"), TOOL_MODELS, "图片工具模型")
    enum(tool.setdefault("action", "auto"), {"auto", "generate", "edit"}, "action")
    enum(
        tool.setdefault("quality", "auto"),
        {"auto", "low", "medium", "high", "xhigh", "max"},
        "quality",
    )
    enum(tool.setdefault("background", "auto"), {"auto", "opaque", "transparent"}, "background")
    enum(tool.setdefault("output_format", "png"), {"png", "jpeg", "webp"}, "output_format")
    size = tool.setdefault("size", "auto")
    require(isinstance(size, str), "size 必须是 auto 或 WIDTHxHEIGHT")
    if size != "auto":
        match = re.fullmatch(r"([1-9][0-9]{0,4})x([1-9][0-9]{0,4})", size)
        require(match is not None, "size 必须是 auto 或 WIDTHxHEIGHT")
        w, h = map(int, match.groups())
        require(w % 16 == 0 and h % 16 == 0, "图片宽高必须为 16 的倍数")
        require(
            max(w, h) <= 3840 and max(w, h) <= 3 * min(w, h),
            "图片边长不能超过 3840，长短边比不能超过 3:1",
        )
        require(655360 <= w * h <= 8294400, "总像素必须在 [655360, 8294400]")
        if w * h > 2560 * 1440:
            log("高于 2560×1440 的输出分辨率属于实验性支持")
    if "output_compression" in tool:
        require(
            type(tool["output_compression"]) is int and 0 <= tool["output_compression"] <= 100,
            "压缩率必须是 0~100 的整数",
        )
        require(tool["output_format"] in {"jpeg", "webp"}, "压缩率仅支持 JPEG/WEBP")
    require(
        tool["background"] != "transparent" or tool["output_format"] in {"png", "webp"},
        "透明背景必须使用 PNG/WEBP",
    )


def validate(payload, timeout=60):
    require(isinstance(payload, dict), "请求必须是 JSON 对象")
    payload = copy.deepcopy(payload)
    require(
        not set(payload)
        - {"model", "input", "tools", "tool_choice", "previous_response_id", "background", "store"},
        "请求包含未支持的字段",
    )
    model = payload.get("model")
    require(
        isinstance(model, str)
        and bool(model.strip())
        and not model.startswith(("gpt-image-", "chatgpt-image-")),
        "model 必须是支持图片工具的对话模型，图片模型请放入 tools",
    )
    for key in ("background", "store"):
        require(payload.setdefault(key, True) is True, f"本 CLI 的后台和多轮工作流要求 {key}=true")
    require(
        payload.setdefault("tool_choice", {"type": "image_generation"})
        == {"type": "image_generation"},
        "必须使用 image_generation tool_choice",
    )
    tools = payload.get("tools")
    require(isinstance(tools, list) and len(tools) == 1, "tools 必须恰好包含一个图片工具")
    tool = tools[0]
    validate_tool(tool)
    previous = payload.get("previous_response_id")
    if "previous_response_id" in payload:
        safe_id(previous, "previous_response_id", 2)
    inputs = payload.get("input")
    if isinstance(inputs, str):
        inputs = [{"role": "user", "content": [{"type": "input_text", "text": inputs}]}]
        payload["input"] = inputs
    require(
        isinstance(inputs, list) and len(inputs) == 1 and isinstance(inputs[0], dict),
        "input 必须是文本或单个 user 消息",
    )
    message = inputs[0]
    require(
        set(message) <= {"role", "content", "type"}
        and message.get("role") == "user"
        and message.get("type", "message") == "message",
        "只支持 user 消息",
    )
    content = message.get("content")
    require(isinstance(content, list) and bool(content), "消息 content 必须是非空数组")
    images = []
    texts = []
    for part in content:
        require(isinstance(part, dict), "content 项必须是对象")
        if part.get("type") == "input_text":
            require(
                set(part) == {"type", "text"}
                and isinstance(part.get("text"), str)
                and bool(part["text"].strip()),
                "提示词必须是非空文本",
            )
            texts.append(part)
        else:
            require(
                part.get("type") == "input_image"
                and not set(part) - {"type", "image_url", "file_id", "detail"},
                "只支持 input_text/input_image",
            )
            enum(part.get("detail", "auto"), {"auto", "low", "high", "original"}, "detail")
            images.append(part)
    require(len(texts) == 1, "必须恰有一个非空 input_text")
    require(len(images) <= MAX_IMAGES, "输入图片数量超过 1500")
    require(
        tool["action"] != "edit" or bool(images) or previous is not None,
        "action=edit 需要输入图片或 previous_response_id",
    )
    mask = tool.get("input_image_mask")
    if mask is not None:
        require(
            isinstance(mask, dict) and not set(mask) - {"image_url", "file_id"},
            "蒙版只允许 image_url 或 file_id",
        )
        require(bool(images), "蒙版编辑必须显式提供第一张输入图片")
    require("input_image_mask" not in tool or mask is not None, "蒙版不能为 null")
    unchecked = []
    total = 0
    infos = []
    with tempfile.TemporaryDirectory(prefix="media-cli-image-") as tmp:
        for index, item in enumerate(images):
            _, info = prepare_image(item, tmp, timeout, MAX_PAYLOAD - total)
            infos.append(info)
            if info is None:
                unchecked.append(f"input_image[{index}]")
            else:
                total += info["bytes"]
        if mask is not None:
            _, mask_info = prepare_image(
                mask, tmp, timeout, min(50 * MB - 1, MAX_PAYLOAD - total), mask=True
            )
            if mask_info is None:
                unchecked.append("input_image_mask")
            else:
                total += mask_info["bytes"]
                require(mask_info["alpha"], "蒙版必须包含 alpha 通道")
            first = infos[0]
            if first is not None:
                require(first["bytes"] < 50 * MB, "被蒙版编辑的第一张图片必须小于 50 MB")
            if first is not None and mask_info is not None:
                require(
                    first["format"] == mask_info["format"] and first["size"] == mask_info["size"],
                    "蒙版与第一张图片必须格式和尺寸一致",
                )
            else:
                unchecked.append("mask_format_and_size_match")
    request = prepare_request(
        payload, MAX_PAYLOAD, "请求体超过 512 MB（含 Base64），请减少素材或使用 URL/file_id"
    )
    return request, {
        "image_count": len(images),
        "unchecked": unchecked,
        "request_bytes": request.size_bytes,
    }
