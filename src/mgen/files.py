"""无 API 鉴权的素材读取与原子文件落盘。"""

import os
import tempfile
from pathlib import Path

import requests

from .common import CliError, redact, require


def fetch_media(url, target, limit, timeout):
    try:
        with requests.Session() as session:
            with session.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length and length.isdigit():
                    require(int(length) <= limit, "远程素材超过文件大小限制")
                size = 0
                with open(target, "wb") as out:
                    for chunk in response.iter_content(64 * 1024):
                        size += len(chunk)
                        require(size <= limit, "远程素材超过文件大小限制")
                        out.write(chunk)
    except requests.RequestException as exc:
        raise CliError(f"无法下载远程素材：{redact(exc)}", 3) from exc


def publish_file(temp, destination, overwrite=False):
    """同文件系统内原子发布；禁止覆盖时避免 TOCTOU。"""
    if overwrite:
        os.replace(temp, destination)
    else:
        os.link(temp, destination)
        Path(temp).unlink()


def atomic_write(destination, data, overwrite=False):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".mgen-", delete=False
        ) as out:
            temp = Path(out.name)
            out.write(data)
        publish_file(temp, destination, overwrite)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def temporary_path(directory):
    """保留唯一临时路径；生命周期由调用方的临时目录管理。"""
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".mgen-", delete=False) as out:
        return Path(out.name)
