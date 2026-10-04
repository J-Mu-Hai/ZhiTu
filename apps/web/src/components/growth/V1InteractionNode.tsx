'use client';

import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { useContext, useState, type PointerEvent as ReactPointerEvent } from 'react';
import type { V1CurrentInteraction } from '@/lib/backend';
import type { V1PhaseKey } from '@/features/growth/v1Workflow';
import {
  QuestionInteractionContext,
  V1InteractionControls,
  type V1NodeActions,
} from './CanvasQuestionNode';

/** A persistent stage action rendered as a child canvas node, never in chat. */
export type V1InteractionNodeData = {
  phaseKey: V1PhaseKey;
  title: string;
  prompt: string;
  interaction: V1CurrentInteraction | null;
  action: 'confirm_goal' | 'continue_strategy' | 'generate_execution' | null;
  isFocused: boolean;
};

export type V1InteractionFlowNode = Node<V1InteractionNodeData, 'v1interaction'>;

export function V1InteractionNodeComponent({ data }: NodeProps<V1InteractionFlowNode>) {
  const context = useContext(QuestionInteractionContext);
  const v1: V1NodeActions | null = context?.v1 ?? null;
  const [busy, setBusy] = useState(false);
  const interaction = data.interaction;

  function press(action: () => Promise<unknown> | void) {
    return (event: ReactPointerEvent) => {
      event.stopPropagation();
      event.preventDefault();
      if (busy) return;
      setBusy(true);
      Promise.resolve(action()).finally(() => setBusy(false));
    };
  }

  return (
    <div
      className={`v1-interaction-node${data.isFocused ? ' is-focused' : ''}`}
      role="group"
      aria-label={`${data.title}：${data.prompt}`}
      data-testid="v1-stage-question"
      data-phase={data.phaseKey}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <span className="v1-interaction-kicker">需要共同确认</span>
      <strong>{data.title}</strong>
      <p>{data.prompt}</p>

      {data.isFocused && interaction && v1 ? (
        <div className="v1-interaction-controls">
          {interaction.whyNow && <p className="cq-why"><span className="cq-label">为什么现在</span>{interaction.whyNow}</p>}
          {interaction.context && <p className="cq-context">{interaction.context}</p>}
          <V1InteractionControls interaction={interaction} v1={v1} />
        </div>
      ) : data.isFocused && data.action === 'confirm_goal' && v1 ? (
        <button type="button" className="cq-submit nodrag" disabled={busy} onPointerDown={press(() => v1.onConfirmGoal())}>
          确认这个目标定义
        </button>
      ) : data.isFocused && data.action === 'continue_strategy' && v1 ? (
        <button type="button" className="cq-submit nodrag" disabled={busy} onPointerDown={press(() => v1.onContinueStrategy())}>
          继续形成战略路径
        </button>
      ) : data.isFocused && data.action === 'generate_execution' && v1 ? (
        <div className="v1-interaction-controls">
          <p className="cq-context">先生成待确认的本周任务；确认写入后，再生成今天的工作块。任务面板与首页只读取确认后的正式计划。</p>
          <button type="button" className="cq-submit nodrag" disabled={busy} onPointerDown={press(() => v1.onRunPlanStep('weekly'))}>
            生成本周任务草案
          </button>
          <button type="button" className="cq-more nodrag" disabled={busy} onPointerDown={press(() => v1.onRunPlanStep('daily'))}>
            已确认周计划后，生成今天工作块
          </button>
        </div>
      ) : (
        <span className="v1-interaction-hint">点开这个节点继续</span>
      )}
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}
