"""共享配置、命令参数与错误处理。"""

import argparse
import json
import math
import os
import re
import sys
from urllib.parse import urlsplit

from dotenv import load_dotenv


class CliError(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def require(ok, message):
    if not ok:
        raise CliError(message)


def http_url(value):
    try:
        p = urlsplit(value)
        return (
            p.scheme in {"http", "https"} and bool(p.hostname) and not p.username and not p.password
        )
    except ValueError:
        return False


def redact(value):
    text = str(value)
    for name in ("VIDEO_API_KEY", "IMAGE_API_KEY"):
        key = os.getenv(name, "")
        if key:
            text = text.replace(key, "[REDACTED]")
    return re.sub(r'data:[^\s"\']+', "[DATA URI]", text)


def log(message):
    print(redact(message), file=sys.stderr)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def serialize(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def positive(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是正数") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须是有限正数")
    return number


def load_config(env_file):
    load_dotenv(env_file, override=False)


def read_json(path):
    def reject(value):
        raise CliError(f"JSON 不允许 {value}")

    with open(path, encoding="utf-8") as source:
        return json.load(source, parse_constant=reject)


def safe_id(value, label="ID", code=3):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise CliError(f"{label} 必须仅含字母、数字、下划线或连字符", code)
    return value
