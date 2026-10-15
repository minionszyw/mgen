# MiniMax 视频 V2 CLI

Python CLI，实现参数与媒体预检、创建任务、查询/等待和视频下载。支持 MiniMax-H3、MiniMax-H3-Max 的文生视频、首帧、尾帧、首尾帧和多模态参考生成。

## 安装与配置

需要 Python 3.10+。音视频完整预检还需要 `ffprobe`，macOS 用 `brew install ffmpeg`，Debian/Ubuntu 用 `sudo apt install ffmpeg`。

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```dotenv
VIDEO_BASE_URL=https://api.minimax.cn
VIDEO_MODEL=MiniMax-H3
VIDEO_API_KEY=你的密钥
```

`VIDEO_BASE_URL` 是根地址，不包含 `/v2`。配置优先级：显式 CLI 参数 > 进程环境变量 > `.env` > 默认值。密钥仅通过环境变量或 `.env` 提供。默认读取当前工作目录的 `.env`，每个子命令支持 `--env-file PATH`。

## 快速使用

```sh
# 只校验，不创建任务，不需要密钥；会读取/下载输入媒体
python video_cli.py validate --prompt '海边的日落，镜头缓缓推进'

# 一次完成创建、等待与下载
python video_cli.py run --prompt '海边的日落，镜头缓缓推进' --output downloads/sunset.mp4

# 分步执行；将输出中的 task_id 代入后续命令
python video_cli.py create --prompt '雨中城市' --resolution 2K --duration 8 --ratio 16:9
python video_cli.py query TASK_ID
python video_cli.py query TASK_ID --wait --interval 10 --max-wait 1800
python video_cli.py download TASK_ID --wait --output downloads/result.mp4

# 首帧、尾帧、首尾帧：本地路径与公网 URL 可混用
python video_cli.py run --prompt '让画面动起来' --first-frame ./start.png
python video_cli.py run --prompt '镜头最终停留在此画面' --last-frame ./end.png
python video_cli.py run --prompt '从白天过渡到夜晚' --first-frame ./start.png --last-frame ./end.png

# 多模态参考；每种 reference 参数可重复
python video_cli.py run --prompt '人物按参考视频运动，声音参考音频' \
  --reference-image ./person.png --reference-video https://example.com/motion.mp4 \
  --reference-audio ./voice.wav

# H3 Max 的提示词扩写与水印
python video_cli.py run --model MiniMax-H3-Max --prompt '海边日落' \
  --resolution 480P --prompt-expansion-mode quality --aigc-watermark

# 从 UTF-8 文件读取提示词
python video_cli.py run --prompt-file prompt.txt
```

所有参数放在子命令之后。使用 `python video_cli.py run --help` 查看帮助。默认输出 768P、5 秒；文生视频默认 16:9，带媒体时默认 adaptive。下载默认写入 `downloads/TASK_ID.mp4`，覆盖已有文件必须传 `--overwrite`。

也可传官方 JSON 请求文件：

```json
{
  "model": "MiniMax-H3",
  "content": [
    {"type": "text", "text": "一只猫在窗边看雨"},
    {"type": "image_url", "image_url": {"url": "./cat.png"}, "role": "first_frame"}
  ],
  "resolution": "768P",
  "duration": 5,
  "ratio": "adaptive"
}
```

```sh
python video_cli.py validate --request-json request.json
python video_cli.py run --request-json request.json --output downloads/cat.mp4
```

JSON 模式与生成快捷参数互斥。仅缺失的 `model` 从环境配置补充；`resolution`、`duration`、`content` 必须填写。相对素材路径基于当前工作目录。JSON 支持本地路径作为 CLI 扩展，发送前会转换为 data URI。

## 协议与校验限制

