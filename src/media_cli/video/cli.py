"""MiniMax 视频命令。"""

import argparse
import os
from pathlib import Path

from ..common import emit, positive, read_json, require, serialize
from .client import VideoClient, check_terminal, download
from .validation import validate


def build_payload(args):
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
        payload.setdefault("model", os.getenv("VIDEO_MODEL", "MiniMax-H3"))
        return payload
    require(
        not (args.prompt is not None and args.prompt_file is not None),
        "--prompt 与 --prompt-file 不能同时使用",
    )
    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
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
        "model": args.model or os.getenv("VIDEO_MODEL", "MiniMax-H3"),
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
    sub = p.add_subparsers(dest="command", required=True)
    for name in ["validate", "create", "query", "download", "run"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("--base-url", help="API 根地址，不含 /v2 路径")
        cmd.add_argument("--timeout", type=positive, default=60, help="单次网络操作超时秒数")
        if name in {"validate", "create", "run"}:
            cmd.add_argument(
                "--request-json", help="官方 JSON 请求文件；相对素材路径基于当前工作目录"
            )
            cmd.add_argument("--prompt")
            cmd.add_argument("--prompt-file")
            cmd.add_argument("--model")
            cmd.add_argument("--resolution")
            cmd.add_argument("--duration", type=int)
            cmd.add_argument("--ratio")
            for role in ["first-frame", "last-frame"]:
                cmd.add_argument("--" + role)
            for role in ["reference-image", "reference-video", "reference-audio"]:
                cmd.add_argument("--" + role, action="append")
            cmd.add_argument("--prompt-expansion-mode")
            cmd.add_argument("--callback-url")
            cmd.add_argument(
                "--aigc-watermark", action=argparse.BooleanOptionalAction, default=None
            )
            cmd.add_argument("--skip-media-check", action="store_true")
        if name in {"query", "download"}:
            cmd.add_argument("task_id")
            cmd.add_argument("--wait", action="store_true")
        if name in {"query", "download", "run"}:
            cmd.add_argument("--interval", type=positive, default=10)
            cmd.add_argument("--max-wait", type=positive, default=1800)
        if name in {"download", "run"}:
            cmd.add_argument("--output", help="输出路径，默认 downloads/TASK_ID.mp4")
            cmd.add_argument("--overwrite", action="store_true")
    p.set_defaults(handler=execute)


def execute(args):
    payload = None
    if args.command in {"validate", "create", "run"}:
        payload = validate(build_payload(args), args.skip_media_check, args.timeout)
    if args.command == "validate":
        emit(
            {
                "valid": True,
                "model": payload["model"],
                "request_bytes": len(serialize(payload)),
                "media_checked": not args.skip_media_check,
            }
        )
        return 0
    client = VideoClient(
        args.base_url or os.getenv("VIDEO_BASE_URL", "https://api.minimax.cn"),
        os.getenv("VIDEO_API_KEY"),
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
