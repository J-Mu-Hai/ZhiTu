"""阶段 10:提问前的**战略判断**。

这一组钉的是「先判断,再提问」在**服务端**的硬闸,不是提示词措辞:

1. 地图轮的战略问题必须带 `analysis_summary` / `recommendation` / `decision_impact`,
   选择题还必须标一个推荐项;缺了就整条拒绝、不写半成品;
2. 战略阶段只问会改变路线的问题 —— 工具 / 资料 / 每天安排 / 代码细节 / 措辞偏好
   一律拦下;
3. 战略阶段每轮最多 1 个活动问题;没有必要追问时可以零问题;
4. 没有可信判断时不伪造 —— 模型要么把“不足以推荐、缺少什么”写进判断里,要么该问题不落库;
5. 已确认战略后(执行层)不强制判断字段,proposal → 用户确认 → 写入不变。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agent.runtime.base import (
    IntakeDecision,
    QuestionDraft,
    QuestionOptionDraft,
    ReasoningMapDraft,
    ReasoningMapNodeDraft,
    ReasoningResult,
)
from backend.core.security import CurrentUser
from backend.db.models import AgentQuestion, Workspace
from backend.db.models.enums import ModelSource
from backend.services.context import WorkspaceContext
from backend.tests.conftest import (
    seed_known_conditions,
)


def _roadmap_draft(stages: int = 4) -> ReasoningMapDraft:
    nodes: list[ReasoningMapNodeDraft] = [
        ReasoningMapNodeDraft(
            handle="r1",
            title="推荐路线:约 10 周从基础到可展示项目",
            node_type="route",
            timeframe="约 10 周",
            deliverable="一个可展示的项目",
            pass_criteria="能端到端做完并讲清结论",
        )
    ]
    for index in range(1, stages + 1):
        nodes.append(
            ReasoningMapNodeDraft(
                handle=f"r{index + 1}",
                title=f"阶段 {index}",
                node_type="stage",
                parent_handle="r1",
                timeframe=f"约 {index} 周",
                deliverable=f"成果 {index}",
                pass_criteria=f"标准 {index}",
            )
        )
    return ReasoningMapDraft(
        nodes=tuple(nodes),
        focus_handle="r2",
        focus_reason="第一阶段最该先定下来",
        phase="roadmap_draft",
        turn_action="ask_user",
    )


def _judged(
    question: str,
    *,
    mode: str = "single_select",
    options: tuple[QuestionOptionDraft, ...] = (),
    analysis: str = "已知每周 150 分钟;先补语法会拉长见效时间。",
    recommendation: str = "先跑通一个最小闭环。",
    impact: str = "不同选择会改变阶段 2 的成果物。",
    confidence: str | None = None,
) -> QuestionDraft:
    return QuestionDraft(
        question=question,
        why_now="它决定阶段顺序",
        response_mode=mode,
        options=options,
        allow_custom_input=True,
        analysis_summary=analysis,
        recommendation=recommendation,
        decision_impact=impact,
        confidence_note=confidence,
    )


@dataclass
class MapReasoner:
    drafts: tuple[ReasoningMapDraft | None, ...] = ()
    questions: tuple[QuestionDraft, ...] = ()
    calls: list = field(default_factory=list)
    _index: int = 0

    async def reason(self, turn):
        self.calls.append(turn)
        if getattr(turn, "purpose", "") == "strategic_intake":
            # 阶段 12:intake 直接“信息够了”,后面用 `questions` 验画布问题判断。
            return ReasoningResult(
                reply="信息已经够了,我直接给你整体时间架构。",
                source=ModelSource.DIRECT_LLM,
                intake_decision=IntakeDecision(action="ready_for_architecture"),
            )
        draft = self.drafts[self._index] if self._index < len(self.drafts) else None
        self._index += 1
        return ReasoningResult(
            reply="这是推荐路线与阶段。",
            source=ModelSource.DIRECT_LLM,
            reasoning_map=draft,
            questions=self.questions,
        )


async def _enter(client: httpx.AsyncClient, account, key: str = "judgment-1") -> dict:
    response = await client.post(
        f"/api/workspaces/{account.workspace_id}/agent/turn",
        json={"trigger": "space_entered", "idempotencyKey": key},
        headers=account.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _questions(db: AsyncSession, account) -> list[AgentQuestion]:
    rows = await db.execute(
        select(AgentQuestion).where(AgentQuestion.workspace_id == uuid.UUID(account.workspace_id))
    )
    return list(rows.scalars())


async def _questions_api(client: httpx.AsyncClient, account) -> list[dict]:
    response = await client.get(
        f"/api/workspaces/{account.workspace_id}/questions", headers=account.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["questions"]


# ---------------------------------------------------------------------------------
# 1. 判断字段真的落库、真的读得回来,推荐项有明确标记
# ---------------------------------------------------------------------------------
async def test_strategic_question_carries_judgment_fields(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    question = _judged(
        "先求能跑通的最小闭环,还是先补统计基础?",
        options=(
            QuestionOptionDraft(id="minimal", label="先跑通最小闭环", recommended=True),
            QuestionOptionDraft(id="stats", label="先补统计基础"),
        ),
        confidence="“尽快见效”是从目标推断的,若不对请纠正。",
    )
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(),), questions=(question,)))

    body = await _enter(app_client, account)
    # 阶段 12:intake 直接“信息够了”,先给路线与阶段,再落一个**画布**战略问题。
    assert body["changed"] is True, "先给路线与阶段"
    assert body["reasoning"]["nodes"]
    assert body["question"] is not None
    assert body["question"]["presentation"] == "canvas_question"

    items = await _questions_api(app_client, account)
    assert len(items) == 1
    item = items[0]
    assert item["analysisSummary"].startswith("已知每周")
    assert item["recommendation"]
    assert item["decisionImpact"]
    assert item["confidenceNote"]
    # 推荐项必须明确标出来(前端据此显示“推荐”)。
    assert [option["recommended"] for option in item["options"]] == [True, False]
    # **不是隐藏思维链**:没有任何 CoT / prompt 字段。
    serialized = str(item)
    assert "chain_of_thought" not in serialized
    assert "prompt" not in serialized.lower()


# ---------------------------------------------------------------------------------
# 2. 缺判断字段 / 缺推荐项 -> 整条拒绝,不写半成品
# ---------------------------------------------------------------------------------
async def test_question_without_judgment_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    # 只有形状,没有战略判断 —— 服务端必须拒绝。
    bare = QuestionDraft(
        question="先求能跑通的最小闭环,还是先补统计基础?",
        why_now="它决定阶段顺序",
        response_mode="single_select",
        options=(QuestionOptionDraft(id="minimal", label="最小闭环", recommended=True),),
    )
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(),), questions=(bare,)))

    body = await _enter(app_client, account)
    assert body["changed"] is True
    assert await _questions(db, account) == [], "缺判断的问题不许落库"


async def test_choice_question_without_recommended_option_is_rejected(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    # 判断字段齐全,但两个选项都没标推荐 —— 前端无法明确标“推荐”,整条拒绝。
    no_recommend = _judged(
        "先求能跑通的最小闭环,还是先补统计基础?",
        options=(
            QuestionOptionDraft(id="minimal", label="最小闭环"),
            QuestionOptionDraft(id="stats", label="补统计基础"),
        ),
    )
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(),), questions=(no_recommend,)))

    await _enter(app_client, account)
    assert await _questions(db, account) == []


# ---------------------------------------------------------------------------------
# 3. 战略阶段拦下“琐碎问题”
# ---------------------------------------------------------------------------------
def test_trivial_question_detector_blocks_execution_details() -> None:
    from backend.services.question_service import strategy_phase_question_conflict

    for text in (
        "你想用哪个 IDE?",
        "看哪本书比较好?",
        "每天学几个小时?",
        "变量名怎么起?",
        "你偏好哪种措辞?",
        "你每周能投入多少小时?",
    ):
        assert strategy_phase_question_conflict(text) is not None, text
    # 真正的战略取舍不被误杀。
    assert strategy_phase_question_conflict("先跑通最小闭环,还是先补统计基础?") is None


async def test_trivial_questions_are_dropped_in_strategy_phase(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    trivial = (
        _judged("你想用哪个 IDE?", mode="free_text", options=()),
        _judged("看哪本书比较好?", mode="free_text", options=()),
        _judged("变量名怎么起?", mode="free_text", options=()),
    )
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(),), questions=trivial))

    await _enter(app_client, account)
    assert await _questions(db, account) == []


# ---------------------------------------------------------------------------------
# 4. 战略阶段每轮最多 1 个问题;允许 0 个
# ---------------------------------------------------------------------------------
async def test_at_most_one_question_in_strategy_phase(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    first = _judged(
        "先跑通最小闭环还是先补统计基础?",
        options=(QuestionOptionDraft(id="a", label="最小闭环", recommended=True),),
    )
    second = _judged(
        "先做自动化还是先做报告?",
        options=(QuestionOptionDraft(id="b", label="自动化", recommended=True),),
    )
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(),), questions=(first, second)))

    await _enter(app_client, account)
    assert len(await _questions(db, account)) == 1


async def test_zero_questions_is_allowed(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(),), questions=()))

    body = await _enter(app_client, account)
    assert body["changed"] is True
    assert await _questions(db, account) == []


# ---------------------------------------------------------------------------------
# 5. “无法可靠推荐”时:不伪造。模型必须把缺什么写进判断里(字段齐全才落库)
# ---------------------------------------------------------------------------------
async def test_insufficient_judgment_is_explicit_and_not_fabricated(
    app_client: httpx.AsyncClient, make_account, use_reasoner, db: AsyncSession
) -> None:
    account = await make_account()
    await seed_known_conditions(account)
    # 模型诚实地说“还不足以推荐,缺 X”,而不是编一个推荐。
    honest = _judged(
        "你更看重尽快见效,还是更看重基础扎实?",
        mode="free_text",
        options=(),
        analysis="当前还不足以给出推荐:还不知道你更看重速度还是深度。",
        recommendation="先确认这个取舍,再给唯一推荐。",
        impact="不同取舍会改变路线是“先做项目”还是“先补基础”。",
    )
    use_reasoner(MapReasoner(drafts=(_roadmap_draft(),), questions=(honest,)))

    await _enter(app_client, account)
    items = await _questions_api(app_client, account)
    assert len(items) == 1
    assert "不足以给出推荐" in items[0]["analysisSummary"]
    assert "更看重" in items[0]["analysisSummary"]


# ---------------------------------------------------------------------------------
# 6. 执行层(已确认战略后)不强制判断字段
# ---------------------------------------------------------------------------------
async def test_execution_phase_does_not_require_judgment(db: AsyncSession, make_account) -> None:
    from backend.services import question_service

    account = await make_account()
    await seed_known_conditions(account)
    workspace = await db.scalar(
        select(Workspace).where(Workspace.id == uuid.UUID(account.workspace_id))
    )
    assert workspace is not None
    ctx = WorkspaceContext(
        workspace=workspace,
        user=CurrentUser(
            user_id=uuid.UUID(account.id),
            session_id=uuid.uuid4(),
            timezone="Asia/Shanghai",
            token_version=1,
        ),
    )
    bare = QuestionDraft(
        question="你希望每天几点开始学习?",
        why_now="执行细化需要",
        response_mode="free_text",
    )
    created = await question_service.create_from_drafts(
        db,
        ctx,
        (bare,),
        source_message_id=None,
        source_node_id=None,
        strategy_phase=False,
        require_judgment=False,
    )
    assert len(created) == 1
    assert created[0].analysis_summary == ""
