'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
  type XYPosition,
} from '@xyflow/react';
import { ArrowUp, Lock, X } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import type { ReasoningNodeView } from '@/lib/backend';

/**
 * 规划智能体重构 V1(P1):阶段一**层级画布 + 节点局部讨论**。
 *
 * ## 它为什么是一棵“折叠的三组树”，而不是平铺的节点海
 *
 * 进入空间时默认只显示根目标、三个一级分组和当前焦点。分组折叠起来,是为了避免
 * 一上来就把十几个卡片铺满画布 —— 那是上一版问卷式体验最刺眼的问题。用户展开某一组,
 * 才看到它下面固定的分析容器。
 *
 * ## 这些不是任务
 *
 * 画布上的分组与分析节点都来自 **reasoning 层**(`v1Kind`),不是 `plan_nodes`。
 * 它们不参与排期、完成度或任务统计;点开只做局部讨论,**不会**生成任务 / 时间线 /
 * 周计划。这一点在界面文案里也说清楚,不让用户以为点一下就会自动排期。
 */

type V1FlowData = {
  role: 'root' | 'group' | 'analysis';
  title: string;
  summary: string;
  status: string;
  nodeKey: string;
  handle: string;
  count: number;
  expanded: boolean;
  pending: boolean;
  focused: boolean;
  onToggle: (key: string) => void;
  onOpen: (handle: string) => void;
};

