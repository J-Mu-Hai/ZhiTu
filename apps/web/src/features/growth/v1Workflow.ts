import type { GoalReasoningView } from '@/lib/backend';

/**
 * V1 工作流的**三阶段骨架**投影。
 *
 * ## 它为什么是投影,不是 PlanNode
 *
 * `想清楚 / 排出来 / 做起来` 是**工作流的阶段容器**,不是正式计划的一部分:
 * 它们不参与排期、任务统计、依赖图,也不该写进 `plan_nodes`(那会污染正式计划)。
 * 所以这里只从会话状态(`v1Stage` / `v1Strategy` / 时间线提案)算出一份只读视图;
 * 会话状态持久化在 `goal_reasoning_sessions` 上,所以刷新、切视图、重新进入空间
 * 之后这份骨架原样重建。
 *
 * ## 为什么非 V1 / 老空间不会看到它
 *
 * `v1Stage` 为 `null`(非 V1)时返回空数组;`initial_thinking`(刚建空间、用户还没
 * 说“开始”)也返回空数组 —— 首屏只留根目标,不抢在用户之前生成骨架。
 */
export type V1PhaseKey = 'think' | 'plan' | 'do';

export type V1PhaseState =
  | 'locked'
  | 'not_started'
  | 'discussing'
  | 'awaiting_confirmation'
  | 'done';

export interface V1PhaseView {
  key: V1PhaseKey;
  title: string;
  subtitle: string;
  state: V1PhaseState;
  /** 当前主轴阶段(第一个既未完成、又未锁定的阶段)。同一时刻最多一个。 */
  active: boolean;
}

/** 战略已确认(想清楚完成)之后才会出现的阶段。 */
const THINK_DONE_STAGES = new Set<string>([
  'strategy_confirmed_for_timeline',
  'timeline_alignment',
  'coarse_timeline_review',
  'weekly_execution',
  'replanning',
]);

/** 粗时间架构已确认(排出来完成)之后才会出现的阶段。 */
const PLAN_DONE_STAGES = new Set<string>(['weekly_execution', 'replanning']);

/** 还在“想清楚”里的阶段。 */
const THINK_ACTIVE_STAGES = new Set<string>([
  'goal_reframe',
  'factor_analysis',
  'strategy_draft',
  'problem_structure',
  'strategy_alignment',
]);

const STATE_LABEL: Record<V1PhaseState, string> = {
  locked: '已锁定',
  not_started: '未开始',
  discussing: '讨论中',
  awaiting_confirmation: '等待确认',
  done: '已完成',
};

export function v1PhaseStateLabel(state: V1PhaseState): string {
  return STATE_LABEL[state];
}

/** 用户是否已经明确开始(第一屏之外的任何 V1 阶段)。 */
export function v1WorkflowStarted(reasoning: GoalReasoningView | null): boolean {
  const stage = reasoning?.v1Stage;
  return Boolean(stage && stage !== 'initial_thinking');
}

/**
 * 三阶段视图。空数组 = 还没有 `想清楚 / 排出来 / 做起来` 这条主链。
 */
export function v1Phases(reasoning: GoalReasoningView | null): V1PhaseView[] {
  if (!reasoning || !v1WorkflowStarted(reasoning)) return [];
  const stage = reasoning.v1Stage ?? '';
  const strategyConfirmed =
    Boolean(reasoning.v1Strategy?.confirmed) || THINK_DONE_STAGES.has(stage);
  const planDone = PLAN_DONE_STAGES.has(stage);
  const next = reasoning.v1NextAction ?? null;
  const waitingStrategy =
    reasoning.v1Status === 'awaiting_user_confirmation' ||
    next === 'confirm_strategy' ||
    next === 'confirm_strategy_understanding' ||
    next === 'confirm_goal' ||
    next === 'select_direction';
  const waitingTimeline =
    reasoning.v1Status === 'awaiting_user_confirmation' ||
    next === 'confirm_timeline' ||
    next === 'confirm_timeline_alignment';

  const thinkState: V1PhaseState = strategyConfirmed
    ? 'done'
    : THINK_ACTIVE_STAGES.has(stage)
      ? waitingStrategy
        ? 'awaiting_confirmation'
        : 'discussing'
      : 'discussing';

  const planState: V1PhaseState = !strategyConfirmed
    ? 'locked'
    : planDone
      ? 'done'
      : stage === 'coarse_timeline_review'
        ? 'awaiting_confirmation'
        : waitingTimeline
          ? 'awaiting_confirmation'
          : 'discussing';

  const doState: V1PhaseState = planDone ? 'discussing' : 'locked';

  const phases: V1PhaseView[] = [
    { key: 'think', title: '想清楚', subtitle: '目的、关键矛盾与战略路径', state: thinkState, active: false },
    { key: 'plan', title: '排出来', subtitle: '按月覆盖的粗时间规划', state: planState, active: false },
    { key: 'do', title: '做起来', subtitle: '具体执行、今日计划与复盘', state: doState, active: false },
  ];
  // 当前主轴阶段 = 第一个“未完成且未锁定”的阶段。全完成后没有 active。
  const activePhase = phases.find(
    (phase) => phase.state !== 'done' && phase.state !== 'locked',
  );
  if (activePhase) activePhase.active = true;
  return phases;
}

/** 首屏的“准备好开始了吗”提示文案(只在 `initial_thinking` 展示)。 */
export const V1_READY_PROMPT =
  '我会先和你想清楚目标与战略，再排出整体时间线，最后才细化到周和日。准备好开始了吗？';

/** 用户开始之后,对话里的简短说明。 */
export const V1_STARTED_NOTE =
  '我已在“想清楚”下面放好当前最重要的问题；先完成这一段，再生成按月覆盖的粗时间规划。';