- 创建：`POST /v2/video_generation`；查询：`GET /v2/query/video_generation/{task_id}`。API 使用 `Authorization: Bearer ...`；创建成功返回 `task_id`，成功任务的视频链接为 `task.content.url`。
- H3：768P / 2K，整数 4–15 秒。H3 Max：480P / 768P，整数 5–15 秒。
- 必须恰有一个非空文本项，最多 7000 个字符。首帧和尾帧各最多一张，参考图最多 9 张，参考视频和参考音频各最多 3 段。首尾帧与参考角色不能混用。
- 比例支持 adaptive、21:9、16:9、4:3、1:1、3:4、9:16；文生视频不允许 adaptive。首尾帧场景按图片决定比例，传其他比例时 CLI 会提示并改成 adaptive。
- 图片支持 JPG/JPEG/PNG/WEBP/HEIC/HEIF，最大 30 MB；图片和视频宽高均为 256–5760 px，宽高比 0.4–2.5。
- 视频支持 MP4/MOV，最大 50 MB；H.264/H.265 视频轨，内部音轨为 AAC/MP3，帧率 23.976–60。音频支持 WAV/MP3，最大 15 MB。
- 视频和音频单段均为 2–15 秒，各类型总时长分别不超过 15 秒。JSON 请求体最多 64 MB，计入 Base64 膨胀。本工具保守使用十进制 MB（1,000,000 字节）。
- 本地素材先检查，再编码为 data URI；公网素材会限量下载到临时目录检查，提交时保留原 URL。URL 内容在预检后发生变化仍可能被服务端拒绝。
- data URI 验证 MIME、Base64、大小及媒体属性。`mm_file://FILE_ID` 无法读取媒体属性，默认拒绝；显式加 `--skip-media-check` 可交给服务端检查。该选项对其他素材同样跳过属性探测，仍检查结构、数量及可获得的大小；远程素材不下载。
- `extra.prompt_expansion_mode` 仅支持 H3 Max，取值 disabled / balanced / quality。`--callback-url` 仅透传已有回调服务地址，接收端需在 3 秒内原样响应 challenge；CLI 不启动回调服务器。

官方页面对单独尾帧有措辞差异：场景清单支持单独尾帧，子字段说明写需配对；本工具按场景清单支持单独尾帧，最终以服务端响应为准。

## 运行与恢复

默认单次网络读写超时 60 秒，可用 `--timeout` 调整；这是网络操作超时，不是整个下载的总时限。默认每 10 秒轮询，最多等待 1800 秒，`--interval` 和 `--max-wait` 可调整。超时或 Ctrl+C 后服务端任务继续运行，使用创建后 stderr 显示的任务 ID 恢复查询和下载。

创建 POST 不自动重试，网络错误后不能保证服务端未创建任务，需在平台确认后再重新提交。GET 查询和下载遇到网络异常、429 或 5xx 最多重试 3 次，指数退避并遵守 Retry-After。下载链接 403/404 时重新查询并刷新一次。

下载使用不带 API 鉴权的独立 HTTP 会话，不向素材或 CDN 发送 API Key。数据先写同目录临时文件，检查非空及已声明长度后原子落盘；失败清理临时文件，不替换已有成品。这里检查的是传输完整性，未对输出视频执行解码验证。

任务仅可查询最近 7 天，超出窗口可能返回 invalid task_id。产物 URL 有时效，请及时下载。官方限速页目前为 H3 列出 300 RPM、30 个并行运行任务；本工具每次处理单个任务，不代替账户级限流管理，H3 Max 以平台实际额度为准。

stdout 为 JSON 结果，stderr 为状态和错误；validate 不输出可能很大的 Base64 请求。退出码：0 成功，2 参数/文件/校验错误，3 网络/API/协议错误，4 任务失败或取消，5 等待超时，130 用户中断。单次 query 对 queued/running 返回 0，失败或取消返回任务 JSON 和退出码 4。

## 测试

```sh
python -m unittest discover -s tests -v
```

测试使用模拟 HTTP 与探测数据，不调用收费生成接口。范围限定为视频 V2 的生成、校验、查询和下载，不包括旧版 API、批量任务、取消/删除及回调服务器。

参考：[创建接口](https://platform.minimax.cn/docs/api-reference/video-generation-v2-create)、[查询接口](https://platform.minimax.cn/docs/api-reference/video-generation-v2-query)、[速率限制](https://platform.minimax.cn/docs/guides/rate-limits)。