function RootNode({ data }: NodeProps<Node<V1FlowData>>) {
  return (
    <div className="v1-node v1-node-root" role="group" aria-label={`根目标:${data.title}`}>
      <span className="v1-node-eyebrow">根目标</span>
      <strong>{data.title}</strong>
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}

function GroupNode({ data }: NodeProps<Node<V1FlowData>>) {
  return (
    <div
      className={`v1-node v1-node-group ${data.focused ? 'is-focus' : ''}`}
      role="group"
      aria-label={`分组:${data.title}`}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <button
        type="button"
        className="v1-node-toggle"
        aria-expanded={data.expanded}
        onClick={() => data.onToggle(data.nodeKey)}
      >
        <span className="v1-node-chevron" aria-hidden>{data.expanded ? '▾' : '▸'}</span>
        <strong>{data.title}</strong>
        <span className="v1-node-count">{data.count}</span>
      </button>
      {data.summary && <p className="v1-node-summary">{data.summary}</p>}
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}

function AnalysisNode({ data }: NodeProps<Node<V1FlowData>>) {
  return (
    <div
      className={`v1-node v1-node-analysis ${data.focused ? 'is-focus' : ''}`}
      role="group"
      aria-label={`分析节点:${data.title}`}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <button type="button" className="v1-node-open" onClick={() => data.onOpen(data.handle)}>
        <span className="v1-node-status">{data.pending ? '待讨论' : '已澄清'}</span>
        <strong>{data.title}</strong>
      </button>
      {data.summary && <p className="v1-node-summary">{data.summary}</p>}
    </div>
  );
}

const nodeTypes = { v1Root: RootNode, v1Group: GroupNode, v1Analysis: AnalysisNode };

/** 一句话摘要:去掉“（待验证）”前缀,只留正文,过长截断。 */
function oneLine(text: string | null): string {
  const value = (text ?? '').replace(/^（待验证）/, '').trim();
  return value.length > 46 ? `${value.slice(0, 46)}…` : value;
}

function layoutKey(workspaceId: string): string {
  return `zhitu:v1-layout:${workspaceId}`;
}

function readLayout(workspaceId: string): Record<string, XYPosition> {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(layoutKey(workspaceId));
    return raw ? (JSON.parse(raw) as Record<string, XYPosition>) : {};
  } catch {
    return {};
  }
}

function Detail({
  node,
  busy,
  onClose,
  onAnswer,
  onEdit,
}: {
  node: ReasoningNodeView;
  busy: boolean;
  onClose: () => void;
  onAnswer: (text: string) => void;
  onEdit: (patch: { userDescription?: string; status?: string }) => void;
}) {
  const [text, setText] = useState('');
  const [body, setBody] = useState(node.userDescription ?? '');
  useEffect(() => {
    setBody(node.userDescription ?? '');
    setText('');
  }, [node.id, node.userDescription]);

  const pending = node.status === 'unexplored' || node.status === 'exploring';
  return (
    <aside className="v1-detail" role="dialog" aria-label={`分析节点:${node.title}`}>
      <header>
        <div>
          <span className="eyebrow">局部讨论</span>
          <strong>{node.title}</strong>
        </div>
        <button type="button" aria-label="关闭局部讨论" onClick={onClose}><X size={15} /></button>
      </header>

      <section className="v1-detail-block">
        <h4>AI 暂定判断</h4>
        <p>{node.summary ?? '（待验证）还没有判断。'}</p>
        {node.rationale && <p className="v1-detail-why">为什么影响整体战略：{node.rationale}</p>}
      </section>

      <section className="v1-detail-block">
        <h4>已知事实</h4>
        {node.evidence.length > 0 ? (
          <ul>{node.evidence.map((item, index) => <li key={index}>{item}</li>)}</ul>
        ) : (
          <p className="v1-detail-empty">目前还没有来自你的确认信息。</p>
        )}
      </section>

      <section className="v1-detail-block">
        <h4>尚未确认之处</h4>
        {node.assumptions.length > 0 ? (
          <ul>{node.assumptions.map((item, index) => <li key={index}>{item}</li>)}</ul>
        ) : (
          <p className="v1-detail-empty">没有额外假设。</p>
        )}
        {node.v1Question && <p className="v1-detail-question">需要你确认：{node.v1Question}</p>}
      </section>

      <section className="v1-detail-block">
        <h4>你的补充（原文，AI 不会覆盖）</h4>
        <textarea
          aria-label="你的补充"
          rows={3}
          value={body}
          placeholder="写下你对这个节点的判断或事实…"
          onChange={event => setBody(event.target.value)}
        />
        <button
          type="button"
          className="v1-detail-save"
          onClick={() => onEdit({ userDescription: body })}
        >
          保存原文
        </button>
      </section>

      <form
        className="v1-detail-ask"
        onSubmit={event => {
          event.preventDefault();
          const value = text.trim();
          if (!value || busy) return;
          onAnswer(value);
          setText('');
        }}
      >
        <input
          aria-label="回答这个节点"
          value={text}
          placeholder="回答这个节点的问题…"
          onChange={event => setText(event.target.value)}
        />
        <button type="submit" aria-label="发送回答" disabled={!text.trim() || busy}>
          <ArrowUp size={15} />
        </button>
      </form>
      <p className="v1-detail-footnote">
        <Lock size={12} /> 只更新这个节点；不会生成任务、时间线或周计划。
        {pending ? '' : ' 它已标记为已澄清。'}
      </p>
    </aside>
  );
}

function Canvas() {
  const { reasoning, growth, workspaceId, agentTurn, editReasoningNode } = useDemo();
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [openHandle, setOpenHandle] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [positions, setPositions] = useState<Record<string, XYPosition>>(() =>
    readLayout(workspaceId),
  );

  const groups = useMemo(
    () =>
      (reasoning?.nodes ?? [])
        .filter(node => node.v1Kind === 'group' || node.v1Kind === 'strategy')
        .sort((a, b) => a.handle.localeCompare(b.handle)),
    [reasoning],
  );
  const analyses = useMemo(
    () => (reasoning?.nodes ?? []).filter(node => node.v1Kind === 'analysis'),
    [reasoning],
  );

  const rootTitle = growth.nodes[growth.goalId]?.title ?? '根目标';
  const openNode = analyses.find(node => node.handle === openHandle) ?? null;

  const onNodesChange = useCallback((changes: NodeChange<Node<V1FlowData>>[]) => {
    setPositions(prev => {
      let next = prev;
      for (const change of changes) {
        if (change.type === 'position' && change.position) {
          if (next === prev) next = { ...prev };
          next[change.id] = change.position;
        }
      }
      return next;
    });
  }, []);

  // 拖动结束后把位置存进本地(按空间隔离)。P1 不落后端 —— 它只是用户偏好。
  useEffect(() => {
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.setItem(layoutKey(workspaceId), JSON.stringify(positions));
    } catch {
      /* 隐私模式等写不进去:位置只影响观感,不阻塞任何流程。 */
    }
  }, [positions, workspaceId]);

  const toggle = useCallback((key: string) => {
    setExpanded(prev => ({ ...prev, [key]: !prev[key] }));
  }, []);

  const onOpen = useCallback(
    (handle: string) => {
      setOpenHandle(handle);
      void agentTurn({ trigger: 'node_selected', reasoningHandle: handle });
    },
    [agentTurn],
  );

  const nodes = useMemo<Node<V1FlowData>[]>(() => {
    const rootId = `root:${growth.goalId}`;
    const result: Node<V1FlowData>[] = [
      {
        id: rootId,
        type: 'v1Root',
        draggable: false,
        position: positions[rootId] ?? { x: 0, y: 0 },
        data: {
          role: 'root',
          title: rootTitle,
          summary: '',
          status: '',
          nodeKey: 'root',
          handle: rootId,
          count: 0,
          expanded: true,
          pending: false,
          focused: false,
          onToggle: toggle,
          onOpen,
        },
      },
    ];
    groups.forEach((group, index) => {
      const id = `group:${group.v1Key ?? group.handle}`;
      const x = (index - (groups.length - 1) / 2) * 360;
      const y = 190;
      const children = analyses.filter(node => node.parentHandle === group.handle);
      result.push({
        id,
        type: 'v1Group',
        position: positions[id] ?? { x, y },
        data: {
          role: 'group',
          title: group.title,
          summary: group.v1Kind === 'strategy' ? '待形成战略路径' : oneLine(group.summary),
          status: group.status,
          nodeKey: group.v1Key ?? group.handle,
          handle: group.handle,
          count: children.length,
          expanded: Boolean(expanded[group.v1Key ?? group.handle]),
          pending: group.status === 'unexplored',
          focused: reasoning?.focusHandle === group.handle,
          onToggle: toggle,
          onOpen,
        },
      });
      if (expanded[group.v1Key ?? group.handle]) {
        children.forEach((child, childIndex) => {
          const childId = `analysis:${child.v1Key ?? child.handle}`;
          result.push({
            id: childId,
            type: 'v1Analysis',
            position: positions[childId] ?? { x, y: y + 150 + childIndex * 150 },
            data: {
              role: 'analysis',
              title: child.title,
              summary: oneLine(child.summary),
              status: child.status,
              nodeKey: child.v1Key ?? child.handle,
              handle: child.handle,
              count: 0,
              expanded: true,
              pending: child.status === 'unexplored' || child.status === 'exploring',
              focused: reasoning?.focusHandle === child.handle,
              onToggle: toggle,
              onOpen,
            },
          });
        });
      }
    });
    return result;
  }, [analyses, expanded, growth.goalId, onOpen, positions, reasoning?.focusHandle, rootTitle, toggle, groups]);

  const edges = useMemo<Edge[]>(() => {
    const rootId = `root:${growth.goalId}`;
    const result: Edge[] = [];
    groups.forEach(group => {
      const groupId = `group:${group.v1Key ?? group.handle}`;
      result.push({
        id: `edge:${rootId}->${groupId}`,
        source: rootId,
        target: groupId,
        type: 'smoothstep',
        markerEnd: { type: MarkerType.ArrowClosed },
        className: 'v1-edge',
      });
      if (expanded[group.v1Key ?? group.handle]) {
        analyses
          .filter(node => node.parentHandle === group.handle)
          .forEach(child => {
            const childId = `analysis:${child.v1Key ?? child.handle}`;
            result.push({
              id: `edge:${groupId}->${childId}`,
              source: groupId,
              target: childId,
              type: 'smoothstep',
              markerEnd: { type: MarkerType.ArrowClosed },
              className: 'v1-edge',
            });
          });
      }
    });
    return result;
  }, [analyses, expanded, growth.goalId, groups]);

  const focusNode = analyses.find(node => node.handle === reasoning?.focusHandle) ?? null;
  // 初始思考阶段画布只有根目标:不显示整体判断/焦点横幅 —— 那两样要等用户提交后才有。
  const initial = reasoning?.v1Stage === 'initial_thinking';

  return (
    <div className="v1-canvas" data-testid="v1-canvas">
      {!initial && <div className="v1-banner">
        <div className="v1-banner-judgment">
          <span className="eyebrow">整体判断</span>
          <p>{reasoning?.v1Judgment ?? '正在判断这个目标…'}</p>
        </div>
        {reasoning?.v1Question && (
          <div className="v1-banner-question">
            <span className="eyebrow">先确认一件事</span>
            <p>{reasoning.v1Question}</p>
          </div>
        )}
        {focusNode && (
          <p className="v1-banner-focus">当前焦点：{focusNode.title}</p>
        )}
      </div>}
      <div className="v1-canvas-flow">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          fitView
          minZoom={0.4}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={22} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      {openNode && (
        <Detail
          node={openNode}
          busy={busy}
          onClose={() => setOpenHandle(null)}
          onAnswer={text => {
            setBusy(true);
            void agentTurn({
              trigger: 'user_message',
              reasoningHandle: openNode.handle,
              message: text,
            }).finally(() => setBusy(false));
          }}
          onEdit={patch => {
            void editReasoningNode(openNode.id, patch);
          }}
        />
      )}
    </div>
  );
}

export function V1Canvas() {
  return (
    <ReactFlowProvider>
      <Canvas />
    </ReactFlowProvider>
  );
}
