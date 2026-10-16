"""无进程环境副作用的配置解析。"""

import os
from dataclasses import dataclass, field, replace

from dotenv import dotenv_values


@dataclass(frozen=True)
class ServiceConfig:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    tool_model: str | None = None

    def with_cli(self, args):
        return replace(
            self,
            **{
                name: value
                for name in ("base_url", "model", "tool_model")
                if (value := getattr(args, name, None)) is not None
            },
        )


DEFAULT_VIDEO = ServiceConfig("https://api.minimax.cn", "MiniMax-H3")
DEFAULT_IMAGE = ServiceConfig(
    "https://api.openai.com/v1", "gpt-6-astra", tool_model="gpt-image-2.5-sunburst"
)


@dataclass(frozen=True)
class AppConfig:
    video: ServiceConfig
    image: ServiceConfig

    @property
    def secrets(self):
        return (self.video.api_key, self.image.api_key)


def load_config(env_file):
    # dotenv_values 不改写 os.environ；进程环境优先，显式空值不回退。
    values = {**dotenv_values(env_file), **os.environ}

    def service(prefix, defaults):
        def value(name, default):
            return values.get(f"{prefix}_{name}", default) or ""

        return ServiceConfig(
            base_url=value("BASE_URL", defaults.base_url),
            model=value("MODEL", defaults.model),
            api_key=value("API_KEY", defaults.api_key),
            tool_model=value("TOOL_MODEL", defaults.tool_model)
            if defaults.tool_model is not None
            else None,
        )

    return AppConfig(service("VIDEO", DEFAULT_VIDEO), service("IMAGE", DEFAULT_IMAGE))
