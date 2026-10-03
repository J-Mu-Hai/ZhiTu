'use client';
import { useState } from 'react';
import { ChevronDown, Target } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';

/**
 * “当前战略判断摘要”(阶段 10)。
 *
 * ## 它是什么
 *
 * 一条**低干扰**的战略摘要:当前目标重述、推荐路线、总时长区间、当前阶段、当前最大
 * 风险 / 待确认取舍,以及一句“为什么这样安排”。它放在对话区首屏,不遮住路线画布;
 * 点击可展开完整的战略说明。
 *
 * ## 它从哪里来
 *
 * 全部来自**已验证的 reasoning route/stage 与当前焦点**(`GoalReasoningView`)以及
 * 当前活动问题的 `decisionImpact`。**前端不硬编码、不编造** —— 每一项都指向服务端
 * 真正落库的那个字段。没有路线时整块不渲染,而不是显示一张空卡片。
 */
export function StrategySummaryCard() {
  const { reasoning, brief, questions, growth, spaceId } = useDemo();
  const [open, setOpen] = useState(false);

  const nodes = reasoning?.nodes ?? [];
  const route = nodes.find(node => node.nodeType === 'route') ?? null;
  const stages = nodes
    .filter(node => node.nodeType === 'stage')
    .sort((a, b) => a.handle.localeCompare(b.handle, 'en', { numeric: true }));
  if (!route && stages.length === 0) return null;

  const focus = nodes.find(node => node.id === reasoning?.focusReasoningNodeId) ?? null;
  const currentStage =
    (focus?.nodeType === 'stage' ? focus : null) ??
    stages.find(stage => stage.handle === focus?.parentHandle) ??
    stages[0] ??
    null;

  // 当前最大风险 / 待确认取舍:优先用当前活动问题的“你的选择会影响”。
  const pendingQuestion = questions.find(question => question.status === 'pending') ?? null;
  const risk =
    pendingQuestion?.decisionImpact?.trim() ||
    focus?.rationale?.trim() ||
    route?.rationale?.trim() ||
    null;

  const goal = brief?.goal?.trim() || growth.nodes[spaceId]?.title?.trim() || null;
  const why = route?.rationale?.trim() || reasoning?.focusReason?.trim() || null;

  return (
    <section className="strategy-summary" data-testid="strategy-summary" aria-label="当前战略判断摘要">
      <button
        type="button"
        className="strategy-summary-head"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        <span className="strategy-summary-icon" aria-hidden="true"><Target size={13} /></span>
        <span className="strategy-summary-title">
          <span className="eyebrow">当前战略判断</span>
          <strong>{route?.title ?? stages[0]?.title ?? '目标梳理中'}</strong>
        </span>
        <ChevronDown size={14} className={open ? 'strategy-summary-chevron is-open' : 'strategy-summary-chevron'} />
      </button>

      <dl className="strategy-summary-grid">
        <div>
          <dt>目标</dt>
          <dd>{goal ?? '—'}</dd>
        </div>
        <div>
          <dt>推荐路线</dt>
          <dd>{route?.title ?? '—'}</dd>
        </div>
        <div>
          <dt>总时长</dt>
          <dd>{route?.timeframe ?? '—'}</dd>
        </div>
        <div>
          <dt>当前阶段</dt>
          <dd>{currentStage?.title ?? '—'}</dd>
        </div>
        <div>
          <dt>待确认</dt>
          <dd>{risk ?? '—'}</dd>
        </div>
      </dl>

      {open && (
        <div className="strategy-summary-detail">
          {why && <p className="strategy-summary-why">为什么这样安排：{why}</p>}
          {stages.length > 0 && (
            <ol className="strategy-summary-stages">
              {stages.map(stage => (
                <li key={stage.id}>
                  <strong>{stage.title}</strong>
                  {stage.timeframe && <span>{stage.timeframe}</span>}
                  {stage.deliverable && <em>成果：{stage.deliverable}</em>}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </section>
  );
}
