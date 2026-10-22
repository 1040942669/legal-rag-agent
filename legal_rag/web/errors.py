"""Turn pipeline exceptions into messages the page can show as they are."""

from __future__ import annotations

from typing import Any

from legal_rag.provider_errors import ProviderCallError

OPERATION_LABELS = {
    "completion": "生成",
    "embedding": "向量",
    "rerank": "重排",
}
ERROR_CODE_MESSAGES = {
    "timeout": "请求超时",
    "rate_limited": "被限流",
    "network": "网络连接失败",
    "unavailable": "服务暂时不可用",
    "auth": "鉴权失败，API key 无效或无权限",
    "invalid_request": "请求被拒绝",
    "configuration": "配置错误",
    "invalid_response": "返回内容无法解析",
    "unknown": "未知错误",
}


def describe_error(error: BaseException) -> dict[str, Any]:
    """Return a safe, displayable description of one failure.

    ``kind`` is one of ``missing_key``, ``network``, ``provider``,
    ``live_calls_disabled`` or ``internal``.  Provider errors keep only the
    classification fields of :class:`ProviderCallError`, never provider text.
    """

    if isinstance(error, ProviderCallError):
        operation = OPERATION_LABELS.get(error.operation, error.operation)
        if error.cause_type == "MissingApiKey":
            kind = "missing_key"
            reason = "缺少 API key，请在 .env 中设置 SILICONFLOW_API_KEY 后重启服务"
        else:
            kind = (
                "network" if error.error_code in {"network", "timeout"} else "provider"
            )
            reason = ERROR_CODE_MESSAGES.get(error.error_code, error.error_code)
            if error.status_code is not None:
                reason += f"（HTTP {error.status_code}）"
            elif error.error_code == "configuration":
                reason += f"（{error.cause_type}）"
        return {
            "kind": kind,
            "message": f"{error.provider} {operation}调用失败：{reason}",
            **error.to_safe_dict(),
        }
    if isinstance(error, RuntimeError) and "ALLOW_LIVE_MODEL_CALLS" in str(error):
        return {
            "kind": "live_calls_disabled",
            "message": "模型调用被离线开关拦截：ALLOW_LIVE_MODEL_CALLS 未设为 true",
            "cause_type": type(error).__name__,
        }
    return {
        "kind": "internal",
        "message": f"{type(error).__name__}: {error}",
        "cause_type": type(error).__name__,
    }
