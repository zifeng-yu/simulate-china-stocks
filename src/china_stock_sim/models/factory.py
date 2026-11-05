"""模型组合入口。换兼容服务只改配置；新协议在这里注册适配器。"""

from .base import ModelClient, ModelError
from .openai_compatible import OpenAICompatibleClient
from .settings import ModelSettings


def create_model_client(settings: ModelSettings) -> ModelClient:
    if settings.provider == "openai_compatible":
        return OpenAICompatibleClient(settings)
    raise ModelError("尚未实现所选 provider 的模型适配器")
