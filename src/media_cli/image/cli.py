"""OpenAI Responses 图片命令。"""

import os
from pathlib import Path

from ..common import emit, positive, read_json, require
from .client import ImageClient, check_terminal, decode_outputs, save_images, summary
from .validation import validate

GENERATION_FIELDS = (
    "prompt",
    "prompt_file",
    "image",
    "image_file_id",
    "mask",
    "mask_file_id",
    "previous_response_id",
    "model",
    "tool_model",
    "action",
    "size",
    "quality",
    "output_format",
    "output_compression",
    "background",
)


def build_payload(args):
    if args.request_json:
        require(
            not any(getattr(args, key) is not None for key in GENERATION_FIELDS),
            "--request-json 不能与生成快捷参数混用",
        )
        payload = read_json(args.request_json)
        require(isinstance(payload, dict), "请求文件必须是 JSON 对象")
        payload.setdefault("model", os.getenv("IMAGE_MODEL", "gpt-6-astra"))
        if (
            isinstance(payload.get("tools"), list)
            and len(payload["tools"]) == 1
            and isinstance(payload["tools"][0], dict)
        ):
            payload["tools"][0].setdefault(
                "model", os.getenv("IMAGE_TOOL_MODEL", "gpt-image-2.5-sunburst")
            )
        return payload
    require(
        not (args.prompt is not None and args.prompt_file is not None),
        "--prompt 与 --prompt-file 不能同时使用",
    )
    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    content = [{"type": "input_text", "text": prompt}]
    for value in args.image or []:
        content.append({"type": "input_image", "image_url": value})
    for value in args.image_file_id or []:
        content.append({"type": "input_image", "file_id": value})
    tool = {
        "type": "image_generation",
        "model": args.tool_model or os.getenv("IMAGE_TOOL_MODEL", "gpt-image-2.5-sunburst"),
    }
    for key in ("action", "size", "quality", "output_format", "output_compression", "background"):
        if getattr(args, key) is not None:
            tool[key] = getattr(args, key)
    require(
        not (args.mask is not None and args.mask_file_id is not None),
        "--mask 与 --mask-file-id 不能同时使用",
    )
    if args.mask is not None:
        tool["input_image_mask"] = {"image_url": args.mask}
    elif args.mask_file_id is not None:
        tool["input_image_mask"] = {"file_id": args.mask_file_id}
    payload = {
        "model": args.model or os.getenv("IMAGE_MODEL", "gpt-6-astra"),
        "input": [{"role": "user", "content": content}],
        "tools": [tool],
    }
    if args.previous_response_id is not None:
        payload["previous_response_id"] = args.previous_response_id
    return payload


def add_commands(parser):
    commands = parser.add_subparsers(dest="command", required=True, title="操作")
    descriptions = {
        "validate": "仅构建和预检请求",
        "create": "后台创建图片响应",
        "query": "查询响应状态",
        "download": "获取并保存全部图片",
        "run": "创建、等待并保存图片",
    }
    for name, description in descriptions.items():
        cmd = commands.add_parser(name, help=description, description=description)
        cmd.add_argument("--base-url", help="Responses API 基础地址，包含 /v1")
        cmd.add_argument(
            "--timeout", type=positive, default=60, help="单次网络操作超时秒数，默认 60"
        )
        if name in {"validate", "create", "run"}:
            cmd.add_argument("--request-json", help="图片工作流 JSON 请求文件")
            cmd.add_argument("--prompt", help="图片生成或修改提示词")
            cmd.add_argument("--prompt-file", help="UTF-8 提示词文件")
            cmd.add_argument("--model", help="顶层对话模型")
            cmd.add_argument("--tool-model", help="图片生成工具模型")
            cmd.add_argument(
                "--image", action="append", help="本地路径、HTTP(S) URL 或 data URI，可重复"
            )
            cmd.add_argument(
                "--image-file-id", action="append", help="已有 file_id，可重复，排列在 --image 之后"
            )
            cmd.add_argument("--mask", help="本地蒙版路径或 data URI")
            cmd.add_argument("--mask-file-id", help="已有蒙版 file_id")
            cmd.add_argument("--previous-response-id", help="继续修改的前序响应 ID")
            cmd.add_argument("--action", help="auto / generate / edit")
            cmd.add_argument("--size", help="auto 或 WIDTHxHEIGHT")
            cmd.add_argument("--quality", help="auto / low / medium / high / xhigh / max")
            cmd.add_argument("--output-format", help="png / jpeg / webp")
            cmd.add_argument("--output-compression", type=int, help="JPEG/WEBP 压缩率 0~100")
            cmd.add_argument("--background", help="auto / opaque / transparent")
        if name in {"query", "download"}:
            cmd.add_argument("response_id", help="Responses API 响应 ID")
            cmd.add_argument("--wait", action="store_true", help="等待后台响应完成")
        if name in {"query", "download", "run"}:
            cmd.add_argument("--interval", type=positive, default=5, help="轮询间隔秒数，默认 5")
            cmd.add_argument(
                "--max-wait", type=positive, default=1800, help="最长等待秒数，默认 1800"
            )
        if name in {"download", "run"}:
            cmd.add_argument("--output-dir", help="图片和结果清单输出目录")
            cmd.add_argument("--overwrite", action="store_true", help="允许覆盖已有结果")
    parser.set_defaults(handler=execute)


def execute(args):
    payload = None
    if args.command in {"validate", "create", "run"}:
        payload, report = validate(build_payload(args), args.timeout)
        if args.command == "validate":
            emit(
                {
                    "valid": True,
                    "model": payload["model"],
                    "tool_model": payload["tools"][0]["model"],
                    **report,
                }
            )
            return 0
    client = ImageClient(
        args.base_url or os.getenv("IMAGE_BASE_URL", "https://api.openai.com/v1"),
        os.getenv("IMAGE_API_KEY"),
        args.timeout,
    )
    try:
        initial = None
        if args.command in {"create", "run"}:
            initial = client.create(payload)
            response_id = initial["id"]
            if args.command == "create":
                emit(summary(initial))
                check_terminal(initial)
                if initial["status"] == "completed":
                    decode_outputs(initial)
                return 0
        else:
            response_id = args.response_id
        if args.command == "run" or args.wait:
            data = client.wait(response_id, args.interval, args.max_wait, initial)
        else:
            data = client.query(response_id)
        if args.command in {"download", "run"}:
            emit(save_images(data, args.output_dir, args.overwrite))
        else:
            emit(summary(data))
            check_terminal(data)
            if data["status"] == "completed":
                decode_outputs(data)
        return 0
    finally:
        client.close()
