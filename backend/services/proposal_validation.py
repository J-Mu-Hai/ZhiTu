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

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from pydantic import ValidationError

from backend.contracts.proposal import (
    ACCEPTED_OPS,
    DEFERRED_OPS,
    MAX_ACTIONS,
    MAX_CREATED_NODES,
    ActionError,
    CreateDependencyAction,
    CreateNodeAction,
    DeleteDependencyAction,
    DeleteNodeAction,
    UpdateNodeAction,
    action_adapter,
)
from backend.db.models.enums import NodeType

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
}


# ---------------------------------------------------------------------------------
# 输入快照
# ---------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """一个**当前真实存在**的节点(软删除的不在其中)。

    只带校验需要的字段。刻意不是 ORM 对象:传 ORM 对象进来会让这个模块意外地
    依赖会话状态,而它的全部价值就在于"不依赖任何会变的东西"。
    """

    id: uuid.UUID
    parent_id: uuid.UUID | None
    title: str
    depth: int
    order_index: int


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


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    """可以落地的完整变更集。"""

    creates: tuple[PlannedNode, ...] = ()
    updates: tuple[NodePatch, ...] = ()
    #: 软删除的节点 id,**已包含被连带删除的全部子节点**。
    deletes: tuple[uuid.UUID, ...] = ()
    dependencies_add: tuple[tuple[uuid.UUID, uuid.UUID], ...] = ()
    dependencies_remove: tuple[tuple[uuid.UUID, uuid.UUID], ...] = ()
    items: tuple[ValidatedItem, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (
            self.creates or self.updates or self.deletes or self.dependencies_add
            or self.dependencies_remove
        )


@dataclass(frozen=True, slots=True)
class ValidationResult:
    plan: ValidatedPlan | None
    errors: tuple[ActionError, ...]

    @property
    def ok(self) -> bool:
        return self.plan is not None


# ---------------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------------
def validate_actions(
    actions: Sequence[Mapping[str, object]],
    *,
    handles: Mapping[str, uuid.UUID],
    nodes: Mapping[uuid.UUID, NodeSnapshot],
    dependencies: Iterable[tuple[uuid.UUID, uuid.UUID]] = (),
    today: date,
    writable: Iterable[uuid.UUID] | None = None,
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
    """
    if len(actions) > MAX_ACTIONS:
        return _fail(
            ActionError(
                ordinal=0,
                code=TOO_MANY_ACTIONS,
                message=f"这一次的变更太多了({len(actions)} 条),最多 {MAX_ACTIONS} 条。",
            )
        )

    book = _Book(
        handles=handles,
        nodes=nodes,
        # 软删除不会级联删除依赖行,所以库里天然躺着一些指向已删节点的边。
        # 在这里一次性滤掉,后面"这条关系存不存在"与成环检测用的就是同一份基准。
        dependencies={
            (a, b) for a, b in dependencies if a in nodes and b in nodes
        },
        today=today,
        writable=None if writable is None else frozenset(writable),
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
    ) -> None:
        self.today = today
        self.handles = dict(handles)
        self.nodes = dict(nodes)
        self.existing_dependencies = dependencies
        #: None = 不设范围限制。见 `validate_actions` 的注释。
        self.writable = writable

        #: 记号 -> 真实 id。开始是已有的句柄,随着 create 逐条长大。
        #: **只增不改**:一个记号一旦绑定就不能再指向别的东西。
        self.resolved: dict[str, uuid.UUID] = dict(handles)
        self.creates: dict[str, PlannedNode] = {}
        self.updates: list[NodePatch] = []
        self.deletes: list[uuid.UUID] = []
        self.dep_add: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.dep_remove: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.items: list[ValidatedItem] = []

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
        return not (self.creates or self.updates or self.deletes or self.dep_add or self.dep_remove)

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

        try:
            action = action_adapter.validate_python(raw)
        except ValidationError as exc:
            return [_err(ordinal, PAYLOAD_SCHEMA_INVALID, _describe(exc))]

        #: 存进提案的是**模型原样的那一段 JSON**,不是校验后重新 dump 出来的。
        #:
        #: 这个区别很关键:`update_node` 的"改了哪些字段"是靠 pydantic 的
        #: `model_fields_set` 判断的,而重新 dump 会把每个字段都写出来(没写的
        #: 变成 null),于是"只改截止时间"到了确认那一步会变成"把所有没提过的
        #: 字段一起清空" —— 用户确认的是改一个日期,实际发生的是删掉了他的说明。
        #: 原样存下来,预览与执行读的就是同一份输入,不可能分叉。
        payload = dict(raw)

        if isinstance(action, CreateNodeAction):
            return self._create(ordinal, action, payload)
        if isinstance(action, UpdateNodeAction):
            return self._update(ordinal, action, payload)
        if isinstance(action, DeleteNodeAction):
            return self._delete(ordinal, action, payload)
        if isinstance(action, CreateDependencyAction):
            return self._add_dependency(ordinal, action, payload)
        if isinstance(action, DeleteDependencyAction):
            return self._remove_dependency(ordinal, action, payload)
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
                summary=_create_summary(action),
                local_id=action.local_id,
                payload=payload,
            )
        )
        return []

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

        self.touched[node_id] = _TOUCH_UPDATE
        self.updates.append(NodePatch(node_id=node_id, changed_fields=changed, action=action))
        self.items.append(
            ValidatedItem(
                ordinal=ordinal,
                op=action.op,
                summary=_update_summary(action, self.title_of(node_id), changed),
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
        self, ordinal: int, predecessor_ref: str, successor_ref: str
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
                        f"前置关系里{label}节点 {ref} 不存在。",
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
            dependencies_add=tuple(self.dep_add),
            dependencies_remove=tuple(self.dep_remove),
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
def _create_summary(action: CreateNodeAction) -> str:
    bits = [f"新建{TYPE_LABELS.get(action.node_type.value, action.node_type.value)}「{action.title}」"]
    if action.estimate_minutes:
        bits.append(f"预计 {action.estimate_minutes} 分钟")
    if action.deadline:
        bits.append(f"截止 {action.deadline.isoformat()}")
    return " · ".join(bits)


def _update_summary(action: UpdateNodeAction, title: str, changed: frozenset[str]) -> str:
    labels = [_FIELD_LABELS.get(field, field) for field in sorted(changed)]
    return f"修改「{title}」的{'、'.join(labels)}"


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
    if kind in {"string_too_short", "string_too_long"}:
        return f"{label}的长度不合适。"
    if kind == "union_tag_invalid":
        return "这条变更的 op 不认识。"
    return f"{label}:{first.get('msg', '格式不正确')}"


__all__ = [
    "CANNOT_DELETE_ROOT",
    "CONFLICTING_OPERATIONS",
    "OUT_OF_SCOPE",
    "DANGLING_PROPOSAL_REF",
    "DEADLINE_IN_PAST",
    "DEPENDENCY_ALREADY_EXISTS",
    "DEPENDENCY_CYCLE",
    "DEPENDENCY_NOT_FOUND",
    "DUPLICATE_LOCAL_ID",
    "LOCAL_ID_CONFLICT",
    "NODE_NOT_IN_WORKSPACE",
    "OP_NOT_YET_AVAILABLE",
    "PAYLOAD_SCHEMA_INVALID",
    "SELF_DEPENDENCY",
    "TOO_MANY_ACTIONS",
    "UNKNOWN_OP_TYPE",
    "NodePatch",
    "NodeSnapshot",
    "PlannedNode",
    "ValidatedItem",
    "ValidatedPlan",
    "ValidationResult",
    "find_cycle",
    "validate_actions",
]
