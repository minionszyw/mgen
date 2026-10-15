"""带鉴权的 JSON API 传输；只有 GET 自动重试。"""

import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from .common import CliError, http_url, log, redact, require, serialize


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


def transient(status, data):
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and (
        error.get("code")
        in ("insufficient_quota", "billing_hard_limit_reached", "moderation_blocked")
        or error.get("type") in ("insufficient_quota", "image_generation_user_error")
    ):
        return False
    return status == 429 or 500 <= status < 600


class JsonClient:
    def __init__(self, base, key, timeout=60, service="API"):
        require(
            http_url(base)
            and not requests.utils.urlparse(base).query
            and not requests.utils.urlparse(base).fragment,
            f"{service}_BASE_URL 必须是无用户名密码、查询参数和片段的 HTTP(S) 地址",
        )
        require(bool(key and key.strip()), f"请设置 {service}_API_KEY")
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.key = key
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {key}"})

    def close(self):
        self.session.close()

    def request(self, method, path, payload=None, deadline=None):
        for attempt in range(4):
            response = None
            remaining = deadline - time.monotonic() if deadline is not None else self.timeout
            if remaining <= 0:
                raise CliError("等待超时，可使用原 ID 继续查询", 5)
            try:
                kwargs = {"timeout": min(self.timeout, remaining), "allow_redirects": False}
                if payload is not None:
                    kwargs.update(
                        data=serialize(payload), headers={"Content-Type": "application/json"}
                    )
                response = self.session.request(method, self.base + path, **kwargs)
            except requests.RequestException as exc:
                if method != "GET" or attempt == 3:
                    message = str(exc).replace(self.key, "[REDACTED]")
                    raise CliError(
                        f"网络请求失败：{redact(message)}；创建请求不会自动重试", 3
                    ) from exc
            else:
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if not (method == "GET" and transient(response.status_code, data) and attempt < 3):
                    if not isinstance(data, dict):
                        raise CliError(f"API 返回非 JSON 对象（HTTP {response.status_code}）", 3)
                    if (
                        not 200 <= response.status_code < 300
                        or data.get("type") == "error"
                        or (data.get("error") and "status" not in data)
                    ):
                        error = data.get("error", {})
                        request_id = data.get("request_id") or response.headers.get(
                            "x-request-id", ""
                        )
                        message = f"HTTP {response.status_code}：{error} request_id={request_id}"
                        raise CliError(redact(message.replace(self.key, "[REDACTED]")), 3)
                    return data
            delay = retry_delay(response, attempt)
            if response is not None:
                response.close()
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            log(f"查询暂时失败，{delay:g} 秒后重试")
            time.sleep(delay)
        raise CliError("请求失败", 3)
