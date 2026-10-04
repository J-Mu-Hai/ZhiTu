import type { GoalReasoningView, V1DimensionView } from '@/lib/backend';

/**
 * V1 画布的**单一分析投影**。
 *
 * 它是服务端 `GoalReasoningView.v1Dimensions` 的浏览器侧形状 —— key / title /
 * judgment / status / visible / isFocus / hasPendingQuestion / discussionSummary /
 * internal / questionId。**PathView 不再自行用 `sourceNodeId` 推断可见性**:
 * 可见性由服务端投影决定,前端只读这一份。
 */
export interface V1AnalysisProjection {
  key: string;
  title: string;
  judgment: string;
  status: string;
  visible: boolean;
  isFocus: boolean;
  hasPendingQuestion: boolean;
  discussionSummary: string;
  internal: boolean;
  questionId: string | null;
  knownFacts: string[];
  assumptions: string[];
  importanceReason: string;
}

export function v1AnalysisProjection(
  reasoning: GoalReasoningView | null,
): V1AnalysisProjection[] {
  return (reasoning?.v1Dimensions ?? []).map((dimension: V1DimensionView) => ({
    key: dimension.key,
    title: dimension.title,
    judgment: dimension.judgment ?? '',
    status: dimension.status ?? 'pending',
    visible: Boolean(dimension.visible),
    isFocus: Boolean(dimension.isFocus),
    hasPendingQuestion: Boolean(dimension.hasPendingQuestion),
    discussionSummary: dimension.discussionSummary ?? '',
    internal: Boolean(dimension.internal),
    questionId: dimension.questionId ?? null,
    knownFacts: dimension.knownFacts ?? [],
    assumptions: dimension.assumptions ?? [],
    importanceReason: dimension.importanceReason ?? '',
  }));
}

/** 主画布默认显示的维度键(可见 + 当前焦点)。 */
export function v1VisibleKeys(reasoning: GoalReasoningView | null): Set<string> {
  return new Set(
    v1AnalysisProjection(reasoning)
      .filter(item => item.visible)
      .map(item => item.key),
  );
}

/**
 * 粗时间架构是否**完整到可以呈现/确认**。
 *
 * 只有每个阶段都有时间范围 + 目标 + 成果 + 完成标准时,才允许自动导航到时间线、
 * 才显示确认按钮 —— 不合格的时间线不得被导航到或确认(服务端也在生成前做了严格校验)。
 */
export function v1TimelineReady(reasoning: GoalReasoningView | null): boolean {
  if (reasoning?.v1Stage !== 'coarse_timeline_review') return false;
  const items = reasoning.v01Timeline ?? [];
  if (items.length < 3 || items.length > 6) return false;
  return items.every(
    item =>
      Boolean(item.title && item.goal && item.deliverable && item.completionCriteria) &&
      ((item.startWeek != null && item.endWeek != null) ||
        (Boolean(item.startDate) && Boolean(item.endDate))),
  );
}
