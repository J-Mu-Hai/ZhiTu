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
  const updated = action.type === 'UPDATE_STATUS'
    ? { ...node, status: action.status }
    : { ...node, startDate: action.startDate, endDate: action.endDate,
        scheduledDate: node.scheduledDate && node.startDate ? shiftDate(node.scheduledDate, daysBetween(node.startDate, action.startDate)) : action.startDate };
  return { ...state, nodes: { ...state.nodes, [node.id]: updated } };
}
export const daysBetween = (a: string, b: string) => Math.round((Date.parse(b + 'T00:00:00Z') - Date.parse(a + 'T00:00:00Z')) / 86400000);
export const shiftDate = (date: string, days: number) => new Date(Date.parse(date + 'T00:00:00Z') + days * 86400000).toISOString().slice(0, 10);
