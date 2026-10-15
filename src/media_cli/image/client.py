"""Responses 后台生命周期及图片结果保存。"""

import base64
import binascii
import io
import json
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from ..common import CliError, log, require, safe_id
from ..files import atomic_write
from ..http import JsonClient

ACTIVE = {"queued", "in_progress"}
STATUSES = ACTIVE | {"completed", "failed", "cancelled", "incomplete"}


def check_response(data, response_id=None):
    if not isinstance(data, dict):
        raise CliError("响应必须是 JSON 对象", 3)
    safe_id(data.get("id"), "响应 ID")
    if response_id is not None and data["id"] != response_id:
        raise CliError("响应 ID 与查询不匹配", 3)
    if not isinstance(data.get("status"), str) or data["status"] not in STATUSES:
        raise CliError("未知或缺失的响应状态", 3)
    if data["status"] == "completed" and not isinstance(data.get("output"), list):
        raise CliError("完成响应缺少 output 数组", 3)
    return data


def check_terminal(data):
    if data["status"] in {"failed", "cancelled", "incomplete"}:
        detail = data.get("error") or data.get("incomplete_details") or {}
        raise CliError(f"响应 {data['id']} {data['status']}：{detail}", 4)


def summary(data):
    """仅返回稳定、无图片字节的结果元数据。"""
    result = {
        key: data[key]
        for key in ("id", "status", "model", "created_at", "error", "incomplete_details", "usage")
        if key in data
    }
    result["output"] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        result["output"].append(
            {
                key: item[key]
                for key in (
                    "id",
                    "type",
                    "status",
                    "revised_prompt",
                    "output_format",
                    "size",
                    "quality",
                    "background",
                    "action",
                )
                if key in item
            }
        )
    return result


class ImageClient(JsonClient):
    def __init__(self, base, key, timeout=60):
        super().__init__(base, key, timeout, service="IMAGE")

    def create(self, payload):
        data = check_response(self.request("POST", "/responses", payload))
        log(f"响应已创建：{data['id']}；可用 image query 或 image download 恢复")
        return data

    def query(self, response_id, deadline=None):
        safe_id(response_id, "响应 ID")
        return check_response(
            self.request("GET", "/responses/" + quote(response_id, safe=""), deadline=deadline),
            response_id,
        )

    def wait(self, response_id, interval, max_wait, initial=None):
        deadline = time.monotonic() + max_wait
        data = initial
        while True:
            if data is None:
                data = self.query(response_id, deadline)
            log(f"响应 {response_id}：{data['status']}")
            check_terminal(data)
            if data["status"] == "completed":
                return data
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CliError(f"等待超时；可继续查询响应 {response_id}", 5)
            time.sleep(min(interval, remaining))
            data = None


def decode_outputs(data):
    check_response(data)
    check_terminal(data)
    require(data["status"] == "completed", "响应尚未完成，请使用 --wait")
    outputs = []
    seen = set()
    for item in data["output"]:
        if not isinstance(item, dict):
            raise CliError("output 项必须是对象", 3)
        if item.get("type") != "image_generation_call":
            continue
        call_id = safe_id(item.get("id"), "图片调用 ID")
        if call_id in seen:
            raise CliError("图片调用 ID 重复", 3)
        seen.add(call_id)
        if item.get("status") != "completed" or not isinstance(item.get("result"), str):
            raise CliError(f"图片调用 {call_id} 未完成或没有图片结果", 4)
        try:
            raw = base64.b64decode(item["result"], validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                fmt = image.format
                if fmt not in {"PNG", "JPEG", "WEBP"}:
                    raise CliError("返回图片格式不受支持", 3)
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
        except (
            ValueError,
            binascii.Error,
            OSError,
            SyntaxError,
            Image.DecompressionBombError,
        ) as exc:
            raise CliError(f"图片调用 {call_id} 的 Base64 或图片内容无效", 3) from exc
        outputs.append((item, raw, fmt.lower()))
    if not outputs:
        raise CliError(f"响应 {data['id']} 已完成，但没有生成图片（可能拒绝了请求）", 4)
    return outputs


def save_images(data, output_dir=None, overwrite=False):
    outputs = decode_outputs(data)  # 所有结果先验证，避免损坏数据造成部分保存。
    directory = Path(output_dir or f"downloads/images/{data['id']}").expanduser()
    destinations = [directory / f"{item['id']}.{fmt}" for item, _, fmt in outputs]
    manifest_path = directory / "result.json"
    for path in [*destinations, manifest_path]:
        require(not path.is_dir(), f"目标不能是目录：{path}")
        require(overwrite or not path.exists(), f"目标文件已存在：{path}；使用 --overwrite 覆盖")
    saved = []
    manifest = summary(data)
    manifest["images"] = saved
    try:
        for (item, raw, fmt), path in zip(outputs, destinations):
            atomic_write(path, raw, overwrite)
            entry = {
                "call_id": item["id"],
                "path": str(path.resolve()),
                "format": fmt,
                "bytes": len(raw),
            }
            if "revised_prompt" in item:
                entry["revised_prompt"] = item["revised_prompt"]
            saved.append(entry)
        atomic_write(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            overwrite,
        )
    except OSError as exc:
        raise CliError(
            f"保存失败：{exc}；已保存路径：{[entry['path'] for entry in saved]}", 2
        ) from exc
    return {"response_id": data["id"], "images": saved, "manifest": str(manifest_path.resolve())}
