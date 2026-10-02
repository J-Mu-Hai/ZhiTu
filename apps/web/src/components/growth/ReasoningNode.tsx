'use client';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import type { ReasoningNodeView } from '@/lib/backend';

/**
 * 目标推理地图节点。
 *
 * ## 它**不是**业务节点
 *
 * 它代表一个决策维度 / 问题 / 风险 / 资源 / 路线 / 假设,不参与排期、任务统计、
 * 依赖或执行记录。画布上它和 `growth`、`question` 是三种不同的节点类型:颜色、
 * 边框、状态徽标都不同,一眼分得开。
 *
 * ## 用户字段与 Agent 字段分区
 *
 * `summary` 是 Agent 维护的摘要;`userDescription` 是用户原文,**两者分区显示**。
 * Agent 的任何一轮都不会覆盖用户字段(标题一旦被用户改过也会锁定)。
 *
 * ## 打开详情只有**一个**入口
 *
 * 这一层原来在根元素上挂 `onPointerDown` 打开详情,而 ReactFlow 的 `onNodeClick`
 * 也做同一件事 —— 一次点击会走两条路。现在统一由 `onNodeClick` 打开(见 `PathView`),
 * 这一层不再自己抢指针事件:指针按下也不代表用户要打开它(拖、点空、点关闭都可能)。
 */
export type ReasoningFlowData = {
  node: ReasoningNodeView;
  isFocus: boolean;
};

export type ReasoningFlowNode = Node<ReasoningFlowData, 'reasoning'>;

const STATUS_LABEL: Record<string, string> = {
  unexplored: '未探索',
  exploring: '探索中',
  resolved: '已澄清',
  paused: '已暂缓',
  archived: '已归档',
};

const TYPE_LABEL: Record<string, string> = {
  dimension: '决策维度',
  question: '问题',
  risk: '风险',
  resource: '资源',
  route: '路线',
  assumption: '假设',
};

export function ReasoningNodeComponent({ data }: NodeProps<ReasoningFlowNode>) {
  const node = data.node;
  const status = node.status;
  const classes = [
    'reasoning-node',
    `reasoning-${status}`,
    data.isFocus ? 'is-focus' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div
      className={classes}
      role="group"
      aria-label={`推理节点:${node.title}`}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="rn-head">
        <span className="rn-badge">{STATUS_LABEL[status] ?? status}</span>
        <span className="rn-type">{TYPE_LABEL[node.nodeType] ?? node.nodeType}</span>
        {data.isFocus && <span className="rn-focus">当前焦点</span>}
      </div>
      <strong className="rn-title">{node.title}</strong>
      {node.summary && <p className="rn-summary">{node.summary}</p>}
      {node.userDescription && (
        <p className="rn-user">
          <span className="rn-user-label">你写的</span>
          {node.userDescription}
        </p>
      )}
      {data.isFocus && node.rationale && <p className="rn-why">为什么先处理它:{node.rationale}</p>}
      {node.assumptions.length > 0 && (
        <p className="rn-assumption">含 {node.assumptions.length} 条假设</p>
      )}
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}
