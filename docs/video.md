# MiniMax 视频 V2

安装和公共配置见[项目入口](../README.md)。以下命令均通过统一入口执行。

## 快速使用

```sh
# 只校验，不创建任务，不需要密钥；会读取/下载输入媒体
mgen video validate --prompt '海边的日落，镜头缓缓推进'

# 一次完成创建、等待与下载
mgen video run --prompt '海边的日落，镜头缓缓推进' --output downloads/sunset.mp4

# 分步执行；将输出中的 task_id 代入后续命令
mgen video create --prompt '雨中城市' --resolution 2K --duration 8 --ratio 16:9
mgen video query TASK_ID
mgen video query TASK_ID --wait --interval 10 --max-wait 1800
mgen video download TASK_ID --wait --output downloads/result.mp4

# 首帧、尾帧、首尾帧：本地路径与公网 URL 可混用
mgen video run --prompt '让画面动起来' --first-frame ./start.png
mgen video run --prompt '镜头最终停留在此画面' --last-frame ./end.png
mgen video run --prompt '从白天过渡到夜晚' --first-frame ./start.png --last-frame ./end.png

# 多模态参考；每种 reference 参数可重复
mgen video run --prompt '人物按参考视频运动，声音参考音频' \
  --reference-image ./person.png --reference-video https://example.com/motion.mp4 \
  --reference-audio ./voice.wav

# H3 Max 的提示词扩写与水印
mgen video run --model MiniMax-H3-Max --prompt '海边日落' \
  --resolution 480P --prompt-expansion-mode quality --aigc-watermark

# 从 UTF-8 文件读取提示词
mgen video run --prompt-file prompt.txt
```

业务参数放在操作名之后；全局配置文件参数放在服务名前，例如 `mgen --env-file config.env video run ...`。使用 `mgen video run --help` 查看帮助。默认输出 768P、5 秒；文生视频默认 16:9，带媒体时默认 adaptive。下载默认写入 `downloads/TASK_ID.mp4`，覆盖已有文件必须传 `--overwrite`。

也可传符合本 CLI 支持范围的 JSON 请求文件：

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
mgen video validate --request-json request.json
mgen video run --request-json request.json --output downloads/cat.mp4
```

JSON 模式与生成快捷参数互斥；这是受限请求结构，支持文档列出的字段及本地素材路径扩展，并非任意 API 请求透传。仅缺失的 `model` 从环境配置补充；`resolution`、`duration`、`content` 必须填写。相对素材路径基于当前工作目录。JSON 支持本地路径作为 CLI 扩展，发送前会转换为 data URI。

## 协议与校验限制

- 创建：`POST /v2/video_generation`；查询：`GET /v2/query/video_generation/{task_id}`。API 使用 `Authorization: Bearer ...`；创建成功返回 `task_id`，成功任务的视频链接为 `task.content.url`。
- H3：768P / 2K，整数 4–15 秒。H3 Max：480P / 768P，整数 5–15 秒。
- 必须恰有一个非空文本项，最多 7000 个字符。首帧和尾帧各最多一张，参考图最多 9 张，参考视频和参考音频各最多 3 段。首尾帧与参考角色不能混用。
- 比例支持 adaptive、21:9、16:9、4:3、1:1、3:4、9:16；文生视频不允许 adaptive。首尾帧场景按图片决定比例，传其他比例时 CLI 会提示并改成 adaptive。
- 图片支持 JPG/JPEG/PNG/WEBP/HEIC/HEIF，最大 30 MB；图片和视频宽高均为 256–5760 px，宽高比 0.4–2.5。
- 视频支持 MP4/MOV，最大 50 MB；H.264/H.265 视频轨，内部音轨为 AAC/MP3，帧率 23.976–60。音频支持 WAV/MP3，最大 15 MB。
- 视频和音频单段均为 2–15 秒，各类型总时长分别不超过 15 秒。JSON 请求体最多 64 MB，计入 Base64 膨胀。本工具保守使用十进制 MB（1,000,000 字节）。
- 本地素材先检查，再编码为 data URI；公网素材会限量下载到临时目录检查，提交时保留原 URL。URL 内容在预检后发生变化仍可能被服务端拒绝。
- 完整预检对 data URI 验证 MIME、Base64、大小及媒体属性，并核对声明 MIME 与实际媒体格式（JPG/JPEG、HEIC/HEIF、音频 MPEG/MP3 按等价别名处理）。`mm_file://FILE_ID` 无法读取媒体属性，默认拒绝；显式加 `--skip-media-check` 可交给服务端检查。该选项对其他素材同样跳过属性探测和 MIME/实际内容比对，仍检查结构、数量、data URI 的格式及 Base64、可获得的大小；远程素材不下载。
- `extra.prompt_expansion_mode` 仅支持 H3 Max，取值 disabled / balanced / quality。`--callback-url` 仅透传已有回调服务地址，接收端需在 3 秒内原样响应 challenge；CLI 不启动回调服务器。

官方页面对单独尾帧有措辞差异：场景清单支持单独尾帧，子字段说明写需配对；本工具按场景清单支持单独尾帧，最终以服务端响应为准。

## 运行与恢复

默认单次网络读写超时 60 秒，可用 `--timeout` 调整；这是网络操作超时，不是整个下载的总时限。默认每 10 秒轮询，最多等待 1800 秒，`--interval` 和 `--max-wait` 可调整。超时或 Ctrl+C 后服务端任务继续运行，使用创建后 stderr 显示的任务 ID 恢复查询和下载。

创建 POST 不自动重试，网络错误后不能保证服务端未创建任务，需在平台确认后再重新提交。GET 查询和下载遇到网络异常、429 或 5xx 最多重试 3 次，指数退避并遵守 Retry-After。下载链接遇到 403/404 时可重新查询并刷新一次，与临时错误的三次重试分别计数；即使先发生网络重试，仍可刷新链接。

下载使用不带 API 鉴权的独立 HTTP 会话，不向素材或 CDN 发送 API Key。数据先写同目录临时文件，检查非空及已声明长度后原子落盘；临时文件在重试等待前或最终失败时清理，传输失败不替换已有成品。成功替换已有文件仍须明确传入 `--overwrite`。这里检查的是传输完整性，未对输出视频执行解码验证。

任务仅可查询最近 7 天，超出窗口可能返回 invalid task_id。产物 URL 有时效，请及时下载。官方限速页目前为 H3 列出 300 RPM、30 个并行运行任务；本工具每次处理单个任务，不代替账户级限流管理，H3 Max 以平台实际额度为准。

stdout 为 JSON 结果，stderr 为状态和错误；按当前配置密钥对输出字符串脱敏并隐藏 data URI，validate 不输出 Base64 请求。退出码：0 成功，2 参数/文件/校验错误，3 网络/API/协议错误，4 任务失败或取消，5 等待超时，130 用户中断。单次 query 对 queued/running 返回 0，失败或取消返回任务 JSON 和退出码 4。

## 测试与范围

测试使用模拟 HTTP 与探测数据，不调用收费生成接口。范围限定为视频 V2 的生成、校验、查询和下载，不包括批量任务、取消/删除及回调服务器。

参考：[创建接口](https://platform.minimax.cn/docs/api-reference/video-generation-v2-create)、[查询接口](https://platform.minimax.cn/docs/api-reference/video-generation-v2-query)、[速率限制](https://platform.minimax.cn/docs/guides/rate-limits)。
