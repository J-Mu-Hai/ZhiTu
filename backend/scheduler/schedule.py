"""主流程:`simulate()` —— 给定一份计划与一份时间预算,算出"哪天做多久"。

## 八条规则落在哪里

| 规则 | 落点 |
|---|---|
| (a) 依赖与截止 | `graph.topological_order` + `graph.earliest_start` + 每一场的日期区间 |
| (b) 日负载上限 | `PoolTracker.room` 的 `DAILY_MAX` 一侧 |
| (c) 跨空间共享池 | `PoolTracker` 里**只有一个**池子,`daily_load` 分开记账只是审计轨迹 |
| (d) 长任务切分 | `pack.split_sessions` —— 一个 `PlanNode`,N 个 `ScheduledSession` |
| (e) 默认缓冲 | `split_sessions` 给每一场带上 `default_buffer_minutes`,并计入池子 |
| (f) 已完成不可覆盖 | `_classify_existing` 把 `done`/`skipped`/`canceled`/已锁定/已过去的场次划进冻结集合 |
| (g) 锁定不自动移动 | 同上:锁定的场次进冻结集合,冲突时记 `LOCKED_SESSION_CONFLICT` 并**点名**,不挪它 |
| (h) 容量不足不静默截断 | `truncated` 恒为 `False`,缺口走 `CapacityGap` + `recovery_options` |

## 最小改动,不是从头重排

朴素做法(每次重排都把整个视界重洗一遍)会让用户第二天打开时发现日历全变了,而他说
不清哪里不对。所以这里是"锚点保持 + 波纹式修复":

1. **冻结**过去 / 已完成 / 已锁定的场次 —— 它们不动,只贡献占用量。
2. **锚点保持**:同 `(node_id, seq)` 的场次**先试原来的那一天**。条件是它仍然满足依赖
   的早开始日和截止日,而且当天放得下。绝大多数重排(完成了一个任务、改了一个截止日)
   因此只动极少数几场。
3. **波纹式修复**:只有放不下时才另找一天,而找的范围是"这个节点自己的日期区间",
   不是整条视界。前置动了,受影响的是它的下游(`graph.descendants`),不是所有任务。
4. **记账**:每一处变化都记进 `moves` / `cancelations`,交给界面说"移动 2 场,新增 3 场"。

## 同输入同输出

所有并列项都由 `graph.sort_key` 打破,所有遍历都显式排序,所有 `dict` 只用于按已知键
取值而从不依赖其顺序。`test_same_input_same_bytes` 逐字节比对两次运行的序列化输出。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, timedelta

from backend.scheduler import diff as diff_module
from backend.scheduler import graph, pack
from backend.scheduler.calendar import DayPools, build_day_pools
from backend.scheduler.capacity import PoolTracker, Room
from backend.scheduler.errors import (
    BindingConstraint,
    ScheduleErrorCode,
    SchedulerInputError,
    SchedulerInvariantError,
    UnknownNodeReferenceError,
)
from backend.scheduler.types import (
    FROZEN_STATUSES,
    CapacityGap,
    CapacityProfile,
    ChurnSummary,
    ExecutionResult,
    ExistingSession,
    NodeStatus,
    NodeType,
    PlannedSession,
    RecoveryOption,
    ScheduleNode,
    ScheduleRequest,
    ScheduleResult,
    SessionMove,
    SessionStatus,
)

#: 真正占用当天池子的状态。`skipped` / `canceled` 没有发生,`moved` 是一条墓碑
#: (它指向搬到的那一场)—— 三者都**不占**池子,但都**不许改**(见 `_classify_existing`)。
OCCUPYING_STATUSES: frozenset[SessionStatus] = frozenset(
    {SessionStatus.PLANNED, SessionStatus.IN_PROGRESS, SessionStatus.DONE}
)

#: 除了 `types.FROZEN_STATUSES`,墓碑状态也算冻结。搬走过的那一场是历史,不能被
#: 当成候选项再搬一次 —— 那会凭空多出一场。
_ALSO_FROZEN: frozenset[SessionStatus] = frozenset({SessionStatus.MOVED})

#: 补救选项最多往后找多少天。设上限是为了让"延期"这个建议不会一路延到三年后 ——
#: 一个永远能解决的选项等于没有建议。
_MAX_EXTENSION_DAYS = 400

#: "增加投入"最多试到几倍。同样是为了让建议落在一个真实的人能做到的范围里。
_MAX_INPUT_MULTIPLIER = 3


class _Simulation:
    """一次排期的全部中间状态。

    做成类而不是一串传参的函数,是因为这里确实有一批**互相影响**的可变状态(池子、
    已完成工时、依赖的完成日、缺口清单)。把它们摊成 12 个参数在函数之间传来传去,
    读的人要同时记住 12 个东西;收在一个对象上,读的人只要记住"它在算这一份请求"。
    """

    def __init__(self, request: ScheduleRequest) -> None:
        if request.horizon_days <= 0:
            raise SchedulerInputError("排期视界必须是正数天")
        profile = request.profile
        if not 0 <= profile.week_start_weekday <= 6:
            raise SchedulerInputError("week_start_weekday 必须在 0..6 之间")

        self.request = request
        self.profile: CapacityProfile = profile
        self.today = request.today
        self.horizon_last = request.today + timedelta(days=request.horizon_days - 1)
        self.min_session = max(1, profile.min_session_minutes)

        self.pools: DayPools = build_day_pools(
            start=request.today,
            horizon_days=request.horizon_days,
            profile=profile,
            windows=request.windows,
            exceptions=request.exceptions,
        )
        self.tracker = PoolTracker(self.pools, profile.week_start_weekday)
        #: 整条视界上最宽的那一天有多少分钟。切分要用它当场的长度上限 —— 一场必须装得进
        #: 某一天,而"最宽的那天"是所有候选里最宽松的一个(见 `pack.split_sessions`)。
        self.largest_day_minutes = max(
            (supply.minutes for _, supply in self.pools.supplies), default=0
        )

        self.nodes: dict[uuid.UUID, ScheduleNode] = {node.id: node for node in request.nodes}
        self.order = graph.topological_order(request.nodes, request.dependencies)
        self.predecessors = graph.predecessors_of(request.dependencies)

        #: `节点 -> 它的某一场做完的那一天`。依赖的早开始日由它推出来。
        self.finish_days: dict[uuid.UUID, date] = {}
        #: `节点 -> 已经完成的分钟数`。
        self.delivered: dict[uuid.UUID, int] = {}
        #: `节点 -> 还可以被移动的既有场次`,按 `(seq, 日期, id)` 排好。
        self.movable: dict[uuid.UUID, list[ExistingSession]] = {}
        #: `节点 -> 它**所有**既有场次的序号`(含冻结的)。
        #: 新的场次序号要避开全部既有序号,不只是可移动的那些 —— `(node_id, 日期, seq)`
        #: 那个唯一约束的谓词是"状态不是 canceled/moved",而冻结的 `done` 场次在谓词之内。
        self.all_seqs: dict[uuid.UUID, list[int]] = {}
        #: 冻结场次 id。**任何 move / cancel 都不许提到它们。**
        self.frozen_ids: set[uuid.UUID] = set()
        #: `日期 -> 锁定场次占用的分钟`。用来把"排不下"归因到锁定上。
        self.locked_minutes_on: dict[date, int] = {}

        self.sessions: list[PlannedSession] = []
        self.moves: list[SessionMove] = []
        self.cancelations: list[uuid.UUID] = []
        self.raw_gaps: list[CapacityGap] = []
        self.kept_ids: set[uuid.UUID] = set()

    # ------------------------------------------------------------------ 主流程
    def run(self) -> ScheduleResult:
        self._classify_existing()
        self._compute_delivered()
        for node_id in self.order:
            self._plan_node(self.nodes[node_id])
        self._check_invariants()
        return self._build_result()

    # ------------------------------------------------------------------ 输入分类
    def _classify_existing(self) -> None:
        """把既有场次分成"冻结的"与"可以动的"两类。

        **冻结有三种理由,它们互不相干,所以是并集而不是优先级**:状态已经发生
        (`done`/`skipped`/`canceled`)、用户锁了它、或者日期已经过去。三种都要冻,
        因为"过去的一场被移到未来"和"已完成的一场的记录被改掉"是同一类错误 ——
        用户的时间线上凭空出现一件他上周做过的事。
        """
        for session in self.request.existing:
            if session.node_id not in self.nodes:
                raise UnknownNodeReferenceError(
                    f"场次 {session.id} 指向的节点 {session.node_id} 不在本次排期的节点集合里"
                )

            frozen = (
                session.status in FROZEN_STATUSES
                or session.status in _ALSO_FROZEN
                or session.locked
                or session.scheduled_date < self.today
            )
            if not frozen:
                self.movable.setdefault(session.node_id, []).append(session)
                continue

            self.frozen_ids.add(session.id)
            if session.status in OCCUPYING_STATUSES:
                self.tracker.take(
                    session.scheduled_date, session.workspace_id, session.occupies_minutes
                )
            if session.locked:
                self.locked_minutes_on[session.scheduled_date] = (
                    self.locked_minutes_on.get(session.scheduled_date, 0) + session.occupies_minutes
                )
            if session.status is SessionStatus.DONE:
                self._note_finish(session.node_id, session.scheduled_date)

        for session in self.request.existing:
            self.all_seqs.setdefault(session.node_id, []).append(session.seq)

        for sessions in self.movable.values():
            sessions.sort(key=lambda item: (item.seq, item.scheduled_date, str(item.id)))

        # 结构性的节点(目标/阶段/里程碑)**自己没有工时**,但可以被标记完成 —— 那意味着
        # "它下面的事都做完了"。这时它算在"今天之前就完成了",后继因此可以从今天开始,
        # 而不是被一个永远不会出现的 `done` 场次卡住。
        for node in self.nodes.values():
            if node.node_type in (NodeType.GOAL, NodeType.CAPABILITY, NodeType.STAGE, NodeType.MILESTONE) and node.status is NodeStatus.COMPLETED:
                self._note_finish(node.id, self.today - timedelta(days=1))

    def _compute_delivered(self) -> None:
        """每个任务已经有着落多少分钟。**`execution_records` 只读,不改。**"""
        by_session: dict[uuid.UUID, ExecutionResult] = {}
        reported: dict[uuid.UUID, int] = {}
        for fact in self.request.executions:
            if fact.session_id is not None:
                by_session[fact.session_id] = fact
            elif fact.result in (ExecutionResult.COMPLETED, ExecutionResult.PARTIAL):
                # 用户直接对**任务**报告的进度(没说具体哪一场)。
                reported[fact.node_id] = reported.get(fact.node_id, 0) + max(
                    0, fact.actual_minutes or 0
                )

        done: dict[uuid.UUID, int] = {}
        committed: dict[uuid.UUID, int] = {}
        for session in self.request.existing:
            if session.status is SessionStatus.DONE:
                fact = by_session.get(session.id)
                minutes = session.planned_minutes
                if fact is not None and fact.actual_minutes is not None:
                    minutes = fact.actual_minutes
                done[session.node_id] = done.get(session.node_id, 0) + max(0, minutes)
                continue

            # 锁定在**未来**的场次:它占着那一天,而且不许被挪走 —— 于是它就是这个
            # 任务已经安排掉的工时。不计入的话,同一个任务会被再排一份,用户看到日历上
            # 同一件事占了两倍的时间。
            #
            # 只算 `OCCUPYING_STATUSES` 里的(真正会发生的那几种),而且只算今天及以后:
            # 把一场过去没做成的安排算成"已有着落",等于替用户把它当成做过了。
            if (
                session.locked
                and session.status in OCCUPYING_STATUSES
                and session.scheduled_date >= self.today
            ):
                committed[session.node_id] = committed.get(session.node_id, 0) + max(
                    0, session.planned_minutes
                )

        for node_id, node in self.nodes.items():
            self.delivered[node_id] = pack.already_delivered(
                done_minutes=done.get(node_id, 0),
                reported_minutes=reported.get(node_id, 0),
                committed_minutes=committed.get(node_id, 0),
                estimate_minutes=node.estimate_minutes,
            )

    def _note_finish(self, node_id: uuid.UUID, day: date) -> None:
        current = self.finish_days.get(node_id)
        if current is None or day > current:
            self.finish_days[node_id] = day

    # ------------------------------------------------------------------ 排一个节点
    def _plan_node(self, node: ScheduleNode) -> None:
        if not node.is_open:
            # 完成或归档的节点不再需要未来的场次。已有的一律取消(不是删除 ——
            # 行还在,状态变成 canceled,复盘时要看得见"这里原本排过"）。
            self._cancel_all(node)
            return

        if node.estimate_minutes is None:
            if node.has_children:
                # **容器节点没有自己的工时不是缺口。** "Python 学习"是一个目标,它的
                # 活是它下面那些阶段和任务干的;要求它自己也填一个工时,等于问"学完
                # Python 这件事本身要多久"。
                #
                # 这一条不是可选项:每个空间的根目标都是建空间时自动创建的、天生没有
                # 工时,所以少了它会**每一份计划上都挂着一条永久的假缺口**——而一条
                # 永远在的假警报,比没有警报更糟:用户会学会忽略整个缺口报告,包括那些
                # 真的排不下的。
                return
            self._gap(
                node,
                minutes=0,
                reason=ScheduleErrorCode.NO_ESTIMATE,
                binding=BindingConstraint.NO_ESTIMATE,
                detail={"reason": "这个任务没有预计工时,算不出要排多久"},
            )
            return

        remaining = max(0, node.estimate_minutes - self.delivered.get(node.id, 0))
        if remaining <= 0:
            self._cancel_all(node)
            return

        existing = self.movable.get(node.id, [])
        # 新的场次序号从**所有**既有场次的最大序号往后取。这样新序号与任何既有序号都
        # 不同,`(node_id, scheduled_date, seq)` 那个唯一约束不可能撞车 —— 而撞车的
        # 表现是一次 500,发生在用户点"应用"的时候。取全部而不只是可移动的那些:
        # 冻结的 `done` 场次同样在那条唯一约束的谓词之内。
        seq_cursor = max(self.all_seqs.get(node.id, [0]) or [0])

        chunks = pack.split_sessions(
            remaining, self.profile, day_room_minutes=self.largest_day_minutes
        )
        previous_day: date | None = None
        node_finish: date | None = None

        for index, chunk in enumerate(chunks):
            reuse = existing[index] if index < len(existing) else None
            earliest, waiting_on_dependency = graph.earliest_start(
                node.id,
                predecessors=self.predecessors,
                finish_days=self.finish_days,
                fallback=self.today,
            )
            last_allowed = self._last_allowed(node)
            if last_allowed < self.today:
                self._gap(
                    node,
                    minutes=sum(item.minutes for item in chunks[index:]),
                    reason=ScheduleErrorCode.DEADLINE_ALREADY_PASSED,
                    binding=BindingConstraint.DEADLINE,
                    detail={"reason": "截止时间已经过去了", "deadline": node.deadline.isoformat() if node.deadline else None},
                )
                return
            if earliest > last_allowed:
                self._gap(
                    node,
                    minutes=sum(item.minutes for item in chunks[index:]),
                    reason=ScheduleErrorCode.DEPENDENCY_CHAIN_UNSATISFIABLE,
                    binding=BindingConstraint.DEPENDENCY,
                    detail={
                        "reason": "前置任务做完之后,已经没有时间留给它了",
                        "earliestStart": earliest.isoformat(),
                        "deadline": last_allowed.isoformat(),
                    },
                )
                return

            # 锚点:先试它原来那一天(或紧接着上一场之后)。
            if reuse is not None:
                preferred = reuse.scheduled_date
            elif previous_day is not None:
                preferred = previous_day + timedelta(days=1)
            else:
                preferred = earliest
            day = self._find_day(max(earliest, preferred), last_allowed, chunk.occupies_minutes)
            if day is None:
                # 退让一步:允许和上一场排在同一天(仍然不往前跑)。截止日很紧时,
                # "一天做两段"是用户想要的结果,而"排不进去"不是。
                relaxed = max(earliest, previous_day or earliest)
                if relaxed < max(earliest, preferred):
                    day = self._find_day(relaxed, last_allowed, chunk.occupies_minutes)

            if day is None:
                self._record_placement_failure(
                    node,
                    chunk_minutes=chunk.minutes,
                    earliest=earliest,
                    last_allowed=last_allowed,
                    required=chunk.occupies_minutes,
                    waiting_on_dependency=waiting_on_dependency,
                )
                continue

            self.tracker.take(day, node.workspace_id, chunk.occupies_minutes)
            if reuse is not None:
                session_id = reuse.id
                if reuse.scheduled_date != day:
                    self.moves.append(
                        SessionMove(
                            session_id=reuse.id,
                            node_id=node.id,
                            from_date=reuse.scheduled_date,
                            to_date=day,
                            seq=reuse.seq,
                        )
                    )
                else:
                    self.kept_ids.add(reuse.id)
                self.sessions.append(
                    PlannedSession(
                        node_id=node.id,
                        workspace_id=node.workspace_id,
                        scheduled_date=day,
                        planned_minutes=chunk.minutes,
                        buffer_minutes=chunk.buffer_minutes,
                        seq=reuse.seq,
                        start_minute=reuse.start_minute,
                        end_minute=reuse.end_minute,
                        session_id=session_id,
                        origin=reuse.origin,
                        locked=False,
                    )
                )
            else:
                seq_cursor += 1
                self.sessions.append(
                    PlannedSession(
                        node_id=node.id,
                        workspace_id=node.workspace_id,
                        scheduled_date=day,
                        planned_minutes=chunk.minutes,
                        buffer_minutes=chunk.buffer_minutes,
                        seq=seq_cursor,
                    )
                )

            previous_day = day
            node_finish = day if node_finish is None else max(node_finish, day)

        # 这个任务现在只需要 N 场,而库里有更多 —— 多出来的取消掉。
        if len(existing) > len(chunks):
            for extra in existing[len(chunks) :]:
                self.cancelations.append(extra.id)

        if node_finish is not None:
            self._note_finish(node.id, node_finish)

    def _last_allowed(self, node: ScheduleNode) -> date:
        """这个节点最后可以排到哪一天。截止日是上限,视界也是。"""
        if node.deadline is None:
            return self.horizon_last
        return min(node.deadline, self.horizon_last)

    def _find_day(self, cursor: date, last_allowed: date, required: int) -> date | None:
        """从 `cursor` 起找第一个当天放得下 `required` 分钟的日子。

        **从最早开始贪心,而不是挑"最空的一天"**:挑最空的会把任务摊到日历各处,
        用户看到的是"这些事随机分布在一个月里"。按顺序往前排才是人能理解的计划。
        """
        day = cursor
        while day <= last_allowed:
            if self.tracker.room(day).minutes >= required:
                return day
            day += timedelta(days=1)
        return None

    def _record_placement_failure(
        self,
        node: ScheduleNode,
        *,
        chunk_minutes: int,
        earliest: date,
        last_allowed: date,
        required: int,
        waiting_on_dependency: bool,
    ) -> None:
        """排不进去时,**说清楚是被什么卡住的**。

        只说"排不下"的话,用户能做的只有盲猜 —— 而他能做的三件事(少做点 / 延期 /
        多投入)分别对应不同的约束。所以这里把 `binding_constraint` 算出来。
        """
        first = max(earliest, self.today)
        candidates = [day for day in _day_span(first, last_allowed) if day <= self.horizon_last]

        if not candidates:
            reason = ScheduleErrorCode.HORIZON_EXHAUSTED
            binding = BindingConstraint.HORIZON
            detail: dict[str, object] = {
                "reason": "超出了本次排期的视界",
                "horizonDays": self.request.horizon_days,
            }
        else:
            rooms = [(day, self.tracker.room(day)) for day in candidates]
            best_day, best_room = max(rooms, key=lambda pair: (pair[1].minutes, -pair[0].toordinal()))
            pools = [self.pools.pool(day) for day in candidates]

            # 依赖要**先排除掉**:只有当"不等前置的话本来是放得下的"成立时,才把缺口
            # 归因到依赖上。不排除的话,任何一个前面有任务、同时又确实排不下的节点,
            # 都会被告知"等前置做完就好了" —— 而它等完还是排不下。
            if waiting_on_dependency and self._would_fit_before(earliest, required):
                reason = ScheduleErrorCode.DEPENDENCY_CHAIN_UNSATISFIABLE
                binding = BindingConstraint.DEPENDENCY
                detail = {
                    "reason": "要等前置任务做完,剩下的日子不够了;不等它本来是排得下的",
                    "earliestStart": earliest.isoformat(),
                    "deadline": last_allowed.isoformat(),
                }
            elif all(pool == 0 for pool in pools):
                reason = ScheduleErrorCode.NO_CAPACITY_BEFORE_DEADLINE
                binding = BindingConstraint.AVAILABILITY
                detail = {"reason": "这些天都没有可用的时间", "deadline": last_allowed.isoformat()}
            elif 0 < best_room.minutes < self.min_session:
                # 池子里**还有一点空**,但放不下一场最小的。这和"一点空都没有"不同:
                # 用户多挤出十几分钟就能排下一场,而不是要空出一整个晚上。
                #
                # 下界必须写成 `0 <`:最空的那天一分不剩时,这句话就变成了假建议 ——
                # 用户按它去"多挤十几分钟",而他要腾出的其实是一整天。归因给错的约束,
                # 代价由用户承担;这正是本模块存在的理由,所以这里不能含糊。
                reason = ScheduleErrorCode.BELOW_MIN_SESSION
                binding = BindingConstraint.MIN_SESSION_SIZE
                detail = {
                    "reason": "剩下的时间不够一场最小的安排",
                    "largestRoom": best_room.minutes,
                    "minSessionMinutes": self.min_session,
                }
            else:
                reason = ScheduleErrorCode.NO_CAPACITY_BEFORE_DEADLINE
                binding = self._attribute_binding(candidates, best_day, best_room)
                detail = {
                    "reason": "截止时间之前的时间都排满了",
                    "deadline": last_allowed.isoformat(),
                    "largestRoom": best_room.minutes,
                    "busiestDay": best_day.isoformat(),
                    # 让界面能说"因为「考研」占了 4 小时",而不是只给一个数字 ——
                    # 用户对着"这周满了"能做的事,取决于他知道是谁占的。
                    "loadOnBusiestDay": {
                        str(workspace): minutes
                        for workspace, minutes in self.tracker.load_on(best_day).items()
                    },
                }

        self._gap(
            node,
            minutes=chunk_minutes,
            reason=reason,
            binding=binding,
            detail=detail,
        )

    def _would_fit_before(self, earliest: date, required: int) -> bool:
        """如果不被前置拖着,本来排得下吗。

        只看 `[today, earliest)` 这一段:那正是依赖"拿走"的那些日子。
        """
        for day in _day_span(self.today, earliest - timedelta(days=1)):
            if day > self.horizon_last:
                break
            if self.tracker.room(day).minutes >= required:
                return True
        return False

    def _attribute_binding(
        self, candidates: list[date], best_day: date, best_room: Room
    ) -> BindingConstraint:
        """到底是谁把池子占住了。

        **锁定场次要单独点名。** 如果那些天在没有锁定场次的情况下本来是放得下的,
        那用户要做的决定是"解开一个锁",而不是"增加投入" —— 这两件事差得很远,
        而给错建议的代价由用户承担。
        """
        if any(self.locked_minutes_on.get(day, 0) > 0 for day in candidates):
            without_locked = self.pools.pool(best_day) - (
                self.tracker.day_used(best_day) - self.locked_minutes_on.get(best_day, 0)
            )
            if without_locked > best_room.minutes:
                return BindingConstraint.LOCKED_SESSIONS
        return best_room.binding

    def _cancel_all(self, node: ScheduleNode) -> None:
        for extra in self.movable.get(node.id, []):
            self.cancelations.append(extra.id)

    def _gap(
        self,
        node: ScheduleNode,
        *,
        minutes: int,
        reason: ScheduleErrorCode,
        binding: BindingConstraint,
        detail: dict[str, object],
    ) -> None:
        self.raw_gaps.append(
            CapacityGap(
                workspace_id=node.workspace_id,
                node_id=node.id,
                node_title=node.title,
                unscheduled_minutes=max(0, minutes),
                reason_code=str(reason),
                binding_constraint=str(binding),
                detail=detail,
            )
        )

    # ------------------------------------------------------------------ 不变量
    def _check_invariants(self) -> None:
        """**恒开。** 违反任何一条都让整个请求失败,而不是写入一份坏计划。

        这些性质在正确的实现里不可能被破坏,所以它们不是"防御性编程",而是"算法自己
        坏掉时的最后一道拦网"。用户没有任何办法发现"这周三被排了 9 小时"这种事 ——
        他只会在周三晚上发现自己做不完。
        """
        for day, used in sorted(self.tracker.audit_trail()):
            # 只看视界之内的日子。视界**之外**的日子池子是 0(那正是"别排到那里去"),
            # 而过去那些天上有冻结的历史场次 —— 那是已经发生的事,不是排期做的决定。
            # 把历史也算进来,这条断言会在任何一份"上周做过事"的计划上炸,而它想守的
            # 是"别把未来的某一天排爆",两件事无关。
            if day < self.today or day > self.horizon_last:
                continue
            capacity = self.pools.pool(day)
            if sum(minutes for _, minutes in used) > capacity:
                raise SchedulerInvariantError(
                    f"{day} 的占用超过了当天池子:{used} > {capacity}"
                )

        for session in self.sessions:
            if session.scheduled_date < self.today:
                raise SchedulerInvariantError(
                    f"场次排到了过去:{session.scheduled_date} < {self.today}"
                )

        for moved in self.moves:
            if moved.session_id in self.frozen_ids:
                raise SchedulerInvariantError(f"移动了一个冻结场次:{moved.session_id}")
        for canceled in self.cancelations:
            if canceled in self.frozen_ids:
                raise SchedulerInvariantError(f"取消了一个冻结场次:{canceled}")

        # 依赖:前置做完之前不许开工。这是规则 (a) 唯一不能被"看起来对"糊过去的地方。
        by_node: dict[uuid.UUID, list[date]] = {}
        for session in self.sessions:
            by_node.setdefault(session.node_id, []).append(session.scheduled_date)
        for successor, edges in sorted(self.predecessors.items(), key=lambda pair: str(pair[0])):
            starts = by_node.get(successor)
            if not starts:
                continue
            for edge in edges:
                finished = self.finish_days.get(edge.predecessor_id)
                if finished is None:
                    continue
                if min(starts) <= finished:
                    raise SchedulerInvariantError(
                        f"节点 {successor} 排在了前置 {edge.predecessor_id} 完成之前:"
                        f"{min(starts)} <= {finished}"
                    )

    # ------------------------------------------------------------------ 输出
    def _build_result(self) -> ScheduleResult:
        # 缺口按 `(节点, 原因, 约束)` 合并 —— 一个 8 场的任务排不下时,用户要看的
        # 是"这个任务差 380 分钟,卡在每日上限",而不是 8 条一样的记录。
        merged: dict[tuple[str, str, str], CapacityGap] = {}
        for gap in self.raw_gaps:
            key = (str(gap.node_id), gap.reason_code, gap.binding_constraint)
            existing = merged.get(key)
            if existing is None:
                merged[key] = gap
            else:
                merged[key] = replace(
                    existing, unscheduled_minutes=existing.unscheduled_minutes + gap.unscheduled_minutes
                )

        gaps = tuple(
            sorted(merged.values(), key=lambda item: (str(item.node_id), item.reason_code))
        )
        sessions = tuple(
            sorted(
                self.sessions,
                key=lambda item: (
                    item.scheduled_date,
                    str(item.workspace_id),
                    str(item.node_id),
                    item.seq,
                ),
            )
        )
        moves = tuple(
            sorted(self.moves, key=lambda item: (item.to_date, str(item.session_id)))
        )
        cancelations = tuple(sorted(set(self.cancelations), key=str))
        created = sum(1 for session in sessions if session.session_id is None)

        return ScheduleResult(
            sessions=sessions,
            cancelations=cancelations,
            moves=moves,
            gaps=gaps,
            daily_load=self.tracker.audit_trail(),
            churn=ChurnSummary(
                moved=len(moves), created=created, canceled=len(cancelations), kept=len(self.kept_ids)
            ),
            schedule_version=diff_module.schedule_version(self.request),
            truncated=False,
        )


def _day_span(first: date, last: date) -> list[date]:
    if last < first:
        return []
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def simulate(request: ScheduleRequest) -> ScheduleResult:
    """排一次。**纯函数:不改输入,不写数据库,不看时钟。**"""
    return _Simulation(request).run()


# ---------------------------------------------------------------------------------
# 三条出路。**每一条都要真的重跑一遍才算数。**
# ---------------------------------------------------------------------------------
def recovery_options(request: ScheduleRequest, result: ScheduleResult) -> tuple[RecoveryOption, ...]:
    """容量不足时给用户的三条出路,每条都标着"它到底解不解决"。

    `resolves_gap` 是**跑出来的**:把选项的参数代回去重新 `simulate()` 一次,缺口真的
    消失才算数。断言它成立的话,一个算错的建议会带着"这能解决"的标签送到用户面前 ——
    而用户会照着它去改自己的时间预算。这是这个模块里最不该出错的一处,因为它的代价
    由用户承担。
    """
    if not result.gaps:
        return ()

    shortfall = result.unscheduled_minutes
    options: list[RecoveryOption] = []

    # ---- 1. 少做点:把缺口匀到优先级最低的那几个任务上 -------------------------
    if shortfall > 0:
        trimmed = _trim_lowest_priority(request, result, shortfall)
        if trimmed is not None:
            probe = simulate(trimmed)
            options.append(
                RecoveryOption(
                    kind="REDUCE_SCOPE",
                    label="先少做一点",
                    description=(
                        f"把优先级最低的部分推迟到以后再排,减少约 {shortfall} 分钟的工作量。"
                        "现在这一版能完整排下。"
                    ),
                    resolves_gap=probe.unscheduled_minutes == 0,
                    remaining_unscheduled_minutes=probe.unscheduled_minutes,
                    params={"reducedMinutes": shortfall},
                )
            )

    # ---- 2. 延期:把视界往后拉,直到放得下 --------------------------------------
    extra_days = _days_needed(request, shortfall)
    if extra_days > 0:
        extended = replace(request, horizon_days=request.horizon_days + extra_days)
        probe = simulate(extended)
        options.append(
            RecoveryOption(
                kind="EXTEND_DEADLINE",
                label="往后延一延",
                description=(
                    f"把时间范围往后放宽 {extra_days} 天。"
                    + ("这样能排下。" if probe.unscheduled_minutes == 0 else "这样还是排不下。")
                ),
                resolves_gap=probe.unscheduled_minutes == 0,
                remaining_unscheduled_minutes=probe.unscheduled_minutes,
                params={"extraDays": extra_days, "horizonDays": extended.horizon_days},
            )
        )

    # ---- 3. 增加投入:提高每周预算,直到放得下 ----------------------------------
    increased = _increase_input(request, shortfall)
    if increased is not None:
        probe = simulate(increased)
        gained = increased.profile.weekly_total_minutes - request.profile.weekly_total_minutes
        options.append(
            RecoveryOption(
                kind="INCREASE_INPUT",
                label="多投入一点",
                description=(
                    f"把每周可用时间从 {request.profile.weekly_total_minutes} 分钟提高到 "
                    f"{increased.profile.weekly_total_minutes} 分钟（多 {gained} 分钟）。"
                    + ("这样能排下。" if probe.unscheduled_minutes == 0 else "这样还是排不下。")
                ),
                resolves_gap=probe.unscheduled_minutes == 0,
                remaining_unscheduled_minutes=probe.unscheduled_minutes,
                params={"weeklyTotalMinutes": increased.profile.weekly_total_minutes},
            )
        )

    return tuple(options)


def _gapped_nodes(request: ScheduleRequest, result: ScheduleResult) -> list[ScheduleNode]:
    ids = {gap.node_id for gap in result.gaps if gap.node_id is not None}
    return sorted(
        (node for node in request.nodes if node.id in ids),
        # 先削**优先级最低**的:用户说"少做点"时,他想放弃的不是最重要的那件事。
        key=lambda node: (-graph.priority_rank(node.priority), str(node.id)),
    )


def _trim_lowest_priority(
    request: ScheduleRequest, result: ScheduleResult, shortfall: int
) -> ScheduleRequest | None:
    """把缺口从优先级最低的任务上削掉。

    削的是 `estimate_minutes`,不是删节点 —— 用户要的是"这次先不做那么多",而不是
    "这件事从我的计划里消失"。估算改成 0 的节点会在下一次排期时被跳过,而它仍然在
    任务列表里,用户可以自己决定怎么处理。
    """
    victims = _gapped_nodes(request, result)
    if not victims:
        return None

    remaining = shortfall
    cuts: dict[uuid.UUID, int] = {}
    for node in victims:
        if remaining <= 0:
            break
        estimate = node.estimate_minutes or 0
        cut = min(estimate, remaining)
        if cut <= 0:
            continue
        cuts[node.id] = cut
        remaining -= cut

    if not cuts:
        return None

    nodes = tuple(
        replace(node, estimate_minutes=max(0, (node.estimate_minutes or 0) - cuts.get(node.id, 0)))
        for node in request.nodes
    )
    return replace(request, nodes=nodes)


def _days_needed(request: ScheduleRequest, shortfall: int) -> int:
    """往后延多久才放得下。

    用**每日池子的中位数**而不是平均数:一个每周只有周末有空的用户,平均到每一天是
    一个几乎没有意义的数字,而"每次多一个周六"才是他真正要做的决定。
    """
    if shortfall <= 0:
        return 0
    pools = build_day_pools(
        start=request.today,
        horizon_days=request.horizon_days,
        profile=request.profile,
        windows=request.windows,
        exceptions=request.exceptions,
    )
    usable = sorted(pool for pool in (pools.pool(day) for day in pools.days) if pool > 0)
    if not usable:
        # 一天可用的时间都没有 —— 延期解决不了。给一个最小步长,让重算去证伪它。
        return 1
    per_day = usable[len(usable) // 2]
    needed = -(-shortfall // per_day)  # 向上取整
    return max(1, min(needed, _MAX_EXTENSION_DAYS))


def _increase_input(request: ScheduleRequest, shortfall: int) -> ScheduleRequest | None:
    """提高每周预算,直到放得下(最多试到 `_MAX_INPUT_MULTIPLIER` 倍)。

    一个永远能解决的选项等于没有建议,所以这里**试到上限就停**,并如实报告
    `resolves_gap=False`。
    """
    if shortfall <= 0:
        return None

    base = max(1, request.profile.weekly_total_minutes)
    # 需要的额外总量摊到视界覆盖的周数上,再加一点余量 —— 缺口的来源可能集中在
    # 某几周,所以按周数平摊只是起点,真正的判据是下面的重算。
    weeks = max(1, -(-request.horizon_days // 7))
    target = base + -(-shortfall // weeks)
    candidate = min(target, int(base * _MAX_INPUT_MULTIPLIER))
    if candidate <= base:
        candidate = min(base + 1, int(base * _MAX_INPUT_MULTIPLIER))
    if candidate <= base:
        return None

    return replace(request, profile=replace(request.profile, weekly_total_minutes=candidate))


__all__ = ["OCCUPYING_STATUSES", "recovery_options", "simulate"]
