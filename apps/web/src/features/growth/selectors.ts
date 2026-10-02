import type { GrowthState } from '@/types/growth';
export function isInSpace(state: GrowthState, nodeId: string, spaceId: string): boolean {
  if (spaceId === state.goalId) return true;
  let node = state.nodes[nodeId];
  const seen = new Set<string>();
  while (node && !seen.has(node.id)) {
    if (node.id === spaceId) return true;
    seen.add(node.id);
    node = state.nodes[node.parentId ?? ''];
  }
  return false;
}
