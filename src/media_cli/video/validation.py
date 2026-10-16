"""MiniMax V2 请求和输入媒体预检。"""

import base64
import binascii
import copy
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pillow_heif
from PIL import Image

from ..common import CliError, http_url, log, prepare_request, require
from ..files import fetch_media, temporary_path

pillow_heif.register_heif_opener()
MB = 1_000_000  # 文档未明确 MB 的二进制含义，采用较保守的十进制。
RATIOS = {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
MODELS = {"MiniMax-H3": ({"768P", "2K"}, 4), "MiniMax-H3-Max": ({"480P", "768P"}, 5)}
ROLES = {
    "image_url": {"first_frame", "last_frame", "reference_image"},
    "video_url": {"reference_video"},
    "audio_url": {"reference_audio"},
}


def probe(path):
    require(
        shutil.which("ffprobe"),
        "音视频预检需要 ffprobe；macOS：brew install ffmpeg；Debian/Ubuntu：apt install ffmpeg",
    )
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            timeout=30,
            check=True,
        )
        return json.loads(p.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        raise CliError("ffprobe 无法解析媒体或探测超时") from exc


def finite_number(value, label):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise CliError(f"无法读取媒体 {label}") from None
    require(math.isfinite(number), f"媒体 {label} 无效")
    return number


def dimensions(width, height):
    require(256 <= width <= 5760 and 256 <= height <= 5760, "媒体宽高必须在 [256, 5760] px")
    require(0.4 <= width / height <= 2.5, "媒体宽高比必须在 [0.4, 2.5]")


def inspect_media(path, kind):
    if kind == "image_url":
        try:
            with Image.open(path) as im:
                require(im.format in {"JPEG", "PNG", "WEBP", "HEIF", "HEIC"}, "不支持的图片格式")
                dimensions(*im.size)
                fmt = im.format.lower()
                im.verify()
            with Image.open(path) as image:
                image.load()
            return 0, "image/" + fmt
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise CliError("无法读取图片") from exc
    info = probe(path)
    fmt = info.get("format", {})
    duration = finite_number(fmt.get("duration"), "时长")
    require(2 <= duration <= 15, "音视频单段时长必须在 [2, 15] 秒")
    names = set(fmt.get("format_name", "").split(","))
    streams = info.get("streams", [])
    videos = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if kind == "video_url":
        require(bool(names & {"mp4", "mov"}), "参考视频必须为 MP4/MOV")
        # MP4 与 MOV 共享 demuxer，用 major_brand 排除同组的 3GP 等容器。
        brand = fmt.get("tags", {}).get("major_brand", "").strip()
        require(
            not brand
            or brand
            in {"isom", "iso2", "iso3", "iso4", "iso5", "iso6", "mp41", "mp42", "avc1", "qt"},
            "不支持的视频容器品牌",
        )
        require(len(videos) == 1, "参考视频必须包含一个视频轨道")
        for s in videos:
            require(s.get("codec_name") in {"h264", "hevc"}, "视频编码必须为 H.264/H.265")
            dimensions(s.get("width", 0), s.get("height", 0))
            rate = s.get("avg_frame_rate", "0/1")
            try:
                n, d = map(float, rate.split("/"))
                fps = n / d
            except (ValueError, ZeroDivisionError):
                raise CliError("无法读取视频帧率") from None
            require(math.isfinite(fps) and 23.976 <= fps <= 60, "视频帧率必须在 [23.976, 60]")
        require(
            all(s.get("codec_name") in {"aac", "mp3"} for s in audios),
            "视频内音频编码必须为 AAC/MP3",
        )
        # 视频 data URI 按官方协议统一使用 video/mp4。
        return duration, "video/mp4"
    require(bool(names & {"wav", "mp3"}) and bool(audios) and not videos, "参考音频必须为 WAV/MP3")
    return duration, "audio/wav" if "wav" in names else "audio/mp3"


def prepare_media(value, kind, skip, tmp, timeout):
    require(isinstance(value, str) and bool(value.strip()), "素材地址必须是非空字符串")
    if value.startswith("mm_file://"):
        require(bool(re.fullmatch(r"mm_file://[A-Za-z0-9_-]+", value)), "无效 mm_file 地址")
        require(skip, "mm_file:// 无法完整预检；请提供可读素材或显式使用 --skip-media-check")
        log("mm_file 素材属性由服务端校验")
        return value, 0
    declared_mime = None
    limit = {"image_url": 30, "video_url": 50, "audio_url": 15}[kind] * MB
    path = temporary_path(tmp)
    remote = http_url(value)
    if remote:
        if skip:
            return value, 0
        fetch_media(value, path, limit, timeout)
    elif value.startswith("data:"):
        match = re.fullmatch(
            r"data:([a-z0-9.+-]+/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)", value
        )
        require(match is not None, "无效 Base64 data URI")
        mime, encoded = match.groups()
        declared_mime = mime
        allowed = {
            "image_url": {
                "image/jpg",
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/heic",
                "image/heif",
            },
            "video_url": {"video/mp4"},
            "audio_url": {"audio/wav", "audio/mp3", "audio/mpeg"},
        }
        require(mime in allowed[kind], "data URI MIME 类型不符合媒体类型或协议")
        require(len(encoded) <= (limit + 2) // 3 * 4 + 1024, "Base64 素材超过文件大小限制")
        try:
            decoded = base64.b64decode(encoded.replace("\r", "").replace("\n", ""), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CliError("Base64 内容无效") from exc
        require(0 < len(decoded) <= limit, "素材为空或超过文件大小限制")
        path.write_bytes(decoded)
    else:
        require("://" not in value, "素材地址仅支持 HTTP(S)、mm_file、data URI 或本地路径")
        path = Path(value).expanduser()
        require(path.is_file(), "本地素材文件不存在")
    require(0 < path.stat().st_size <= limit, "素材为空或超过文件大小限制")
    if skip:
        log("已跳过媒体属性检查，大小和请求结构仍会检查")
        mime = {
            "image_url": "image/" + path.suffix.lstrip(".").lower(),
            "video_url": "video/mp4",
            "audio_url": "audio/" + path.suffix.lstrip(".").lower(),
        }[kind]
        if value.startswith("data:"):
            return value, 0
        require(
            mime
            in {
                "image/jpg",
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/heic",
                "image/heif",
                "video/mp4",
                "audio/wav",
                "audio/mp3",
            },
            "无法根据本地扩展名确定媒体类型",
        )
        duration = 0
    else:
        duration, mime = inspect_media(path, kind)
        if declared_mime is not None:
            aliases = {
                "image/jpg": "image/jpeg",
                "image/heic": "image/heif",
                "audio/mpeg": "audio/mp3",
            }
            require(
                aliases.get(declared_mime, declared_mime) == aliases.get(mime, mime),
                "data URI MIME 与实际媒体格式不匹配",
            )
    if remote or value.startswith("data:"):
        return value, duration
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii"), duration


def validate(payload, skip=False, timeout=60):
    require(isinstance(payload, dict), "请求必须是 JSON 对象")
    payload = copy.deepcopy(payload)
    require(
        not set(payload)
        - {
            "model",
            "content",
            "resolution",
            "duration",
            "ratio",
            "extra",
            "callback_url",
            "aigc_watermark",
        },
        "请求含未声明字段",
    )
    model = payload.get("model")
    require(isinstance(model, str) and model in MODELS, "模型必须是 MiniMax-H3 或 MiniMax-H3-Max")
    resolutions, minimum = MODELS[model]
    require(
        isinstance(payload.get("resolution"), str) and payload["resolution"] in resolutions,
        "分辨率不受当前模型支持",
    )
    require(
        type(payload.get("duration")) is int and minimum <= payload["duration"] <= 15,
        f"当前模型时长必须是 {minimum}~15 的整数",
    )
    content = payload.get("content")
    require(isinstance(content, list) and bool(content), "content 必须是非空数组")
    counts = {}
    texts = 0
    for item in content:
        require(isinstance(item, dict), "content 元素必须是对象")
        kind = item.get("type")
        require(isinstance(kind, str) and kind in {"text", *ROLES}, "未知 content 类型")
        if kind == "text":
            require(set(item) <= {"type", "text"}, "text 项含不支持字段")
            require(
                isinstance(item.get("text"), str)
                and bool(item["text"].strip())
                and len(item["text"]) <= 7000,
                "prompt 必须非空且不超过 7000 字符",
            )
            texts += 1
            continue
        require(set(item) <= {"type", kind, "role"}, "素材项含不支持字段")
        media = item.get(kind)
        require(isinstance(media, dict) and set(media) == {"url"}, "素材对象必须仅含 url 字段")
        role = item.get("role", "first_frame" if kind == "image_url" else None)
        require(isinstance(role, str) and role in ROLES[kind], "素材 role 与类型不匹配")
        counts[role] = counts.get(role, 0) + 1
    require(texts == 1, "content 必须恰有一个非空 text 项")
    for role, count in counts.items():
        require(
            count
            <= {
                "first_frame": 1,
                "last_frame": 1,
                "reference_image": 9,
                "reference_video": 3,
                "reference_audio": 3,
            }[role],
            f"{role} 数量超限",
        )
    frames = bool(set(counts) & {"first_frame", "last_frame"})
    refs = any(role.startswith("reference_") for role in counts)
    require(not (frames and refs), "首尾帧与参考素材不能混用")
    ratio = payload.setdefault("ratio", "adaptive" if counts else "16:9")
    require(isinstance(ratio, str) and ratio in RATIOS, "无效 ratio")
    require(bool(counts) or ratio != "adaptive", "文生视频不能使用 adaptive 比例")
    if frames and ratio != "adaptive":
        log("首尾帧生成的比例由图片决定，已设为 adaptive")
        payload["ratio"] = "adaptive"
    if "extra" in payload:
        extra = payload["extra"]
        require(model == "MiniMax-H3-Max", "extra 仅支持 MiniMax-H3-Max")
        require(
            isinstance(extra, dict) and not set(extra) - {"prompt_expansion_mode"},
            "extra 含未声明字段",
        )
        mode = extra.get("prompt_expansion_mode", "balanced")
        require(
            isinstance(mode, str) and mode in {"disabled", "balanced", "quality"},
            "无效 prompt_expansion_mode",
        )
    if "aigc_watermark" in payload:
        require(type(payload["aigc_watermark"]) is bool, "aigc_watermark 必须是布尔值")
    if "callback_url" in payload:
        require(
            isinstance(payload["callback_url"], str) and http_url(payload["callback_url"]),
            "callback_url 必须是 HTTP(S) 地址",
        )
    totals = {"video_url": 0, "audio_url": 0}
    with tempfile.TemporaryDirectory(prefix="minimax-preflight-") as tmp:
        for item in content:
            kind = item["type"]
            if kind == "text":
                continue
            value, duration = prepare_media(item[kind]["url"], kind, skip, tmp, timeout)
            item[kind]["url"] = value
            if kind in totals:
                totals[kind] += duration
    require(
        all(total <= 15 for total in totals.values()),
        "参考视频或参考音频的各自总时长不得超过 15 秒",
    )
    return prepare_request(
        payload, 64 * MB, "JSON 请求体超过 64 MB（含 Base64 膨胀），请改用公网 URL"
    )
