import type { GrowthState, PlanAction } from '@/types/growth';

export function collectNodeBranch(state: GrowthState, nodeId: string): Set<string> {
  const branch = new Set<string>([nodeId]);
  let foundChild = true;
  while (foundChild) {
    foundChild = false;
    for (const node of Object.values(state.nodes)) {
      if (node.parentId && branch.has(node.parentId) && !branch.has(node.id)) {
        branch.add(node.id);
        foundChild = true;
      }
    }
  }
  return branch;
}

export function growthReducer(state: GrowthState, action: PlanAction): GrowthState {
  if (action.type === 'CREATE_NODE') return { ...state, nodes: { ...state.nodes, [action.node.id]: action.node } };
  if (action.type === 'DELETE_NODE') {
    if (action.nodeId === state.goalId || !state.nodes[action.nodeId]) return state;
    const branch = collectNodeBranch(state, action.nodeId);
    return {
      ...state,
      nodes: Object.fromEntries(Object.entries(state.nodes).filter(([id]) => !branch.has(id))),
      edges: state.edges.filter(edge => !branch.has(edge.source) && !branch.has(edge.target)),
    };
  }
  if (action.type === 'UPDATE_PLAN_META') return {
    ...state,
    title: `${action.title}计划`,
    nodes: { ...state.nodes, [state.goalId]: { ...state.nodes[state.goalId], title: action.title, description: action.description } },
  };
  const node = state.nodes[action.nodeId];
  if (!node) return state;
  if (action.type === 'UPDATE_NODE') {
    // `patch.deadline` / `patch.estimateMinutes` 允许是 `null` —— 后端用 null 表示
    // "把这一个清掉",而 `GrowthNode` 上没有 null 这一档。在这里归一成 `undefined`,
    // 否则 `null` 会顺着 `...patch` 渗进整个视图模型,而所有 `node.deadline && ...`
    // 这类判断仍然"碰巧"是对的 —— 于是它只在类型检查器里报错,在别处都看不出来。
    //
    // `estimateMinutes` 尤其不能漏:它是**分钟**,而视图模型里的 `estimatedHours` 是
    // **小时**。一个 `estimateMinutes: null` 混进去之后,时间线上那张卡片会去读一个
    // 不存在的字段,把工时显示成空 —— 而不是显示成"没填"。
    const { deadline, estimateMinutes, ...rest } = action.patch;
    const patch = {
      ...rest,
      ...(deadline == null ? {} : { deadline }),
      ...(estimateMinutes == null ? {} : { estimateMinutes }),
    };
    return { ...state, nodes: { ...state.nodes, [node.id]: { ...node, ...patch } } };
  }
  const updated = action.type === 'UPDATE_STATUS'
    ? { ...node, status: action.status }
    : { ...node, startDate: action.startDate, endDate: action.endDate,
        scheduledDate: node.scheduledDate && node.startDate ? shiftDate(node.scheduledDate, daysBetween(node.startDate, action.startDate)) : action.startDate };
  return { ...state, nodes: { ...state.nodes, [node.id]: updated } };
}
export const daysBetween = (a: string, b: string) => Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000);
export const shiftDate = (date: string, days: number) => new Date(Date.parse(date + 'T00:00:00Z') + days * 86400000).toISOString().slice(0, 10);
