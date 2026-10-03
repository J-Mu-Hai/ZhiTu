"""阶段 12:Conversation-first Strategic Intake。

这一组钉的是**新的状态机与数据边界**:

1. intake 首先是一次**对话**:助手消息 = 一段判断 + 一个关键问题;
2. intake 期间**不落任何问题实体**(`agent_questions` 一行都不新增),
   也不写 `reasoning_nodes` / `reasoning_node_links` / PlanNode;
3. 待回答问题只存在会话状态里(`pending_intake_*`),刷新/重试可靠;
4. 用户下一条消息就是这轮回答(自由输入与快捷回复等价);
5. 服务端守卫拒绝执行细节与重复已知信息;不合格输出重试后仍不合格则拒绝,不写半成品;
6. 信息足够或到 5 问上限后,一次生成时间架构(route + 3–5 stage),再才写节点。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    IntakeDecision,
    ReasoningMapDraft,
    ReasoningMapNodeDraft,
    ReasoningResult,
)
from backend.db.models import (
    AgentQuestion,
    GoalReasoningSession,
    Message,
    PlanningBrief,
    PlanNode,
    ReasoningNode,
    ReasoningNodeLink,
)
from backend.db.models.enums import BriefStatus, ModelSource, ReasoningSessionPhase


# ---------------------------------------------------------------------------------
# 草稿与假模型
# ---------------------------------------------------------------------------------
def _roadmap_draft(*, kind: str = "relative", stages: int = 4) -> ReasoningMapDraft:
    nodes: list[ReasoningMapNodeDraft] = [
        ReasoningMapNodeDraft(
            handle="r1",
            title="推荐路线:约 10 周从基础到可展示项目",
            node_type="route",
            timeframe="约 10 周",
            timeframe_kind=kind,
            deliverable="一个可展示的数据分析项目",
            pass_criteria="能端到端做完并讲清结论",
        )
    ]
    for index in range(1, stages + 1):
        start_week = (index - 1) * 2 + 1
        end_week = index * 2
        nodes.append(
            ReasoningMapNodeDraft(
                handle=f"r{index + 1}",
                title=f"阶段 {index}",
                node_type="stage",
                parent_handle="r1",
                timeframe=f"第 {start_week}–{end_week} 周" if kind == "relative" else None,
                deliverable=f"成果 {index}",
                pass_criteria=f"通过标准 {index}",
                timeframe_kind=kind,
                start_week=start_week if kind == "relative" else None,
                end_week=end_week if kind == "relative" else None,
                start_date=f"2026-0{index}-01" if kind == "dated" else None,
                end_date=f"2026-0{index}-28" if kind == "dated" else None,
            )
        )
    return ReasoningMapDraft(
        nodes=tuple(nodes),
        focus_handle="r2",
        focus_reason="第一阶段最该先定下来",
        phase="roadmap_draft",
        turn_action="ask_user",
    )


def _ask(
    question: str,
    *,
    scope: str = "deliverable",
    why: str = "它会改变整体路线",
    quick: tuple[str, ...] = (),
) -> IntakeDecision:
    return IntakeDecision(
        action="ask",
        question=question,
        decision_scope=scope,
        why_this_matters=why,
        quick_replies=quick,
    )


READY = IntakeDecision(action="ready_for_architecture")


@dataclass
class IntakeReasoner:
    """intake 模式按 `decisions` 念稿;架构模式按 `drafts` 念稿。**互不消耗。**"""

    decisions: tuple[IntakeDecision | None, ...] = (READY,)
    drafts: tuple[ReasoningMapDraft | None, ...] = ()
    calls: list = field(default_factory=list)
    _decision_index: int = 0
    _draft_index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        if getattr(turn, "purpose", "") == "strategic_intake":
            index = min(self._decision_index, len(self.decisions) - 1)
            decision = self.decisions[index]
            self._decision_index += 1
            reply = "先问一个最关键的问题。" if decision and decision.action == "ask" else "信息够了,给你整体时间架构。"
            return ReasoningResult(reply=reply, source=ModelSource.DIRECT_LLM, intake_decision=decision)
        draft = self.drafts[self._draft_index] if self._draft_index < len(self.drafts) else None
        self._draft_index += 1
        return ReasoningResult(reply="这是推荐路线与阶段。", source=ModelSource.DIRECT_LLM, reasoning_map=draft)


async def _enter(client: httpx.AsyncClient, account, key: str = "intake-1") -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _answer(client: httpx.AsyncClient, account, text: str, key: str) -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/messages",
        json={"content": text, "clientMessageId": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _db_rows(db: AsyncSession, model):
    await db.rollback()
    return list((await db.execute(select(model))).scalars())


async def _questions(client: httpx.AsyncClient, account) -> list[dict]:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/questions", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["questions"]


# ---------------------------------------------------------------------------------
# 1. 首轮是对话提问,不落任何问题实体
# ---------------------------------------------------------------------------------
async def test_first_turn_asks_intake_question_instead_of_roadmap(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(IntakeReasoner(decisions=(_ask("你希望最终获得什么成果?", quick=("一个作品",)),)))

    body = await _enter(app_client, account, "intake-1")
    view = body["reasoning"]
    assert view["nodes"] == [], "intake 期间画布只有根目标"
    assert view["phase"] == ReasoningSessionPhase.INTAKE.value
    assert view["intakeQuestionsAsked"] == 1
    assert view["pendingIntake"] is not None
    assert view["pendingIntake"]["question"] == "你希望最终获得什么成果?"
    assert view["pendingIntake"]["quickReplies"] == ["一个作品"]

    # 问题原文写进了助手消息正文(回答归档后仍然留在对话里)。
    assert body["message"] is not None
    assert "你希望最终获得什么成果?" in body["message"]["content"]
    # **没有 agent_question,也没有画布问题。**
    assert body["question"] is None
    assert await _questions(app_client, account) == []
    assert await _db_rows(db, AgentQuestion) == []
    assert await _db_rows(db, ReasoningNode) == []
    assert await _db_rows(db, ReasoningNodeLink) == []


# ---------------------------------------------------------------------------------
# 2. 已知信息不重复问;没说过的时间维度允许问
# ---------------------------------------------------------------------------------
async def test_intake_skips_known_weekly_time(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    db.add(
        PlanningBrief(
            workspace_id=uuid.UUID(account.workspace_id),
            version=1,
            status=BriefStatus.DRAFT,
            weekly_available_minutes=150,
        )
    )
    await db.commit()
    use_reasoner(
        IntakeReasoner(
            decisions=(
                _ask("你每周能投入多少小时?", scope="duration"),
                _ask("你希望最终获得什么成果?"),
            )
        )
    )

    body = await _enter(app_client, account, "intake-known")
    pending = body["reasoning"]["pendingIntake"]
    assert pending is not None
    assert pending["question"] == "你希望最终获得什么成果?"
    assert "每周" not in pending["question"]


async def test_intake_allows_weekly_time_when_unknown(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(IntakeReasoner(decisions=(_ask("你每周能稳定投入多少时间?", scope="duration"),)))

    body = await _enter(app_client, account, "intake-weekly")
    assert body["reasoning"]["pendingIntake"]["question"] == "你每周能稳定投入多少时间?"


# ---------------------------------------------------------------------------------
# 3. 执行细节被服务端拒绝;拒绝后不写半成品,可继续
# ---------------------------------------------------------------------------------
async def test_intake_blocks_trivial_questions(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(
        IntakeReasoner(
            decisions=(_ask("你想用哪个 IDE?"), READY),
            drafts=(_roadmap_draft(),),
        )
    )

    body = await _enter(app_client, account, "intake-trivial")
    # 细节问题被拒 -> 本轮重试拿到 ready -> 直接生成时间架构。
    assert body["changed"] is True
    assert body["reasoning"]["nodes"], "拒绝细节问题后必须继续生成架构"
    assert await _db_rows(db, AgentQuestion) == []


async def test_rejected_intake_question_is_never_shown(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    # 两轮都只给执行细节 -> 拒绝该输出,写失败终态。
    use_reasoner(IntakeReasoner(decisions=(_ask("你想用哪个 IDE?"),)))

    body = await _enter(app_client, account, "intake-reject")
    assert body["reasoning"]["status"] == "failed"
    assert body["reasoning"]["nodes"] == []
    assert await _db_rows(db, AgentQuestion) == []
    contents = [row.content for row in await _db_rows(db, Message)]
    assert all("IDE" not in content for content in contents), "被拒的问题不得显示在对话里"


def test_intake_guard_rejects_execution_details() -> None:
    from backend.services.question_service import intake_question_conflict

    for text in (
        "你想用哪个 IDE?",
        "你打算买哪门课程?",
        "你每天几点学?",
        "变量名想怎么起?",
        "先把任务清单列出来吗?",
    ):
        assert intake_question_conflict(text) is not None, text

    for text in (
        "你希望最终获得什么成果?",
        "你希望什么时候完成?",
        "你每周能稳定投入多少时间?",
        "你现在的基础如何?",
        "你更看重先跑通还是先打基础?",
    ):
        assert intake_question_conflict(text) is None, text

    assert intake_question_conflict(
        "你每周能投入多少小时?", known_dimensions=frozenset({"weekly_available_minutes"})
    ) is not None


# ---------------------------------------------------------------------------------
# 4. 用户下一条消息就是回答;自由输入与快捷回复等价
# ---------------------------------------------------------------------------------
async def test_answering_free_text_advances_intake_without_question_rows(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(
        IntakeReasoner(
            decisions=(_ask("你希望最终获得什么成果?"), _ask("你希望什么时候完成?", scope="duration")),
        )
    )

    first = await _enter(app_client, account, "intake-answer-1")
    assert first["reasoning"]["pendingIntake"] is not None

    # 用户自由输入 -> 下一轮 intake(只改会话状态,不建问题实体)。
    answered = await _answer(app_client, account, "我想做一个能展示的数据分析作品。", "ans-1")
    assert answered["userMessage"]["content"] == "我想做一个能展示的数据分析作品。"
    assert answered["assistantMessage"]["content"].strip()
    assert await _db_rows(db, AgentQuestion) == []

    view = (await _enter(app_client, account, "intake-answer-state"))["reasoning"]
    # 第二次进入是幂等重放:当前待回答的是第二问。
    assert view["pendingIntake"] is not None
    assert view["pendingIntake"]["question"] == "你希望什么时候完成?"


# ---------------------------------------------------------------------------------
# 5. intake 上限:到顶必须生成架构,不能无限追问
# ---------------------------------------------------------------------------------
async def test_intake_max_questions_then_architecture(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(
        IntakeReasoner(
            decisions=(_ask("问题 1?"),),
            drafts=(_roadmap_draft(),),
        )
    )
    await _enter(app_client, account, "intake-max-1")

    session = await db.scalar(
        select(GoalReasoningSession).where(
            GoalReasoningSession.workspace_id == uuid.UUID(account.workspace_id)
        )
    )
    assert session is not None
    session.intake_questions_asked = 5
    await db.commit()

    body = await _enter(app_client, account, "intake-max-2")
    assert body["reasoning"]["nodes"], "到顶必须生成时间架构,不能无限追问"


# ---------------------------------------------------------------------------------
# 6. 时间架构:相对周 / 有日期;确认之前不写 PlanNode
# ---------------------------------------------------------------------------------
async def test_architecture_uses_relative_weeks_when_no_dates(
    app_client: httpx.AsyncClient, make_account, use_reasoner
) -> None:
    account = await make_account()
    use_reasoner(IntakeReasoner(decisions=(READY,), drafts=(_roadmap_draft(kind="relative"),)))

    body = await _enter(app_client, account, "arch-relative")
    view = body["reasoning"]
    assert view["phase"] == ReasoningSessionPhase.TEMPORAL_ARCHITECTURE_DRAFT.value
    assert view["datesCalibrated"] is False
    stages = [node for node in view["nodes"] if node["nodeType"] == "stage"]
    assert 3 <= len(stages) <= 5
    assert all(stage["timeframeKind"] == "relative" for stage in stages)
    assert all(stage["startWeek"] and stage["endWeek"] for stage in stages)
    assert all(stage["startDate"] is None and stage["endDate"] is None for stage in stages)


async def test_architecture_writes_no_plan_nodes_before_confirmation(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(IntakeReasoner(decisions=(READY,), drafts=(_roadmap_draft(kind="relative"),)))
    await _enter(app_client, account, "arch-no-plan")

    rows = await _db_rows(db, PlanNode)
    assert len(rows) == 1, "确认前不得把战略架构写成计划节点"


# ---------------------------------------------------------------------------------
# 7. 问完才一次性生成时间架构:首轮 ask,回答后 ready + 架构
# ---------------------------------------------------------------------------------
async def test_intake_defers_architecture_until_answers_are_done(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    use_reasoner(
        IntakeReasoner(
            decisions=(_ask("你希望最终获得什么成果?"), READY),
            drafts=(_roadmap_draft(kind="relative"),),
        )
    )

    first = await _enter(app_client, account, "defer-1")
    assert first["reasoning"]["nodes"] == [], "intake 期间画布只有根目标"
    assert first["reasoning"]["pendingIntake"] is not None

    answered = await _answer(app_client, account, "一个能展示的数据分析作品", "defer-ans")
    assert answered["assistantMessage"]["content"].strip()

    view = (await _enter(app_client, account, "defer-state"))["reasoning"]
    assert view["nodes"], "问完之后一次性生成时间架构"
    assert view["phase"] == ReasoningSessionPhase.TEMPORAL_ARCHITECTURE_DRAFT.value
    assert view["pendingIntake"] is None
    assert await _db_rows(db, AgentQuestion) == []
