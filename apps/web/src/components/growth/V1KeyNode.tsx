'use client';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';

/**
 * 「想清楚」下的**问题框架节点**。
 *
 * 第一个战略分析之后,「想清楚」下面只生成固定的 4 个框架节点:
 *
 * ```
 * 想清楚
 * ├─ 最终想做到什么？ ── 具体问题
 * ├─ 你现在在哪？     ── 具体问题
 * ├─ 现实能投入什么？ ── 具体问题
 * └─ 什么算真正完成？ ── 具体问题
 * ```
 *
 * 框架标题是固定产品文案(`V1_KEY_QUESTIONS`);它下面的**具体问题**才是画布上可
 * 回答的问题节点。框架节点本身只做归类与状态,不承载长解释(长解释在右侧对话)。
 */
export type V1KeyData = {
  frameKey: string;
  title: string;
  /** 该框架下是否还挂着一个待回答的具体问题。 */
  hasQuestion: boolean;
  /** 分析维度状态(resolved / investigating / pending)。 */
  status: string;
  isFocused: boolean;
};

export type V1KeyFlowNode = Node<V1KeyData, 'v1key'>;

const STATUS_LABEL: Record<string, string> = {
  resolved: '已确认',
  investigating: '讨论中',
  pending: '待讨论',
  deferred: '已暂缓',
  archived: '已归档',
};

export function V1KeyNodeComponent({ data }: NodeProps<V1KeyFlowNode>) {
  // `isFocused` 是服务端状态机唯一的当前讨论焦点；不能因为多个节点都有
  // 后端分析记录，就把它们都标成“讨论中”。
  const badge = data.isFocused
    ? '当前讨论'
    : STATUS_LABEL[data.status] ?? (data.hasQuestion ? '待讨论' : '已确认');
  return (
    <div
      className={`v1-key-node${data.isFocused ? ' is-focus' : ''}${data.hasQuestion ? '' : ' is-done'}`}
      role="group"
      aria-label={`关键问题：${data.title}`}
      data-testid="v1-key"
      data-frame-key={data.frameKey}
    >
      <Handle type="target" id="key-in" position={Position.Top} isConnectable={false} />
      <p className="v1-key-title">{data.title}</p>
      <span className="v1-key-badge">{badge}</span>
      <Handle type="source" id="key-out" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}
