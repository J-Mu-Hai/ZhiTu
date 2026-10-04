'use client';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { useContext } from 'react';
import type { V1CurrentInteraction } from '@/lib/backend';
import { v1PhaseStateLabel, type V1PhaseKey, type V1PhaseView } from '@/features/growth/v1Workflow';
import {
  QuestionInteractionContext,
  V1InteractionControls,
  type V1NodeActions,
} from './CanvasQuestionNode';

/**
 * V1 工作流的**阶段容器节点**。
 *
 * `想清楚 / 排出来 / 做起来` 是主轴上的三个阶段。它们是纯投影(见
 * `features/growth/v1Workflow.ts`),不是 `plan_nodes`。
 *
 * - 默认紧凑:序号 + 标题 + 状态徽标 + 一句职责说明;
 * - 当前阶段静态高亮,不做动画;
 * - 需要确认战略 / 时间节奏 / 时间架构时,结构控件在这里展开 —— **对话区不再承载它们**。
 */
export type V1PhaseData = {
  phase: V1PhaseView;
  childCount: number;
  interaction: V1CurrentInteraction | null;
  isActive: boolean;
  isFocused: boolean;
  goalConfirmable: boolean;
  continueStrategy: boolean;
  /** 「做起来」阶段是否显示三个生成入口。 */
  runPlanEnabled: boolean;
};

export type V1PhaseFlowNode = Node<V1PhaseData, 'v1phase'>;

const INDEX: Record<V1PhaseKey, string> = { think: '01', plan: '02', do: '03' };

export function V1PhaseNodeComponent({ data }: NodeProps<V1PhaseFlowNode>) {
  const interaction = useContext(QuestionInteractionContext);
  const v1: V1NodeActions | null = interaction?.v1 ?? null;
  const phase = data.phase;
  const nodeInteraction = data.interaction;

  const classes = [
    'v1-phase-node',
    `is-${phase.state}`,
    data.isActive ? 'is-active' : '',
    data.isFocused ? 'is-focused' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className={classes} role="group" aria-label={`阶段：${phase.title}`}>
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="v1-phase-head">
        <span className="v1-phase-index">{INDEX[phase.key]}</span>
        <strong className="v1-phase-title">{phase.title}</strong>
        <span className="v1-phase-badge">{v1PhaseStateLabel(phase.state)}</span>
      </div>
      <p className="v1-phase-subtitle">{phase.subtitle}</p>

      {data.isFocused && nodeInteraction && v1 ? (
        <div className="cq-interaction" data-testid="v1-phase-interaction">
          {nodeInteraction.whyNow && (
            <p className="cq-why"><span className="cq-label">为什么现在</span>{nodeInteraction.whyNow}</p>
          )}
          {nodeInteraction.context && <p className="cq-context">{nodeInteraction.context}</p>}
          <span className="cq-label cq-label-question">{nodeInteraction.title || '需要你确认的一点'}</span>
          <p className="cq-question">{nodeInteraction.prompt}</p>
          <V1InteractionControls interaction={nodeInteraction} v1={v1} />
        </div>
      ) : data.isFocused && (data.goalConfirmable || data.continueStrategy) && v1 ? (
        <div className="cq-interaction" data-testid="v1-phase-flow-action">
          {data.goalConfirmable && (
            <button
              type="button"
              className="cq-submit nodrag"
              onPointerDown={event => { event.stopPropagation(); event.preventDefault(); void v1.onConfirmGoal(); }}
            >
              确认这个目标定义
            </button>
          )}
          {data.continueStrategy && (
            <button
              type="button"
              className="cq-submit nodrag"
              onPointerDown={event => { event.stopPropagation(); event.preventDefault(); void v1.onContinueStrategy(); }}
            >
              继续形成战略路径
            </button>
          )}
        </div>
      ) : data.isFocused && data.runPlanEnabled && phase.key === 'do' && v1 ? (
        <div className="cq-interaction" data-testid="v1-phase-plan-actions">
          <button
            type="button"
            className="cq-submit nodrag"
            onPointerDown={event => { event.stopPropagation(); event.preventDefault(); void v1.onRunPlanStep('weekly'); }}
          >
            生成本周计划
          </button>
          <button
            type="button"
            className="cq-submit nodrag"
            onPointerDown={event => { event.stopPropagation(); event.preventDefault(); void v1.onRunPlanStep('daily'); }}
          >
            生成日计划
          </button>
          <button
            type="button"
            className="cq-submit nodrag"
            onPointerDown={event => { event.stopPropagation(); event.preventDefault(); void v1.onRunPlanStep('review'); }}
          >
            本周回顾
          </button>
        </div>
      ) : data.isFocused ? (
        <p className="v1-phase-note">
          {phase.state === 'done'
            ? '这一阶段已完成。'
            : phase.state === 'locked'
              ? '完成上一阶段后解锁。'
              : data.childCount > 0
                ? `这一阶段有 ${data.childCount} 个需要共同判断的节点。`
                : '这一阶段还没有需要处理的节点。'}
        </p>
      ) : data.isActive ? (
        <p className="v1-phase-hint">当前阶段，点开继续。</p>
      ) : null}

      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}
