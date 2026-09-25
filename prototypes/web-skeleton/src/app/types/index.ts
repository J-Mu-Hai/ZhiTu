/**
 * 前端类型定义。
 *
 * 与 shared/schemas/ 下的 JSON Schema 一一对应 —— 改动契约时先改 schema,
 * 再同步这里。不要在这个文件里发明后端不存在的字段。
 */

export type PlanActionType = "task" | "habit" | "learning" | "reflection";

export type PlanActionStatus = "pending" | "in_progress" | "done" | "skipped";

export interface PlanAction {
  id: string;
  plan_node_id: string;
  title: string;
  description?: string;
  type: PlanActionType;
  status: PlanActionStatus;
  recurrence: { rule: "daily" | "weekly" | "monthly"; times_per_period?: number } | null;
  scheduled_at: string | null;
  estimate_minutes: number | null;
  completed_at: string | null;
  created_at?: string;
  updated_at?: string;
}

export type PlanNodeStatus = "draft" | "active" | "paused" | "completed" | "archived";

export interface PlanNode {
  id: string;
  growth_space_id: string;
  parent_id: string | null;
  title: string;
  intent?: string | null;
  status: PlanNodeStatus;
  order?: number;
  actions?: PlanAction[];
  created_at?: string;
  updated_at?: string;
}

export type GrowthPhase = "exploring" | "building" | "consolidating" | "reviewing";

export interface GrowthState {
  growth_space_id: string;
  updated_at: string;
  phase: GrowthPhase;
  metrics: {
    streak_days: number;
    completion_rate_7d: number;
    actions_done_30d?: number;
    active_minutes_30d?: number;
  };
  signals?: {
    kind: "momentum" | "stagnation" | "overload" | "imbalance" | "milestone";
    message: string;
    severity?: "info" | "attention" | "urgent";
  }[];
  summary?: string | null;
}
