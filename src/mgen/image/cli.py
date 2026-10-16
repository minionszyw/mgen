"""OpenAI Responses 图片命令。"""

from ..arguments import add_network_arguments, add_prompt_arguments, add_wait_arguments, read_prompt
from ..common import CliError, emit, read_json, require
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


def build_payload(args, config):
    if args.request_json:
        require(
            not any(getattr(args, key) is not None for key in GENERATION_FIELDS),
            "--request-json 不能与生成快捷参数混用",
        )
        payload = read_json(args.request_json)
        require(isinstance(payload, dict), "请求文件必须是 JSON 对象")
        payload.setdefault("model", config.model)
        if (
            isinstance(payload.get("tools"), list)
            and len(payload["tools"]) == 1
            and isinstance(payload["tools"][0], dict)
        ):
            payload["tools"][0].setdefault("model", config.tool_model)
        return payload
    prompt = read_prompt(args)
    content = [{"type": "input_text", "text": prompt}]
    for value in args.image or []:
        content.append({"type": "input_image", "image_url": value})
    for value in args.image_file_id or []:
        content.append({"type": "input_image", "file_id": value})
    tool = {
        "type": "image_generation",
        "model": config.tool_model,
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
        "model": config.model,
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
        "create": "创建图片响应，默认后台执行",
        "query": "查询响应状态",
        "download": "获取并保存全部图片",
        "run": "创建、等待并保存图片",
    }
    for name, description in descriptions.items():
        cmd = commands.add_parser(name, help=description, description=description)
        add_network_arguments(cmd, "Responses API 基础地址，包含 /v1")
        if name in {"validate", "create", "run"}:
            add_prompt_arguments(cmd)
            cmd.add_argument(
                "--sync",
                action="store_true",
                help="同步执行，不轮询；第三方不支持后台模式时建议开启，可用 --timeout 300 延长等待",
            )
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
            add_wait_arguments(cmd, 5)
        if name in {"download", "run"}:
            cmd.add_argument("--output-dir", help="图片和结果清单输出目录")
            cmd.add_argument("--overwrite", action="store_true", help="允许覆盖已有结果")
    parser.set_defaults(handler=execute)


def execute(args, config):
    if args.command in {"validate", "create", "run"}:
        payload, report = validate(build_payload(args, config), args.timeout, sync=args.sync)
        if args.command == "validate":
            emit(
                {
                    "valid": True,
                    "model": payload.payload["model"],
                    "tool_model": payload.payload["tools"][0]["model"],
                    **report,
                }
            )
            return 0
    client = ImageClient(
        config.base_url,
        config.api_key,
        args.timeout,
    )
    try:
        initial = None
        if args.command in {"create", "run"}:
            initial = client.create(payload)
            response_id = initial["id"]
            if args.sync and initial["status"] != "completed":
                emit(summary(initial))
                check_terminal(initial)
                raise CliError(f"同步响应 {response_id} 尚未完成；未自动查询或重新创建", 4)
        else:
            response_id = args.response_id
        if args.command == "create" or (args.command == "run" and args.sync):
            data = initial
        elif args.command == "run" or args.wait:
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
