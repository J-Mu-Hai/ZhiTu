import type { GrowthState, PlanAction } from '@/types/growth';
export function growthReducer(state: GrowthState, action: PlanAction): GrowthState {
  if (action.type === 'CREATE_NODE') return { ...state, nodes: { ...state.nodes, [action.node.id]: action.node } };
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
