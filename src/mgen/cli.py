"""统一命令入口。"""

import argparse
import sys

from . import __version__
from .common import CliError, log, output_secrets
from .config import load_config
from .image import cli as image
from .video import cli as video


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self.add_argument("-h", "--help", action="help", help="显示帮助并退出")
        self._optionals.title = "选项"

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误：{message}\n")


def parser():
    root = Parser(prog="mgen", description="MiniMax 视频与 OpenAI Responses 图片生成工具")
    root.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}", help="显示版本并退出"
    )
    root.add_argument("--env-file", default=".env", help="配置文件路径，默认当前目录 .env")
    services = root.add_subparsers(dest="service", required=True, title="服务")
    video.add_commands(services.add_parser("video", help="MiniMax 视频 V2"))
    image.add_commands(services.add_parser("image", help="OpenAI Responses 图片"))
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = load_config(args.env_file)
    except (OSError, ValueError) as exc:
        log(f"配置读取失败：{exc}")
        return 2
    with output_secrets(config.secrets):
        try:
            return args.handler(args, getattr(config, args.service).with_cli(args))
        except CliError as exc:
            log(exc)
            return exc.code
        except (OSError, ValueError) as exc:
            log(f"文件或数据错误：{exc}")
            return 2
        except KeyboardInterrupt:
            log("已中断；服务端任务未取消，请使用已显示的 ID 继续查询")
            return 130
