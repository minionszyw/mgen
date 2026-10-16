"""MiniMax 视频命令。"""

import argparse

from ..arguments import add_network_arguments, add_prompt_arguments, add_wait_arguments, read_prompt
from ..common import emit, read_json, require
from .client import VideoClient, check_terminal, download
from .validation import validate


def build_payload(args, config):
    if args.request_json:
        generation = [
            "prompt",
            "prompt_file",
            "first_frame",
            "last_frame",
            "reference_image",
            "reference_video",
            "reference_audio",
            "model",
            "resolution",
            "duration",
            "ratio",
            "prompt_expansion_mode",
            "callback_url",
            "aigc_watermark",
        ]
        require(
            not any(getattr(args, name) is not None for name in generation),
            "--request-json 不能与生成快捷参数混用",
        )
        payload = read_json(args.request_json)
        require(isinstance(payload, dict), "请求文件必须是 JSON 对象")
        payload.setdefault("model", config.model)
        return payload
    prompt = read_prompt(args)
    items = [{"type": "text", "text": prompt}]
    for role in [
        "first_frame",
        "last_frame",
        "reference_image",
        "reference_video",
        "reference_audio",
    ]:
        values = getattr(args, role)
        if values is None:
            continue
        if isinstance(values, str):
            values = [values]
        kind = (
            "image_url"
            if role in {"first_frame", "last_frame", "reference_image"}
            else role.replace("reference_", "") + "_url"
        )
        for value in values:
            items.append({"type": kind, kind: {"url": value}, "role": role})
    payload = {
        "model": config.model,
        "content": items,
        "resolution": args.resolution if args.resolution is not None else "768P",
        "duration": args.duration if args.duration is not None else 5,
    }
    for key in ["ratio", "callback_url", "aigc_watermark"]:
        if getattr(args, key) is not None:
            payload[key] = getattr(args, key)
    if args.prompt_expansion_mode is not None:
        payload["extra"] = {"prompt_expansion_mode": args.prompt_expansion_mode}
    return payload


def add_commands(p):
    sub = p.add_subparsers(dest="command", required=True, title="操作")
    descriptions = {
        "validate": "仅构建和预检请求",
        "create": "创建视频任务",
        "query": "查询任务状态",
        "download": "下载视频结果",
        "run": "创建、等待并下载视频",
    }
    for name, description in descriptions.items():
        cmd = sub.add_parser(name, help=description, description=description)
        add_network_arguments(cmd, "API 根地址，不含 /v2 路径")
        if name in {"validate", "create", "run"}:
            add_prompt_arguments(cmd)
            cmd.add_argument("--resolution", help="模型支持的输出分辨率")
            cmd.add_argument("--duration", type=int, help="生成时长，整数秒")
            cmd.add_argument("--ratio", help="输出比例，图生视频使用 adaptive")
            for role in ["first-frame", "last-frame"]:
                cmd.add_argument("--" + role, help="首帧/尾帧素材：路径、URL 或 data URI")
            for role in ["reference-image", "reference-video", "reference-audio"]:
                cmd.add_argument(
                    "--" + role, action="append", help="参考素材：路径、URL 或 data URI，可重复"
                )
            cmd.add_argument(
                "--prompt-expansion-mode", help="H3 Max 提示词扩写：disabled / balanced / quality"
            )
            cmd.add_argument("--callback-url", help="已有回调服务的 URL")
            cmd.add_argument(
                "--aigc-watermark",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="生成视频时添加 AIGC 标识水印",
            )
            cmd.add_argument(
                "--skip-media-check",
                action="store_true",
                help="跳过媒体属性探测，保留结构和可获得的大小检查",
            )
        if name in {"query", "download"}:
            cmd.add_argument("task_id", help="创建时返回的任务 ID")
            cmd.add_argument("--wait", action="store_true", help="等待任务完成")
        if name in {"query", "download", "run"}:
            add_wait_arguments(cmd, 10)
        if name in {"download", "run"}:
            cmd.add_argument("--output", help="输出路径，默认 downloads/TASK_ID.mp4")
            cmd.add_argument("--overwrite", action="store_true", help="允许覆盖已有文件")
    p.set_defaults(handler=execute)


def execute(args, config):
    payload = None
    if args.command in {"validate", "create", "run"}:
        payload = validate(build_payload(args, config), args.skip_media_check, args.timeout)
    if args.command == "validate":
        emit(
            {
                "valid": True,
                "model": payload.payload["model"],
                "request_bytes": payload.size_bytes,
                "media_checked": not args.skip_media_check,
            }
        )
        return 0
    client = VideoClient(
        config.base_url,
        config.api_key,
        args.timeout,
    )
    try:
        if args.command in {"create", "run"}:
            data = client.create(payload)
            if args.command == "create":
                emit(data)
                return 0
            task_id = data["task_id"]
        else:
            task_id = args.task_id
        if args.command == "run" or args.wait:
            data = client.wait(task_id, args.interval, args.max_wait)
        else:
            data = client.query(task_id)
        if args.command in {"run", "download"}:
            emit(download(client, task_id, data, args.output, args.overwrite))
        else:
            emit(data)
            check_terminal(data)
        return 0
    finally:
        client.close()
