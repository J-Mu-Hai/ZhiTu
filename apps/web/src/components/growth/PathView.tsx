'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
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
  CheckSquare,
  Circle,
  FileText,
  Flag,
  Focus,
  FolderOpen,
  Plus,
  Trash2,
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

const colors = {
  academic: '#749ce1',
  research: '#61ad9e',
  experience: '#c7a06e',
  personal: '#a294ce',
};

function GrowthNodeComponent({ data, selected }: NodeProps<FlowNode>) {
  // 这里**不挂 `onDoubleClick`**。双击进入子空间统一由 Canvas 的 `onNodeDoubleClick`
  // 处理 —— 原因见那边 `pendingOpen` 的注释:编辑器弹窗会在第一次点击后就盖住画布,
  // 双击的第二次点击落在弹窗遮罩上,节点上的 `dblclick` 永远不会触发。两边都挂的话,
  // 看似双保险,实际是两条路径抢同一次操作。
  const { enterSpace, deleteNode } = useDemo();
  const node = data.object;
  const sourcePosition = data.vertical ? Position.Bottom : Position.Right;
  const targetPosition = data.vertical ? Position.Top : Position.Left;

  return (
    <div
      className={`growth-node ${data.root ? 'goal' : node.type} ${node.category ?? ''} ${selected ? 'is-selected' : ''} ${node.status === 'completed' ? 'is-complete' : ''}`}
    >
      {!data.root && <Handle type="target" position={targetPosition} />}
      {data.root ? (
        <>
          <Flag size={23} />
          <strong>{node.title}</strong>
          {/* 这里原来写死了 `'2027 · AI 方向'` —— 演示数据主人的目标年份和方向。
              它在**每一个**空间的根目标下面都印着,包括一个叫「Python 学习」的新空间:
              用户在画布正中看到一句自己从没说过的话。现在只显示真实存在的描述。 */}
          <span>{node.description || (data.root ? '根目标' : `${data.children} 片树叶 · 中心路径`)}</span>
        </>
      ) : node.type === 'capability' ? (
        <>
          <span className="node-category-dot" aria-hidden="true" />
          <div>
            <strong>{node.title}</strong>
            <span>{node.description || `${data.children} 片树叶`}</span>
          </div>
        </>
      ) : (
        <>
          <span className="node-bullet">
            {node.status === 'completed' ? <CheckSquare size={13} /> : <Circle size={8} fill="currentColor" />}
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
          className="node-delete nodrag nopan"
          aria-label={`删除${node.title}及其子节点`}
          title={data.children > 0 ? `删除该节点及 ${data.children} 个直接子节点` : '删除该节点'}
          onClick={(event) => {
            event.stopPropagation();
            deleteNode(node.id);
          }}
        >
          <Trash2 size={12} />
        </button>
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
  const { growth, selectedId, select, positions, setPositions, spaceId, enterSpace, addNode, updateNode, files, isRealSpace, planSaving, planLoading, planError, setPlanError } = useDemo();
  const { fitView } = useReactFlow();
  /**
   * 待打开的节点详情。
   *
   * 单击节点要打开编辑器,双击要进入子空间 —— 而浏览器在双击时**先发两次 click**。
   * 直接在第一下 click 里开弹窗的话,弹窗的遮罩会在第二下之前盖住画布,`dblclick`
   * 落在遮罩上,于是"双击进入子路径"(弹窗里自己印着的那句话)永远不会发生:用户
   * 双击一个节点,得到的是打开编辑器两次。
   *
   * 所以第一次点击**先等 240 毫秒**。这个窗口里来了 dblclick 就取消,没有才开编辑器。
   * 240 毫秒是双击的常见上限,人感觉不到;而"双击没反应"是立刻能感觉到的。
   */
  const pendingOpen = useRef<number | null>(null);
  const cancelPendingOpen = () => {
    if (pendingOpen.current !== null) { window.clearTimeout(pendingOpen.current); pendingOpen.current = null; }
  };
  useEffect(() => cancelPendingOpen, []);
  const [measurements, setMeasurements] = useState<Record<string, { width: number; height: number }>>({});
  const [dragging, setDragging] = useState<Record<string, { x: number; y: number }>>({});
  const [dialog, setDialog] = useState<'node' | 'files' | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [type, setType] = useState<GrowthNode['type']>('task');
  const [detailNodeId, setDetailNodeId] = useState<string | null>(null);
  const [detailTitle, setDetailTitle] = useState('');
  const [detailDescription, setDetailDescription] = useState('');
  const [detailPriority, setDetailPriority] = useState<GrowthNode['priority']>('medium');
  // 真实空间里编辑的是**截止时间**(后端有这个概念);示例空间里编辑的是开始/结束
  // 日期(那是演示数据自带的)。两个不同的东西,不共用一个输入框。
  const [detailDeadline, setDetailDeadline] = useState('');
  /**
   * 预计工时。**它是排期的输入,不是一句备注。**
   *
   * 后端的排期算法拿 `estimate_minutes` 去切场次;一个没有工时的叶子节点排不进去,
   * 只会得到一条"这个任务没有工时"的缺口。这个输入框以前不存在,于是从浏览器里
   * 建出来的每个任务都排不了 —— 「排期」永远是空的,「今天」也永远是空的,而链路上
   * 没有任何地方会报错。所以它必须在新建时就能填,而不是建完再回来补。
   *
   * 单位用**分钟**(后端就是这个单位)。用户在"预计多久"上想的是"90 分钟""两小时",
   * 而小时的换算由输入框下面那行提示实时显示出来。
   */
  const [estimate, setEstimate] = useState('');
  const [detailEstimate, setDetailEstimate] = useState('');
  const [detailStart, setDetailStart] = useState('');
  const [detailEnd, setDetailEnd] = useState('');

  /**
   * 把输入框里那串字变成后端的分钟数。
   *
   * 三种情况分开处理,不能混成一个 `Number(...)`:空着 = "没说"(发 null),
   * 填了合法正数 = 就用它,别的(0、负数、`abc`)= 当成没说 —— 但**要在界面上说明**
   * 它是被忽略的,否则用户填了 0 会以为系统记下了"这件事不要时间"。
   */
  function parseEstimate(raw: string): number | null {
    const trimmed = raw.trim();
    if (!trimmed) return null;
    const value = Number(trimmed);
    if (!Number.isFinite(value) || value <= 0) return null;
    return Math.max(1, Math.round(value));
  }

  const estimateHint = (raw: string) => {
    const minutes = parseEstimate(raw);
    if (!raw.trim()) return '不填的话，排期排不进去这一天要做多久。';
    if (minutes === null) return '请填一个大于 0 的分钟数，否则会被当成没填。';
    return minutes >= 60 ? `约 ${Math.round(minutes / 6) / 10} 小时` : `${minutes} 分钟`;
  };
  // **这里原来是 `&& node.type !== 'stage'`。** 那个过滤是"AI 生成的阶段在图上看不见"
  // 的直接原因:确认一份计划之后,根目标下面挂着的正是那几个阶段,而它们被这一句
  // 全部滤掉了 —— 画布上什么都不剩,反而显示"这里还可以长出更多可能"。
  // 后端全是好的,接口也返回了 200,没有任何东西会报错。
  const direct = Object.values(growth.nodes).filter((node) => node.parentId === spaceId);
  const isRootSpace = spaceId === growth.goalId;
  // 对话框标题用。真实空间的根节点标题可能是空的(建空间时用户只填了空间名),
  // 那时退回空间名 —— 对话框上写着「在「」中新建节点」是一句废话。
  const spaceTitle = growth.nodes[spaceId]?.title || growth.title;

  const { nodes, edges } = useMemo(() => {
    const nextNodes: FlowNode[] = [];
    const nextEdges: Edge[] = [];
    const all = Object.values(growth.nodes);
    const directChildren = all.filter((node) => node.parentId === spaceId);
    const vertical = !isRootSpace;

    // 兜底:`spaceId` 的契约是"一定指得到一个节点"(见 provider 里的 `currentSpaceId`)。
    // 万一将来这个契约被破坏,**这里不画比整个页面崩掉好** —— 在渲染中抛异常会把整棵
    // React 树卸掉,用户看到的是 "Application error: a client-side exception has
    // occurred",连左边导航都没了。少画一个节点难看,但那个状态是可用的、可导航的,
    // 而且节点数对不上的断言会立刻指出问题。
    if (!growth.nodes[spaceId]) return { nodes: [], edges: [] };

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
  }, [growth, spaceId, isRootSpace, selectedId, positions, dragging, files, measurements]);

  const createLabel = isRootSpace ? '新建节点' : '添加树叶';
  /**
   * 计划还没从后端拿到的时候**不能新建节点**。
   *
   * 那时 `spaceId` 是哨兵值 `'goal'`,而 `POST /nodes` 的 `parentId` 要的是一个真
   * UUID —— 发过去是 422。用户看到的是"点了创建、弹窗关了、什么都没有",因为那条
   * 错误进的是 `planError`,而它只显示在**详情**弹窗里,对创建弹窗不可见。
   *
   * 这个窗口通常只有几百毫秒,禁掉是诚实的:数据还没到,就还不该能改它。
   */
  const canCreate = !isRealSpace || !planLoading;
  const detailNode = detailNodeId ? growth.nodes[detailNodeId] : null;
  function openDetail(node: GrowthNode) {
    select(node.id); setDetailNodeId(node.id); setDetailTitle(node.title); setDetailDescription(node.description ?? '');
    setDetailPriority(node.priority); setDetailStart(node.startDate ?? ''); setDetailEnd(node.endDate ?? '');
    // 真实的预计工时读**原样的分钟数**(`estimateMinutes`),不读那个四舍五入过的小时
    // ——读后者的话,用户打开编辑器什么也不改、点一下保存,工时就会被改成另一个数。
    // 示例数据里没有分钟那一档,退回小时再换算。空着就是空着:填 0 会让"没填"和
    // "这件事不要时间"变成同一件事。
    setDetailEstimate(
      node.estimateMinutes ? String(node.estimateMinutes)
        : node.estimatedHours ? String(Math.round(node.estimatedHours * 60))
          : '',
    );
    // 真实节点的 `startDate`/`endDate` 是从 `deadline` 映出来的(见 planProjection),
    // 所以这里读的是同一个值。排期上线后它会换成真正的场次范围。
    setDetailDeadline(isRealSpace ? (node.endDate ?? '') : '');
  }

  return (
    <div className={`path-canvas ${isRootSpace ? 'root-path' : 'leaf-path'}`}>
      <div className="space-floating-tools">
        <span>{isRootSpace ? '点击四个成长分类进入专属路径' : '当前主题位于中心 · 树叶从下方生长'}</span>
        <button disabled={!canCreate} title={canCreate ? undefined : '正在读取计划…'} onClick={() => { setPlanError(null); setDialog('node'); }}>
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
          // 见 `pendingOpen`:先等一个双击窗口,真双击来了就取消,不当成"单击"。
          cancelPendingOpen();
          pendingOpen.current = window.setTimeout(() => { pendingOpen.current = null; openDetail(object); }, 240);
        }}
        onNodeDoubleClick={(_, node) => { cancelPendingOpen(); enterSpace(node.id); }}
        onPaneClick={() => { cancelPendingOpen(); select(null); }}
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
        <Background gap={28} size={1} color="rgba(124, 153, 188, .14)" />
        <MiniMap
          position="bottom-left"
          style={{ width: 120, height: 78 }}
          nodeColor={(node) => colors[(node.data.object as GrowthNode).category ?? 'academic']}
          maskColor="rgba(238,244,244,.78)"
          pannable
          zoomable
        />
        <Controls position="bottom-right" showInteractive={false} />
      </ReactFlow>
      {direct.length === 0 && (
        <div className="empty-space-note">
          <span>这里，还可以长出更多可能。</span>
          <button onClick={() => { setPlanError(null); setDialog('node'); }}>
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
          title={isRootSpace ? `在「${spaceTitle}」中新建节点` : `为「${spaceTitle}」添加树叶`}
          onClose={() => setDialog(null)}
        >
          <form
            className="node-form"
            onSubmit={async (event) => {
              event.preventDefault();
              if (!title.trim() || submitting) return;
              setSubmitting(true);
              // **失败了就不关弹窗。** 关掉的话用户看到的是"我填了、点了、没了",
              // 而失败原因在下面那行红字里 —— 他得看得见才谈得上重试。
              const created = await addNode(title, type, description, parseEstimate(estimate));
              setSubmitting(false);
              if (!created) return;
              setTitle('');
              setDescription('');
              setEstimate('');
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
            {/* 预计工时。**只有真实空间问它** —— 示例空间没有排期算法,问了也
                没有东西会用它,而一个填了却没有下文的输入框是在骗人。
                放在这里而不是"建完再说",是因为排期读的正是这个字段:一个没有
                工时的任务排不进任何一天。 */}
            {isRealSpace && (
              <label>
                预计要做多久（分钟）
                <input
                  type="number"
                  min={1}
                  inputMode="numeric"
                  value={estimate}
                  onChange={(event) => setEstimate(event.target.value)}
                  placeholder="例如 90"
                />
                <small className="field-hint">{estimateHint(estimate)}</small>
              </label>
            )}
            <p>每片树叶也可以继续进入，生长成自己的子路径。</p>
            {/* 创建失败时**必须在这里说**。这条错误以前只出现在详情弹窗里,于是创建
                失败看起来像"点了没反应",而用户会再点一次。 */}
            {isRealSpace && planError && <p className="form-error" role="alert">{planError}</p>}
            <button className="primary-button" disabled={!title.trim() || submitting || planSaving}>
              {submitting ? '正在保存…' : createLabel}
            </button>
          </form>
        </Dialog>
      )}
      {dialog === 'files' && (
        <Dialog title={`${spaceTitle} · 空间文件`} onClose={() => setDialog(null)}>
          <SpaceFiles ownerId={spaceId} />
        </Dialog>
      )}
      {detailNode && (
        <Dialog title={`编辑节点 · ${detailNode.title}`} onClose={() => setDetailNodeId(null)}>
          <form className="node-form" onSubmit={(event) => { event.preventDefault(); updateNode(detailNode.id, isRealSpace
            ? { title: detailTitle.trim() || detailNode.title, description: detailDescription.trim() || undefined, priority: detailPriority, deadline: detailDeadline || null, estimateMinutes: parseEstimate(detailEstimate) }
            : { title: detailTitle.trim() || detailNode.title, description: detailDescription.trim() || undefined, priority: detailPriority, startDate: detailStart || undefined, endDate: detailEnd || undefined }); setDetailNodeId(null); }}>
            <label>节点名称<input autoFocus value={detailTitle} maxLength={80} onChange={(event) => setDetailTitle(event.target.value)} /></label>
            <label>详细说明<textarea value={detailDescription} maxLength={1000} onChange={(event) => setDetailDescription(event.target.value)} placeholder="记录这个节点的目标、约束、判断和下一步…" /></label>
            <div className="node-editor-grid"><label>优先级<select value={detailPriority} onChange={(event) => setDetailPriority(event.target.value as GrowthNode['priority'])}><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></label>
              {/* 真实空间只给"截止时间"这一个日期 —— 后端有这个概念,别的没有。
                  把开始/结束日期也画出来、保存时又悄悄丢掉,用户会以为改了日期而
                  计划没动。排期(哪天做、做多久)在阶段 6。 */}
              {isRealSpace
                ? <>
                    <label>截止时间<input type="date" value={detailDeadline} onChange={(event) => setDetailDeadline(event.target.value)} /></label>
                    {/* 预计工时是排期的输入:没有它,这个任务排不进任何一天,
                        「排期」和「今天」都会是空的,而链路上没有一处会报错。 */}
                    <label>
                      预计工时（分钟）
                      <input type="number" min={1} inputMode="numeric" value={detailEstimate} onChange={(event) => setDetailEstimate(event.target.value)} placeholder="例如 90" />
                      <small className="field-hint">{estimateHint(detailEstimate)}</small>
                    </label>
                  </>
                : <><label>开始日期<input type="date" value={detailStart} onChange={(event) => setDetailStart(event.target.value)} /></label><label>结束日期<input type="date" value={detailEnd} onChange={(event) => setDetailEnd(event.target.value)} /></label></>}
            </div>
            <p>{isRealSpace ? '双击节点可进入其子路径；保存会立刻写入计划，并产生一个新的计划版本。' : '双击节点可进入其子路径；在这里保存的说明会保留在当前成长空间。'}</p>
            {isRealSpace && planError && <p className="form-error" role="alert">{planError}</p>}
            <button className="primary-button" disabled={!detailTitle.trim() || planSaving}>{planSaving ? '保存中…' : '保存节点'}</button>
          </form>
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
