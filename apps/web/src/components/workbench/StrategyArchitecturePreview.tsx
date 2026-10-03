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
  const { reasoning, growth, agentTurn, requestChat, refineStrategy, refining } = useDemo();
  const nodes = reasoning?.nodes ?? [];
  const route = nodes.find(node => node.nodeType === 'route') ?? null;
  const stages = nodes
    .filter(node => node.nodeType === 'stage')
    .sort((a, b) => a.handle.localeCompare(b.handle, 'en', { numeric: true }));
  const hasStrategy = Object.values(growth.nodes).some(node => node.planningLevel === 'strategy');

  /*
   * 已确认:**草案预览收起来**,只保留“细化第一阶段”的入口。
   *
   * `细化第一阶段` 只在战略被用户确认写入计划之后才出现 —— 这是执行细化的门槛,
   * 不是装饰。之前它只在路径页的角落,现在时间线上也有一个,因为架构生成后用户
   * 就停在这里。
   */
  if (hasStrategy) {
    if (
      reasoning?.phase !== 'strategy_confirmed' &&
      reasoning?.phase !== 'execution_planning' &&
      reasoning?.phase !== 'execution_refinement' &&
      reasoning?.phase !== 'weekly_planning'
    ) {
      return null;
    }
    return (
      <section className="arch-preview is-confirmed" data-testid="strategy-confirm-bar" aria-label="战略已确认">
        <header className="arch-preview-head">
          <span className="arch-preview-pill is-confirmed">战略已确认</span>
          <div className="arch-preview-title">
            <strong>{route?.title ?? '已按你的确认写入计划'}</strong>
          </div>
          <button
            type="button"
            className="arch-preview-confirm"
            disabled={refining}
            onClick={() => { void refineStrategy(); }}
          >
            {refining ? '正在细化…' : '细化第一阶段'}
          </button>
        </header>
      </section>
    );
  }

  if (!route || stages.length === 0) return null;
  const calibrated = Boolean(reasoning?.datesCalibrated);

  return (
    <section className="arch-preview" data-testid="strategy-architecture-preview" aria-label="战略时间架构预览">
      <header className="arch-preview-head">
        <span className="arch-preview-pill">草案，尚未写入计划</span>
        <div className="arch-preview-title">
          <strong>{route.title}</strong>
          {route.timeframe && <span>{route.timeframe}</span>}
        </div>
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
      <div className="arch-preview-actions">
        <button
          type="button"
          className="arch-preview-confirm"
          data-testid="strategy-confirm"
          onClick={() => {
            // 提案走既有 `proposal -> 用户确认 -> 版本校验 -> 事务写入` 流程,
            // 这里只负责发起,不直接改计划。
            requestChat();
            void agentTurn({ trigger: 'strategy_confirmation', reasoningHandle: route.handle });
          }}
        >
          确认这条战略
        </button>
        <button
          type="button"
          className="arch-preview-adjust"
          data-testid="strategy-adjust"
          onClick={requestChat}
        >
          调整战略
        </button>
        <button type="button" className="arch-preview-link" onClick={onOpenPath}>回到路径页看说明</button>
      </div>
    </section>
  );
}
