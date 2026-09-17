export type Category = 'academic' | 'research' | 'experience' | 'personal';
export interface GrowthNode {
  id: string;
  title: string;
  description?: string;
  type: 'goal' | 'capability' | 'stage' | 'task' | 'milestone';
  parentId?: string;
  category?: Category;
  stageId?: string;
  status: 'pending' | 'doing' | 'completed';
  priority: 'low' | 'medium' | 'high';
  startDate?: string;
  endDate?: string;
  scheduledDate?: string;
  estimatedHours?: number;
  timelineLevel?: 'major' | 'task' | 'action';
}
export type Task = GrowthNode & { type: 'task' };
export type Milestone = GrowthNode & { type: 'milestone' };
export interface GrowthEdge { id: string; source: string; target: string; type: 'dependency' | 'support' }
export interface GrowthState {
  id: string;
  title: string;
  goalId: string;
  currentStageId: string;
  nodes: Record<string, GrowthNode>;
  edges: GrowthEdge[];
}
export type PlanAction =
  | { type: 'CREATE_NODE'; node: GrowthNode }
  | { type: 'UPDATE_STATUS'; nodeId: string; status: GrowthNode['status'] }
  | { type: 'UPDATE_TIME'; nodeId: string; startDate: string; endDate: string };
export interface Message { id: string; role: 'user' | 'assistant'; text: string; contextId?: string; proposalId?: string }
export interface Conversation { id: string; title: string; linkedNodeIds: string[]; messages: Message[] }
export interface User { id: string; name: string; major: string; year: string; rank: number; targetYear: number }
export interface JournalEntry { id: string; content: string; date: string; linkedNodeIds: string[]; tags?: string[]; insight?: string }
export interface FileAsset { id: string; ownerId: string; name: string; size: number; mime: string; url: string; file: File }
export interface AISettings { mode: string; frequency: string; proactive: boolean; adjust: boolean; critique: boolean; rest: boolean }
export interface Proposal { id: string; nodeId: string; status: 'pending' | 'accepted' | 'outdated'; originalStart: string; actions: PlanAction[] }
