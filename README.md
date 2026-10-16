# mgen

统一管理 MiniMax 视频 V2 和 OpenAI Responses 图片生成的 Python CLI。支持输入预检、图片同步或后台创建、查询等待、结果恢复与保存。

## 安装

需要 Python 3.10+。从源码安装：

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

按 `.env.example` 填写对应服务的密钥。仅图片操作无需安装 ffmpeg；视频的音视频输入完整预检需要 `ffprobe`，macOS 用 `brew install ffmpeg`，Debian/Ubuntu 用 `sudo apt install ffmpeg`。

## 统一命令

```sh
# 请求预检：无需密钥，远程素材会下载检查
mgen video validate --prompt '海边日落'
mgen image validate --prompt '一幅水彩风景'

# 创建、等待、保存
mgen video run --prompt '海边日落' --output downloads/sunset.mp4
mgen image run --prompt '一幅水彩风景' --output-dir downloads/landscape

# 第三方图片服务不支持后台模式时，建议显式开启同步模式
mgen image run --sync --timeout 300 --prompt '一幅水彩风景'

# 指定配置文件；全局参数放在服务名前
mgen --env-file ./config.env image run --prompt '一幅水彩风景'

# 查看帮助和版本
mgen --help
mgen image run --help
mgen --version
```

也可使用 `python -m mgen`。两个服务均提供 `validate`、`create`、`query`、`download` 和 `run`，通过任务/响应 ID 恢复后续操作。

详细用法与协议限制：[视频 CLI](docs/video.md)、[图片 CLI](docs/image.md)。

## 配置与输出

| 配置 | 用途 | 默认值 |
| --- | --- | --- |
| `VIDEO_BASE_URL` | MiniMax API 根地址 | `https://api.minimax.cn` |
| `VIDEO_MODEL` | 视频模型 | `MiniMax-H3` |
| `VIDEO_API_KEY` | MiniMax 密钥 | 空，调用时必填 |
| `IMAGE_BASE_URL` | Responses API 基础地址，包含 `/v1` | `https://api.openai.com/v1` |
| `IMAGE_MODEL` | 顶层对话模型 | `gpt-6-astra` |
| `IMAGE_TOOL_MODEL` | 图片生成工具模型 | `gpt-image-2.5-sunburst` |
| `IMAGE_API_KEY` | OpenAI 密钥 | 空，调用时必填 |

优先级：显式命令参数 > 进程环境变量 > `.env` > 默认值。显式空值按输入校验，不回退默认值。默认从运行目录读取 `.env`，与安装位置无关；读取配置不修改进程环境，同一进程内多次调用互不污染。密钥只通过环境配置提供；`validate` 不要求密钥。只有涉及相应服务的命令才要求其密钥。

stdout 输出 JSON，stderr 输出进度和错误；使用本次配置中的密钥对输出字符串脱敏，并隐藏 data URI。图片查询摘要不输出 Base64 结果。脱敏不修改发送请求、图片字节或落盘清单，JSON 的字段结构和数值类型保持不变。参数、文件和校验错误退出码为 2；网络/API/协议错误为 3；任务失败、取消或图片结果不可用为 4；等待超时为 5；Ctrl+C 为 130。单次查询仍在运行的任务返回 0。

创建 POST 不自动重试，避免重复计费；失败后请先确认平台状态再重新创建。Ctrl+C 或等待超时不会取消服务端任务。API 鉴权会话与素材/产物下载会话独立。

## 开发、测试与构建

```sh
ruff check src tests
ruff format --check src tests
python -m unittest discover -s tests -v
python -m build
```

构建产物位于 `dist/`，wheel 安装示例：

```sh
python -m pip install dist/mgen-0.1.0-py3-none-any.whl
mgen --version
```

源码使用 `src` 布局：

```text
src/mgen/
  cli.py              # 顶层参数与命令分发
  config.py           # 集中默认值、不可变配置与环境优先级
  arguments.py        # 共用参数声明、提示词读取
  common.py           # 错误、输出脱敏与已编码 JSON 请求
  http.py             # API 鉴权、响应释放与查询重试
  files.py            # 无 API 鉴权素材读取、原子文件操作
  video/              # MiniMax：命令、客户端、校验
  image/              # OpenAI：命令、客户端、校验
```

服务客户端负责各自的错误信封、不可重试条件和任务状态；两种校验器均返回规范化副本和已校验的请求字节，发送时复用，不修改调用方输入。

运行时依赖和开发依赖统一维护于 `pyproject.toml`。测试使用模拟网络、媒体探测数据和本地 HTTP 服务，不消耗真实生成额度。wheel 只包含运行代码和包元数据，`.env`、下载内容、测试及构建缓存不进入 wheel；配置示例和文档随源码分发。
