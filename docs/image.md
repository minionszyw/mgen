# OpenAI Responses 图片 CLI

安装和公共配置见[项目入口](../README.md)。本工具使用 Responses API 的 `image_generation` 工具，提供生成、参考图编辑、蒙版编辑、多轮修改及后台任务恢复；支持同步和后台执行，均采用非流式请求。

## 模型和配置

`IMAGE_MODEL` 是调用工具的顶层对话模型，不应填 GPT Image 模型。`IMAGE_TOOL_MODEL` 才是图片模型；支持 `gpt-image-2.5-sunburst`、`gpt-image-2.5-flare`，以及各自的 `-2026-09-08` 固定版本。Sunburst 适合精细编辑，Flare 适合日常快速生成。顶层模型须支持图片工具，默认执行方式还要求支持后台模式；权限和兼容性由服务端判断。

不自动回退其他模型、提供商或 Images API；第三方服务不支持后台模式时，建议显式加 `--sync`。

## 常用命令

```sh
# 不需要 API Key；包含 URL 时会下载预检
mgen image validate --prompt '一幅水彩风景'

# 一次完成后台创建、等待和结果保存
mgen image run --prompt '一幅水彩风景' --output-dir downloads/landscape

# 同步生成和编辑
mgen image run --sync --timeout 300 --prompt '桌面上的红色玩具汽车' --action generate
mgen image run --sync --timeout 300 --prompt '将车身改为蓝色' --action edit --image ./car.png

# 自定义输出参数
mgen image run --prompt '一幅横向电影海报' --size 1536x1024 --quality high
mgen image run --prompt '透明背景的红色玩具车' --background transparent --output-format webp
mgen image run --prompt '手绘卡片' --output-format jpeg --output-compression 80

# 参考图生成/编辑；--image 可重复，支持本地路径、URL、data URI
mgen image run --prompt '将这只猫改为水彩风格' --image ./cat.png --action edit
mgen image run --prompt '将两个角色放在同一场景' --image ./cat.png --image ./dog.png
mgen image run --prompt '根据参考图生成海报' --image https://example.com/reference.png

# 蒙版编辑：透明区域指示需要修改的部分，第一张图片为编辑目标
mgen image run --prompt '将背景换成花园' --image ./scene.png --mask ./mask.png --action edit

# 使用已有 Files API 文件 ID，不自动上传文件
mgen image run --prompt '修改图中背景' --image-file-id file-INPUT --mask-file-id file-MASK --action edit

# 多轮修改：将上次结果中的 response_id 代入
mgen image run --prompt '将背景改为夜晚' --previous-response-id resp_PREVIOUS --action edit

# 分步创建与恢复
mgen image create --prompt '一幅水彩风景'
mgen image query resp_RESPONSE
mgen image query resp_RESPONSE --wait --interval 5 --max-wait 1800
mgen image download resp_RESPONSE --wait --output-dir downloads/restored

# 提示词文件、工具模型和配置文件
mgen image run --prompt-file prompt.txt --tool-model gpt-image-2.5-flare
mgen --env-file image.env image run --prompt '一幅水彩风景'
```

参数放在操作名之后；只有 `--env-file` 和 `--version` 为根命令参数。`--image-file-id` 输入排列在所有 `--image` 输入之后；混用时蒙版目标仍是最终数组中的第一张图片。需要精确控制顺序可使用 JSON 请求。

`action` 支持 auto（默认）、generate、edit；edit 需要输入图片或 `previous_response_id`。蒙版编辑必须显式提供输入图片，不能只提供前序响应。

## JSON 请求

```json
{
  "model": "gpt-6-astra",
  "input": [{
    "role": "user",
    "content": [
      {"type": "input_text", "text": "将背景替换为花园"},
      {"type": "input_image", "image_url": "./scene.png"}
    ]
  }],
  "tools": [{
    "type": "image_generation",
    "model": "gpt-image-2.5-sunburst",
    "action": "edit",
    "input_image_mask": {"image_url": "./mask.png"},
    "quality": "high"
  }]
}
```

```sh
mgen image validate --request-json request.json
mgen image run --request-json request.json --output-dir downloads/edited
```

JSON 模式与生成快捷参数互斥，允许同时使用 `--sync`、超时、基础 URL、等待和输出选项。缺失的顶层 model 与工具 model 由配置补充。`input` 支持字符串或一条 user 消息，消息包含恰好一个非空 input_text 和可选 input_image；图片引用恰好使用 image_url 或 file_id。本地路径基于运行目录，发送前编码为 data URI。

仅接收本 CLI 声明的工作流字段：model、input、tools、tool_choice、previous_response_id、background、store。工具仅支持 type、model、action、size、quality、output_format、output_compression、background、input_image_mask。不接收其他工具、流式字段或 Images API 的 `n` 参数；若模型返回多个图片调用，会保存全部结果。

## 预检和协议限制

