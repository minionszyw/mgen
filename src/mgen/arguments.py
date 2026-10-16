"""两种服务共用的参数声明和提示词读取。"""

from pathlib import Path

from .common import positive, require


def add_network_arguments(parser, base_help):
    parser.add_argument("--base-url", help=base_help)
    parser.add_argument(
        "--timeout", type=positive, default=60, help="单次网络操作超时秒数，默认 60"
    )


def add_prompt_arguments(parser):
    parser.add_argument("--request-json", help="受限请求 JSON；相对素材路径基于运行目录")
    parser.add_argument("--prompt", help="生成或修改提示词")
    parser.add_argument("--prompt-file", help="UTF-8 提示词文件")
    parser.add_argument("--model", help="服务模型；图片服务使用顶层对话模型")


def add_wait_arguments(parser, interval):
    parser.add_argument(
        "--interval", type=positive, default=interval, help=f"轮询间隔秒数，默认 {interval}"
    )
    parser.add_argument("--max-wait", type=positive, default=1800, help="最长等待秒数，默认 1800")


def read_prompt(args):
    require(
        not (args.prompt is not None and args.prompt_file is not None),
        "--prompt 与 --prompt-file 不能同时使用",
    )
    return (
        Path(args.prompt_file).read_text(encoding="utf-8")
        if args.prompt_file is not None
        else args.prompt
    )
