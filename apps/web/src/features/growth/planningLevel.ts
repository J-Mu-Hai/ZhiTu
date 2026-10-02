/**
 * 规划层级的中文标签。**只在这里定义一处** —— 节点卡、详情面板、提案相关展示都读它。
 *
 * 层级是**语义**上的("这一层是战略 / 周 / 日"),不代表已排期。`null` 不在这里 ——
 * 没有层级的节点必须**不显示任何层级标签**,而不是显示一个"未指定"的占位符:
 * 存量节点全部没有层级,给它们一个标签会凭空造出一种它们从未表达过的语义。
 */
export const PLANNING_LEVEL_LABELS: Record<string, string> = {
  strategy: '战略层',
  phase: '阶段层',
  month: '月计划',
  week: '周重点',
  day: '今日行动',
};

/** 层级标签;`null`/未知返回 `null`,调用方据此决定不渲染。 */
export function planningLevelLabel(level: string | null | undefined): string | null {
  if (!level) return null;
  return PLANNING_LEVEL_LABELS[level] ?? level;
}
