"""共享错误、输出脱敏、JSON 请求及基本校验。"""

import argparse
import json
import math
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit


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


_secrets = ContextVar("output_secrets", default=())


@contextmanager
def output_secrets(secrets):
    token = _secrets.set(tuple(secret for secret in secrets if secret))
    try:
        yield
    finally:
        _secrets.reset(token)


def redact(value, secrets=()):
    text = str(value)
    for key in sorted(set((*_secrets.get(), *secrets)) - {""}, key=len, reverse=True):
        text = text.replace(key, "[REDACTED]")
    return re.sub(r'data:[^\s"\']+', "[DATA URI]", text)


def redact_json(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_json(item) for item in value]
    return value


def log(message):
    print(redact(message), file=sys.stderr)


def emit(value):
    print(json.dumps(redact_json(value), ensure_ascii=False, indent=2))


@dataclass(frozen=True)
class JsonRequest:
    """规范化请求及已校验的 UTF-8 字节，发送时复用，避免重复编码。"""

    payload: dict
    body: bytes

    @property
    def size_bytes(self):
        return len(self.body)


def prepare_request(payload, limit, message):
    body = serialize(payload)
    require(len(body) <= limit, message)
    return JsonRequest(payload, body)


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


def read_json(path):
    def reject(value):
        raise CliError(f"JSON 不允许 {value}")

    with open(path, encoding="utf-8") as source:
        return json.load(source, parse_constant=reject)


def safe_id(value, label="ID", code=3):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise CliError(f"{label} 必须仅含字母、数字、下划线或连字符", code)
    return value
