'use client';

import { useMemo, useState } from 'react';
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import {
  ArrowUpRight,
  Check,
  FileText,
  Flag,
  Focus,
  FolderOpen,
  FlaskConical,
  GraduationCap,
  Package,
  Plus,
  Sprout,
} from 'lucide-react';
import { Dialog } from '@/components/ui/Dialog';
import { useDemo } from '@/features/growth/provider';
import type { GrowthNode } from '@/types/growth';
import { SpaceFiles } from './SpaceFiles';

type FlowNode = Node<{
  object: GrowthNode;
  root: boolean;
  children: number;
  files: number;
  vertical: boolean;
}, 'growth'>;

const icons = {
  academic: GraduationCap,
  research: FlaskConical,
  experience: Package,
  personal: Sprout,
};
const colors = {
  academic: '#749ce1',
  research: '#61ad9e',
  experience: '#c7a06e',
  personal: '#a294ce',
};

function GrowthNodeComponent({ data, selected }: NodeProps<FlowNode>) {
  const { enterSpace } = useDemo();
  const node = data.object;
  const Icon = node.category ? icons[node.category] : Flag;
  const sourcePosition = data.vertical ? Position.Bottom : Position.Right;
  const targetPosition = data.vertical ? Position.Top : Position.Left;

  return (
    <div
      onDoubleClick={(event) => {
        event.stopPropagation();
        enterSpace(node.id);
      }}
      className={`growth-node ${data.root ? 'goal' : node.type} ${node.category ?? ''} ${selected ? 'is-selected' : ''} ${node.status === 'completed' ? 'is-complete' : ''}`}
    >
      {!data.root && <Handle type="target" position={targetPosition} />}
      {data.root ? (
        <>
          <Flag size={23} />
          <strong>{node.title}</strong>
          <span>{node.id === 'goal' ? '2027 · AI 方向' : `${data.children} 片树叶 · 中心路径`}</span>
        </>
      ) : node.type === 'capability' ? (
        <>
          <Icon size={22} />
          <div>
            <strong>{node.title}</strong>
            <span>{node.description || `${data.children} 片树叶`}</span>
          </div>
        </>
      ) : (
        <>
          <span className="node-bullet">
            {node.type === 'milestone' ? '◇' : node.status === 'completed' ? <Check size={12} /> : '·'}
          </span>
          <div className="leaf-copy">
            <span className="leaf-title">{node.title}</span>
            {node.description && <small className="leaf-summary">{node.description}</small>}
          </div>
          {node.status === 'doing' && <span className="node-doing" />}
        </>
      )}
      {!data.root && (
        <button
          className="node-enter nodrag"
          aria-label={`进入${node.title}空间`}
          onClick={(event) => {
            event.stopPropagation();
            enterSpace(node.id);
          }}
        >
          <ArrowUpRight size={12} />
        </button>
      )}
      {data.files > 0 && (
        <span className="node-file-count">
          <FileText size={10} />
          {data.files}
        </span>
      )}
      <Handle type="source" position={sourcePosition} />
    </div>
  );
}

const nodeTypes = { growth: GrowthNodeComponent };

