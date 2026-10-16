"""带鉴权的 JSON 传输；服务错误策略通过客户端方法定义。"""

import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from .common import CliError, JsonRequest, http_url, log, redact, require, serialize

MAX_RETRIES = 3


def retry_delay(response, attempt):
    delay = min(2**attempt, 30)
    if response is not None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                parsed = float(header)
            except ValueError:
                try:
                    parsed = (
                        parsedate_to_datetime(header) - datetime.now(timezone.utc)
                    ).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    return delay
            if math.isfinite(parsed):
                delay = max(0, parsed)
    return delay


class JsonClient:
    def __init__(self, base, key, timeout=60, service="API"):
        require(
            http_url(base) and not urlsplit(base).query and not urlsplit(base).fragment,
            f"{service}_BASE_URL 必须是无用户名密码、查询参数和片段的 HTTP(S) 地址",
        )
        require(isinstance(key, str) and bool(key.strip()), f"请设置 {service}_API_KEY")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.key = key
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {key}"})

    def close(self):
        self.session.close()

    def is_api_error(self, data):
        return data.get("type") == "error" or bool(data.get("error"))

    def retryable(self, status, data):
        return status == 429 or 500 <= status < 600

    def request(self, method, path, payload=None, deadline=None):
        encoded = (
            payload.body
            if isinstance(payload, JsonRequest)
            else serialize(payload)
            if payload is not None
            else None
        )
        attempt = 0
        while True:
            remaining = deadline - time.monotonic() if deadline is not None else self.timeout
            if remaining <= 0:
                raise CliError("等待超时，可使用原 ID 继续查询", 5)
            response = None
            try:
                kwargs = {"timeout": min(self.timeout, remaining), "allow_redirects": False}
                if encoded is not None:
                    kwargs.update(data=encoded, headers={"Content-Type": "application/json"})
                response = self.session.request(method, self.base + path, **kwargs)
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if not (
                    method == "GET"
                    and self.retryable(response.status_code, data)
                    and attempt < MAX_RETRIES
                ):
                    if not isinstance(data, dict):
                        raise CliError(f"API 返回非 JSON 对象（HTTP {response.status_code}）", 3)
                    if not 200 <= response.status_code < 300 or self.is_api_error(data):
                        request_id = data.get("request_id") or response.headers.get(
                            "x-request-id", ""
                        )
                        message = f"HTTP {response.status_code}：{data.get('error', {})} request_id={request_id}"
                        raise CliError(redact(message, (self.key,)), 3)
                    return data
                delay = retry_delay(response, attempt)
            except requests.RequestException as exc:
                if method != "GET" or attempt >= MAX_RETRIES:
                    suffix = "；创建请求不会自动重试" if method == "POST" else "；查询重试已耗尽"
                    raise CliError(f"网络请求失败：{redact(exc, (self.key,))}{suffix}", 3) from exc
                delay = retry_delay(response, attempt)
            finally:
                if response is not None:
                    response.close()
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            log(f"查询暂时失败，{delay:g} 秒后重试")
            time.sleep(delay)
            attempt += 1
