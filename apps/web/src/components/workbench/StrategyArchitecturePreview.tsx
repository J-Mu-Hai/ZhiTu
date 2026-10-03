'use client';
import { useDemo } from '@/features/growth/provider';
import type { ReasoningNodeView } from '@/lib/backend';

/**
 * 战略时间架构预览(阶段 11)。
 *
 * ## 它为什么在时间线上
 *
 * 时间架构生成后,工作台会自动切到时间线,让用户先看到“总时长 + 3–5 个阶段”的
 * 全局,而不是先被问细节。这块预览显示:根目标、阶段条、阶段时间范围、阶段成果物,
 * 并明确标注 **`草案，尚未写入计划`**。
 *
 * ## 数据来自唯一一处
 *
 * 它读的是 `GoalReasoningView` 的 route / stage —— **与路径页同一份**,
 * 前端不另算一套时间。有日期用年月日,没日期用相对第 N 周,并说清“日期待校准”。
 * 已确认战略后(计划里出现 strategy 节点)整块隐藏,时间线以计划为准。
 */
function stageRange(stage: ReasoningNodeView, calibrated: boolean): string {
  if (calibrated && stage.startDate && stage.endDate) {
    return `${stage.startDate} → ${stage.endDate}`;
  }
  if (stage.startWeek && stage.endWeek) {
    return `第 ${stage.startWeek}–${stage.endWeek} 周`;
  }
  return stage.timeframe ?? (calibrated ? '日期待定' : '周次待定');
}

export function StrategyArchitecturePreview({ onOpenPath }: { onOpenPath: () => void }) {
  const { reasoning, growth } = useDemo();
  const nodes = reasoning?.nodes ?? [];
  const route = nodes.find(node => node.nodeType === 'route') ?? null;
  const stages = nodes
    .filter(node => node.nodeType === 'stage')
    .sort((a, b) => a.handle.localeCompare(b.handle, 'en', { numeric: true }));
  const hasStrategy = Object.values(growth.nodes).some(node => node.planningLevel === 'strategy');
  if (!route || stages.length === 0 || hasStrategy) return null;
  const calibrated = Boolean(reasoning?.datesCalibrated);

  return (
    <section className="arch-preview" data-testid="strategy-architecture-preview" aria-label="战略时间架构预览">
      <header className="arch-preview-head">
        <span className="arch-preview-pill">草案，尚未写入计划</span>
        <div className="arch-preview-title">
          <strong>{route.title}</strong>
          {route.timeframe && <span>{route.timeframe}</span>}
        </div>
        <button type="button" className="arch-preview-link" onClick={onOpenPath}>回到路径页看说明</button>
      </header>
      <ol className="arch-stages">
        {stages.map(stage => (
          <li key={stage.id} data-stage-handle={stage.handle}>
            <button type="button" className="arch-stage" onClick={onOpenPath}>
              <span className="arch-stage-head">
                <strong>{stage.title}</strong>
                <em>{stageRange(stage, calibrated)}</em>
              </span>
              {stage.deliverable && <span className="arch-stage-deliverable">成果：{stage.deliverable}</span>}
            </button>
          </li>
        ))}
      </ol>
      {!calibrated && (
        <p className="arch-preview-note" data-testid="arch-dates-pending">
          日期待校准：上面是相对第 N 周的时间架构，不是日历日期。给出截止或开始日期后会转成具体年月日。
        </p>
      )}
    </section>
  );
}
