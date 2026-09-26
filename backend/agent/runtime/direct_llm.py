"""直连 DeepSeek 的实现 —— 这是接缝的**降级路径**,不是主路径。

主路径是 `openjiuwen_runtime`(见那里的说明)。这个文件留着,是因为"模型这条路"
必须有一个不依赖 openJiuwen 也能通的实现:SDK 装不上、版本不兼容、或运维明确
指定 `AGENT_REASONER=direct` 时,产品仍然能生成计划,而不是退化成一句"模型不可用"。

响应里的 `source` 如实写成 `direct_llm`,界面据此显示"直连模型" —— 走的是哪条路
必须能从响应里看出来,因为"我们用了 openJiuwen"是产品要对用户说的一句话,
不能只是代码里的一个配置项。

## 这个文件只负责传输

提示词渲染、brief 清洗、actions 筛选、结果组装全在 `response.py`。两条调用模型的路
共用那一份规则,所以这里只剩下"发一个 POST、把响应交给解析器、把失败翻译成降级结果"。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx

from backend.agent.prompts.planning import PROMPT_VERSION, SYSTEM_PROMPT
from backend.agent.runtime.base import ReasoningResult, TurnContext
from backend.agent.runtime.response import (
    PayloadInvalid,
    payload_from_chat_completion,
    payload_to_result,
    render_turn,
)
from backend.core.config import Settings
from backend.db.models.enums import DegradedReason, ModelSource

logger = logging.getLogger(__name__)



def classify_http_error(exc: httpx.HTTPStatusError) -> tuple[DegradedReason, bool, str]:
    """把 HTTP 状态码翻译成"用户该看到什么 + 重试有没有用"。

    401 单独拎出来:key 错或过期时重试一万次都是 401,而如果把它报成
    "服务暂时不可用",运维会去查网络,真正的问题(key 配错了)反而没人看。
    """
    status = exc.response.status_code
    if status in (401, 403):
        return DegradedReason.MODEL_AUTH_FAILED, False, "模型密钥无效或已过期,请联系管理员。"
    if status == 429:
        return DegradedReason.MODEL_RATE_LIMITED, True, "模型服务限流了,过一会儿再试。"
    if status >= 500:
        return DegradedReason.MODEL_UNAVAILABLE, True, "模型服务暂时不可用,可以再试一次。"
    return DegradedReason.MODEL_UNAVAILABLE, True, "模型调用出错了,可以再试一次。"


class DirectLLMReasoner:
    """调用 DeepSeek 的 chat/completions。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def reason(self, turn: TurnContext) -> ReasoningResult:
        request_id = uuid.uuid4().hex
        started = time.monotonic()

        if not self._settings.llm_api_key:
            # 没配 key 不算"失败",算"这条路没开"。retryable=False 是因为
            # 重试一万次也还是没有 key,让用户点重试按钮是折磨。
            return self._degraded(
                DegradedReason.NO_API_KEY,
                retryable=False,
                request_id=request_id,
                reply="还没有配置模型密钥,现在没法生成计划。",
            )

        payload = self._build_payload(turn)
        try:
            raw = await self._post(payload)
        except httpx.TimeoutException:
            return self._degraded(
                DegradedReason.MODEL_TIMEOUT,
                retryable=True,
                request_id=request_id,
                reply="这次响应太慢了,没能拿到结果。可以再试一次。",
                started=started,
            )
        except httpx.HTTPStatusError as exc:
            reason, retryable, reply = classify_http_error(exc)
            logger.warning("模型调用被拒绝: HTTP %s", exc.response.status_code)
            return self._degraded(
                reason, retryable=retryable, request_id=request_id, reply=reply, started=started
            )
        except httpx.RequestError:
            # 连不上(DNS、断网、代理)。**不把异常往上抛** ——
            # 上游会把它变成 500 页面,而用户此刻需要的是"再试一次"。
            logger.warning("模型调用连不上", exc_info=True)
            return self._degraded(
                DegradedReason.MODEL_UNAVAILABLE,
                retryable=True,
                request_id=request_id,
                reply="连不上模型服务,没能生成回复。可以再试一次。",
                started=started,
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        return self._parse(raw, request_id=request_id, latency_ms=latency_ms, payload=payload)

    # ---------------------------------------------------------------------------
    def _build_payload(self, turn: TurnContext) -> dict[str, Any]:
        return {
            "model": self._settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                # 渲染出来的那段文本里全是 JSON 花括号 —— 它是**字面量**,不是模板。
                {"role": "user", "content": render_turn(turn)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
            "max_tokens": self._settings.agent_max_tokens,
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self._settings.llm_timeout_seconds) as client:
            response = await client.post(
                f"{self._settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self._settings.llm_api_key}"},
                json=payload,
            )
            response.raise_for_status()
        return response.json()

    def _parse(
        self,
        raw: dict[str, Any],
        *,
        request_id: str,
        latency_ms: int,
        payload: dict[str, Any],
    ) -> ReasoningResult:
        try:
            body, usage = payload_from_chat_completion(raw)
            return payload_to_result(
                body,
                source=ModelSource.DIRECT_LLM,
                request_id=request_id,
                prompt_version=PROMPT_VERSION,
                model_name=payload.get("model"),
                latency_ms=latency_ms,
                usage=usage,
            )
        except (KeyError, IndexError, TypeError, ValueError, PayloadInvalid) as exc:
            # 模型没按格式回。**这是可预期失败,不是崩溃** —— 它有时候会包一层
            # ```json 代码块,有时候会漏掉 reply。结果里如实写 MODEL_OUTPUT_INVALID,
            # 界面显示"模型这次没按格式回答,可以重试",而不是假装成功。
            logger.warning("模型输出无法解析: %s", exc)
            return self._degraded(
                DegradedReason.MODEL_OUTPUT_INVALID,
                retryable=True,
                request_id=request_id,
                reply="模型这次的回答没能解析出结果,可以再试一次。",
                latency_ms=latency_ms,
            )

    def _degraded(
        self,
        reason: DegradedReason,
        *,
        retryable: bool,
        request_id: str,
        reply: str,
        started: float | None = None,
        latency_ms: int | None = None,
    ) -> ReasoningResult:
        if latency_ms is None:
            latency_ms = int((time.monotonic() - started) * 1000) if started else None
        return ReasoningResult(
            reply=reply,
            source=ModelSource.UNAVAILABLE,
            degraded=True,
            degraded_reason=reason,
            retryable=retryable,
            request_id=request_id,
            prompt_version=PROMPT_VERSION,
            model_name=self._settings.llm_model,
            latency_ms=latency_ms,
        )


__all__ = ["DirectLLMReasoner", "classify_http_error"]
