'use client';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { v1PhaseStateLabel, type V1PhaseKey, type V1PhaseView } from '@/features/growth/v1Workflow';

/**
 * V1 工作流的**阶段容器节点**。
 *
 * `想清楚 / 排出来 / 做起来` 是主轴上的三个阶段。它们是纯投影(见
 * `features/growth/v1Workflow.ts`),不是 `plan_nodes`。
 *
 * - 默认紧凑:序号 + 标题 + 状态徽标 + 一句职责说明;
 * - 当前阶段静态高亮,不做动画;
 * - 结构化问题与确认按钮都作为这个阶段的子节点出现，阶段卡不承载问答。
 */
export type V1PhaseData = {
  phase: V1PhaseView;
  childCount: number;
  isActive: boolean;
  isFocused: boolean;
};

export type V1PhaseFlowNode = Node<V1PhaseData, 'v1phase'>;

const INDEX: Record<V1PhaseKey, string> = { think: '01', plan: '02', do: '03' };

export function V1PhaseNodeComponent({ data }: NodeProps<V1PhaseFlowNode>) {
  const phase = data.phase;

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
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="v1-phase-head">
        <span className="v1-phase-index">{INDEX[phase.key]}</span>
        <strong className="v1-phase-title">{phase.title}</strong>
        <span className="v1-phase-badge">{v1PhaseStateLabel(phase.state)}</span>
      </div>
      <p className="v1-phase-subtitle">{phase.subtitle}</p>

      {data.isFocused ? (
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
