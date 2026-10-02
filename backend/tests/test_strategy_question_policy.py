"""产品纠偏:战略层提问策略、信息顺序、公开研究的诚实边界。

这些测试钉的是**服务端强制**的规则,而不是提示词措辞:
- 战略阶段(还没有已确认战略)里,排期类问题会被丢弃;
- 公开研究默认未配置时如实返回,不伪造来源;
- 提示词里确实写了战略/Orientation/信息顺序规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy import select

from backend.agent.runtime.base import ReasoningResult, ToolRequest
from backend.db.models import ToolCallRecord
from backend.db.models.enums import ModelSource, ToolCallStatus
from backend.services.question_service import strategy_phase_question_conflict
from backend.tests.conftest import FakeReasoner


def _question(text: str, *, mode: str = "single_select", options=None) -> dict:
    if options is None:
        options = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    if mode == "free_text":
        options = []
    return {
        "question": text,
        "whyNow": "它会改变结论",
        "responseMode": mode,
        "options": options,
        "allowCustomInput": True,
    }


@dataclass
class SequenceReasoner:
    results: tuple[ReasoningResult, ...]
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        result = self.results[self._index] if self._index < len(self.results) else ReasoningResult(reply="没轮次了。", source=ModelSource.DIRECT_LLM)
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


async def _questions(client: httpx.AsyncClient, account) -> list[dict]:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/questions", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["questions"]


async def _plan(client: httpx.AsyncClient, account) -> dict:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/plan", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _confirm_strategy(client: httpx.AsyncClient, account) -> None:
    root = next(
        node["id"] for node in (await _plan(client, account))["nodes"] if node["parentId"] is None
    )
    created = await client.post(
        f"/api/workspaces/{account.workspace_id}/nodes",
        json={"parentId": root, "title": "战略:先保研", "nodeType": "goal", "planningLevel": "strategy"},
        headers=account.headers,
    )
    assert created.status_code == 201, created.text


# ---------------------------------------------------------------------------------
# A. 战略层提问策略
# ---------------------------------------------------------------------------------
def test_strategy_phase_conflict_detects_scheduling_questions() -> None:
    for text in (
        "你每周能投入多少小时?",
        "你每天大概有几个小时?",
        "这件事的截止日期是什么时候?",
        "你现在大概是什么水平?",
        "你希望日程怎么安排?",
    ):
        assert strategy_phase_question_conflict(text) is not None, text
    for text in (
        "你更愿意把主要精力放在保研、创业还是内容积累?",
        "这两个目标冲突时,你更看重哪一个?",
        "哪些方向可以先暂缓?",
    ):
        assert strategy_phase_question_conflict(text) is None, text


async def test_strategy_phase_drops_scheduling_questions_but_keeps_the_strategic_one(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    strategic = _question("你更愿意把主要精力放在保研、创业还是内容积累?")
    scheduling = _question("你每周能投入多少小时?", mode="free_text")
    use_reasoner(FakeReasoner(questions=(strategic, scheduling)))

    body = await _send(app_client, account, "我同时想保研、做创业项目和做自媒体")
    assert body["proposal"] is None  # 第一轮不问排期、也不生成任务树

    questions = await _questions(app_client, account)
    assert [q["question"] for q in questions] == [strategic["question"]]
    # 画布计划没有被这一轮改动。
    assert len((await _plan(app_client, account))["nodes"]) == 1


async def test_scheduling_questions_are_allowed_after_strategy_is_confirmed(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    await _confirm_strategy(app_client, account)
    use_reasoner(FakeReasoner(questions=(_question("你每周能投入多少小时?", mode="free_text"),)))
    await _send(app_client, account, "帮我排这周")
    questions = await _questions(app_client, account)
    assert [q["question"] for q in questions] == ["你每周能投入多少小时?"]


def test_prompt_encodes_strategy_and_information_policy() -> None:
    from backend.agent.prompts.planning import PROMPT_VERSION, SYSTEM_PROMPT

    assert PROMPT_VERSION == "planning-v15"
    assert "战略阶段先问取舍" in SYSTEM_PROMPT
    assert "有界 Orientation" in SYSTEM_PROMPT
    assert "系统已知" in SYSTEM_PROMPT
    assert "时间不用问" in SYSTEM_PROMPT
    assert "research_public" in SYSTEM_PROMPT
    # 6B-3:公开研究的顺序、禁令与诚实边界必须写死在提示词里。
    assert "公开可研究" in SYSTEM_PROMPT
    assert "不得为下面这些发公网搜索" in SYSTEM_PROMPT
    assert "用户资源承诺" in SYSTEM_PROMPT
    assert "搜索只能提供背景与可选项" in SYSTEM_PROMPT
    assert "公开研究的诚实边界" in SYSTEM_PROMPT
    assert "研究结果是依据,不是写入" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------------
# C. 公开研究:未配置时如实说明,不伪造来源
# ---------------------------------------------------------------------------------
async def test_research_public_is_honestly_unconfigured(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db
) -> None:
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="我查一下公开信息。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=(
                    ToolRequest(id="t1", name="research_public", arguments={"query": "某大学 保研 政策"}),
                ),
            ),
            ReasoningResult(
                reply="研究工具没配置,我不编造来源。",
                source=ModelSource.DIRECT_LLM,
                stop_reason="insufficient_evidence",
            ),
        )
    )
    use_reasoner(reasoner)
    await _send(app_client, account, "帮我查一下保研政策")

    exchange = reasoner.calls[1].tool_exchanges[0]
    assert exchange.tool_name == "research_public"
    assert exchange.summary.get("configured") is False
    assert "未配置" in str(exchange.summary.get("note", ""))
    # 没有伪造 sources / 来源列表。
    assert "sources" not in exchange.summary
    assert "results" not in exchange.summary

    await db.rollback()
    record = await db.scalar(select(ToolCallRecord))
    assert record is not None
    assert record.tool_name == "research_public"
    assert record.status is ToolCallStatus.OK


async def test_research_query_with_private_id_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    reasoner = SequenceReasoner(
        results=(
            ReasoningResult(
                reply="我查一下。",
                source=ModelSource.DIRECT_LLM,
                tool_requests=(
                    ToolRequest(
                        id="t1",
                        name="research_public",
                        arguments={"query": "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"},
                    ),
                ),
            ),
            ReasoningResult(reply="这个查询被拒了。", source=ModelSource.DIRECT_LLM, stop_reason="failed"),
        )
    )
    use_reasoner(reasoner)
    await _send(app_client, account, "查一下")
    exchange = reasoner.calls[1].tool_exchanges[0]
    assert exchange.status == "rejected"
