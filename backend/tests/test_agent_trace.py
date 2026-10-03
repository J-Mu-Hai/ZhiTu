"""运行轨迹的持久状态、读取 API、脱敏与权限(阶段 9)。

这一组用例盯的是三件事:

1. `waiting_model` **在模型返回之前**就已经落库可读(轨迹不是前端假进度);
2. 终态错误码与脱敏摘要来自服务端真实判据,而不是模型自由文本;
3. 读取接口按 workspace 授权、不泄漏 prompt / 用户原文 / 密钥,开关关闭时不可见。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import ReasoningResult, ToolRequest
from backend.core.config import settings
from backend.db.models import Message, ReasoningState, ToolCallRecord
from backend.db.models.enums import (
    AgentTraceStep,
    DegradedReason,
    ModelSource,
    ToolCallStatus,
)
from backend.db.session import SessionLocal
from backend.services import agent_loop_service


@dataclass
class InspectingReasoner:
    """在模型"思考期间"回读轨迹的假模型。

    它用**另一个会话**读库(不是请求那个会话):这正是诊断入口在真实场景里做的事 ——
    模型还在等响应时,浏览器轮询读到的必须是已经提交的 `waiting_model`。
    """

    observed: dict = field(default_factory=dict)
    reply: str = "看完了,这次不用改计划。"

    async def reason(self, turn):
        async with SessionLocal() as other:
            state = await other.scalar(
                select(ReasoningState).order_by(ReasoningState.created_at.desc()).limit(1)
            )
            if state is not None:
                self.observed["step"] = state.current_step
                self.observed["started_at"] = state.started_at
                self.observed["trigger"] = state.trigger
        return ReasoningResult(
            reply=self.reply,
            source=ModelSource.DIRECT_LLM,
            stop_reason="ready_to_propose",
        )


@dataclass
class ToolThenDoneReasoner:
    results: tuple[ReasoningResult, ...]
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        result = self.results[min(self._index, len(self.results) - 1)]
        self._index += 1
        return result


async def _send(client: httpx.AsyncClient, account, content: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": content, "clientMessageId": content},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _trace(client: httpx.AsyncClient, account, limit: int | None = None) -> dict:
    url = f"/api/workspaces/{account.workspace_id}/agent/trace"
    if limit is not None:
        url += f"?limit={limit}"
    response = await client.get(url, headers=account.headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------------
# 1. 发请求前就写 waiting_model
# ---------------------------------------------------------------------------------
async def test_waiting_model_is_persisted_before_the_model_returns(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    reasoner = use_reasoner(InspectingReasoner())
    await _send(app_client, account, "先帮我看看")

    assert reasoner.observed.get("step") is AgentTraceStep.WAITING_MODEL
    assert reasoner.observed.get("started_at") is not None

    body = await _trace(app_client, account)
    assert body["enabled"] is True
    assert len(body["turns"]) == 1
    turn = body["turns"][0]
    assert turn["currentStep"] == "completed"
    assert turn["status"] == "completed"
    assert turn["trigger"] == "user_message"
    assert turn["durationMs"] is not None
    assert turn["safeSummary"]


# ---------------------------------------------------------------------------------
# 2. 超时 / 模型错误:不同的可读终态
# ---------------------------------------------------------------------------------
async def test_model_timeout_has_its_own_readable_terminal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(
        _degraded_reasoner(DegradedReason.MODEL_TIMEOUT)
    )
    await _send(app_client, account, "这句话要超时了")
    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["status"] == "timed_out"
    assert turn["terminalCode"] == "MODEL_TIMEOUT"
    assert "超时" in turn["safeSummary"]
    assert turn["retryable"] is True


async def test_model_output_invalid_has_a_distinct_terminal(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(_degraded_reasoner(DegradedReason.MODEL_OUTPUT_INVALID))
    await _send(app_client, account, "这段输出不合法")
    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["status"] == "failed"
    assert turn["terminalCode"] == "MODEL_OUTPUT_INVALID"
    assert "无法解析" in turn["safeSummary"]


# ---------------------------------------------------------------------------------
# 3. 工具摘要脱敏
# ---------------------------------------------------------------------------------
async def test_tool_rejection_is_summarized_without_raw_arguments(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(
        ToolThenDoneReasoner(
            results=(
                ReasoningResult(
                    reply="我试一下。",
                    source=ModelSource.DIRECT_LLM,
                    tool_requests=(
                        ToolRequest(
                            id="t1",
                            name="drop_table",
                            arguments={"table": "plan_nodes", "sql": "DROP TABLE plan_nodes"},
                            reason="危险操作",
                        ),
                    ),
                ),
                ReasoningResult(
                    reply="没有这个工具。",
                    source=ModelSource.DIRECT_LLM,
                    stop_reason="insufficient_evidence",
                ),
            )
        )
    )
    await _send(app_client, account, "查一个不存在的东西")

    await db.rollback()
    records = list((await db.execute(select(ToolCallRecord))).scalars())
    assert records and records[0].status is ToolCallStatus.REJECTED

    turn = (await _trace(app_client, account))["turns"][0]
    assert turn["tools"], "工具轨迹必须在接口上可见"
    tool = turn["tools"][0]
    assert tool["toolName"] == "drop_table"
    assert tool["status"] == "rejected"
    assert "被拒绝" in tool["summary"]
    # 原始参数里的表名 / SQL 不能出现在接口响应里。
    serialized = str(turn)
    assert "DROP TABLE" not in serialized
    assert "plan_nodes" not in serialized


# ---------------------------------------------------------------------------------
# 4. 脱敏:接口响应里没有用户原文 / prompt / 密钥
# ---------------------------------------------------------------------------------
async def test_trace_does_not_leak_user_text_or_secrets(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    secret = "我的私密原文 SECRET_COOKIE_abc123"
    use_reasoner(InspectingReasoner(reply="收到。"))
    await _send(app_client, account, secret)
    raw = str(await _trace(app_client, account))
    assert secret not in raw
    assert "SECRET_COOKIE" not in raw
    assert "prompt" not in raw.lower()


# ---------------------------------------------------------------------------------
# 5. 权限:跨空间读取被拒;固定上限
# ---------------------------------------------------------------------------------
async def test_cross_workspace_trace_read_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account_a = await make_account(email="a@example.com")
    account_b = await make_account(email="b@example.com", workspace_title="另一个空间")
    use_reasoner(InspectingReasoner())
    await _send(app_client, account_a, "写点东西")

    response = await app_client.get(
        f"/api/workspaces/{account_a.workspace_id}/agent/trace",
        headers=account_b.headers,
    )
    assert response.status_code == 404


async def test_trace_limit_is_capped(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(InspectingReasoner())
    await _send(app_client, account, "第一次")
    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/agent/trace?limit=9999",
        headers=account.headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------------
# 6. 开关:关闭时入口不可见(404),本地开发默认可用
# ---------------------------------------------------------------------------------
async def test_trace_api_is_hidden_when_disabled(
    app_client: httpx.AsyncClient, make_account, use_reasoner, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await make_account()
    use_reasoner(InspectingReasoner())
    await _send(app_client, account, "本地写点东西")

    # 模拟生产环境且没有显式打开开关。
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "agent_trace_ui_enabled", False)
    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/agent/trace",
        headers=account.headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TRACE_DISABLED"


async def test_trace_api_is_available_when_explicitly_enabled(
    app_client: httpx.AsyncClient, make_account, use_reasoner, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await make_account()
    use_reasoner(InspectingReasoner())
    await _send(app_client, account, "生产里显式打开")

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "agent_trace_ui_enabled", True)
    response = await app_client.get(
        f"/api/workspaces/{account.workspace_id}/agent/trace",
        headers=account.headers,
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True


# ---------------------------------------------------------------------------------
# 7. 重试:失败回合可重试,且不产生重复用户消息
# ---------------------------------------------------------------------------------
async def test_failed_turn_can_be_retried_without_duplicate_message(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(_degraded_reasoner(DegradedReason.MODEL_UNAVAILABLE))
    await _send(app_client, account, "失败一次")
    failed = (await _trace(app_client, account))["turns"][0]
    assert failed["retryable"] is True

    # 用同一个 clientMessageId 重发:这是既有幂等重试路径。
    use_reasoner(InspectingReasoner(reply="这次成功了。"))
    response = await app_client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": "失败一次", "clientMessageId": "失败一次"},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text

    await db.rollback()
    count = len(list((await db.execute(select(Message))).scalars()))
    assert count == 2, "重试不应产生第二条用户消息"


def _degraded_reasoner(reason: DegradedReason) -> InspectingReasoner:
    """返回一个**降级**的假模型,同时保留可读的降级原因。"""

    @dataclass
    class _Degraded:
        observed: dict = field(default_factory=dict)

        async def reason(self, turn):
            return ReasoningResult(
                reply="模型这次没有成功。",
                source=ModelSource.UNAVAILABLE,
                degraded=True,
                degraded_reason=reason,
                retryable=True,
            )

    return _Degraded()  # type: ignore[return-value]


#: 引用一下,避免 ruff 把未使用的 import 报掉(它在类型注解里用到)。
_ = agent_loop_service.MAX_MODEL_CALLS
