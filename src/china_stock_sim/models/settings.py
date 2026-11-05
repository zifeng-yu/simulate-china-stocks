"""非敏感配置与本机凭据分开；不会执行 .env 中的内容。"""

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .audit import ModelAudit
from .base import ModelError


@dataclass(frozen=True, repr=False)
class ModelSettings:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    timeout_seconds: int
    max_output_tokens: int
    temperature: float
    options: dict


def local_values(root: Path) -> dict[str, str]:
    path = root / ".env"
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ModelError("本机 .env 必须是项目内的普通文件")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ModelError("本机 .env 权限过宽，请执行 chmod 600 .env")
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip().isidentifier():
            raise ModelError(f"本机 .env 第 {number} 行格式错误，仅支持 KEY=VALUE")
        name, value = name.strip(), value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ModelError(f"本机 .env 第 {number} 行引号不匹配")
            value = value[1:-1]
        if name.startswith("CHINA_STOCK_SIM_LLM_"):
            if name in values:
                raise ModelError(f"本机 .env 第 {number} 行重复配置")
            values[name] = value
    return values


def load_model_settings(root: Path, ref: str = "daily_planner") -> ModelSettings:
    try:
        with (root / "configs/models.toml").open("rb") as stream:
            config = tomllib.load(stream)
        if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
            raise ModelError("模型配置版本不受支持")
        value = config["models"][ref]
        local = local_values(root)
        resolve = lambda key, default="": os.environ.get(key, local.get(key, default))  # noqa: E731
        base_url = resolve(value["base_url_env"]).strip().rstrip("/")
        api_key = resolve(value["api_key_env"]).strip()
        model = resolve(value["model_env"], value["model"]).strip()
        if not base_url or not api_key or not model:
            raise ModelError("模型连接未配置完整：需要服务地址、API 密钥和模型名")
        if any(ord(char) < 32 or ord(char) == 127 for char in base_url):
            raise ModelError("模型服务基础地址不能包含控制字符")
        parsed = urlsplit(base_url)
        # Accessing port validates malformed/out-of-range values before HTTP use.
        _ = parsed.port
        local_http = parsed.scheme == "http" and parsed.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if (
            not parsed.hostname
            or (parsed.scheme != "https" and not local_http)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ModelError("模型服务必须为 HTTPS 或本机 HTTP 基础地址，不能携带凭据或查询参数")
        timeout, max_tokens = value["timeout_seconds"], value["max_output_tokens"]
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ModelError("模型超时必须为 1—300 秒的整数")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise ModelError("模型输出上限必须为 1—8192 的整数")
        options = value.get("options", {})
        if set(options) - {"enable_thinking"}:
            raise ModelError("当前适配器不支持所配置的额外模型参数")
        if "enable_thinking" in options and type(options["enable_thinking"]) is not bool:
            raise ModelError("enable_thinking 必须为布尔值")
        temperature = value.get("temperature", 0.0)
        if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
            raise ModelError("temperature 必须为 0—2 的数值")
        return ModelSettings(
            value["provider"],
            model,
            base_url,
            api_key,
            timeout,
            max_tokens,
            float(temperature),
            options,
        )
    except ModelError:
        raise
    except (OSError, KeyError, ValueError, TypeError):
        # Parser errors may contain portions of configuration. Do not echo them.
        raise ModelError("无法读取模型配置，请检查 configs/models.toml 与本机 .env") from None


def model_status(root: Path, ref: str = "daily_planner") -> dict:
    try:
        settings = load_model_settings(root, ref)
        if settings.provider != "openai_compatible":
            raise ModelError("当前模型适配器不支持此 provider")
        return {
            "configured": True,
            "provider": settings.provider,
            "model": ModelAudit(settings).text(settings.model),
            "credential_present": True,
            "network_tested": False,
        }
    except ModelError as exc:
        return {"configured": False, "network_tested": False, "reason": str(exc)}
