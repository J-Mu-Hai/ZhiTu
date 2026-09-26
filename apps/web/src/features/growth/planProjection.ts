import type { PlanNodePayload, PlanPayload, ScheduledSessionPayload } from '@/lib/backend';
import type { GrowthNode, GrowthSession, GrowthState } from '@/types/growth';

/**
 * 后端的 `PlanPayload` -> 界面用的 `GrowthState`。
 *
 * ## 为什么保留 `GrowthState` 这个形状,而不是让组件直接读 `PlanPayload`
 *
 * 路径图、时间线、任务列表、今日视图都建立在 `GrowthState` 上(它的 `nodes` 是
 * `Record<id, node>`,查父节点是 O(1),树的行走到处都是)。为了换数据源把它们全部
 * 重写一遍,风险远大于收益,而且改完之后它们仍然只是同一份计划的四个投影。
 *
 * 所以这一层的职责就是**把后端的载荷翻译成视图模型**,一个方向,没有状态。
 * 真实空间里 `GrowthState` 不再是"那份数据",它只是"那份数据的一个投影" ——
 * 唯一真相在后端,这一点靠 provider 里"每次写入后重新拉 `/plan`"来保证。
 *
 * ## 这里最容易做错的一件事:不要在投影层补出后端没有的字段
 *
 * `GrowthNode` 有 `startDate` / `endDate` / `scheduledDate`,而后端把它们分成两种
 * 完全不同的事实:
 *
 * - **`deadline`**:用户或模型说的"我要在这天之前做完"。这是意图。
 * - **`sessions`**:排期算出来的"哪几天做、每次多久"。这是安排。
 *
 * 一个节点可能只有前者(还没排期)、两者都有(排完期了),或者(理论上)只有在
 * 没有截止日时才有后者。把 deadline 当成"安排"渲染出来的话,屏幕上会出现一条
 * "这个阶段从 3 月做到 9 月"的进度条,而那个区间是前端编的 —— 用户从没说过,
 * 更没有任何算法排过。
 *
 * 所以规则是**有场次就用场次,没有场次才把截止日画成一个点**:
 *
 * - 有场次:start/end 是场次的最早/最晚一天。它是真的。
 * - 没有场次、有 deadline:start = end = deadline,渲染成一个**点**,标签是
 *   "截止 2026-10-23"。这是句真话 —— 那天确实什么都没排。
 * - 都没有:不画。由 `timeline.ts::unscheduledNodes` 负责说"还有 N 项没有日期",
 *   而不是从子节点推导出一个区间。
 */

/** 后端场次 -> 界面场次。只保留界面用得上的字段。 */
function toSession(session: ScheduledSessionPayload): GrowthSession {
  return {
    id: session.id,
    date: session.scheduledDate,
    plannedMinutes: session.plannedMinutes,
    bufferMinutes: session.bufferMinutes,
    seq: session.seq,
    status: session.status,
    locked: session.locked,
    startMinute: session.startMinute,
  };
}

/**
 * 这场还没结束吗。**`moved` 不算**:它表示这一场已经被挪到别处了,原来的那行是
 * 一条指向过去的记录,不该再参与"这几天要做多久"。
 */
function isOpenSession(session: GrowthSession): boolean {
  return session.status === 'planned' || session.status === 'in_progress';
}

/** 后端节点类型 -> 界面节点类型。两边的取值本来就一致,这个函数是**显式的**。 */
function toNodeType(node: PlanNodePayload): GrowthNode['type'] {
  return node.nodeType;
}

/**
 * 后端状态 -> 界面状态。
 *
 * `archived` 在界面上没有对应值。映射成 `pending` 而不是新加一个状态:界面上的
 * `GrowthNode.status` 只有三档,加第四档要改的组件比"归档与待办长得一样"更多 ——
 * 而 MVP 里根本没有任何代码路径会把节点设成 `archived`。
 * 真接上归档功能时,这里必须改,所以留这句话。
 */
function toStatus(status: PlanNodePayload['status']): GrowthNode['status'] {
  if (status === 'completed') return 'completed';
  if (status === 'doing') return 'doing';
  return 'pending';
}

