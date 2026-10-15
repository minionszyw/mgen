"""MiniMax V2 任务和视频下载。"""

import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import requests

from ..common import CliError, http_url, log, redact, require, safe_id
from ..files import publish_file
from ..http import JsonClient, retry_delay


class VideoClient(JsonClient):
    def __init__(self, base, key, timeout=60):
        super().__init__(base, key, timeout, service="VIDEO")

    def create(self, payload):
        data = self.request("POST", "/v2/video_generation", payload)
        require_task_id(data.get("task_id"))
        log(f"任务已创建：{data['task_id']}；可用 video query 或 video download 继续处理")
        return data

    def query(self, task_id, deadline=None):
        require_task_id(task_id)
        data = self.request(
            "GET", "/v2/query/video_generation/" + quote(task_id, safe=""), deadline=deadline
        )
        task = data.get("task")
        if not isinstance(task, dict) or task.get("id") != task_id:
            raise CliError("查询响应缺少 task 或任务 ID 不匹配", 3)
        if not isinstance(task.get("status"), str) or task["status"] not in {
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise CliError("查询响应包含未知或缺失的任务状态", 3)
        return data

    def wait(self, task_id, interval, max_wait):
        deadline = time.monotonic() + max_wait
        while True:
            data = self.query(task_id, deadline)
            status = data["task"]["status"]
            log(f"任务 {task_id}：{status}")
            check_terminal(data)
            if status == "succeeded":
                return data
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CliError(f"等待超时；可继续查询任务 {task_id}", 5)
            time.sleep(min(interval, remaining))


def require_task_id(task_id):
    safe_id(task_id, "任务 ID")


def check_terminal(data):
    task = data["task"]
    if task["status"] in {"failed", "cancelled"}:
        raise CliError(f"任务 {task['id']} {task['status']}：{redact(task.get('error', {}))}", 4)


def download(client, task_id, data, output, overwrite):
    check_terminal(data)
    require(data["task"]["status"] == "succeeded", "任务尚未成功，请使用 --wait")
    destination = Path(output or f"downloads/{task_id}.mp4").expanduser()
    require(overwrite or not destination.exists(), "目标文件已存在，使用 --overwrite 覆盖")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        temp = None
        response = None
        try:
            content = data["task"].get("content")
            url = content.get("url") if isinstance(content, dict) else None
            if not isinstance(url, str) or not http_url(url):
                raise CliError("成功响应缺少有效 task.content.url", 3)
            with requests.Session() as session:
                with session.get(url, stream=True, timeout=client.timeout) as response:
                    if response.status_code in {403, 404}:
                        if attempt == 0:
                            data = client.query(task_id)
                            check_terminal(data)
                            continue
                    response.raise_for_status()
                    size = 0
                    with tempfile.NamedTemporaryFile(
                        dir=destination.parent, prefix=".media-cli-", delete=False
                    ) as out:
                        temp = Path(out.name)
                        for chunk in response.iter_content(256 * 1024):
                            out.write(chunk)
                            size += len(chunk)
                    length = response.headers.get("Content-Length")
                    if size == 0 or (
                        length
                        and not response.headers.get("Content-Encoding")
                        and size != int(length)
                    ):
                        raise requests.ConnectionError("下载为空或长度与 Content-Length 不符")
            publish_file(temp, destination, overwrite)
            return {"task_id": task_id, "path": str(destination.resolve()), "bytes": size}
        except requests.RequestException as exc:
            retryable = (
                response is None
                or response.status_code == 429
                or response.status_code >= 500
                or 200 <= response.status_code < 300
            )
            if not retryable or attempt == 3:
                raise CliError(f"下载失败：{redact(exc)}；任务 ID：{task_id}", 3) from exc
            delay = retry_delay(response, attempt)
            log(f"下载暂时失败，{delay:g} 秒后重试")
            time.sleep(delay)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    raise CliError("下载失败", 3)