function Canvas() {
  const { growth, selectedId, select, positions, setPositions, spaceId, enterSpace, addNode, files } = useDemo();
  const { fitView } = useReactFlow();
  const [measurements, setMeasurements] = useState<Record<string, { width: number; height: number }>>({});
  const [dragging, setDragging] = useState<Record<string, { x: number; y: number }>>({});
  const [dialog, setDialog] = useState<'node' | 'files' | null>(null);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [type, setType] = useState<GrowthNode['type']>('task');
  const direct = Object.values(growth.nodes).filter((node) => node.parentId === spaceId && node.type !== 'stage');
  const isRootSpace = spaceId === 'goal';

  const { nodes, edges } = useMemo(() => {
    const nextNodes: FlowNode[] = [];
    const nextEdges: Edge[] = [];
    const all = Object.values(growth.nodes);
    const directChildren = all.filter((node) => node.parentId === spaceId && node.type !== 'stage');
    const vertical = spaceId !== 'goal';

    function add(node: GrowthNode, x: number, y: number, root = false) {
      const key = `${spaceId}:${node.id}`;
      nextNodes.push({
        id: node.id,
        measured: measurements[node.id],
        type: 'growth',
        data: {
          object: node,
          root,
          children: all.filter((child) => child.parentId === node.id).length,
          files: files.filter((file) => file.ownerId === node.id).length,
          vertical,
        },
        position: dragging[key] ?? positions[key] ?? { x, y },
        selected: selectedId === node.id,
        ariaLabel: node.title,
      });
    }

    function connect(parent: string, node: GrowthNode) {
      nextEdges.push({
        id: `${parent}-${node.id}`,
        source: parent,
        target: node.id,
        type: 'smoothstep',
        style: {
          stroke: colors[node.category ?? 'academic'],
          opacity: selectedId === node.id ? 0.7 : 0.4,
          strokeWidth: 1,
        },
      });
    }

    if (vertical) {
      // 子空间以当前主题为中心，树叶从中心向下生长。三列布局可在内容增加时保持可读性。
      const columns = Math.min(Math.max(directChildren.length, 1), 3);
      const canvasWidth = (columns - 1) * 230 + 190;
      add(growth.nodes[spaceId], (canvasWidth - 155) / 2, 20, true);
      directChildren.forEach((node, index) => {
        const column = index % columns;
        const row = Math.floor(index / columns);
        add(node, column * 230, 230 + row * 145);
        connect(spaceId, node);
      });
    } else {
      add(growth.nodes[spaceId], 0, Math.max(20, (directChildren.length - 1) * 125), true);
      directChildren.forEach((node, index) => {
        const x = 260;
        const y = 20 + index * 250;
        add(node, x, y);
        connect(spaceId, node);
        if (node.type === 'capability') {
          all
            .filter((child) => child.parentId === node.id)
            .filter((child) => !['attention', 'screen'].includes(child.id))
            .slice(0, 4)
            .forEach((child, childIndex) => {
              add(child, x + 235, y - 20 + childIndex * 54);
              connect(node.id, child);
            });
        }
      });
    }

    growth.edges.forEach((edge) => {
      if (nextNodes.some((node) => node.id === edge.source) && nextNodes.some((node) => node.id === edge.target)) {
        nextEdges.push({
          ...edge,
          type: 'default',
          style: { stroke: '#78a89a', strokeDasharray: '4 5', opacity: 0.45 },
        });
      }
    });
    return { nodes: nextNodes, edges: nextEdges };
  }, [growth, spaceId, selectedId, positions, dragging, files, measurements]);

  const createLabel = isRootSpace ? '新建节点' : '添加树叶';

  return (
    <div className={`path-canvas ${isRootSpace ? 'root-path' : 'leaf-path'}`}>
      <div className="space-floating-tools">
        <span>{isRootSpace ? '点击四个成长分类进入专属路径' : '当前主题位于中心 · 树叶从下方生长'}</span>
        <button onClick={() => setDialog('node')}>
          <Plus size={15} />
          {createLabel}
        </button>
        <button onClick={() => setDialog('files')}>
          <FolderOpen size={15} />
          空间文件 <small>{files.filter((file) => file.ownerId === spaceId).length || ''}</small>
        </button>
      </div>
      <ReactFlow<FlowNode>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.18, maxZoom: 1 }}
        zoomOnDoubleClick={false}
        minZoom={0.25}
        maxZoom={1.7}
        onNodeClick={(_, node) => {
          const object = node.data.object;
          if (spaceId === 'goal' && object.parentId === 'goal' && object.type === 'capability') {
            enterSpace(node.id);
            return;
          }
          select(node.id);
        }}
        onNodeDoubleClick={(_, node) => enterSpace(node.id)}
        onPaneClick={() => select(null)}
        onNodesChange={(changes) => {
          for (const change of changes) {
            if (change.type === 'position' && change.position) {
              setDragging((old) => ({ ...old, [`${spaceId}:${change.id}`]: change.position! }));
            }
            if (change.type === 'dimensions' && change.dimensions) {
              const dimensions = change.dimensions;
              setMeasurements((old) =>
                old[change.id]?.width === dimensions.width && old[change.id]?.height === dimensions.height
                  ? old
                  : { ...old, [change.id]: dimensions },
              );
            }
          }
        }}
        onNodeDragStop={(_, node) =>
          setPositions((old) => ({ ...old, [`${spaceId}:${node.id}`]: node.position }))
        }
        nodesConnectable={false}
        deleteKeyCode={null}
        colorMode="light"
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={28} size={1} color="#dfe6ef" />
        <MiniMap
          position="bottom-left"
          style={{ width: 120, height: 78 }}
          nodeColor={(node) => colors[(node.data.object as GrowthNode).category ?? 'academic']}
          maskColor="rgba(245,248,252,.72)"
          pannable
          zoomable
        />
        <Controls position="bottom-right" showInteractive={false} />
      </ReactFlow>
      {direct.length === 0 && (
        <div className="empty-space-note">
          <span>这里，还可以长出更多可能。</span>
          <button onClick={() => setDialog('node')}>
            <Plus size={14} />
            {isRootSpace ? '添加第一个子节点' : '添加第一片树叶'}
          </button>
        </div>
      )}
      <button
        className="focus-button"
        disabled={!selectedId || !nodes.some((node) => node.id === selectedId)}
        onClick={() => {
          if (selectedId) void fitView({ nodes: [{ id: selectedId }], duration: 220, maxZoom: 1.2, padding: 0.8 });
        }}
      >
        <Focus size={15} />
        聚焦所选
      </button>
      {dialog === 'node' && (
        <Dialog
          title={isRootSpace ? `在「${growth.nodes[spaceId].title}」中新建节点` : `为「${growth.nodes[spaceId].title}」添加树叶`}
          onClose={() => setDialog(null)}
        >
          <form
            className="node-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!title.trim()) return;
              addNode(title, type, description);
              setTitle('');
              setDescription('');
              setDialog(null);
              setTimeout(() => void fitView({ duration: 200, padding: 0.2, maxZoom: 1 }), 80);
            }}
          >
            <label>
              {isRootSpace ? '节点名称' : '树叶名称'}
              <input
                autoFocus
                maxLength={80}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="一个想法、一个行动，或新的方向"
              />
            </label>
            <label>
              {isRootSpace ? '节点类型' : '树叶类型'}
              <select value={type} onChange={(event) => setType(event.target.value as GrowthNode['type'])}>
                <option value="task">行动</option>
                <option value="capability">能力 / 方向</option>
                <option value="milestone">里程碑</option>
              </select>
            </label>
            <label>
              {isRootSpace ? '节点说明（可选）' : '树叶说明'}
              <textarea
                maxLength={240}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="写清楚这片树叶要积累什么、下一步做什么"
              />
            </label>
            <p>每片树叶也可以继续进入，生长成自己的子路径。</p>
            <button className="primary-button" disabled={!title.trim()}>
              {createLabel}
            </button>
          </form>
        </Dialog>
      )}
      {dialog === 'files' && (
        <Dialog title={`${growth.nodes[spaceId].title} · 空间文件`} onClose={() => setDialog(null)}>
          <SpaceFiles ownerId={spaceId} />
        </Dialog>
      )}
    </div>
  );
}

export function PathView() {
  return (
    <ReactFlowProvider>
      <Canvas />
    </ReactFlowProvider>
  );
}
