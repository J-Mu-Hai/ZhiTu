"""提案校验。**纯函数,一次写入都没有。**

## 这个模块的职责边界

输入是"模型说的那些变更" + "这个空间此刻真实的节点与依赖",输出是"能不能落地"
以及"落地的话具体要写哪几行"。它不碰数据库、不碰会话、不产生任何副作用 ——
**连 `uuid4()` 分配新节点 id 都是在这一层完成的**,因为提前分配让"依赖指向一个
还不存在的节点"退化成普通的 uuid 引用,而不是一个需要特殊处理的状态。

于是"非法的模型输出不会产生部分写入"不是一条要小心遵守的纪律,而是结构性的:
这一层根本没有写的能力。它判失败,调用方拿到的 `plan` 就是 None,没有任何东西
可以写进库。

## 为什么 op 分三类而不是两类

- 认得的 op(能落地)
- **枚举里有、但这个版本做不到的 op**(排期类)—— `OP_NOT_YET_AVAILABLE`
- **根本不存在的 op**(模型胡编的)—— `UNKNOWN_OP_TYPE`

后两者对用户意味着完全不同的事:前者是"这个功能还没做",后者是"AI 这次输出了
无效内容"。混成一个错误码,用户就无法判断该等一等还是该重试。
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from pydantic import ValidationError

from backend.contracts.plan import (
    MAX_NOTE_CODEPOINTS,
    child_level_conflicts,
    information_node_conflicts,
)
from backend.contracts.proposal import (
    ACCEPTED_OPS,
    DEFERRED_OPS,
    MAX_ACTIONS,
    MAX_CREATED_NODES,
    ActionError,
    CreateDependencyAction,
    CreateNodeAction,
    CreateRelationAction,
    DeleteDependencyAction,
    DeleteNodeAction,
    UpdateNodeAction,
    UpdateNoteAction,
    action_adapter,
)
from backend.db.models.enums import NodePurpose, NodeRelationType, NodeType, PlanningLevel

# ---------------------------------------------------------------------------------
# 错误码。**这是对外的接口**,与 services/errors.py 里的 code 同级:
# 客户端按它分支,文案会改它不会。
# ---------------------------------------------------------------------------------
UNKNOWN_OP_TYPE = "UNKNOWN_OP_TYPE"
OP_NOT_YET_AVAILABLE = "OP_NOT_YET_AVAILABLE"
PAYLOAD_SCHEMA_INVALID = "PAYLOAD_SCHEMA_INVALID"
DANGLING_PROPOSAL_REF = "DANGLING_PROPOSAL_REF"
NODE_NOT_IN_WORKSPACE = "NODE_NOT_IN_WORKSPACE"
#: 这条变更要动的东西在当前作用范围之外。**与 `DANGLING_PROPOSAL_REF` 是两回事**:
#: 那个是"根本没这个节点",这个是"节点在,但不在你这一轮能改的范围里"。混成一个
#: 错误码,用户会以为节点丢了,而实际上他只需要先进入那一层。
OUT_OF_SCOPE = "OUT_OF_SCOPE"
LOCAL_ID_CONFLICT = "LOCAL_ID_CONFLICT"
DUPLICATE_LOCAL_ID = "DUPLICATE_LOCAL_ID"
TOO_MANY_ACTIONS = "TOO_MANY_ACTIONS"
CONFLICTING_OPERATIONS = "CONFLICTING_OPERATIONS"
DEADLINE_IN_PAST = "DEADLINE_IN_PAST"
CANNOT_DELETE_ROOT = "CANNOT_DELETE_ROOT"
SELF_DEPENDENCY = "SELF_DEPENDENCY"
DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
DEPENDENCY_ALREADY_EXISTS = "DEPENDENCY_ALREADY_EXISTS"
DEPENDENCY_NOT_FOUND = "DEPENDENCY_NOT_FOUND"
#: 一个节点和自己连「相关 / 影响」。与 `SELF_DEPENDENCY` 分开:那个说的是排期依赖,
#: 这个说的是画布上的说明边 —— 两张表、两条路,错误码也应当分得清。
SELF_RELATION = "SELF_RELATION"
#: 这条「相关 / 影响」边已经存在了。`related_to` 是无向的,所以 A→B 已存在时
#: 再提 B→A 也算重复 —— 规范化的键让两种画法落到同一行。
RELATION_ALREADY_EXISTS = "RELATION_ALREADY_EXISTS"
#: 想把一个信息主题当**排期依赖**的端点。与手工路径的 `InformationNodeNotSchedulable`
#: 同一个 code,见 `services/errors.py`。信息主题可以参与「相关 / 影响」,但依赖
#: (finish-to-start)是排期输入 —— 信息主题没有"完成"那一刻,那条边永远不成立。
INFORMATION_NODE_NOT_DEPENDABLE = "INFORMATION_NODE_NOT_DEPENDABLE"
#: 同一个父节点下已经有一个**同一主题**的节点,而且用途不同,所以合并不了。
#: 见 `_create`:合并键里含 `purpose`(信息用途与规划用途的"学业情况"是两回事),
#: 但**拒绝**键里不含 —— 同一层里叫同一个名字的两样东西,正是 §4.4 与 §2.5 要消掉的重复。
DUPLICATE_NODE_TITLE = "DUPLICATE_NODE_TITLE"
#: 信息用途的节点带了工时或截止时间。§4.1:"信息主题不需要具备工时、完成勾选或截止日期"。
INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE = "INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE"
#: 规划层级不相容:父子层级反向(较细的当父、较粗的当子),或信息主题带了层级。
PLANNING_LEVEL_INVALID = "PLANNING_LEVEL_INVALID"
#: 还没有已确认的战略,却提出了阶段/月/周/日层级的动作。
#: **与 `PLANNING_LEVEL_INVALID` 分开**,因为用户要做的事不同:这个要先去确认战略。
STRATEGY_NOT_CONFIRMED = "STRATEGY_NOT_CONFIRMED"
#: 提案写笔记时,那段笔记在生成之后被改过了。**与 `CONFLICTING_OPERATIONS` 分开**:
#: 那个是"同一份提案内部自相矛盾",这个是"你读的那一版已经不是现在的这一版了",
#: 用户该做的是重新生成,而不是去改提案。
NOTE_CHANGED = "NOTE_CHANGED"

#: `_Book.touched` 里那两个取值。用常量而不是就地写中文字符串:它们是要被比较的
#: 哨兵值,写错一个字就会静默变成"没被碰过",而那种错误不会报错。
_TOUCH_UPDATE = "修改"
_TOUCH_DELETE = "删除"

TYPE_LABELS: dict[str, str] = {
    NodeType.GOAL.value: "目标",
    NodeType.CAPABILITY.value: "能力",
    NodeType.STAGE.value: "阶段",
    NodeType.TASK.value: "任务",
    NodeType.MILESTONE.value: "里程碑",
}

_FIELD_LABELS: dict[str, str] = {
    "op": "op",
    "localId": "localId",
    "local_id": "localId",
    "parentRef": "parentRef",
    "parent_ref": "parentRef",
    "targetRef": "targetRef",
    "target_ref": "targetRef",
    "predecessorRef": "predecessorRef",
    "predecessor_ref": "predecessorRef",
    "successorRef": "successorRef",
    "successor_ref": "successorRef",
    "title": "标题",
    "description": "说明",
    "acceptanceCriteria": "验收标准",
    "acceptance_criteria": "验收标准",
    "nodeType": "节点类型",
    "node_type": "节点类型",
    "estimateMinutes": "预计工时",
    "estimate_minutes": "预计工时",
    "deadline": "截止时间",
    "priority": "优先级",
    "status": "状态",
    "purpose": "用途",
    "body": "长正文",
    #: 两种拼法都要留着:模型写这个字段时如果写错了类型(比如写成 `"第3版"`),
    #: pydantic 那条英文报错要经 `_describe` 变成"笔记版本"才读得懂。它只在
    #: **报错**里出现 —— 值本身不参与任何判断(见 `consume` 里那一段)。
    "expectedNoteVersion": "笔记版本",
    "expected_note_version": "笔记版本",
    "planningLevel": "规划层级",
    "planning_level": "规划层级",
}

#: 服务端在 payload 里记的"这份提案生成时,那段笔记是第几版"。
#:
#: **它和 `expectedNoteVersion` 是两个东西**,尽管说的都是笔记版本:那一个是模型
#: 可能写的字段(丢掉不认),这一个只有 `_update_note` 写得出来 —— 而"是不是我们
#: 自己写的"正是确认时那道闸成立的前提(见 `_Book.consume`)。
_NOTE_BASE_VERSION_KEY = "_noteBaseVersion"


# ---------------------------------------------------------------------------------
# 输入快照
# ---------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """一个**当前真实存在**的节点(软删除的不在其中)。

    只带校验需要的字段。刻意不是 ORM 对象:传 ORM 对象进来会让这个模块意外地
    依赖会话状态,而它的全部价值就在于"不依赖任何会变的东西"。

    ## 后三个字段都是**后来才需要**的,各有各的用途

    - `node_type` / `purpose`:去重(§4.4)要判断"这一层里是不是已经有同一个主题了",
      而"同一个主题"是按 `(父节点, 用途, 规范化标题)` 认的(见 `coalesce_creates`)。
      `node_type` 不是为了匹配,是为了**在拒绝的时候说得出对方是什么**
      ("已经有一个同名的「能力」节点")—— 一句指不出对方的错误,用户没法行动。
    - `note_version`:笔记的乐观锁号,`update_note` 用它判断"我这一份是照哪一版写的"。
      它在这里的原因和 `content_version` 不在 `PlanNode` 上而在别处是一样的:
      **笔记有自己的版本号**(见 `db/models/note.py`),拿节点的号去比是两个尺子。
    - `estimate_minutes` / `deadline`:「信息主题不能带工时/截止」这条规则判的是**改完之后
      的状态**,不是"这次动了哪几个字段"(与 `node_service.update_node` 同一个判法)。
      而"改完之后"要有两侧:patch 里的新值与节点上的旧值。少一个字段,规则就退化成
      "这次没提工时所以没问题" —— 于是"把一个已经排了期的任务改成信息主题"从
      AI 这条路就过得去,而手工那条路会拒。这个不对称正是 §2.5 要消掉的那种。
    """

    id: uuid.UUID
    parent_id: uuid.UUID | None
    title: str
    depth: int
    order_index: int
    node_type: str = NodeType.TASK.value
    purpose: str = NodePurpose.PLANNING.value
    #: 这个节点当前的长笔记版本号。`0` = 还没有笔记(见 `note_service.payload_of`)。
    note_version: int = 0
    estimate_minutes: int | None = None
    deadline: date | None = None
    #: 规划层级(`strategy` / `phase` / `month` / `week` / `day`)。`None` = unspecified。
    planning_level: str | None = None


# ---------------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PlannedNode:
    """一个将要被新建的节点。id 在这一层就分配好了。"""

    id: uuid.UUID
    local_id: str
    parent_id: uuid.UUID
    depth: int
    order_index: int
    action: CreateNodeAction


@dataclass(frozen=True, slots=True)
class NodePatch:
    """一个将要被修改的节点。`changed_fields` 是模型**显式写过**的字段名集合。

    用它而不是 `action.model_dump()`,是因为 dump 出来的每个字段都有值(没写的
    就是默认值 None)。照着 dump 全量覆盖,会把模型没提过的字段一并清空 ——
    "我只想改个截止时间,结果说明被删了"这种 bug 就是这么来的。
    """

    node_id: uuid.UUID
    changed_fields: frozenset[str]
    action: UpdateNodeAction


@dataclass(frozen=True, slots=True)
class ValidatedItem:
    """一条变更的展示形态。校验通过后按原顺序留在 `plan.items` 里。"""

    ordinal: int
    op: str
    summary: str
    local_id: str | None = None
    target_node_id: uuid.UUID | None = None
    target_title: str | None = None
    payload: dict = field(default_factory=dict)
    affected_children: int = 0
    #: 这一条**是被合并出来的**,值是它合并到的那个东西的标题。
    #:
    #: 有值就说明模型说的是"新建",而实际会写的是"补充/并入"。§7.2 要求预览
    #: 如实反映将要发生的事 —— 一条读作「新建「学业情况」」而实际改了一个已有节点的
    #: 预览,让用户确认了一件他没看过的事。
    coalesced_from: str | None = None


@dataclass(frozen=True, slots=True)
class NotePatch:
    """一份将要被写进**长笔记**的正文。§2.2。

    与 `NodePatch` 并排而不是塞进去:它们写的是两张表、比的是两个版本号。
    合成一个的后果是"改说明"和"改笔记"在预览与执行里长得一样,而它们在确认时
    该比两个不同的号(见 `contracts/proposal.UpdateNoteAction`)。
    """

    node_id: uuid.UUID
    body: str
    #: **服务端认定的**那个版本号,不是模型填的。执行时用它再比一次 ——
    #: 读 `action.expected_note_version` 是错的:那个值可能本来就是 None,
    #: 拿它去比等于不设闸(见 `_Book._update_note`)。
    expected_version: int
    action: UpdateNoteAction


@dataclass(frozen=True, slots=True)
class PlannedRelation:
    """一条将要被写进 `node_relations` 的「相关 / 影响」边。

    `source_id` / `target_id` 是**已经规范化过**的存储方向:`related_to` 按 UUID 的
    整数序排好,`influences` 保持模型给的方向。`_apply` 直接拿它建行,不再自己排一次
    —— 规范化只应当有一处,否则校验用的是无向键、写入用的是原方向,两者会分叉。
    """

    source_id: uuid.UUID
    target_id: uuid.UUID
    relation_type: NodeRelationType
    note: str | None
    action: CreateRelationAction


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    """可以落地的完整变更集。"""

    creates: tuple[PlannedNode, ...] = ()
    updates: tuple[NodePatch, ...] = ()
    #: 软删除的节点 id,**已包含被连带删除的全部子节点**。
    deletes: tuple[uuid.UUID, ...] = ()
    notes: tuple[NotePatch, ...] = ()
    dependencies_add: tuple[tuple[uuid.UUID, uuid.UUID], ...] = ()
    dependencies_remove: tuple[tuple[uuid.UUID, uuid.UUID], ...] = ()
    #: 「相关 / 影响」边。与 `dependencies_add` 分开:它们写两张表、语义也不同。
    relations: tuple[PlannedRelation, ...] = ()
    items: tuple[ValidatedItem, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.creates or self.updates or self.deletes or self.notes
            or self.dependencies_add or self.dependencies_remove or self.relations
        )


@dataclass(frozen=True, slots=True)
class ValidationResult:
    plan: ValidatedPlan | None
    errors: tuple[ActionError, ...]

    @property
    def ok(self) -> bool:
        return self.plan is not None


# ---------------------------------------------------------------------------------
# 去重:同一主题不再建第二个节点(§4.4 / §2.5)
# ---------------------------------------------------------------------------------
#: 合并进既有节点时,从 `create_node` 搬过来的那些字段。
#:
#: **`title` 与 `node_type` 刻意不在其中。** 这两个描述的是"这是哪个东西",
#: 而那个东西已经存在了 —— 搬过去就是把用户节点的名字按模型的拼法改一遍。
#: 去重的匹配用的是**规范化**标题(全角折半角、大小写折叠、空白压平),匹配上
#: 恰恰意味着两者的书写不同;而"不同"不该变成一次重命名。
_MERGE_FIELDS = (
    "description",
    "acceptanceCriteria",
    "acceptance_criteria",
    "estimateMinutes",
    "estimate_minutes",
    "deadline",
    "priority",
)


def normalize_title(title: str) -> str:
    """标题的**比较形状**。只用来判断"这两个是不是同一个主题",不用来显示。

    两步,缺一不可:

    1. `NFKC` —— 这一半才是重点。这个产品的输入里全角与半角是混着来的
       (「排名３８」与「排名38」是同一件事,前者往往是从别处粘进来的),
       而它们在码点上完全不同。少了这一步,重复照样建出来,而"去重没生效"
       这件事看起来和"模型又提了一个新节点"一模一样。
    2. `casefold` + 空白压平 + 去掉两端 —— 大小写与排版差异同理。
    """
    folded = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(folded.split())


def coalesce_creates(
    actions: Sequence[Mapping[str, object]],
    *,
    handles: Mapping[str, uuid.UUID],
    nodes: Mapping[uuid.UUID, NodeSnapshot],
) -> tuple[tuple[dict, ...], dict[int, str]]:
    """把"新建一个**已经有**的节点"改写成"补充那个节点"。**纯函数。**

    返回改写后的动作,以及一张 `序号(从 1 数) -> 被合并到的那个对象的标题` 的表。
    那张表最终会变成 `ValidatedItem.coalesced_from`,所以它说的必须是**用户认得的
    东西的名字**(既有节点的标题),不是 id、不是一句内部说明。

    ## 为什么必须在 `validate_actions` **之前**跑

    因为 `_persist` 存的是校验后的 `item.payload`,而确认时会拿**同一份 payload**
    再校验一遍。校验之后才改写的话:库里躺的 payload 与实际写入的行不一致,
    而确认时的重校验看到的还是原件 —— 于是"预览说新建,应用时改了一个已有节点"。
    两处跑同一个纯函数、输入也一致,预览与应用才不可能分叉。

    ## 三种命中,三种处理

    - **命中既有节点** → 改写成指向它的 `update_node`。预览里那条会读作
      「补充已有节点「学业情况」」,合并这件事是**看得见**的(§7.2:悄悄把新建
      改成更新,等于用户确认了一件他没看过的事)。
    - **命中同一批里更早的一条 `create_node`** → 字段并进那一条、这一条**去掉**。
      这是唯一一条真的会让条目数变少的路径,而它是安全的:写入本来就只有一个节点。
      被并进去的那一条会在摘要里说明它吸收了谁。
    - **没命中** → 原样不动。

    ## 匹配键里为什么有 `purpose`

    §2.5 说信息用途与规划层级是**两个维度**。"学业情况"既可以是一个信息主题,
    也可以是一项行动 —— 那是两个不同的对象,不该互相撞。所以合并键含 `purpose`。

    而**拒绝**键不含(见 `_Book._create`):同一层里叫同一个名字的两样东西,正是
    §4.4 要消掉的重复;合并不了的那种,整份拒绝,而不是放一个重复进去。
    """
    seen: dict[tuple[str, str, str], int] = {}
    absorbed: dict[int, str] = {}
    notes: dict[int, str] = {}
    rewritten: list[dict] = []

    for raw in actions:
        action = dict(raw)
        if action.get("op") != "create_node":
            rewritten.append(action)
            continue

        parent_ref = action.get("parentRef") or action.get("parent_ref")
        title = action.get("title")
        purpose = str(action.get("purpose") or NodePurpose.PLANNING.value)
        if not isinstance(parent_ref, str) or not isinstance(title, str):
            # 缺字段的那一条不是这里能修正的:它会在校验里报成"没填",而那是用户
            # 看得懂的一条错误。在这里猜一个默认值只会把错误藏起来。
            rewritten.append(action)
            continue

        key = (parent_ref, purpose, normalize_title(title))

        if key in seen:
            # 同一批里的第二条:字段并进第一条(**先说的算**),这一条不产出 ——
            # 写入只有一个节点,预览也只该有一条。
            earlier = rewritten[seen[key]]
            for field in _MERGE_FIELDS:
                if action.get(field) is not None and earlier.get(field) is None:
                    earlier[field] = action[field]
            absorbed[seen[key]] = str(earlier.get("title") or title)
            continue

        found = _find_same_topic(nodes, handles, parent_ref, purpose, key[2])
        if found is None:
            seen[key] = len(rewritten)
            rewritten.append(action)
            continue

        handle, snapshot = found
        merged: dict = {"op": "update_node", "targetRef": handle}
        for field in _MERGE_FIELDS:
            if action.get(field) is not None:
                merged[field] = action[field]
        # **把现有标题原样写回去。**
        #
        # 两个作用:一是这一条一定有一个显式字段,"没说要改什么"那道校验不会把它
        # 拒掉(否则用户看到的是整轮更新一起失败);二是写回的与原值等价 ——
        # 用户的标题一个字都不会变,变的只是它作为哪个字段被提到。
        merged["title"] = snapshot.title
        seen[key] = len(rewritten)
        rewritten.append(merged)
        notes[len(rewritten)] = snapshot.title

    for index, title in absorbed.items():
        notes[index + 1] = title
    return tuple(rewritten), notes


def _find_same_topic(
    nodes: Mapping[uuid.UUID, NodeSnapshot],
    handles: Mapping[str, uuid.UUID],
    parent_ref: str,
    purpose: str,
    normalized: str,
) -> tuple[str, NodeSnapshot] | None:
    """这一层里有没有一个同主题的节点。有就返回**它的记号**与快照。

    返回记号而不是 id:改写出来的 `update_node` 引用的必须是记号 —— 这一层的语言
    只有记号。把一个真实 uuid 写进模型产出的那条 payload 里,等于让 `targetRef`
    长得像 `n3` 却不是 `n3`,而那一层没有任何地方认得它。
    """
    parent_id = handles.get(parent_ref)
    if parent_id is None:
        return None
    handle_of = {node_id: handle for handle, node_id in handles.items()}
    for snapshot in nodes.values():
        if snapshot.parent_id != parent_id:
            continue
        if snapshot.purpose != purpose:
            continue
        if normalize_title(snapshot.title) != normalized:
            continue
        handle = handle_of.get(snapshot.id)
        if handle is not None:
            return handle, snapshot
    return None


def canonical_relation_key(
    relation_type: NodeRelationType, source: uuid.UUID, target: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """一条「相关 / 影响」边的**比较健**。纯函数。

    `related_to` 是无向的,所以两端要按 UUID 的**整数序**排成一个固定顺序 ——
    字符串序是实现细节,不能当语义。于是 A→B 与 B→A 得到同一个键,只算一条边。
    `influences` 有向,方向原样保留,反方向是另一条边。

    比较键与存储方向用的是同一套排序:`_apply` 拿 `key[:2]` 去建行。两处都用它,
    "校验说重复"与"库里真有一行"才不可能分叉。
    """
    if relation_type is NodeRelationType.INFLUENCES:
        return (source, target, relation_type.value)
    first, second = sorted((source, target), key=lambda value: value.int)
    return (first, second, relation_type.value)


# ---------------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------------
def validate_actions(
    actions: Sequence[Mapping[str, object]],
    *,
    handles: Mapping[str, uuid.UUID],
    nodes: Mapping[uuid.UUID, NodeSnapshot],
    dependencies: Iterable[tuple[uuid.UUID, uuid.UUID]] = (),
    relations: Iterable[tuple[uuid.UUID, uuid.UUID, NodeRelationType]] = (),
    today: date,
    writable: Iterable[uuid.UUID] | None = None,
    strategy_exists: bool = False,
) -> ValidationResult:
    """把模型的一段变更校验成一个可以落地的变更集。

    `handles` 是本轮送给模型的那份记号表。**这是唯一合法的指涉来源** ——
    模型写出的、不在其中的 id 一律按悬空引用拒绝,而不是"查一下数据库看看这个 id
    存不存在"。后者会让别的空间里的节点 id 变成一条可用的输入。

    ## `writable`:提示词里的范围**也必须是服务端的检查**

    `None` 表示不设范围限制(直接调用这个函数的少数场景:测试、复盘那条工作区级的
    路径 —— 那里用户要调整的是整个空间)。给了集合就表示"只能动这些":其余节点
    一律拒绝,即使模型看得见它们。

    为什么不能只靠提示词说"范围外别动":提示词是一段建议,而范围是一条权限。
    模型被注入的输入带偏、或者只是自作主张地"顺手把上面那条也改了",提示词都拦不住 ——
    用户看到的结果会是"我明明只在看这个阶段,它把我整份计划改了"。

    ## 去重在这里、也只在这里

    同一个纯函数被两个地方调用(生成时、确认时),而**合并必须两处一致**:只有
    生成时合并的话,确认时看到的还是原件,于是"预览说新建、应用时改了一个已有节点"。
    放到别处(比如 `node_service.create_node`)是错的另一侧:手工建两个同名节点今天就是
    合法的,§4.4 的去重针对的是**生成**。
    """
    if len(actions) > MAX_ACTIONS:
        return _fail(
            ActionError(
                ordinal=0,
                code=TOO_MANY_ACTIONS,
                message=f"这一次的变更太多了({len(actions)} 条),最多 {MAX_ACTIONS} 条。",
            )
        )

    actions, coalesced = coalesce_creates(actions, handles=handles, nodes=nodes)

    book = _Book(
        handles=handles,
        nodes=nodes,
        # 软删除不会级联删除依赖行,所以库里天然躺着一些指向已删节点的边。
        # 在这里一次性滤掉,后面"这条关系存不存在"与成环检测用的就是同一份基准。
        dependencies={
            (a, b) for a, b in dependencies if a in nodes and b in nodes
        },
        # 同样的理由套在「相关 / 影响」上:两端都还活着的边才是可比较的基准。
        # 键按 `related_to` 无向规范化,所以 A→B 与 B→A 在这里就是同一个键。
        relations={
            canonical_relation_key(relation_type, a, b)
            for a, b, relation_type in relations
            if a in nodes and b in nodes
        },
        today=today,
        writable=None if writable is None else frozenset(writable),
        coalesced=coalesced,
        strategy_exists=strategy_exists,
    )
    errors: list[ActionError] = []

    for ordinal, raw in enumerate(actions, start=1):
        errors.extend(book.consume(ordinal, raw))

    if errors:
        return ValidationResult(plan=None, errors=tuple(errors))

    cycle_errors = book.detect_cycles()
    if cycle_errors:
        return ValidationResult(plan=None, errors=tuple(cycle_errors))

    if book.is_empty:
        # 模型给了一堆 op,但一条也没落地(比如全是空更新)。这不当作错误 ——
        # 它就是"这一轮没有变更",和 actions=[] 走同一条路。
        return ValidationResult(plan=None, errors=())

    return ValidationResult(plan=book.to_plan(), errors=())


def _fail(error: ActionError) -> ValidationResult:
    return ValidationResult(plan=None, errors=(error,))


# ---------------------------------------------------------------------------------
# 累积状态
# ---------------------------------------------------------------------------------
class _Book:
    """校验过程中的累积状态。

    做成可变对象而不是一串返回值,是因为校验天然是顺序的:后一条能不能成立,
    取决于前一条建了什么。把这个顺序写进一个对象里,读起来就是它本来的样子。
    """

    def __init__(
        self,
        *,
        handles: Mapping[str, uuid.UUID],
        nodes: Mapping[uuid.UUID, NodeSnapshot],
        dependencies: set[tuple[uuid.UUID, uuid.UUID]],
        today: date,
        writable: frozenset[uuid.UUID] | None = None,
        coalesced: Mapping[int, str] | None = None,
        relations: set[tuple[uuid.UUID, uuid.UUID, str]] | None = None,
        strategy_exists: bool = False,
    ) -> None:
        self.today = today
        self.handles = dict(handles)
        self.nodes = dict(nodes)
        self.existing_dependencies = dependencies
        #: 库里已经存在的「相关 / 影响」边(规范化的键,两端都活着)。
        self.existing_relations = set(relations or ())
        #: None = 不设范围限制。见 `validate_actions` 的注释。
        self.writable = writable
        #: 这个空间里有没有一个**已经存在的、活着的**战略节点(`planning_level=strategy`)。
        #: 有才允许提出更细的层级 —— 战略必须先被用户确认。
        self.strategy_exists = strategy_exists
        #: 序号 -> 这一条是合并来的,合并到了谁。见 `coalesce_creates`。
        self.coalesced = dict(coalesced or {})

        #: 记号 -> 真实 id。开始是已有的句柄,随着 create 逐条长大。
        #: **只增不改**:一个记号一旦绑定就不能再指向别的东西。
        self.resolved: dict[str, uuid.UUID] = dict(handles)
        self.creates: dict[str, PlannedNode] = {}
        self.updates: list[NodePatch] = []
        self.deletes: list[uuid.UUID] = []
        self.notes: list[NotePatch] = []
        self.dep_add: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.dep_remove: list[tuple[uuid.UUID, uuid.UUID]] = []
        #: 「相关 / 影响」边。键集用来查重(与库里已存的用同一套规范化),列表用来落盘。
        self.rel_keys: set[tuple[uuid.UUID, uuid.UUID, str]] = set()
        self.rel_add: list[PlannedRelation] = []
        self.items: list[ValidatedItem] = []
        #: 已经被写过笔记的节点 -> 那条动作的 `expected_note_version`。
        #: 同一个节点在一次提案里被写两次笔记是矛盾的(第二份照哪一版写?)。
        self.touched_notes: dict[uuid.UUID, int | None] = {}

        #: 已经被改过或删过的节点 -> 到底做了什么(见 _TOUCH_*)。
        #:
        #: 同一个节点在一次提案里被处理两次是矛盾的 —— "把 n4 改名"和"删掉 n4"同时
        #: 成立,谁先谁后都说不通。但**矛盾的程度取决于第二件事是什么**:
        #:
        #: - 「改」之后再「删」,或「删」之后再「改」「删」: 说不通,一律拒绝。
        #: - 「改」之后再「往它下面加子节点」:**完全说得通**。`update_node` 根本没有
        #:   parent_ref,改标题、改说明、改截止时间都不会动到树的结构。
        #:
        #: 最后这条差别不是理论上的。真实模型第一次跑通时就栽在这里:它先给根目标
        #: 补了一句说明,再往根目标下面挂四个阶段 —— 一个再合理不过的计划,被
        #: 一条过宽的规则整份拒掉,还连带报出 14 条"引用了不存在的节点"(那些节点
        #: 本来是要被前一条建出来的)。所以这里必须区分「改过」和「删掉」。
        self.touched: dict[uuid.UUID, str] = {}
        #: 每个父节点当前最大的 order_index,用于给新节点排队尾。
        self.next_order: dict[uuid.UUID, int] = {}
        for node in self.nodes.values():
            current = self.next_order.get(node.parent_id or _NO_PARENT, -1)
            self.next_order[node.parent_id or _NO_PARENT] = max(current, node.order_index)

    # -- 只读视图 ---------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not (
            self.creates or self.updates or self.deletes or self.notes
            or self.dep_add or self.dep_remove or self.rel_add
        )

    def title_of(self, node_id: uuid.UUID) -> str:
        for planned in self.creates.values():
            if planned.id == node_id:
                return planned.action.title
        snapshot = self.nodes.get(node_id)
        return snapshot.title if snapshot else "(未知节点)"

    # -- 逐条处理 ---------------------------------------------------------------
    def consume(self, ordinal: int, raw: Mapping[str, object]) -> list[ActionError]:
        op = raw.get("op")
        if not isinstance(op, str):
            return [_err(ordinal, PAYLOAD_SCHEMA_INVALID, "这一条缺少 op 字段。")]
        if op in DEFERRED_OPS:
            return [
                _err(
                    ordinal,
                    OP_NOT_YET_AVAILABLE,
                    f"「{op}」需要排期能力,当前版本还做不到。"
                    "可以先只提节点结构的变更,哪天做会在下一步排。",
                )
            ]
        if op not in ACCEPTED_OPS:
            return [_err(ordinal, UNKNOWN_OP_TYPE, f"不认识「{op}」这种变更。")]

        # 服务端自己记的账先摘出来再交给 pydantic:`extra="forbid"` 会把任何多余的键
        # 当成模型的错,而它是**我们**写进去的(见 `_NOTE_BASE_VERSION_KEY`)。
        source = dict(raw)
        recorded_note_version = source.pop(_NOTE_BASE_VERSION_KEY, None)

        try:
            action = action_adapter.validate_python(source)
        except ValidationError as exc:
            return [_err(ordinal, PAYLOAD_SCHEMA_INVALID, _describe(exc))]

        #: 存进提案的是**模型原样的那一段 JSON**,不是校验后重新 dump 出来的。
        #:
        #: 这个区别很关键:`update_node` 的"改了哪些字段"是靠 pydantic 的
        #: `model_fields_set` 判断的,而重新 dump 会把每个字段都写出来(没写的
        #: 变成 null),于是"只改截止时间"到了确认那一步会变成"把所有没提过的
        #: 字段一起清空" —— 用户确认的是改一个日期,实际发生的是删掉了他的说明。
        #: 原样存下来,预览与执行读的就是同一份输入,不可能分叉。
        payload = dict(source)

        # 笔记版本是**唯一一个不"原样"的东西**,而它不原样是因为它根本不该由模型说:
        # 它回答的是"这份提案生成时那段笔记是第几版",只有服务端知道,也只有服务端
        # 能判断"生成之后有没有人改过"。所以这里把两件事分开:
        #
        # - 模型写的那一份(`expectedNoteVersion`,两种拼法都算)**丢掉**:它已经进过
        #   pydantic(于是写错了类型会得到一条人看得懂的报错),但不进 payload、永不参与
        #   比较。不丢的话,确认时的重校验会把模型编的数字当成"生成时的版本" ——
        #   要么凭空拒掉一整轮(用户什么都没做错),要么把真正的冲突看漏(它碰巧写对了)。
        # - 服务端自己记的账(`_noteBaseVersion`)是 `_update_note` 写进去的,也只认它。
        payload.pop("expectedNoteVersion", None)
        payload.pop("expected_note_version", None)

        if isinstance(action, CreateNodeAction):
            return self._create(ordinal, action, payload)
        if isinstance(action, UpdateNodeAction):
            return self._update(ordinal, action, payload)
        if isinstance(action, DeleteNodeAction):
            return self._delete(ordinal, action, payload)
        if isinstance(action, UpdateNoteAction):
            return self._update_note(ordinal, action, payload, recorded_note_version)
        if isinstance(action, CreateDependencyAction):
            return self._add_dependency(ordinal, action, payload)
        if isinstance(action, DeleteDependencyAction):
            return self._remove_dependency(ordinal, action, payload)
        if isinstance(action, CreateRelationAction):
            return self._add_relation(ordinal, action, payload)
        # action_adapter 的判别联合已经穷尽了所有分支,走到这里说明契约层加了新 op
        # 而这里忘了处理。**如实报成不认识,而不是静默放过去** —— 静默放过去的后果是
        # 用户看到一条"AI 提了变更"却什么都没发生。
        return [_err(ordinal, UNKNOWN_OP_TYPE, f"不认识「{action.op}」这种变更。")]

    def _create(
        self, ordinal: int, action: CreateNodeAction, payload: dict
    ) -> list[ActionError]:
        if action.local_id in self.handles:
            return [
                _err(
                    ordinal,
                    LOCAL_ID_CONFLICT,
                    f"记号 {action.local_id} 已经被现有节点占用了,新节点请换一个编号。",
                )
            ]
        if action.local_id in self.creates:
            return [_err(ordinal, DUPLICATE_LOCAL_ID, f"记号 {action.local_id} 用了两次。")]
        if len(self.creates) >= MAX_CREATED_NODES:
            return [
                _err(
                    ordinal,
                    TOO_MANY_ACTIONS,
                    f"一次最多新建 {MAX_CREATED_NODES} 个节点,请拆成几次。",
                )
            ]

        parent_id = self.resolved.get(action.parent_ref)
        if parent_id is None:
            return [
                _err(
                    ordinal,
                    DANGLING_PROPOSAL_REF,
                    f"引用了不存在的 {action.parent_ref}。"
                    "只能引用上面列出的已有节点,或本条之前刚新建的节点。",
                )
            ]
        if parent_id not in self.nodes and parent_id not in {p.id for p in self.creates.values()}:
            return [_err(ordinal, NODE_NOT_IN_WORKSPACE, f"{action.parent_ref} 不在这个空间里。")]
        # 同一条提案里刚建出来的父节点不用查范围:它的父节点已经查过了,而范围对
        # "往下"是封闭的 —— 范围内的节点下面新建的东西,也在范围内。
        if parent_id not in self._planned_ids():
            blocked_by_scope = self._out_of_scope(ordinal, parent_id, action.parent_ref)
            if blocked_by_scope is not None:
                return [blocked_by_scope]
        # 只挡"往一个正在被删的节点下面加子节点"。父节点**被改过**不影响新建 ——
        # 见 `touched` 那段注释,这是真实模型踩出来的一条。
        if self.touched.get(parent_id) == _TOUCH_DELETE:
            return [
                _err(
                    ordinal,
                    CONFLICTING_OPERATIONS,
                    f"父节点「{self.title_of(parent_id)}」在同一次提案里被删掉了,"
                    "不能同时又往它下面加东西。",
                )
            ]

        if action.deadline is not None and action.deadline < self.today:
            return [
                _err(
                    ordinal,
                    DEADLINE_IN_PAST,
                    f"「{action.title}」的截止时间 {action.deadline.isoformat()} 已经过去了。",
                )
            ]

        # 信息用途的节点不能带工时、不能带截止(§4.1)。手工那条路走的是**同一个**
        # 判定函数(`node_service.create_node`),所以"手动与 AI 创建路径采用相同校验"
        # 是结构性的,不是靠两处各写一遍、然后有一条忘了改。
        conflict = information_node_conflicts(
            action.purpose, action.estimate_minutes, action.deadline, action.planning_level
        )
        if conflict is not None:
            return [_err(ordinal, INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE, conflict)]

        level_error = self._create_level_error(ordinal, action, parent_id)
        if level_error is not None:
            return [level_error]

        duplicate = self._duplicate_title(ordinal, parent_id, action)
        if duplicate is not None:
            return [duplicate]

        parent_depth = self._depth_of(parent_id)
        node_id = uuid.uuid4()
        order_index = self.next_order.get(parent_id, -1) + 1
        self.next_order[parent_id] = order_index

        self.creates[action.local_id] = PlannedNode(
            id=node_id,
            local_id=action.local_id,
            parent_id=parent_id,
            depth=parent_depth + 1,
            order_index=order_index,
            action=action,
        )
        # 绑定之后,后面的条目就能引用这个记号了。**只在这里绑定** ——
        # 允许"引用一条还没处理到的条目"会让父子成环成为可能,而现在它不可能。
        self.resolved[action.local_id] = node_id
        self.items.append(
            ValidatedItem(
                ordinal=ordinal,
                op=action.op,
                summary=_create_summary(action, self.coalesced.get(ordinal)),
                local_id=action.local_id,
                payload=payload,
                coalesced_from=self.coalesced.get(ordinal),
            )
        )
        return []

    def _duplicate_title(
        self, ordinal: int, parent_id: uuid.UUID, action: CreateNodeAction
    ) -> ActionError | None:
        """这一层里已经有一个**同名的、用途不同**的节点。

        同用途的那种在 `coalesce_creates` 里就被改写成"补充"了,所以走到这里的
        只可能是用途不同的那一类 —— 而它是 §4.4 与 §2.5 都要消掉的重复:
        同一层里叫同一个名字的两样东西,用户看到的是两个他分不清哪个是哪个的框。

        **拒绝键不含 `purpose`,合并键含** —— 这个不对称是刻意的:能合并的合并,
        不能合并的整份拒绝,而不是放一个重复进去。

        消息要给出出路(§7.2 的"用户要能行动"):改名,或者先去处理那一个。
        """
        normalized = normalize_title(action.title)
        for planned in self.creates.values():
            if planned.parent_id != parent_id:
                continue
            if normalize_title(planned.action.title) != normalized:
                continue
            if planned.action.purpose is action.purpose:
                continue
            return _err(
                ordinal,
                DUPLICATE_NODE_TITLE,
                f"「{action.title}」在这一层里已经存在了(是"
                f"{_purpose_label(planned.action.purpose)}用途的)。"
                "同一个名字在同一层里只能有一个 —— 要新建就换个标题,"
                "要补充它就把它写成一条修改。",
            )

        for snapshot in self.nodes.values():
            if snapshot.parent_id != parent_id:
                continue
            if normalize_title(snapshot.title) != normalized:
                continue
            if snapshot.purpose == action.purpose:
                continue
            return _err(
                ordinal,
                DUPLICATE_NODE_TITLE,
                f"「{snapshot.title}」在这一层里已经存在了(是"
                f"{_purpose_label(NodePurpose(snapshot.purpose))}用途的)。"
                "同一个名字在同一层里只能有一个 —— 要新建就换个标题,"
                "要补充它就把它写成一条修改。",
            )
        return None

    def _update(
        self, ordinal: int, action: UpdateNodeAction, payload: dict
    ) -> list[ActionError]:
        node_id = self.resolved.get(action.target_ref)
        if node_id is None or node_id not in self.nodes:
            return [
                _err(
                    ordinal,
                    DANGLING_PROPOSAL_REF,
                    f"要修改的 {action.target_ref} 不是这个空间里已有的节点。",
                )
            ]

        changed = frozenset(action.model_fields_set - {"op", "target_ref"})
        if not changed:
            return [_err(ordinal, PAYLOAD_SCHEMA_INVALID, "这一条没有说要改什么。")]

        blocked_by_scope = self._out_of_scope(ordinal, node_id, action.target_ref)
        if blocked_by_scope is not None:
            return [blocked_by_scope]

        blocked = self.touched.get(node_id)
        if blocked is not None:
            return [
                _err(
                    ordinal,
                    CONFLICTING_OPERATIONS,
                    f"「{self.title_of(node_id)}」在同一次提案里被{blocked}过了。",
                )
            ]

        if "deadline" in changed and action.deadline is not None and action.deadline < self.today:
            return [
                _err(
                    ordinal,
                    DEADLINE_IN_PAST,
                    f"新截止时间 {action.deadline.isoformat()} 已经过去了。",
                )
            ]

        # 用途/工时/截止**一起**看改完之后的状态,与 `node_service.update_node` 同一个
        # 判法(那里读的是 ORM 行上的旧值,这里读快照上的)。只判"这次提没提工时"是不够的:
        # 节点上本来就有工时的话,一条只改了别的东西的修改也会把它带进去 ——
        # 而"它现在是什么用途"是快照说的,不是这次的动作说的。
        #
        # **用途取快照,不取动作。** `UpdateNodeAction` 刻意没有 `purpose`(见那份契约
        # 的注释:把行动改成主题是个用户该看清楚的决定),所以这里能变的只有工时与截止。
        #
        # **这条路径不只由 `update_node` 走到。** `coalesce_creates` 会把"新建一个已经有
        # 的信息主题、还带着工时"改写成一条修改 —— 也就是说,一个本来会被 `_create`
        # 拦下的 over-reach,会换一件衣服从这里进来。两处都判才是"拦得住"。
        snapshot = self.nodes[node_id]
        planning_level = (
            action.planning_level if "planning_level" in changed else snapshot.planning_level
        )
        conflict = information_node_conflicts(
            snapshot.purpose,
            action.estimate_minutes
            if action.estimate_minutes is not None
            else snapshot.estimate_minutes,
            action.deadline if action.deadline is not None else snapshot.deadline,
            planning_level,
        )
        if conflict is not None:
            return [_err(ordinal, INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE, conflict)]

        if "planning_level" in changed:
            level_error = self._update_level_error(ordinal, node_id, action.planning_level)
            if level_error is not None:
                return [level_error]

        self.touched[node_id] = _TOUCH_UPDATE
        self.updates.append(NodePatch(node_id=node_id, changed_fields=changed, action=action))
        title = self.title_of(node_id)
        self.items.append(
            ValidatedItem(
                ordinal=ordinal,
                op=action.op,
                summary=_update_summary(action, title, changed, self.coalesced.get(ordinal)),
                target_node_id=node_id,
                target_title=title,
                payload=payload,
                coalesced_from=self.coalesced.get(ordinal),
            )
        )
        return []

    def _update_note(
        self,
        ordinal: int,
        action: UpdateNoteAction,
        payload: dict,
        recorded_note_version: object = None,
    ) -> list[ActionError]:
        """写一个节点的长笔记。§2.2 与 §7 的矩阵:**笔记只能提案**。

        两处与"改说明"不同的地方,都在这里:

        - **版本号是服务端记的。** `recorded_note_version` 是**上一次校验写进 payload
          的那个号**(第一次校验时它是 `None` —— 模型写的那一份已经在 `consume` 里
          被摘掉了)。它和此刻库里的号对不上,就说明"提案生成之后、用户点确认之前,
          这段笔记被人改过",这一条不执行。比完再把此刻的号写回 payload —— 不写回去,
          确认时的重校验只会重新记一次"此刻的号",于是那道闸永远不响。
        - **长度用 `MAX_NOTE_CODEPOINTS` 先拒。** 理由是那份契约里写的那条:
          截断一段用户/AI 写的长文比拒绝它糟得多 —— 两边都会以为存下来了。
        """
        node_id = self.resolved.get(action.target_ref)
        if node_id is None or node_id not in self.nodes:
            return [
                _err(
                    ordinal,
                    DANGLING_PROPOSAL_REF,
                    f"要写笔记的 {action.target_ref} 不是这个空间里已有的节点。",
                )
            ]
        if len(action.body) > MAX_NOTE_CODEPOINTS:
            return [
                _err(
                    ordinal,
                    PAYLOAD_SCHEMA_INVALID,
                    f"长正文最多 {MAX_NOTE_CODEPOINTS} 个字(按 Unicode 码点计),"
                    f"这一份有 {len(action.body)} 个。",
                )
            ]

        blocked_by_scope = self._out_of_scope(ordinal, node_id, action.target_ref)
        if blocked_by_scope is not None:
            return [blocked_by_scope]

        if node_id in self.touched_notes:
            return [
                _err(
                    ordinal,
                    CONFLICTING_OPERATIONS,
                    f"「{self.title_of(node_id)}」的笔记在同一次提案里被写了两次。",
                )
            ]

        snapshot = self.nodes[node_id]
        expected = snapshot.note_version
        if isinstance(recorded_note_version, int) and recorded_note_version != expected:
            # 生成之后、用户点确认之前,这段笔记被改过 —— 这份提案是照旧的那一版写的。
            # 直接写下去就是一次安静的文字替换,而那正是版本号要防的事。
            return [
                _err(
                    ordinal,
                    NOTE_CHANGED,
                    f"「{self.title_of(node_id)}」的长笔记在提案生成之后被改过了"
                    f"(提案基于第 {recorded_note_version} 版,现在是第 {expected} 版),"
                    "这一条没有执行。请重新生成一份。",
                )
            ]

        # 把服务端此刻看到的那个号写回 payload。见本方法的 docstring:确认时的重校验
        # 读的就是它,不写回去等于这道闸没有闸。
        payload[_NOTE_BASE_VERSION_KEY] = expected
        self.touched_notes[node_id] = expected
        self.notes.append(
            NotePatch(
                node_id=node_id, body=action.body, expected_version=expected, action=action
            )
        )
        self.items.append(
            ValidatedItem(
                ordinal=ordinal,
                op=action.op,
                summary=_note_summary(action, self.title_of(node_id), len(action.body)),
                target_node_id=node_id,
                target_title=self.title_of(node_id),
                payload=payload,
            )
        )
        return []

    def _delete(
        self, ordinal: int, action: DeleteNodeAction, payload: dict
    ) -> list[ActionError]:
        node_id = self.resolved.get(action.target_ref)
        if node_id is None or node_id not in self.nodes:
            return [
                _err(
                    ordinal,
                    DANGLING_PROPOSAL_REF,
                    f"要删除的 {action.target_ref} 不是这个空间里已有的节点。",
                )
            ]
        if self.nodes[node_id].parent_id is None:
            return [
                _err(
                    ordinal,
                    CANNOT_DELETE_ROOT,
                    "根目标是这个空间的锚点,删不掉。想换目标就改它的标题。",
                )
            ]

        blocked_by_scope = self._out_of_scope(ordinal, node_id, action.target_ref)
        if blocked_by_scope is not None:
            return [blocked_by_scope]

        blocked = self.touched.get(node_id)
        if blocked is not None:
            return [
                _err(
                    ordinal,
                    CONFLICTING_OPERATIONS,
                    f"「{self.title_of(node_id)}」在同一次提案里被{blocked}过了。",
                )
            ]

        # **子树一起删。** 只删父节点会让它的子节点变成孤儿:它们还在库里、
        # 还会出现在时间线上,而用户找不到任何入口去处理它们。
        subtree = self._descendants(node_id)
        for member in subtree:
            self.touched[member] = _TOUCH_DELETE
        self.deletes.extend(subtree)

        title = self.title_of(node_id)
        self.items.append(
            ValidatedItem(
                ordinal=ordinal,
                op=action.op,
                summary=_delete_summary(title, len(subtree)),
                target_node_id=node_id,
                target_title=title,
                payload=payload,
                affected_children=len(subtree),
            )
        )
        return []

    def _add_dependency(
        self, ordinal: int, action: CreateDependencyAction, payload: dict
    ) -> list[ActionError]:
        pair = self._resolve_pair(ordinal, action.predecessor_ref, action.successor_ref)
        if isinstance(pair, list):
            return pair
        blocked = self._pair_out_of_scope(
            ordinal, pair, action.predecessor_ref, action.successor_ref
        )
        if blocked is not None:
            return [blocked]
        predecessor, successor = pair
        if predecessor == successor:
            return [_err(ordinal, SELF_DEPENDENCY, "一个节点不能以自己为前提。")]
        # 信息主题不能当**排期依赖**的端点(§2.5)。手工路径走的是
        # `node_service._reject_information_endpoints`;这里必须再判一次,因为提案这条
        # 路不经过那个函数 —— 少了它,AI 就能建出一条永远不成立的依赖边。
        for node_id in pair:
            if self._purpose_of(node_id) == NodePurpose.INFORMATION.value:
                return [
                    _err(
                        ordinal,
                        INFORMATION_NODE_NOT_DEPENDABLE,
                        f"「{self.title_of(node_id)}」是信息主题,不参与排期,"
                        "不能作为依赖的端点。要让它参与排期,先把它的用途改成「行动」。",
                    )
                ]
        if pair in self.dep_add:
            return [_err(ordinal, DEPENDENCY_ALREADY_EXISTS, "这条前置关系在同一次提案里写了两遍。")]
        if pair in self.existing_dependencies and pair not in self.dep_remove:
            return [
                _err(
                    ordinal,
                    DEPENDENCY_ALREADY_EXISTS,
                    f"「{self.title_of(pair[0])}」→「{self.title_of(pair[1])}」"
                    "这条前置关系已经存在了。",
                )
            ]
        self.dep_add.append(pair)
        self.items.append(
            ValidatedItem(
                ordinal=ordinal,
                op=action.op,
                summary=(
                    f"「{self.title_of(predecessor)}」完成后,才开始"
                    f"「{self.title_of(successor)}」"
                ),
                payload=payload,
            )
        )
        return []

    def _remove_dependency(
        self, ordinal: int, action: DeleteDependencyAction, payload: dict
    ) -> list[ActionError]:
        pair = self._resolve_pair(ordinal, action.predecessor_ref, action.successor_ref)
        if isinstance(pair, list):
            return pair
        blocked = self._pair_out_of_scope(
            ordinal, pair, action.predecessor_ref, action.successor_ref
        )
        if blocked is not None:
            return [blocked]
        if pair not in self.existing_dependencies:
            return [
                _err(
                    ordinal,
                    DEPENDENCY_NOT_FOUND,
                    f"「{self.title_of(pair[0])}」和「{self.title_of(pair[1])}」"
                    "之间本来就没有前置关系。",
                )
            ]
        if pair in self.dep_remove:
            return [_err(ordinal, DEPENDENCY_NOT_FOUND, "这条前置关系在同一次提案里取消了两遍。")]
        self.dep_remove.append(pair)
        self.items.append(
            ValidatedItem(
                ordinal=ordinal,
                op=action.op,
                summary=(
                    f"取消「{self.title_of(pair[0])}」→「{self.title_of(pair[1])}」的前置关系"
                ),
                payload=payload,
            )
        )
        return []

    def _add_relation(
        self, ordinal: int, action: CreateRelationAction, payload: dict
    ) -> list[ActionError]:
        """连一条「相关 / 影响」边。§2.5。

        与依赖分开校验,因为规则不同:依赖查环、信息主题不能当端点;这两种边**允许成环**、
        信息主题**可以参与**。方向规则也不同 —— `related_to` 无向去重。

        允许引用同一份提案里**前面刚建**的节点:`self.resolved` 在 `_create` 里已经绑好,
        所以"先建节点、再连关系"天然成立。
        """
        pair = self._resolve_pair(
            ordinal, action.source_ref, action.target_ref, subject="关系里"
        )
        if isinstance(pair, list):
            return pair
        blocked = self._pair_out_of_scope(
            ordinal, pair, action.source_ref, action.target_ref
        )
        if blocked is not None:
            return [blocked]
        source, target = pair
        if source == target:
            return [_err(ordinal, SELF_RELATION, "一个节点不能和自己建立关系。")]

        key = canonical_relation_key(action.relation_type, source, target)
        first, second, _ = key
        label = _relation_label(action.relation_type)
        if key in self.existing_relations:
            return [
                _err(
                    ordinal,
                    RELATION_ALREADY_EXISTS,
                    f"「{self.title_of(first)}」和「{self.title_of(second)}」之间"
                    f"已经有一条「{label}」关系了。",
                )
            ]
        if key in self.rel_keys:
            return [
                _err(
                    ordinal,
                    RELATION_ALREADY_EXISTS,
                    f"这条「{label}」关系在同一次提案里写了两遍。",
                )
            ]

        self.rel_keys.add(key)
        self.rel_add.append(
            PlannedRelation(
                source_id=first,
                target_id=second,
                relation_type=action.relation_type,
                note=(action.note or None),
                action=action,
            )
        )
        self.items.append(
            ValidatedItem(
                ordinal=ordinal,
                op=action.op,
                summary=_relation_summary(
                    action.relation_type, self.title_of(first), self.title_of(second)
                ),
                payload=payload,
            )
        )
        return []

    # -- 范围 ------------------------------------------------------------------
    def _out_of_scope(self, ordinal: int, node_id: uuid.UUID, ref: str) -> ActionError | None:
        """这个节点在不在"这一轮能改的"里面。不在就返回一条错误,在就返回 None。

        消息里同时给记号(`targetRef` 里的那个)和标题:用户要判断的是"它说的哪个节点",
        而他看到的是画布上的标题,不是记号。
        """
        if self.writable is None or node_id in self.writable:
            return None
        title = self.title_of(node_id) if node_id in self.nodes else ref
        return _err(
            ordinal,
            OUT_OF_SCOPE,
            f"「{title}」在当前范围内是只读的,这一条没有执行。"
            "只能改你正在看的这一片 —— 要动它,先进入它所在的层级再说。",
        )

    def _pair_out_of_scope(
        self,
        ordinal: int,
        pair: tuple[uuid.UUID, uuid.UUID],
        predecessor_ref: str,
        successor_ref: str,
    ) -> ActionError | None:
        """关系是**两端**的事:有一端在范围外,改的就是范围外那个节点的处境。"""
        for ref, node_id in ((predecessor_ref, pair[0]), (successor_ref, pair[1])):
            # 同一条提案里刚建出来的节点不用查:它的父节点已经查过了,范围往下是封闭的。
            if node_id in self._planned_ids():
                continue
            blocked = self._out_of_scope(ordinal, node_id, ref)
            if blocked is not None:
                return blocked
        return None

    # -- 辅助 ------------------------------------------------------------------
    def _resolve_pair(
        self,
        ordinal: int,
        predecessor_ref: str,
        successor_ref: str,
        *,
        subject: str = "前置关系里",
    ) -> tuple[uuid.UUID, uuid.UUID] | list[ActionError]:
        """把两个记号解析成一对真实 id。解析不了就返回错误列表(用类型区分两种返回)。

        这里返回"联合类型"而不是抛异常,是因为调用方本来就要把这个错误放进
        `errors` 里继续校验后面的条目 —— 抛异常会让"第一条错了"变成"后面的都不看了",
        而用户需要一次看到全部问题,否则他要改一轮、再点一次、再看新的一轮错误。
        """
        resolved: dict[str, uuid.UUID] = {}
        for label, ref in (("前一个", predecessor_ref), ("后一个", successor_ref)):
            node_id = self.resolved.get(ref)
            if node_id is None or (node_id not in self.nodes and node_id not in self._planned_ids()):
                return [
                    _err(
                        ordinal,
                        DANGLING_PROPOSAL_REF,
                        f"{subject}{label}节点 {ref} 不存在。",
                    )
                ]
            resolved[ref] = node_id
        return (resolved[predecessor_ref], resolved[successor_ref])

    def _planned_ids(self) -> set[uuid.UUID]:
        return {planned.id for planned in self.creates.values()}

    def _depth_of(self, node_id: uuid.UUID) -> int:
        for planned in self.creates.values():
            if planned.id == node_id:
                return planned.depth
        snapshot = self.nodes.get(node_id)
        return snapshot.depth if snapshot else 0

    def _purpose_of(self, node_id: uuid.UUID) -> str:
        """一个节点的用途 —— **同一条提案里刚建的节点也要算**。

        只看 `self.nodes` 会漏掉 `create_node` 刚建出来、同一条里又要给它连关系的节点,
        而那种节点是一个完全合法的依赖端点(只要它是 planning)。
        """
        for planned in self.creates.values():
            if planned.id == node_id:
                return planned.action.purpose.value
        snapshot = self.nodes.get(node_id)
        return snapshot.purpose if snapshot else NodePurpose.PLANNING.value

    # -- 规划层级 ------------------------------------------------------------
    def _level_of(self, node_id: uuid.UUID) -> str | None:
        """一个节点的层级,同一条提案里刚建的节点也算。"""
        for planned in self.creates.values():
            if planned.id == node_id:
                level = planned.action.planning_level
                return level.value if level is not None else None
        snapshot = self.nodes.get(node_id)
        return snapshot.planning_level if snapshot else None

    def _has_strategy_ancestor(self, node_id: uuid.UUID) -> bool:
        """从 `node_id`(含自己)沿父链上找,有没有一个**已存在**的 strategy 节点。

        **只认 `self.nodes`(库里已有的)。** 计划中刚建的节点不算 —— 战略必须先被
        用户确认,而确认发生在提案通过之后。所以一条"同一个提案里先建战略、再建周"
        的动作里,那个周找不到已确认的战略祖先,会被拒。
        """
        seen: set[uuid.UUID] = set()
        current: uuid.UUID | None = node_id
        while current is not None and current not in seen:
            seen.add(current)
            snapshot = self.nodes.get(current)
            if snapshot is None:
                return False
            if snapshot.planning_level == PlanningLevel.STRATEGY.value:
                return True
            current = snapshot.parent_id
        return False

    def _create_level_error(
        self, ordinal: int, action: CreateNodeAction, parent_id: uuid.UUID
    ) -> ActionError | None:
        """新建一个带层级的节点时的全部层级规则。"""
        level = action.planning_level
        if level is None:
            return None
        # 1) 父子层级只能从粗到细(允许跳过中间层;None 与任何层级相容)。
        parent_conflict = child_level_conflicts(self._level_of(parent_id), level)
        if parent_conflict is not None:
            return _err(ordinal, PLANNING_LEVEL_INVALID, parent_conflict)
        # 2) strategy 本身不受"必须有已确认战略"的约束。
        if level is PlanningLevel.STRATEGY:
            return None
        # 3) 没有已确认战略时,只能提战略选择,不能下钻。
        if not self.strategy_exists:
            return _err(
                ordinal,
                STRATEGY_NOT_CONFIRMED,
                "还没有已确认的战略。先把战略选择提出来、让用户确认,再往下拆"
                "阶段/月/周/日。",
            )
        # 4) 更细的层级必须挂在一个已确认的战略或它下面的节点上。
        if not self._has_strategy_ancestor(parent_id):
            return _err(
                ordinal,
                STRATEGY_NOT_CONFIRMED,
                "这一层计划要挂在一个已确认的战略(或它下面的节点)上,不能凭空新建。",
            )
        return None

    def _update_level_error(
        self, ordinal: int, node_id: uuid.UUID, level: PlanningLevel | None
    ) -> ActionError | None:
        """把一个已有节点改成某个层级时的全部层级规则。"""
        if level is None:
            return None
        snapshot = self.nodes.get(node_id)
        parent_level = (
            self._level_of(snapshot.parent_id)
            if snapshot is not None and snapshot.parent_id is not None
            else None
        )
        parent_conflict = child_level_conflicts(parent_level, level)
        if parent_conflict is not None:
            return _err(ordinal, PLANNING_LEVEL_INVALID, parent_conflict)
        for child in self.nodes.values():
            if child.parent_id != node_id:
                continue
            child_conflict = child_level_conflicts(level, child.planning_level)
            if child_conflict is not None:
                return _err(ordinal, PLANNING_LEVEL_INVALID, child_conflict)
        if level is PlanningLevel.STRATEGY:
            return None
        if not self.strategy_exists:
            return _err(
                ordinal,
                STRATEGY_NOT_CONFIRMED,
                "还没有已确认的战略。先把战略选择提出来、让用户确认,再往下拆。",
            )
        if not self._has_strategy_ancestor(node_id):
            return _err(
                ordinal,
                STRATEGY_NOT_CONFIRMED,
                "这个节点不在任何一个已确认的战略下面,不能直接改成一个更细的层级。",
            )
        return None

    def _descendants(self, root_id: uuid.UUID) -> list[uuid.UUID]:
        """收集一棵子树的全部节点(含根自己),父节点一定排在自己的子节点之前。

        用的是显式栈而不是递归 —— 理由和 `_find_cycle` 一样:深树会在 Python 的
        默认递归深度上踩线,而那个报错和"子节点收集"看起来毫无关系。

        `ordered[0]` 恒等于 `root_id`,调用方靠它把"连带删掉几个"算成
        `len(ordered) - 1`。
        """
        children: dict[uuid.UUID, list[uuid.UUID]] = {}
        for node in self.nodes.values():
            if node.parent_id is not None:
                children.setdefault(node.parent_id, []).append(node.id)

        ordered: list[uuid.UUID] = []
        pending = [root_id]
        while pending:
            current = pending.pop(0)  # 队列式取出 -> 父先子后
            ordered.append(current)
            pending.extend(children.get(current, []))
        return ordered

    # -- 成环检测 --------------------------------------------------------------
    def detect_cycles(self) -> list[ActionError]:
        """在合并后的依赖图上找环:已有的边 - 取消的边 + 新增的边。"""
        edges: set[tuple[uuid.UUID, uuid.UUID]] = set(self.existing_dependencies)
        edges.difference_update(self.dep_remove)
        edges.update(self.dep_add)

        removed = set(self.deletes)
        edges = {(a, b) for a, b in edges if a not in removed and b not in removed}

        cycle = find_cycle(edges)
        if cycle is None:
            return []

        path = " → ".join(f"「{self.title_of(node)}」" for node in cycle)
        return [
            ActionError(
                ordinal=0,
                code=DEPENDENCY_CYCLE,
                message=f"这些前置关系绕成环了:{path}。环里的任务谁都开始不了。",
            )
        ]

    # -- 收尾 ------------------------------------------------------------------
    def to_plan(self) -> ValidatedPlan:
        return ValidatedPlan(
            # 按处理顺序建:父节点一定排在自己的子节点前面(只允许向后引用),
            # 所以入库顺序天然满足外键。
            creates=tuple(self.creates.values()),
            updates=tuple(self.updates),
            deletes=tuple(self.deletes),
            notes=tuple(self.notes),
            dependencies_add=tuple(self.dep_add),
            dependencies_remove=tuple(self.dep_remove),
            relations=tuple(self.rel_add),
            items=tuple(self.items),
        )


#: `order_index` 的哨兵。根节点没有父节点,但它也有兄弟(理论上),用一个不可能
#: 与真实 uuid 相撞的键把它和"没有父节点的那些"归到一组。
_NO_PARENT = uuid.UUID(int=0)


# ---------------------------------------------------------------------------------
# 环检测
# ---------------------------------------------------------------------------------
def find_cycle(edges: set[tuple[uuid.UUID, uuid.UUID]]) -> list[uuid.UUID] | None:
    """返回一个环上的节点序列。没有环返回 None。

    用显式栈的迭代式 DFS,不用递归:一份计划几百个节点时递归会在 Python 的默认
    递归深度上踩线,而那个报错(RecursionError)看起来和"依赖成环"毫无关系。

    **不以下划线开头,因为它被 node_service 复用。** 用户手工加一条依赖和 AI 提一条
    依赖是同一件事,查环的规则必须只有一份 —— 复制一份过去的话,其中一份修了 bug
    另一份没修,表现是"AI 加的依赖会成环,我自己加的不会"。
    """
    adjacency: dict[uuid.UUID, list[uuid.UUID]] = {}
    for source, target in edges:
        adjacency.setdefault(source, []).append(target)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[uuid.UUID, int] = {}
    parent: dict[uuid.UUID, uuid.UUID] = {}

    for start in list(adjacency):
        if color.get(start, WHITE) != WHITE:
            continue
        stack: list[tuple[uuid.UUID, int]] = [(start, 0)]
        color[start] = GRAY
        while stack:
            node, index = stack[-1]
            neighbours = adjacency.get(node, [])
            if index >= len(neighbours):
                color[node] = BLACK
                stack.pop()
                continue
            stack[-1] = (node, index + 1)
            neighbour = neighbours[index]
            state = color.get(neighbour, WHITE)
            if state == GRAY:
                # 回到了一条还在栈上的边 -> 找到了环。沿着 parent 链回溯出这条环。
                path = [neighbour, node]
                while path[-1] != neighbour and path[-1] in parent:
                    path.append(parent[path[-1]])
                path.reverse()
                return path
            if state == WHITE:
                parent[neighbour] = node
                color[neighbour] = GRAY
                stack.append((neighbour, 0))
    return None


# ---------------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------------
def _purpose_label(purpose: object) -> str:
    """用途的中文名。**空字符串表示"规划用途"** —— 它是默认值,印出来是噪音。

    用在重复拒绝的消息里:"它已经存在了(是信息用途的)"。少了这半截,用户不知道
    该去改哪一个,而两个同名节点摆在一起时他本来就分不清。
    """
    return "信息" if getattr(purpose, "value", purpose) == NodePurpose.INFORMATION.value else ""


def _create_summary(action: CreateNodeAction, coalesced_from: str | None = None) -> str:
    """新建一条的预览文案。

    信息用途单独印成「新建信息主题「学业情况」」而不是「新建阶段「学业情况」」:
    这两件事在用户那边的后续完全不同(一个进排期、一个只是记事),而 `node_type`
    是 `capability` 时会读成"能力",更看不出它其实是信息。
    """
    if getattr(action.purpose, "value", action.purpose) == NodePurpose.INFORMATION.value:
        head = f"新建信息主题「{action.title}」"
    elif action.planning_level is PlanningLevel.STRATEGY:
        head = f"建立战略选择「{action.title}」"
    elif action.planning_level is PlanningLevel.PHASE:
        head = f"新增阶段重点「{action.title}」"
    elif action.planning_level is PlanningLevel.MONTH:
        head = f"新增本月重点「{action.title}」"
    elif action.planning_level is PlanningLevel.WEEK:
        head = f"新增本周重点「{action.title}」"
    elif action.planning_level is PlanningLevel.DAY:
        head = f"新增今日行动「{action.title}」"
    else:
        head = f"新建{TYPE_LABELS.get(action.node_type.value, action.node_type.value)}「{action.title}」"
    bits = [head]
    if action.estimate_minutes:
        bits.append(f"预计 {action.estimate_minutes} 分钟")
    if action.deadline:
        bits.append(f"截止 {action.deadline.isoformat()}")
    if coalesced_from:
        # 模型想新建的是**已经存在**的那个主题,于是这一条会被并入它。预览必须
        # 说出来 —— 不说的话用户确认的是一件他没看过的事(§7.2)。
        bits.append(f"已并入同名的「{coalesced_from}」")
    return " · ".join(bits)


def _update_summary(
    action: UpdateNodeAction,
    title: str,
    changed: frozenset[str],
    coalesced_from: str | None = None,
) -> str:
    labels = [_FIELD_LABELS.get(field, field) for field in sorted(changed)]
    if coalesced_from:
        return f"补充已有节点「{coalesced_from}」的{'、'.join(labels)}"
    return f"修改「{title}」的{'、'.join(labels)}"


def _note_summary(action: UpdateNoteAction, title: str, chars: int) -> str:
    return f"把 {chars} 字写进「{title}」的长笔记"


def _relation_label(relation_type: NodeRelationType) -> str:
    return "影响" if relation_type is NodeRelationType.INFLUENCES else "相关"


def _relation_summary(
    relation_type: NodeRelationType, first_title: str, second_title: str
) -> str:
    """一条关系在预览里的中文。**方向要读得出来** —— `influences` 是有向的,
    写成"A 与 B 相关"会让用户以为反方向也一样,而反方向是另一条边。"""
    if relation_type is NodeRelationType.INFLUENCES:
        return f"建立影响关系:「{first_title}」会影响「{second_title}」"
    return f"建立相关关系:「{first_title}」与「{second_title}」"


def _delete_summary(title: str, subtree_size: int) -> str:
    children = subtree_size - 1
    if children <= 0:
        return f"删除「{title}」"
    return f"删除「{title}」及其下 {children} 个节点"


def _err(ordinal: int, code: str, message: str) -> ActionError:
    return ActionError(ordinal=ordinal, code=code, message=message)


def _describe(exc: ValidationError) -> str:
    """把 pydantic 的英文报错翻译成一句能读的中文。

    不直接把 `exc.errors()` 丢给用户,是因为那些消息里带着 `Input should be a valid
    date, input was '2026-13-45'` 这类内部措辞。用户要判断的是"AI 提的东西哪儿不对",
    给他一段英文 pydantic 报错,他只能猜。
    """
    first = exc.errors()[0] if exc.errors() else {}
    location = [str(part) for part in first.get("loc", ()) if part != "__root__"]
    raw_field = ".".join(location) or "这一条"
    label = _FIELD_LABELS.get(raw_field, raw_field)
    kind = str(first.get("type", ""))

    if kind == "extra_forbidden":
        return f"{label}不是能识别的字段。"
    if kind == "missing":
        return f"{label}没有填。"
    if kind.startswith("literal_error"):
        return f"{label}的取值不在允许范围内。"
    if "date" in kind:
        return f"{label}不是合法的日期(要写成 YYYY-MM-DD)。"
    if kind in {"greater_than", "greater_than_equal", "less_than", "less_than_equal"}:
        return f"{label}超出了允许范围。"
    if kind == "string_too_long":
        # 把上限说出来。pydantic 的 `max_length` 对 `str` 数的是 Python 的 `len()`,
        # 也就是**码点数** —— 与 `MAX_DESCRIPTION_CODEPOINTS` 那条规则是同一个定义,
        # 所以这句"按 Unicode 码点计"不是修辞。
        limit = (first.get("ctx") or {}).get("max_length")
        if isinstance(limit, int):
            return f"{label}最多 {limit} 个字(按 Unicode 码点计)。"
    if kind in {"string_too_short", "string_too_long"}:
        return f"{label}的长度不合适。"
    if kind == "union_tag_invalid":
        return "这条变更的 op 不认识。"
    return f"{label}:{first.get('msg', '格式不正确')}"


__all__ = [
    "CANNOT_DELETE_ROOT",
    "CONFLICTING_OPERATIONS",
    "DANGLING_PROPOSAL_REF",
    "DEADLINE_IN_PAST",
    "DEPENDENCY_ALREADY_EXISTS",
    "DEPENDENCY_CYCLE",
    "DEPENDENCY_NOT_FOUND",
    "DUPLICATE_LOCAL_ID",
    "DUPLICATE_NODE_TITLE",
    "INFORMATION_NODE_MUST_NOT_BE_SCHEDULABLE",
    "INFORMATION_NODE_NOT_DEPENDABLE",
    "LOCAL_ID_CONFLICT",
    "NODE_NOT_IN_WORKSPACE",
    "NOTE_CHANGED",
    "OP_NOT_YET_AVAILABLE",
    "OUT_OF_SCOPE",
    "PAYLOAD_SCHEMA_INVALID",
    "PLANNING_LEVEL_INVALID",
    "RELATION_ALREADY_EXISTS",
    "SELF_DEPENDENCY",
    "SELF_RELATION",
    "STRATEGY_NOT_CONFIRMED",
    "TOO_MANY_ACTIONS",
    "UNKNOWN_OP_TYPE",
    "NodePatch",
    "NodeSnapshot",
    "NotePatch",
    "PlannedNode",
    "PlannedRelation",
    "ValidatedItem",
    "ValidatedPlan",
    "ValidationResult",
    "canonical_relation_key",
    "coalesce_creates",
    "find_cycle",
    "normalize_title",
    "validate_actions",
]