export function toGrowthNode(node: PlanNodePayload, sessions: GrowthSession[] = []): GrowthNode {
  const dates = sessions.map((session) => session.date).sort();
  // 有场次:区间来自**真实排出来的那几天**。没场次:退回截止日那个点。
  const start = dates[0] ?? node.deadline ?? undefined;
  const end = dates.at(-1) ?? node.deadline ?? undefined;
  // "最近一场还没做的安排"。全部做完了就退回最早那一场 —— 用户问"这事安排在
  // 什么时候",对一个已经做完的任务,答案是"它做过的那几天",不是空。
  const nextUp = sessions.filter(isOpenSession).map((session) => session.date).sort()[0];

  return {
    id: node.id,
    title: node.title,
    description: node.description ?? undefined,
    type: toNodeType(node),
    parentId: node.parentId ?? undefined,
    status: toStatus(node.status),
    priority: node.priority,
    startDate: start,
    endDate: end,
    scheduledDate: nextUp ?? dates[0],
    // 原始语义也带上 —— 时间线靠它把那个点标成"截止",而不是一个裸日期;
    // 而 `sessions` 非空时,时间线靠它知道"这个区间是排出来的",不用另立字段。
    deadline: node.deadline ?? undefined,
    sessions: sessions.length ? sessions : undefined,
    // **两个单位都留着,而且不是冗余。**
    //
    // `estimatedHours` 是给人看的("约 1.5 小时"),它是**四舍五入过的**;
    // `estimateMinutes` 是原样的那个数,编辑器和写回用它。
    //
    // 只留小时那一份会引入一种安静的漂移:用户填了 100 分钟,投影成 1.7 小时,
    // 他打开节点编辑器再点保存(什么都没改),写回去的就是 102 分钟。每次开一次
    // 编辑器就多两分钟,而没有任何一步会提示他。排期读的正是这个数。
    estimateMinutes: node.estimateMinutes ?? undefined,
    estimatedHours:
      node.estimateMinutes === null ? undefined : Math.round((node.estimateMinutes / 60) * 10) / 10,
  };
}

/** 按节点把场次分好,顺便排好序 —— 界面各处都要按日期次序读它。 */
function groupSessions(plan: PlanPayload): Map<string, GrowthSession[]> {
  const grouped = new Map<string, GrowthSession[]>();
  for (const session of plan.sessions) {
    const list = grouped.get(session.nodeId);
    if (list) list.push(toSession(session));
    else grouped.set(session.nodeId, [toSession(session)]);
  }
  for (const list of grouped.values()) {
    list.sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq || a.id.localeCompare(b.id));
  }
  return grouped;
}

/**
 * 把一份计划载荷翻译成视图模型。
 *
 * `title` 从空间详情来 —— `PlanPayload` 里没有它,而根目标节点自己在某些空间里
 * 可能只有一个空标题(建空间时用户只填了空间名)。用空间名兜底,免得路径图正中
 * 那个旗标下面是一片空白。
 */
export function planToGrowth(plan: PlanPayload, title: string): GrowthState {
  const sessions = groupSessions(plan);
  const nodes: Record<string, GrowthNode> = {};
  for (const node of plan.nodes) nodes[node.id] = toGrowthNode(node, sessions.get(node.id) ?? []);

  const root = plan.nodes.find((node) => node.parentId === null);
  const goalId = root?.id ?? '';
  if (root && !root.title.trim()) nodes[root.id] = { ...nodes[root.id], title };

  /*
   * 每个节点属于哪个阶段。
   *
   * 后端**没有**这个字段:`plan_nodes` 里只有 `parent_id`,阶段是树的一层而不是一个
   * 属性。而任务视图要按阶段分组、要能筛"本阶段",所以这里从父链上算出来。
   *
   * 判据是"最近的 `stage` 祖先",不是"根的直接子节点":`目标 → 阶段 → 任务` 和
   * `目标 → 阶段 → 子阶段 → 任务` 两种形状都得对。没有 stage 祖先的(比如根自己、
   * 或者直接挂在根下面的任务)归到根目标 —— 于是"本阶段"在只有一层计划的空间里
   * 等于"当前空间",这比返回空列表诚实。
   */
  const stageOf = (node: GrowthNode): string => {
    const seen = new Set<string>([node.id]);
    let current = node.parentId ? nodes[node.parentId] : undefined;
    while (current && !seen.has(current.id)) {
      if (current.type === 'stage') return current.id;
      seen.add(current.id);
      current = current.parentId ? nodes[current.parentId] : undefined;
    }
    return goalId;
  };
  for (const node of Object.values(nodes)) nodes[node.id] = { ...node, stageId: stageOf(node) };

  return {
    id: `growth-${plan.workspaceId}`,
    title,
    goalId,
    currentStageId: goalId,
    nodes,
    edges: plan.dependencies.map((dep) => ({
      id: dep.id,
      source: dep.predecessorId,
      target: dep.successorId,
      type: 'dependency' as const,
    })),
  };
}

/**
 * 一个只有根目标的空间 —— 计划还没拉到 / 拉失败了时候的形状。
 *
 * 它**不代表任何后端事实**,所以 `id` 用的是 `goal` 这个哨兵值而不是 UUID:
 * 界面上任何"把当前节点发给后端"的地方都要靠 `UUID_RE` 把它挡掉。
 * 阶段 5 之后,成功拉到计划时所有节点 id 都是真实的 UUID。
 */
export function emptyGrowth(title: string, intent = ''): GrowthState {
  const goal: GrowthNode = {
    id: 'goal',
    title: title || '还没有选择成长空间',
    description: intent || undefined,
    type: 'goal',
    status: 'pending',
    priority: 'high',
  };
  return {
    id: 'growth-empty',
    title,
    goalId: 'goal',
    currentStageId: 'goal',
    nodes: { goal },
    edges: [],
  };
}
