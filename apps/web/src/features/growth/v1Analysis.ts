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
 * V1 画布**只保留的三个基石节点** —— 目标重构组里真正长期讨论的三个。
 *
 * 其余分析维度(问题结构组里的约束 / 杠杆 / 风险等)不再各自投影成一张卡片,
 * 而是作为“关联”挂到最相关的基石节点上;找不到归属的维度不投影(等同删除)。
 */
export const V1_CORE_GOAL_KEYS: readonly string[] = ['true_intent', 'key_conflict', 'goal_definition'];

/**
 * 「想清楚」下面的**四个固定问题框架节点**。
 *
 * 产品规则:第一个战略分析之后,「想清楚」下面只生成这 4 个框架节点,具体问题挂在
 * 它们下面。不要再生成第五、第六、第七个发散问题。
 *
 * 标题是固定的产品文案;`key` 指向后端已有的分析维度 —— 框架节点下的具体问题就是
 * 那个维度的问题(**不新建后端实体**,只是换个层级展示)。
 */
export const V1_KEY_QUESTIONS: readonly { key: string; title: string }[] = [
  { key: 'true_intent', title: '最终想做到什么？' },
  { key: 'current_state', title: '你现在在哪？' },
  { key: 'hard_constraints', title: '现实能投入什么？' },
  { key: 'goal_definition', title: '什么算真正完成？' },
];

/**
 * 非核心分析维度 **挂到哪个基石节点**上。
 *
 * 键来自后端固定十维(`_ANALYSIS`);这里只映射分组关系,不改业务语义:
 * - `current_state` / `value_assessment` 属于目标重构组,挂在「真实意图」;
 * - `problem_structure` 五个因素描述“什么卡住你”,挂在「关键矛盾」。
 */
export const V1_LINKED_CORE: Record<string, string> = {
  current_state: 'true_intent',
  value_assessment: 'true_intent',
  hard_constraints: 'key_conflict',
  controllable_factors: 'key_conflict',
  key_levers: 'key_conflict',
  major_risks: 'key_conflict',
  external_conditions: 'key_conflict',
};

/** 把一个分析维度键归到基石节点键;不是基石也不是已知关联时返回 `null`。 */
export function v1CoreKeyFor(key: string | null | undefined): string | null {
  if (!key) return null;
  if (V1_CORE_GOAL_KEYS.includes(key)) return key;
  return V1_LINKED_CORE[key] ?? null;
}

/**
 * V1 旧版**固定分组容器**键:目标重构 / 问题结构 / 战略路径。
 *
 * 它们是 `plan_nodes`(`node_type=capability`, `purpose=information`,
 * `origin=ai`, `v1_key=…`)。新版工作流用 `想清楚 / 排出来 / 做起来` 三阶段取代了
 * 这三个容器,所以根画布**不再投影**它们(数据保留,不删库)。
 *
 * 键取自后端 `v1_service._GROUPS` —— 用**稳定字段**判断,绝不按中文标题过滤。
 */
export const V1_LEGACY_GROUP_KEYS: readonly string[] = [
  'goal_reframe',
  'problem_structure',
  'strategy_path',
];

/** 一个 `v1_key` 是不是旧版固定分组容器。 */
export function isLegacyV1GroupKey(key: string | null | undefined): boolean {
  return Boolean(key) && V1_LEGACY_GROUP_KEYS.includes(key as string);
}

/**
 * 当前 interaction 的**回答渠道**。服务端给了 `answerChannel` 就用它;
 * 旧数据没有该字段时,按稳定的 `kind` 回退(`strategic_question` = 对话回答)。
 */
export function v1AnswerChannel(
  interaction: { kind?: string | null; answerChannel?: string | null } | null | undefined,
): 'conversation' | 'canvas_node' {
  if (!interaction) return 'canvas_node';
  if (interaction.answerChannel === 'conversation' || interaction.answerChannel === 'canvas_node') {
    return interaction.answerChannel;
  }
  return interaction.kind === 'strategic_question' ? 'conversation' : 'canvas_node';
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
