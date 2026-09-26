"""规划简报的读写。

## 这个模块是"不把猜测存成事实"的执行者

模型每一轮会从对话里读出若干条件,并给每条标一个 `source`:
`user_stated`(用户自己说的)或 `model_assumed`(模型推断的)。

**服务端不检查这个标签对不对,但严格执行它的后果:**

| 字段 | user_stated | model_assumed |
| --- | --- | --- |
| 写进 `planning_briefs` 的列 | 是 | **否** |
| 写进 `assumptions` 审计表 | 是 | 是 |

所以 `weekly_available_minutes` 这一列在用户亲口说出一个数字之前**恒为 NULL**。
它驱动排期,一个"模型猜的 5 小时"如果进了这一列,用户看到的每一份周计划都会建立在一个
他从未同意过的前提上,而且界面不会显示任何异常 —— 这正是最难发现的一类错误。

代价是诚实的:标签由模型给出,一个存心说谎的模型可以给一个编出来的数字打上
`user_stated`。这条防线不是密码学意义上的,它挡的是**模型的疏漏**(顺手填一个合理
默认值,这在语言模型身上是高频行为),挡不住蓄意的注入。真正需要用户点头的那一步
是阶段 4 的确认事务 —— 简报会被摊开给用户看。这里如实记下这个边界。

## 为什么是就地更新而不是每次新建一行

`planning_briefs` 有 (workspace_id, version) 唯一约束,看上去像版本链。但简报在确认
之前一直处于草稿状态,每轮对话都新开一行只会产生一长串没人看的草稿。所以:
**一行草稿就地更新,`version` 自增**,而"改了什么"完整记在 `assumptions` 的审计里
(每条带时间戳和来源),审计才是真正需要保留历史的地方。

已确认的简报(阶段 4)不适用这条 —— 那时会有新行,旧行转 `superseded`。
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import case, select

from backend.agent.runtime.base import BriefClaim, KnownConditions
from backend.db.base import utcnow
from backend.db.models import PlanningBrief
from backend.db.models.enums import BriefStatus

#: 会写进列的字段。**这是闭集,不是白名单的子集关系** ——
#: 模型自创的字段在 agent 层就被丢掉了,这里再做一次类型分发。
_COLUMN_FIELDS = frozenset(
    {"goal", "deadline", "weekly_available_minutes", "current_level", "success_criteria", "constraints"}
)

#: 审计表保留多少条。够回溯"每周 10 小时什么时候变成 4 小时"即可,
#: 不设上限的话一个话多的用户能把这一格撑成几兆的 JSON。
_MAX_AUDIT = 200


#: 简报状态 -> 优先级。**必须显式写出来,不能靠 `status.desc()` 的字母序。**
#:
#: 按字母序降序排是 `superseded` > `draft` > `confirmed` —— 正好把最该被忽略的那一份
#: 排在了最前面,而"确认过的优先"这句话在代码里就变成了空话。今天看不出来(还没有
#: 任何代码写 `confirmed` 或 `superseded`),等阶段 4 开始写它们的时候,这个排序会让
#: 用户看到的是**被取代的旧简报**。数值越大越优先。
_STATUS_RANK = {BriefStatus.CONFIRMED: 2, BriefStatus.DRAFT: 1, BriefStatus.SUPERSEDED: 0}


async def load_brief(db, workspace_id: uuid.UUID) -> PlanningBrief | None:
    """取这个空间当前那份简报。确认过的优先,否则取最新草稿。**被取代的不算。**"""
    rank = case(
        *((PlanningBrief.status == status, weight) for status, weight in _STATUS_RANK.items()),
        else_=0,
    )
    result = await db.execute(
        select(PlanningBrief)
        .where(PlanningBrief.workspace_id == workspace_id)
        .order_by(rank.desc(), PlanningBrief.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def to_known_conditions(brief: PlanningBrief | None) -> KnownConditions:
    """简报 -> 送进模型/排期算法的条件对象。

    **NULL 就是"还不知道",不是零。** 这个转换里不做任何补默认值的动作 ——
    补一次,后面所有"用户还没说过"的判断就全失效了。
    """
    if brief is None:
        return KnownConditions()
    constraints = brief.constraints if isinstance(brief.constraints, dict) else {}
    raw = constraints.get("items") if isinstance(constraints, dict) else None
    return KnownConditions(
        goal=brief.goal,
        deadline=brief.deadline.isoformat() if isinstance(brief.deadline, date) else None,
        weekly_available_minutes=brief.weekly_available_minutes,
        current_level=brief.current_level,
        success_criteria=brief.success_criteria,
        constraints=tuple(str(x) for x in raw) if isinstance(raw, list) else (),
    )


async def apply_claims(
    db,
    workspace_id: uuid.UUID,
    claims: tuple[BriefClaim, ...] | list[BriefClaim],
    *,
    source_message_id: uuid.UUID | None = None,
) -> tuple[PlanningBrief | None, tuple[str, ...]]:
    """把这一轮读出的条件落到简报上。返回 (简报, 本轮真正改变的字段名)。

    返回改了哪些字段,是为了让接口能如实告诉用户"我记下了:每周 4 小时"。
    这个反馈很重要:用户改了一次数值却看不到任何确认,下次就不会相信系统记住了。
    """
    if not claims:
        return await load_brief(db, workspace_id), ()

    brief = await load_brief(db, workspace_id)
    if brief is None:
        brief = PlanningBrief(workspace_id=workspace_id, version=1, status=BriefStatus.DRAFT)
        brief.assumptions = {"audit": []}
        db.add(brief)
        await db.flush()

    audit = brief.assumptions if isinstance(brief.assumptions, dict) else {}
    entries = audit.get("audit")
    entries = list(entries) if isinstance(entries, list) else []
    constraints = brief.constraints if isinstance(brief.constraints, dict) else {}
    constraint_items = list(constraints.get("items") or [])
    used_constraint_items = False

    changed: list[str] = []
    now = utcnow()

    for claim in claims:
        if claim.field not in _COLUMN_FIELDS:
            continue

        if claim.is_user_stated:
            if claim.field == "constraints":
                incoming = [str(x) for x in claim.value]  # 已由 agent 层清洗成 list[str]
                merged = list(dict.fromkeys([*constraint_items, *incoming]))
                if merged != constraint_items:
                    constraint_items = merged
                    used_constraint_items = True
                    changed.append(claim.field)
            else:
                value = _coerce(claim.field, claim.value)
                if value is None:
                    continue
                if getattr(brief, claim.field) != value:
                    setattr(brief, claim.field, value)
                    changed.append(claim.field)
        # model_assumed:**除了审计,什么都不写。** 上面表格里那个"否"。
        entries.append(
            {
                "field": claim.field,
                "value": _jsonable(claim.value),
                "source": claim.source,
                "at": now.isoformat(),
                "messageId": str(source_message_id) if source_message_id else None,
            }
        )

    if used_constraint_items:
        brief.constraints = {"items": constraint_items}
    brief.assumptions = {"audit": entries[-_MAX_AUDIT:]}

    if changed:
        # version 表示"这份简报被改动过几次"。用户在界面上看到它递增,
        # 就知道系统确实记住了他说的话。
        brief.version = (brief.version or 0) + 1

    await db.flush()
    return brief, tuple(dict.fromkeys(changed))


def _coerce(field: str, value: object) -> object | None:
    """把已经校验过的值转成列需要的类型。

    这里的类型转换必须和列的声明完全一致:把字符串塞进 `Date` 列在 SQLite 上会被
    静默接受(SQLite 的列类型是建议性的),到 PostgreSQL 上才炸 —— 又一个本地与
    线上分叉。所以日期在这里就转成 `date`。
    """
    if field == "deadline":
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return None
    if field == "weekly_available_minutes":
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value > 0 else None
    return str(value)


def _jsonable(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def known_summary(brief: PlanningBrief | None) -> dict[str, object]:
    """给接口响应用的简报快照。字段名与前端既有的 camelCase 靠契约层转。"""
    known = to_known_conditions(brief)
    return {
        "goal": known.goal,
        "deadline": known.deadline,
        "weekly_available_minutes": known.weekly_available_minutes,
        "current_level": known.current_level,
        "success_criteria": known.success_criteria,
        "constraints": list(known.constraints),
        "missing": list(known.missing),
        "version": brief.version if brief else 0,
    }


def audit_entries(brief: PlanningBrief | None) -> list[dict[str, object]]:
    if brief is None or not isinstance(brief.assumptions, dict):
        return []
    entries = brief.assumptions.get("audit")
    return list(entries) if isinstance(entries, list) else []


__all__ = [
    "apply_claims",
    "audit_entries",
    "known_summary",
    "load_brief",
    "to_known_conditions",
]
