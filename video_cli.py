#!/usr/bin/env python3
"""MiniMax 视频 V2：预检、创建、查询、下载。"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from dotenv import load_dotenv
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()
MB = 1_000_000  # 文档未明确 MB 的二进制含义，采用较保守的十进制。
RATIOS = {'adaptive', '21:9', '16:9', '4:3', '1:1', '3:4', '9:16'}
MODELS = {'MiniMax-H3': ({'768P', '2K'}, 4), 'MiniMax-H3-Max': ({'480P', '768P'}, 5)}
ROLES = {'image_url': {'first_frame', 'last_frame', 'reference_image'},
         'video_url': {'reference_video'}, 'audio_url': {'reference_audio'}}


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
        return p.scheme in {'http', 'https'} and bool(p.hostname) and not p.username and not p.password
    except ValueError:
        return False


def redact(value):
    text = str(value)
    key = os.getenv('VIDEO_API_KEY', '')
    if key:
        text = text.replace(key, '[REDACTED]')
    return re.sub(r'data:[^\s"\']+', '[DATA URI]', text)


def log(message):
    print(redact(message), file=sys.stderr)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def retry_delay(response, attempt):
    delay = min(2 ** attempt, 30)
    if response is not None:
        header = response.headers.get('Retry-After')
        if header:
            try:
                delay = max(0, float(header))
            except ValueError:
                try:
                    delay = max(0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
    return delay


class Client:
    def __init__(self, base, key, timeout=60):
        require(http_url(base), 'VIDEO_BASE_URL 必须是无用户名密码的 HTTP(S) 地址')
        require(bool(key and key.strip()), '请设置 VIDEO_API_KEY')
        self.base = base.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {key}'})

    def request(self, method, path, payload=None, deadline=None):
        for attempt in range(4):
            response = None
            remaining = deadline - time.monotonic() if deadline is not None else self.timeout
            if remaining <= 0:
                raise CliError('等待超时，可使用原任务 ID 继续查询', 5)
            try:
                kwargs = {'timeout': min(self.timeout, remaining), 'allow_redirects': False}
                if payload is not None:
                    kwargs.update(data=serialize(payload), headers={'Content-Type': 'application/json'})
                response = self.session.request(method, self.base + path, **kwargs)
            except requests.RequestException as exc:
                if method != 'GET' or attempt == 3:
                    raise CliError(f'网络请求失败：{redact(exc)}；创建请求不会自动重试', 3) from exc
            else:
                if not (method == 'GET' and (response.status_code == 429 or response.status_code >= 500) and attempt < 3):
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise CliError(f'API 返回非 JSON 数据（HTTP {response.status_code}）', 3) from exc
                    if not isinstance(data, dict):
                        raise CliError('API 响应必须是 JSON 对象', 3)
                    if not 200 <= response.status_code < 300 or data.get('type') == 'error' or 'error' in data:
                        err = data.get('error', {})
                        raise CliError(redact(f'HTTP {response.status_code}：{err} request_id={data.get("request_id", "")}'), 3)
                    return data
            delay = retry_delay(response, attempt)
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            log(f'查询暂时失败，{delay:g} 秒后重试')
            time.sleep(delay)
        raise CliError('请求失败', 3)

    def create(self, payload):
        data = self.request('POST', '/v2/video_generation', payload)
        require_task_id(data.get('task_id'))
        log(f'任务已创建：{data["task_id"]}；可用 query 或 download 继续处理')
        return data

    def query(self, task_id, deadline=None):
        require_task_id(task_id)
        data = self.request('GET', '/v2/query/video_generation/' + quote(task_id, safe=''), deadline=deadline)
        task = data.get('task')
        if not isinstance(task, dict) or task.get('id') != task_id:
            raise CliError('查询响应缺少 task 或任务 ID 不匹配', 3)
        if task.get('status') not in {'queued', 'running', 'succeeded', 'failed', 'cancelled'}:
            raise CliError('查询响应包含未知或缺失的任务状态', 3)
        return data

    def wait(self, task_id, interval, max_wait):
        deadline = time.monotonic() + max_wait
        while True:
            data = self.query(task_id, deadline)
            status = data['task']['status']
            log(f'任务 {task_id}：{status}')
            check_terminal(data)
            if status == 'succeeded':
                return data
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CliError(f'等待超时；可继续查询任务 {task_id}', 5)
            time.sleep(min(interval, remaining))


def require_task_id(task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', task_id):
        raise CliError('任务 ID 必须是仅含字母、数字、下划线或连字符的非空字符串', 3)


def check_terminal(data):
    task = data['task']
    if task['status'] in {'failed', 'cancelled'}:
        raise CliError(f'任务 {task["id"]} {task["status"]}：{redact(task.get("error", {}))}', 4)


def serialize(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def probe(path):
    require(shutil.which('ffprobe'), '音视频预检需要 ffprobe；macOS：brew install ffmpeg；Debian/Ubuntu：apt install ffmpeg')
    try:
        p = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
                           capture_output=True, timeout=30, check=True)
        return json.loads(p.stdout)
    except (subprocess.SubprocessError, ValueError) as exc:
        raise CliError('ffprobe 无法解析媒体或探测超时') from exc


def finite_number(value, label):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise CliError(f'无法读取媒体 {label}') from None
    require(math.isfinite(number), f'媒体 {label} 无效')
    return number


def dimensions(width, height):
    require(256 <= width <= 5760 and 256 <= height <= 5760, '媒体宽高必须在 [256, 5760] px')
    require(0.4 <= width / height <= 2.5, '媒体宽高比必须在 [0.4, 2.5]')


def inspect_media(path, kind):
    if kind == 'image_url':
        try:
            with Image.open(path) as im:
                require(im.format in {'JPEG', 'PNG', 'WEBP', 'HEIF', 'HEIC'}, '不支持的图片格式')
                dimensions(*im.size)
                fmt = im.format.lower()
                im.verify()
                return 0, 'image/' + ('jpeg' if fmt == 'jpeg' else fmt)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise CliError('无法读取图片') from exc
    info = probe(path)
    fmt = info.get('format', {})
    duration = finite_number(fmt.get('duration'), '时长')
    require(2 <= duration <= 15, '音视频单段时长必须在 [2, 15] 秒')
    names = set(fmt.get('format_name', '').split(','))
    streams = info.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    if kind == 'video_url':
        require(bool(names & {'mp4', 'mov'}), '参考视频必须为 MP4/MOV')
        # MP4 与 MOV 共享 demuxer，用 major_brand 排除同组的 3GP 等容器。
        brand = fmt.get('tags', {}).get('major_brand', '').strip()
        require(not brand or brand in {'isom', 'iso2', 'iso3', 'iso4', 'iso5', 'iso6', 'mp41', 'mp42', 'avc1', 'qt'}, '不支持的视频容器品牌')
        require(len(videos) == 1, '参考视频必须包含一个视频轨道')
        for s in videos:
            require(s.get('codec_name') in {'h264', 'hevc'}, '视频编码必须为 H.264/H.265')
            dimensions(s.get('width', 0), s.get('height', 0))
            rate = s.get('avg_frame_rate', '0/1')
            try:
                n, d = map(float, rate.split('/'))
                fps = n / d
            except (ValueError, ZeroDivisionError):
                raise CliError('无法读取视频帧率') from None
            require(math.isfinite(fps) and 23.976 <= fps <= 60, '视频帧率必须在 [23.976, 60]')
        require(all(s.get('codec_name') in {'aac', 'mp3'} for s in audios), '视频内音频编码必须为 AAC/MP3')
        # 视频 data URI 按官方协议统一使用 video/mp4。
        return duration, 'video/mp4'
    require(bool(names & {'wav', 'mp3'}) and bool(audios) and not videos, '参考音频必须为 WAV/MP3')
    return duration, 'audio/wav' if 'wav' in names else 'audio/mp3'


def fetch_media(url, target, limit, timeout):
    try:
        with requests.Session() as session:
            with session.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                length = response.headers.get('Content-Length')
                if length and length.isdigit():
                    require(int(length) <= limit, '远程素材超过文件大小限制')
                size = 0
                with open(target, 'wb') as out:
                    for chunk in response.iter_content(64 * 1024):
                        size += len(chunk)
                        require(size <= limit, '远程素材超过文件大小限制')
                        out.write(chunk)
    except requests.RequestException as exc:
        raise CliError(f'无法下载远程素材：{redact(exc)}', 3) from exc


def prepare_media(value, kind, skip, tmp, timeout):
    require(isinstance(value, str) and bool(value.strip()), '素材地址必须是非空字符串')
    if value.startswith('mm_file://'):
        require(bool(re.fullmatch(r'mm_file://[A-Za-z0-9_-]+', value)), '无效 mm_file 地址')
        require(skip, 'mm_file:// 无法完整预检；请提供可读素材或显式使用 --skip-media-check')
        log('mm_file 素材属性由服务端校验')
        return value, 0
    limit = {'image_url': 30, 'video_url': 50, 'audio_url': 15}[kind] * MB
    path = Path(tmp) / str(time.monotonic_ns())
    remote = http_url(value)
    if remote:
        if skip:
            return value, 0
        fetch_media(value, path, limit, timeout)
    elif value.startswith('data:'):
        match = re.fullmatch(r'data:([a-z0-9.+-]+/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)', value)
        require(match is not None, '无效 Base64 data URI')
        mime, encoded = match.groups()
        allowed = {'image_url': {'image/jpg', 'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'},
                   'video_url': {'video/mp4'}, 'audio_url': {'audio/wav', 'audio/mp3', 'audio/mpeg'}}
        require(mime in allowed[kind], 'data URI MIME 类型不符合媒体类型或协议')
        require(len(encoded) <= (limit + 2) // 3 * 4 + 1024, 'Base64 素材超过文件大小限制')
        try:
            decoded = base64.b64decode(encoded.replace('\r', '').replace('\n', ''), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CliError('Base64 内容无效') from exc
        require(0 < len(decoded) <= limit, '素材为空或超过文件大小限制')
        path.write_bytes(decoded)
    else:
        require('://' not in value, '素材地址仅支持 HTTP(S)、mm_file、data URI 或本地路径')
        path = Path(value).expanduser()
        require(path.is_file(), '本地素材文件不存在')
    require(0 < path.stat().st_size <= limit, '素材为空或超过文件大小限制')
    if skip:
        log('已跳过媒体属性检查，大小和请求结构仍会检查')
        mime = {'image_url': 'image/' + path.suffix.lstrip('.').lower(), 'video_url': 'video/mp4',
                'audio_url': 'audio/' + path.suffix.lstrip('.').lower()}[kind]
        if value.startswith('data:'):
            return value, 0
        require(mime in {'image/jpg', 'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif',
                         'video/mp4', 'audio/wav', 'audio/mp3'}, '无法根据本地扩展名确定媒体类型')
        duration = 0
    else:
        duration, mime = inspect_media(path, kind)
    if remote or value.startswith('data:'):
        return value, duration
    return f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode('ascii'), duration


def validate(payload, skip=False, timeout=60):
    require(isinstance(payload, dict), '请求必须是 JSON 对象')
    require(not set(payload) - {'model', 'content', 'resolution', 'duration', 'ratio', 'extra', 'callback_url', 'aigc_watermark'}, '请求含未声明字段')
    model = payload.get('model')
    require(isinstance(model, str) and model in MODELS, '模型必须是 MiniMax-H3 或 MiniMax-H3-Max')
    resolutions, minimum = MODELS[model]
    require(isinstance(payload.get('resolution'), str) and payload['resolution'] in resolutions, '分辨率不受当前模型支持')
    require(type(payload.get('duration')) is int and minimum <= payload['duration'] <= 15, f'当前模型时长必须是 {minimum}~15 的整数')
    content = payload.get('content')
    require(isinstance(content, list) and bool(content), 'content 必须是非空数组')
    counts = {}
    texts = 0
    for item in content:
        require(isinstance(item, dict), 'content 元素必须是对象')
        kind = item.get('type')
        require(isinstance(kind, str) and kind in {'text', *ROLES}, '未知 content 类型')
        if kind == 'text':
            require(set(item) <= {'type', 'text'}, 'text 项含不支持字段')
            require(isinstance(item.get('text'), str) and bool(item['text'].strip()) and len(item['text']) <= 7000, 'prompt 必须非空且不超过 7000 字符')
            texts += 1
            continue
        require(set(item) <= {'type', kind, 'role'}, '素材项含不支持字段')
        media = item.get(kind)
        require(isinstance(media, dict) and set(media) == {'url'}, '素材对象必须仅含 url 字段')
        role = item.get('role', 'first_frame' if kind == 'image_url' else None)
        require(isinstance(role, str) and role in ROLES[kind], '素材 role 与类型不匹配')
        counts[role] = counts.get(role, 0) + 1
    require(texts == 1, 'content 必须恰有一个非空 text 项')
    for role, count in counts.items():
        require(count <= {'first_frame': 1, 'last_frame': 1, 'reference_image': 9, 'reference_video': 3, 'reference_audio': 3}[role], f'{role} 数量超限')
    frames = bool(set(counts) & {'first_frame', 'last_frame'})
    refs = any(role.startswith('reference_') for role in counts)
    require(not (frames and refs), '首尾帧与参考素材不能混用')
    ratio = payload.setdefault('ratio', 'adaptive' if counts else '16:9')
    require(isinstance(ratio, str) and ratio in RATIOS, '无效 ratio')
    require(bool(counts) or ratio != 'adaptive', '文生视频不能使用 adaptive 比例')
    if frames and ratio != 'adaptive':
        log('首尾帧生成的比例由图片决定，已设为 adaptive')
        payload['ratio'] = 'adaptive'
    if 'extra' in payload:
        extra = payload['extra']
        require(model == 'MiniMax-H3-Max', 'extra 仅支持 MiniMax-H3-Max')
        require(isinstance(extra, dict) and not set(extra) - {'prompt_expansion_mode'}, 'extra 含未声明字段')
        mode = extra.get('prompt_expansion_mode', 'balanced')
        require(isinstance(mode, str) and mode in {'disabled', 'balanced', 'quality'}, '无效 prompt_expansion_mode')
    if 'aigc_watermark' in payload:
        require(type(payload['aigc_watermark']) is bool, 'aigc_watermark 必须是布尔值')
    if 'callback_url' in payload:
        require(isinstance(payload['callback_url'], str) and http_url(payload['callback_url']), 'callback_url 必须是 HTTP(S) 地址')
    totals = {'video_url': 0, 'audio_url': 0}
    with tempfile.TemporaryDirectory(prefix='minimax-preflight-') as tmp:
        for item in content:
            kind = item['type']
            if kind == 'text':
                continue
            value, duration = prepare_media(item[kind]['url'], kind, skip, tmp, timeout)
            item[kind]['url'] = value
            if kind in totals:
                totals[kind] += duration
    require(all(total <= 15 for total in totals.values()), '参考视频或参考音频的各自总时长不得超过 15 秒')
    require(len(serialize(payload)) <= 64 * MB, 'JSON 请求体超过 64 MB（含 Base64 膨胀），请改用公网 URL')
    return payload


def build_payload(args):
    if args.request_json:
        generation = ['prompt', 'prompt_file', 'first_frame', 'last_frame', 'reference_image', 'reference_video',
                      'reference_audio', 'model', 'resolution', 'duration', 'ratio', 'prompt_expansion_mode', 'callback_url', 'aigc_watermark']
        require(not any(getattr(args, name) is not None for name in generation), '--request-json 不能与生成快捷参数混用')
        with open(args.request_json, encoding='utf-8') as f:
            def reject_constant(value):
                raise CliError(f'JSON 不允许 {value}')
            payload = json.load(f, parse_constant=reject_constant)
        require(isinstance(payload, dict), '请求文件必须是 JSON 对象')
        payload.setdefault('model', os.getenv('VIDEO_MODEL', 'MiniMax-H3'))
        return payload
    require(not (args.prompt is not None and args.prompt_file is not None), '--prompt 与 --prompt-file 不能同时使用')
    prompt = Path(args.prompt_file).read_text(encoding='utf-8') if args.prompt_file else args.prompt
    items = [{'type': 'text', 'text': prompt}]
    for role in ['first_frame', 'last_frame', 'reference_image', 'reference_video', 'reference_audio']:
        values = getattr(args, role)
        if values is None:
            continue
        if isinstance(values, str):
            values = [values]
        kind = 'image_url' if role in {'first_frame', 'last_frame', 'reference_image'} else role.replace('reference_', '') + '_url'
        for value in values:
            items.append({'type': kind, kind: {'url': value}, 'role': role})
    payload = {'model': args.model or os.getenv('VIDEO_MODEL', 'MiniMax-H3'), 'content': items,
               'resolution': args.resolution if args.resolution is not None else '768P',
               'duration': args.duration if args.duration is not None else 5}
    for key in ['ratio', 'callback_url', 'aigc_watermark']:
        if getattr(args, key) is not None:
            payload[key] = getattr(args, key)
    if args.prompt_expansion_mode is not None:
        payload['extra'] = {'prompt_expansion_mode': args.prompt_expansion_mode}
    return payload


def download(client, task_id, data, output, overwrite):
    check_terminal(data)
    require(data['task']['status'] == 'succeeded', '任务尚未成功，请使用 --wait')
    destination = Path(output or f'downloads/{task_id}.mp4').expanduser()
    require(overwrite or not destination.exists(), '目标文件已存在，使用 --overwrite 覆盖')
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        temp = None
        response = None
        try:
            content = data['task'].get('content')
            url = content.get('url') if isinstance(content, dict) else None
            if not isinstance(url, str) or not http_url(url):
                raise CliError('成功响应缺少有效 task.content.url', 3)
            with requests.Session() as session:
                with session.get(url, stream=True, timeout=client.timeout) as response:
                    if response.status_code in {403, 404}:
                        if attempt == 0:
                            data = client.query(task_id)
                            check_terminal(data)
                            continue
                    response.raise_for_status()
                    size = 0
                    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.minimax-', delete=False) as out:
                        temp = Path(out.name)
                        for chunk in response.iter_content(256 * 1024):
                            out.write(chunk)
                            size += len(chunk)
                    length = response.headers.get('Content-Length')
                    if size == 0 or (length and not response.headers.get('Content-Encoding') and size != int(length)):
                        raise requests.ConnectionError('下载为空或长度与 Content-Length 不符')
            if overwrite:
                os.replace(temp, destination)
            else:
                # 原子创建，避免预检查后出现同名文件时被覆盖。
                os.link(temp, destination)
                temp.unlink()
            return {'task_id': task_id, 'path': str(destination.resolve()), 'bytes': size}
        except requests.RequestException as exc:
            retryable = response is None or response.status_code == 429 or response.status_code >= 500 or 200 <= response.status_code < 300
            if not retryable or attempt == 3:
                raise CliError(f'下载失败：{redact(exc)}；任务 ID：{task_id}', 3) from exc
            delay = retry_delay(response, attempt)
            log(f'下载暂时失败，{delay:g} 秒后重试')
            time.sleep(delay)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    raise CliError('下载失败', 3)


def positive(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('必须是正数') from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('必须是有限正数')
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ['validate', 'create', 'query', 'download', 'run']:
        cmd = sub.add_parser(name)
        cmd.add_argument('--env-file', default='.env', help='dotenv 路径，默认当前目录 .env')
        cmd.add_argument('--base-url', help='API 根地址，不含 /v2 路径')
        cmd.add_argument('--timeout', type=positive, default=60, help='单次网络操作超时秒数')
        if name in {'validate', 'create', 'run'}:
            cmd.add_argument('--request-json', help='官方 JSON 请求文件；相对素材路径基于当前工作目录')
            cmd.add_argument('--prompt')
            cmd.add_argument('--prompt-file')
            cmd.add_argument('--model')
            cmd.add_argument('--resolution')
            cmd.add_argument('--duration', type=int)
            cmd.add_argument('--ratio')
            for role in ['first-frame', 'last-frame']:
                cmd.add_argument('--' + role)
            for role in ['reference-image', 'reference-video', 'reference-audio']:
                cmd.add_argument('--' + role, action='append')
            cmd.add_argument('--prompt-expansion-mode')
            cmd.add_argument('--callback-url')
            cmd.add_argument('--aigc-watermark', action=argparse.BooleanOptionalAction, default=None)
            cmd.add_argument('--skip-media-check', action='store_true')
        if name in {'query', 'download'}:
            cmd.add_argument('task_id')
            cmd.add_argument('--wait', action='store_true')
        if name in {'query', 'download', 'run'}:
            cmd.add_argument('--interval', type=positive, default=10)
            cmd.add_argument('--max-wait', type=positive, default=1800)
        if name in {'download', 'run'}:
            cmd.add_argument('--output', help='输出路径，默认 downloads/TASK_ID.mp4')
            cmd.add_argument('--overwrite', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        load_dotenv(args.env_file, override=False)
        payload = None
        if args.command in {'validate', 'create', 'run'}:
            payload = validate(build_payload(args), args.skip_media_check, args.timeout)
        if args.command == 'validate':
            emit({'valid': True, 'model': payload['model'], 'request_bytes': len(serialize(payload)),
                  'media_checked': not args.skip_media_check})
            return 0
        client = Client(args.base_url or os.getenv('VIDEO_BASE_URL', 'https://api.minimax.cn'), os.getenv('VIDEO_API_KEY'), args.timeout)
        if args.command in {'create', 'run'}:
            data = client.create(payload)
            if args.command == 'create':
                emit(data)
                return 0
            task_id = data['task_id']
        else:
            task_id = args.task_id
        if args.command == 'run' or args.wait:
            data = client.wait(task_id, args.interval, args.max_wait)
        else:
            data = client.query(task_id)
        if args.command in {'run', 'download'}:
            emit(download(client, task_id, data, args.output, args.overwrite))
        else:
            emit(data)
            check_terminal(data)
        return 0
    except CliError as exc:
        log(str(exc))
        return exc.code
    except (OSError, ValueError) as exc:
        log(f'文件或数据错误：{redact(exc)}')
        return 2
    except KeyboardInterrupt:
        log('已中断；服务端任务仍保留，请使用已显示的任务 ID 继续查询')
        return 130


if __name__ == '__main__':
    sys.exit(main())