- 使用 POST `/responses` 和 GET `/responses/{id}`，强制 `tool_choice={"type":"image_generation"}`。
- 输入支持 PNG、JPEG、WEBP、非动画 GIF；所有动画图片均拒绝。单次图片输入最多 1500 张，按 UTF-8 JSON 字节检查请求体最多 512 MB，计入 Base64 膨胀。本工具保守使用十进制 MB；同时限制可读取素材的合计原始大小不超过 512 MB。
- 本地文件和 data URI 检查实际格式、内容和大小。远程图片限量下载到临时目录预检，提交时仍保留 URL；预检后的远程内容变化可能导致服务端拒绝。
- file_id 仅检查引用结构，属性交给服务端；validate 输出的 `unchecked` 明确标记无法探测的字段。未使用密钥访问 Files API，因此本地预检不会验证文件权限或存在性。
- 蒙版和目标图片必须格式、尺寸一致且各自小于 50 MB；蒙版需有 alpha 通道，作用于第一张输入图。file_id 无法在本地比较格式和尺寸，此部分明确标记为未检查。蒙版提供指导，不保证像素级严格遵循。
- 默认 size、quality、background 为 auto，output_format 为 png。质量支持 low / medium / high / xhigh / max / auto；背景支持 opaque / transparent / auto；格式支持 png / jpeg / webp。
- 自定义尺寸 WIDTHxHEIGHT：宽高均为 16 的倍数，单边不超过 3840，长短边比不超过 3:1，总像素 655360–8294400。高于 2560×1440 的像素规模会提示实验性支持。常用尺寸包括 1024x1024、1536x1024、1024x1536。
- output_compression 为 0–100 整数，仅支持 JPEG/WEBP；透明背景必须使用 PNG/WEBP。

官方限制、模型权限和账户限额可能变化，预检不能保证请求一定被接受。组织可能需要完成验证才能使用图片模型。复杂图片请求可能耗时较长，文字布局、角色一致性和精确构图仍有局限。

## 执行模式、恢复和结果保存

`create` 输出精简响应 JSON，`run` 校验并保存图片；stderr 显示响应 ID。执行方式如下：

| 模式 | 顶层请求字段 | 结果获取 |
| --- | --- | --- |
| 默认后台 | `background=true`、`store=true` | `run` 每 5 秒查询，最多等待 1800 秒；可用 `--interval`、`--max-wait` 调整 |
| `--sync` | 缺省时省略 `background`、`store` | 直接处理 POST 返回的完整响应，不轮询 |

`validate`、`create`、`run` 均支持 `--sync`。JSON 请求中，后台模式要求 `background`、`store` 为 true；同步模式可显式传 `background=false` 和布尔值 `store`，原样保留，`background=true` 与 `--sync` 冲突。工具内的 `background` 控制图片背景，与顶层执行方式无关。

网络操作超时默认 60 秒，由 `--timeout` 控制。同步生成耗时较长时可用 `--timeout 300`；`--interval`、`--max-wait` 不控制同步 POST。`create --sync` 不保存图片，需落盘时使用 `run --sync`。

识别 queued、in_progress、completed、failed、cancelled、incomplete。同步请求返回 queued/in_progress 时输出摘要并以退出码 4 结束，不自动查询。失败、取消、未完成或完成但没有图片均返回非零退出码；异常响应字段类型按协议错误处理（退出码 3）。终端摘要不含图片 Base64，遵守[公共输出与脱敏规则](../README.md#配置与输出)；落盘清单保留原始元数据。

后台模式的 `store=true` 用于恢复和多轮引用。同步模式的存储默认值由服务端决定，query/download 和 previous_response_id 是否可用取决于提供商。保留期限也依赖账户和项目策略，ID 不保证永久可查，请及时保存图片。

成功后读取所有已完成的 `image_generation_call.result`，严格解码 Base64 并验证实际图片内容；默认保存为：

```text
downloads/images/resp_RESPONSE/
  ig_CALL.png
  result.json
```

扩展名依据实际格式确定，JPEG 使用 `.jpeg`。`result.json` 包含响应 ID、状态、工具调用元数据、可用的修订提示词、usage、图片绝对路径和字节数，不保存 Base64；stdout 返回 response_id、images、manifest。

保存前验证所有结果和路径冲突，默认不覆盖已有图片或清单；显式 `--overwrite` 才允许替换。每个文件先写同目录临时文件，再原子发布；多文件保存不是整体事务，失败报告已保存路径，重试时可换目录或明确允许覆盖。

后台模式下，Ctrl+C 或等待超时不会取消响应，之后可通过 `query` 或 `download` 恢复；尚未收到响应 ID 的创建网络错误无法自动恢复。创建 POST 不自动重试；查询 GET 对临时网络、429、5xx 错误最多重试三次并遵守 Retry-After。额度不足、审核拦截和需修改输入的错误不自动重试。错误包含可用的 API code/message 及请求追踪 ID。

Responses 请求还包含顶层模型用量，不能仅按图片工具价格估算全部费用。usage 原样记录可用信息，不推算固定图片费用。

参考：[官方图片生成指南](https://developers.openai.com/api/docs/guides/image-generation)、[图片输入要求](https://developers.openai.com/api/docs/guides/images-vision)、[后台执行指南](https://developers.openai.com/api/docs/guides/background)。文档核对日期：2026-10-15。
