'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Background,
  BaseEdge,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useNodesInitialized,
  getBezierPath,
  getSmoothStepPath,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import {
  ArrowUpRight,
  CheckSquare,
  FileText,
  Flag,
  Focus,
  FolderOpen,
  GitBranch,
  Plus,
  Trash2,
} from 'lucide-react';
import { Dialog } from '@/components/ui/Dialog';
import { useCanvasDraft } from '@/features/growth/drafts';
import { useDemo } from '@/features/growth/provider';
import type { GrowthEdge, GrowthNode, GrowthRelationType } from '@/types/growth';
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
      <div className="node-heading">
        <span className="node-marker" aria-hidden="true">
          {data.root ? <Flag size={21} /> : node.type === 'task'
            ? (node.status === 'completed' ? <CheckSquare size={14} /> : <span className="node-task-box" />)
            : <span className="node-title-dot" />}
        </span>
        <strong className="node-title">{node.title}</strong>
      </div>
      {(node.description || data.root) && <p className="node-description">{node.description || '根目标'}</p>}
      {node.status === 'doing' && <span className="node-doing" />}
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

/** Siblings share an outgoing lane, without drawing extra junction dots. */
function BranchEdge(props: EdgeProps) {
  const forward = props.targetX > props.sourceX + 100;
  const [path] = getSmoothStepPath({
    sourceX: props.sourceX, sourceY: props.sourceY,
    targetX: props.targetX, targetY: props.targetY,
    sourcePosition: props.sourcePosition, targetPosition: props.targetPosition,
    borderRadius: 38, offset: 24,
    ...(forward ? { centerX: props.sourceX + 78 } : {}),
  });
  // Almost level siblings should have a gentle join, not a tiny staircase.
  const nearLevel = forward && Math.abs(props.targetY - props.sourceY) < 12;
  const renderedPath = nearLevel
    ? `M ${props.sourceX},${props.sourceY} C ${props.sourceX + 78},${props.sourceY} ${props.targetX - 60},${props.targetY} ${props.targetX},${props.targetY}`
    : path;
  return <BaseEdge id={props.id} path={renderedPath} style={props.style} />;
}
/**
 * 一条关系边怎么画。**三种类型必须一眼分得开,而且不能只靠颜色深浅。**
 *
 * 这里画的不是装饰:`depends_on` 会真的改变排期(它让后续任务不能早于前置),
 * 另外两种不会。用户看着一条线要能回答"我画的这条会不会让某个任务被推迟"。
 * 所以除了颜色,线型和箭头也各不相同 —— 色觉障碍者、投影仪、截图压缩都还在。
 *
 * | 类型 | 画法 | 有向吗 |
 * | --- | --- | --- |
 * | `depends_on` 前置 → 后续 | 实线、最粗、带箭头 | 有向,箭头从**前置**指向**后续** |
 * | `related_to` 相关 | 虚线、无箭头 | **无向** —— 库里按 UUID 排过序,source 是哪一头不代表方向 |
 * | `influences` 影响 | 点线、带箭头 | 有向 |
 *
 * 颜色只从节点已经在用的那四个里取(`colors`),不引入新色。`related_to` 特意用
 * 紫色而不是蓝色:真实空间里**每一条父子连线都是蓝色**(真实节点没有 `category`,
 * 见 `planProjection`),再拿蓝色画关系线,两种线在画布上就分不开了。
 */
const relationLook: Record<GrowthRelationType, {
  color: string; dash?: string; width: number; arrow: boolean; label: string;
}> = {
  depends_on: { color: colors.research, width: 2, arrow: true, label: '前置' },
  related_to: { color: colors.personal, dash: '6 5', width: 1.6, arrow: false, label: '相关' },
  influences: { color: colors.experience, dash: '2 4', width: 1.6, arrow: true, label: '影响' },
};

function RelationEdge(props: EdgeProps) {
  const kind = (props.data?.kind as GrowthRelationType) ?? 'related_to';
  const note = (props.data?.note as string | undefined) ?? '';
  const look = relationLook[kind] ?? relationLook.related_to;
  const [path, labelX, labelY] = getBezierPath({
    sourceX: props.sourceX, sourceY: props.sourceY, sourcePosition: props.sourcePosition,
    targetX: props.targetX, targetY: props.targetY, targetPosition: props.targetPosition,
    curvature: 0.32,
  });
  return (
    <BaseEdge
      id={props.id}
      path={path}
      // 点得中比好看要紧:线只有一两像素宽,而"点这条线"是打开关系编辑器的入口。
      interactionWidth={24}
      style={{
        stroke: look.color,
        strokeWidth: props.selected ? look.width + 1 : look.width,
        strokeDasharray: look.dash,
        opacity: props.selected ? 1 : 0.9,
      }}
      // 箭头**不能在这里现场造**:`<marker>` 的定义由 ReactFlow 按
      // **边对象上的 `markerEnd`** 生成,再把这个 `url(...)` 字符串传进来
      // (见 `EdgeWrapper` 的 `markerEndUrl`)。自定义边里传一个对象是没有出路的
      // —— 类型上就不接受,渲染出来也指向一个不存在的 marker。所以箭头在下面
      // 那个 memo 里挂在边对象上,这里只把它转交出去。
      markerEnd={props.markerEnd}
      // 说明直接印在线上,而不是藏进编辑器里 —— 用户写了"这条为什么存在",
      // 下一个人(以及三个月后的他自己)应该在图上一眼看到它。
      label={note ? (note.length > 16 ? `${note.slice(0, 16)}…` : note) : undefined}
      labelX={labelX}
      labelY={labelY}
      labelShowBg
      labelBgStyle={{ fill: 'rgba(248, 246, 239, .92)' }}
      labelBgPadding={[4, 2]}
      labelBgBorderRadius={6}
      labelStyle={{ fontSize: 11, fill: look.color }}
    />
  );
}

const edgeTypes = { branch: BranchEdge, relation: RelationEdge };

/**
 * `__zhituCanvasLifecycle` 最多留多少条。理由见 `Canvas` 里那个 effect 的说明。
 *
 * 它是**给测试看的**,所以这个数只对测试有意义;`canvas-stability.spec.ts` 里那条
 * "不许重挂载"的用例会检查"没数到上限"(数到上限那条断言就恒真了)。
 * 改这里的话,顺手看一眼那个用例里写着的同一个数。
 */
const CANVAS_LIFECYCLE_LIMIT = 50;
function Canvas() {
  const {
    growth, selectedId, select, positions, setPositions, spaceId, workspaceId, canvasKey, viewports, setScopeViewport,
    enterSpace, addNode, updateNode, addRelation, updateRelation, removeRelation,
    files, isRealSpace, planSaving, planLoading, planError, setPlanError,
  } = useDemo();
  const { fitView, setViewport } = useReactFlow();
  const nodesInitialized = useNodesInitialized();
  const fittedScope = useRef<string | null>(null);

  /**
   * **组件生命周期记录**:这个层级在这一页里被挂载过几次。
   *
   * 它不是产品逻辑,是给浏览器测试的一个可读证据(`tests/canvas-stability.spec.ts`)。
   * 需要的理由很具体:画布上的输入和视口活在组件内部,而"它们丢了"和"整棵子树被重建了"
   * 从界面上看常常长得一样。测试要能直接问"这中间它重挂载过几次",否则一条断言红了
   * 会分不清是选择器写错了、还是真的重挂了。
   *
   * 记在 `window` 上而不是组件里:**重挂载会把组件里的一切清掉,包括用来记数的 ref。**
   *
   * ## 它**封顶**(2026-09-27 补)
   *
   * 这是写在全局对象上的一根数组,而**正常使用里每切一次视图就会往上加一条** ——
   * 一个整天开着工作台、来回切视图的人会得到一个一直涨、谁也不清的数组。
   * 留最后 `CANVAS_LIFECYCLE_LIMIT` 条就够测试用了:那些断言问的是"这中间**又**挂了没有",
   * 不是"一共挂过几次"。上限比任何一条用例的过程大一个量级,而
   * `canvas-stability.spec.ts` 里"不许重挂载"那条会另看一眼有没有数到上限(数到上限,
   * 它就成了恒真的断言)。**要改成"只在测试模式记录"的话,得让测试能提前告诉这个页面
   * 一声** —— 那要过 `localStorage` 或构建期环境变量,会让"跑开发模式"和"跑验收"
   * 两套路径不一致,收益只有一点点内存,不值当。
   */
  useEffect(() => {
    const holder = window as unknown as { __zhituCanvasLifecycle?: string[] };
    // 就地 push,不每次复制一份新数组:切视图很频繁,复制是 O(n²)。
    const log = holder.__zhituCanvasLifecycle ?? (holder.__zhituCanvasLifecycle = []);
    log.push(canvasKey);
    if (log.length > CANVAS_LIFECYCLE_LIMIT) log.splice(0, log.length - CANVAS_LIFECYCLE_LIMIT);
    // `canvasKey` 故意不进依赖数组:进了的话,同一个组件实例里换层级也会记一笔,
    // "挂载过几次"这个问题就被答错了。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
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
  const [submitting, setSubmitting] = useState(false);
  /**
   * 当前选中的**边**。与节点的 `selectedId` 是两回事:`selectedId` 决定"聚焦所选"
   * 和节点高亮,这个只决定哪条线是加粗的。
   *
   * 它不进草稿存储:选中态不是"用户打了一半的输入",卸载丢掉没有任何损失 ——
   * 和 `selectedId` 一个待遇。
   */
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);

  /**
   * 弹窗与编辑器里**还没提交的输入**。它们住在组件外面 —— 见 `drafts.ts` 的文件头。
   *
   * 这里原来是一排 `useState`。区别只有一处,但很要紧:**切一次视图、或者离开工作台
   * 再回来,这棵子树会被整个卸载**,那时组件里的 state 什么都不剩。用户在"新建节点"
   * 里打了一半的字会自己消失,而界面上不会报任何错 —— 看起来像这个产品记不住东西。
   * 放进草稿存储之后,卸载再重挂载拿到的是同一份输入。
   *
   * 键用的是 `canvasKey`(而不是 `spaceId`):同一个层级在"计划还没到"和"计划到了"
   * 之间会换一次 `spaceId`,而那不是用户换了层级。
   */
  const { draft, patch: patchDraft } = useCanvasDraft(workspaceId, canvasKey);
  const {
    dialog, title, description, type, estimate,
    detailNodeId, detailTitle, detailDescription, detailPriority, detailDeadline, detailEstimate, detailStart, detailEnd,
    relationId, relationType, relationNote, relationSource, relationTarget,
  } = draft;

  /**
   * 关掉一个编辑器,**并丢弃里面那份输入**。
   *
   * 只有用户明确关(×、Esc)才走这里。切视图、离开再回来都**不**走 —— 那是这次修复
   * 的全部要点:卸载对用户来说什么都没发生,他的输入不该因此消失。
   *
   * 两个编辑器各清各的:它们理论上可以同时存在(点节点打开详情,再点"新建节点"),
   * 一起清会让另一个跟着没。
   */
  const closeCreateDialog = () => patchDraft({ dialog: null, title: '', description: '', type: 'task', estimate: '' });
  const closeDetailEditor = () => patchDraft({
    detailNodeId: null, detailTitle: '', detailDescription: '', detailPriority: 'medium',
    detailDeadline: '', detailEstimate: '', detailStart: '', detailEnd: '',
  });
  /**
   * 关掉关系编辑器,并丢掉它那一组输入。
   *
   * `relationId` 一起清掉是要紧的:留着它的话下次点「建立关系」会以"编辑那条边"
   * 的身份打开,而用户以为自己在新建。
   */
  const closeRelationDialog = () => patchDraft({
    dialog: null, relationId: null, relationType: 'related_to', relationNote: '',
    relationSource: '', relationTarget: '',
  });

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
    const vertical = false;

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
        type: 'branch',
        style: {
          stroke: colors[node.category ?? 'academic'],
          opacity: 1,
          strokeWidth: selectedId === node.id ? 1.8 : 1.35,
        },
      });
    }

    // Use measured content height rather than fixed rows: long copy must not overlap.
    const height = (node: GrowthNode) => measurements[node.id]?.height ?? 86;
    const width = (node: GrowthNode) => measurements[node.id]?.width ?? 300;
    const root = growth.nodes[spaceId];
    const childX = width(root) + 190;
    let cursorY = 30;
    const centers: number[] = [];
    directChildren.forEach((node) => {
      const descendants = isRootSpace && node.type === 'capability'
        ? all.filter((child) => child.parentId === node.id && !['attention', 'screen'].includes(child.id)).slice(0, 4)
        : [];
      const descendantHeight = descendants.reduce((sum, child) => sum + height(child) + 32, 0) - (descendants.length ? 32 : 0);
      const blockHeight = Math.max(height(node), descendantHeight);
      const center = cursorY + blockHeight / 2;
      centers.push(center);
      add(node, childX, center - height(node) / 2);
      connect(spaceId, node);
      let descendantY = cursorY;
      descendants.forEach((child) => {
        add(child, childX + width(node) + 170, descendantY);
        connect(node.id, child);
        descendantY += height(child) + 32;
      });
      cursorY += blockHeight + 56;
    });
    const rootCenter = centers.length ? (centers[0] + centers[centers.length - 1]) / 2 : 90;
    add(root, 0, rootCenter - height(root) / 2, true);

    /*
     * 关系边。**只画两头都在场的那一条。**
     *
     * 关系可以连同一空间里任意两个节点,而这一层画出来的只有"根 + 直接子节点"
     * (根层多几个能力节点的后代)。连到别的层级去的边在这里有一头不在场,画出来
     * 会是一条通向画布外的线 —— 那不如不画。见「建立关系」表单里为什么只列这一层
     * 的节点:建了却看不见、点不到的一条边,比"建不了"更让人困惑。
     */
    const inScope = new Set(nextNodes.map((node) => node.id));
    growth.edges.forEach((edge) => {
      if (!inScope.has(edge.source) || !inScope.has(edge.target)) return;
      const look = relationLook[edge.type] ?? relationLook.related_to;
      nextEdges.push({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'relation',
        data: { kind: edge.type, note: edge.note },
        selected: selectedEdgeId === edge.id,
        // 箭头挂在这里,不在 `RelationEdge` 里 —— 见那边的注释。
        markerEnd: look.arrow
          ? { type: MarkerType.ArrowClosed, color: look.color, width: 15, height: 15 }
          : undefined,
      });
    });
    return { nodes: nextNodes, edges: nextEdges };
  }, [growth, spaceId, isRootSpace, selectedId, selectedEdgeId, positions, dragging, files, measurements]);

  // Initial fit must wait for wrapped text to be measured and layout to settle.
  // Do not re-fit while the user drags or edits an already opened scope.
  //
  // **有记忆就回到记忆里,没有才 fit。** 这里以前无条件 fit,于是"切一下视图回来"
  // 就等于"你刚才挪过的位置不算了"。`fittedScope` 也说明了另一半:它是组件里的一个
  // ref,**跟着组件一起被卸载** —— 这就是为什么重挂载会让 fit 再跑一遍。
  useEffect(() => {
    if (!nodesInitialized || planLoading || fittedScope.current === spaceId) return;
    const timer = window.setTimeout(() => {
      fittedScope.current = spaceId;
      const remembered = viewports[spaceId];
      if (remembered) void setViewport(remembered, { duration: 0 });
      else void fitView({ padding: 0.18, maxZoom: 1, duration: 0 });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [nodesInitialized, planLoading, spaceId, measurements, fitView, setViewport, viewports]);

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
  /**
   * 这一层上一次看到的视口。
   *
   * 有记忆时**不用 ReactFlow 自带的初次 fit**(下面那个 `fitView` 属性):它会在挂载
   * 那一刻先 fit 一次,而我们那个 effect 稍后再跳回记忆里的位置 —— 用户会看到画面
   * 先跳一下再回来。少这一次跳动,也让"视口是哪来的"只有一个答案。
   */
  const rememberedViewport = viewports[spaceId];
  function openDetail(node: GrowthNode) {
    select(node.id);
    // 真实的预计工时读**原样的分钟数**(`estimateMinutes`),不读那个四舍五入过的小时
    // ——读后者的话,用户打开编辑器什么也不改、点一下保存,工时就会被改成另一个数。
    // 示例数据里没有分钟那一档,退回小时再换算。空着就是空着:填 0 会让"没填"和
    // "这件事不要时间"变成同一件事。
    //
    // 真实节点的 `startDate`/`endDate` 是从 `deadline` 映出来的(见 planProjection),
    // 所以截止时间读的是同一个值。
    patchDraft({
      detailNodeId: node.id,
      detailTitle: node.title,
      detailDescription: node.description ?? '',
      detailPriority: node.priority,
      detailStart: node.startDate ?? '',
      detailEnd: node.endDate ?? '',
      detailEstimate: node.estimateMinutes ? String(node.estimateMinutes)
        : node.estimatedHours ? String(Math.round(node.estimatedHours * 60))
          : '',
      detailDeadline: isRealSpace ? (node.endDate ?? '') : '',
    });
  }

  /**
   * 这一层能连的节点 —— **就是画布上看得见的那些**,不是这个空间里的全部节点。
   *
   * 见画边那里的注释:连到别的层级去的边在当前这一层看不见(有一头不在场)。
   * 让用户在表单里选中一个画布上没有的节点,得到的是"我建了,但图上找不到" ——
   * 不如把选择范围收成与眼睛看到的一致。跨层关系是步骤 4 的事(那时每层都能看到
   * 挂在自己下面的东西),这一批不含。
   */
  const relationCandidates = nodes.map((node) => node.data.object);
  const nodeLabel = (id: string) => growth.nodes[id]?.title || '（已删除的节点）';

  /** 一条关系用一句话说清方向。**依赖必须写成「前置 → 后续」**,别处也照这个说法。 */
  function relationSentence(kind: GrowthRelationType, from: string, to: string): string {
    // 还没选齐两端时说"请选",不拿空 id 去拼一句「先做「（已删除的节点）」」——
    // 那是一句看起来像数据坏了的话,而实际只是还没选。
    if (!from || !to) return '请选择两端的节点。';
    if (kind === 'depends_on') return `前置 → 后续：先做「${nodeLabel(from)}」，才轮得到「${nodeLabel(to)}」。`;
    if (kind === 'influences') return `影响方向：「${nodeLabel(from)}」影响「${nodeLabel(to)}」。`;
    return `无向关联：「${nodeLabel(from)}」和「${nodeLabel(to)}」是对等的，谁先谁后都一样。`;
  }

  /**
   * 打开「建立关系」表单。
   *
   * 工具栏那个按钮不带参数:起点默认拿当前选中的那个,终点**不给默认** —— 用户多半是
   * "选中一个,再给它连一个",但终点猜错就是一条他马上要删掉的边。两个下拉里都写着
   * "请选择…",选齐之前那个按钮是禁用的,这就是这一屏的全部引导。
   *
   * 拖线进来时(`connectNodes`)两个端点都是他刚拖出来的,**直接预填** —— 那不是猜的。
   * 注意:工具栏上必须写成 `onClick={() => openRelationForm()}`,不能直接传这个函数,
   * 否则点击事件会被当成 `from`。
   */
  function openRelationForm(from?: string, to = '') {
    setPlanError(null);
    const source = from ?? relationCandidates.find((node) => node.id === selectedId)?.id ?? '';
    patchDraft({
      dialog: 'relation', relationId: null, relationType: 'related_to', relationNote: '',
      relationSource: source, relationTarget: to,
    });
  }

  function openRelationEditor(edge: GrowthEdge) {
    setPlanError(null);
    setSelectedEdgeId(edge.id);
    patchDraft({
      dialog: 'relation', relationId: edge.id, relationType: edge.type,
      relationNote: edge.note ?? '', relationSource: edge.source, relationTarget: edge.target,
    });
  }

  /** 表单建边。 */
  async function submitRelationCreate() {
    if (!relationSource || !relationTarget || relationSource === relationTarget || submitting) return;
    setSubmitting(true);
    // 前置关系不带说明:后端那张表没有说明列,带过去会被**拒绝**(不是静默丢掉)。
    // 界面上那个输入框在这种类型下是禁用的,这里是第二道 —— 两道都要有,因为类型
    // 可以在写好说明之后再改。
    const created = await addRelation(
      relationSource, relationTarget, relationType,
      relationType === 'depends_on' ? undefined : relationNote,
    );
    setSubmitting(false);
    // 失败就**不关弹窗**:原因显示在下面那行红字里,他得看得见才谈得上重试。
    if (!created) return;
    closeRelationDialog();
    setSelectedEdgeId(created.id);
  }

  /** 改一条已经存在的边。 */
  async function submitRelationEdit() {
    if (!relationId || submitting) return;
    setSubmitting(true);
    const saved = await updateRelation(relationId, {
      relationType,
      // 前置关系没有说明栏:不传这个字段,而不是传个空串去"清空"一个不存在的东西。
      ...(relationType === 'depends_on' ? {} : { note: relationNote.trim() || null }),
    });
    setSubmitting(false);
    if (!saved) return;
    closeRelationDialog();
  }

  /** 删边。**不删节点** —— 线和点是两件事,这里只发一条 DELETE /relations/{id}。 */
  async function deleteRelation() {
    if (!relationId) return;
    const removed = await removeRelation(relationId);
    if (!removed) return;
    setSelectedEdgeId(null);
    closeRelationDialog();
  }

  /**
   * 从节点上拖一条线到另一个节点。**默认「相关」,而且拖完先不写库。**
   *
   * 拖完打开的是「建立关系」那张表单:起点终点就是拖的方向,类型默认最轻的「相关」。
   * 用户把两个东西拖到一起,系统并不知道那是"前置"还是"相关" —— 而猜成前置会真的
   * 改变排期结果。所以由他自己确认,确认这一下就是这一次写入。
   *
   * ## 为什么不是"先落一条 `related_to`,再让用户在编辑器里改"
   *
   * 这里原来就是那么做的:先 `POST related_to`,再把**返回的那一行**灌进编辑器。
   * 问题出在「相关」是**无向**的 —— 后端按 UUID 排序规范化两端
   * (`node_service._endpoints`),所以返回的行里哪一头是 `source` **不由用户拖的方向决定**。
   * 用户从 A 拖到 B、在编辑器里改成有向的「影响」,存下来的方向就在 A→B 与 B→A 之间
   * 听天由命:有向关系被**静默翻了向**,而这件事一半的运行里看不出来。
   * (`tests/relations.spec.ts` 那条拖线用例第一次红就是撞上了这一半。)
   *
   * 要在"先建后改"这条路上修,得让 PATCH 能改端点,而契约里没有
   * (`UpdateRelationRequest` 只有类型与说明)。所以拖线回到和表单同一条路:
   * **先确认,再写一次** —— 一次写入,方向就是用户拖的那一个。
   */
  function connectNodes(source: string, target: string) {
    if (source === target) { setPlanError('一个节点不能和自己连关系。'); return; }
    openRelationForm(source, target);
  }

  return (
    <div className={`path-canvas ${isRootSpace ? 'root-path' : 'leaf-path'}`}>
      <div className="space-floating-tools">
        {/* 提示里必须写清"怎么连线" —— 拖线这件事没有任何别的入口在教。
            也说明**点线**能打开编辑器:线很细,不提示的话没人会去点它。 */}
        <span>单击编辑内容 · 双击进入子路径 · 拖动节点右侧圆点连线 · 点线可改关系</span>
        <button disabled={!canCreate} title={canCreate ? undefined : '正在读取计划…'} onClick={() => { setPlanError(null); patchDraft({ dialog: 'node' }); }}>
          <Plus size={15} />
          {createLabel}
        </button>
        {/* 表单建边。拖线是快,但触屏、精确对齐、以及"就是要连到某个具体节点"
            这三种情况下拖线都不好用 —— 所以两条路都要有。 */}
        <button
          disabled={!canCreate || relationCandidates.length < 2}
          title={relationCandidates.length < 2 ? '这一层至少要有两个节点才能连关系' : undefined}
          onClick={() => openRelationForm()}
        >
          <GitBranch size={15} />
          建立关系
        </button>
        <button onClick={() => patchDraft({ dialog: 'files' })}>
          <FolderOpen size={15} />
          空间文件 <small>{files.filter((file) => file.ownerId === spaceId).length || ''}</small>
        </button>
      </div>
      <ReactFlow<FlowNode>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        fitView={!rememberedViewport}
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
        // 拖线建边。**在节点上拖,不在空白处拖** —— 空白处拖动是平移画布。
        onConnect={(connection) => {
          if (!connection.source || !connection.target) return;
          cancelPendingOpen();
          connectNodes(connection.source, connection.target);
        }}
        onEdgeClick={(_, edge) => {
          // 这个 handler 只收到关系边(父子连线由 `BranchEdge` 画,没有 onClick)。
          const relation = growth.edges.find((item) => item.id === edge.id);
          if (relation) openRelationEditor(relation);
        }}
        onPaneClick={() => { cancelPendingOpen(); select(null); setSelectedEdgeId(null); }}
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
        onMoveEnd={(_, viewport) => {
          // **初始定位跑完之前不记。** 那之前的视口是 ReactFlow 的默认值(0,0,1),
          // 把它存下来会把用户真正的视口覆盖掉 —— 而"有记忆"的那一层本来就不该 fit,
          // 两边一撞就是"切回来一看,画面跑到别处去了"。
          if (fittedScope.current !== spaceId) return;
          setScopeViewport(spaceId, { x: viewport.x, y: viewport.y, zoom: viewport.zoom });
        }}
        // 连线的入口也要等计划到位:`'goal'` 那个哨兵值不是真节点 id,拖出来的边
        // 发到后端是 422。同 `canCreate` 的理由。
        nodesConnectable={isRealSpace && !planLoading}
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
          <button onClick={() => { setPlanError(null); patchDraft({ dialog: 'node' }); }}>
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
          onClose={closeCreateDialog}
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
              // 建成了才丢草稿:这一份已经变成库里的节点了。
              closeCreateDialog();
              setTimeout(() => void fitView({ duration: 200, padding: 0.2, maxZoom: 1 }), 80);
            }}
          >
            <label>
              {isRootSpace ? '节点名称' : '树叶名称'}
              <input
                autoFocus
                maxLength={80}
                value={title}
                onChange={(event) => patchDraft({ title: event.target.value })}
                placeholder="一个想法、一个行动，或新的方向"
              />
            </label>
            <label>
              {isRootSpace ? '节点类型' : '树叶类型'}
              <select value={type} onChange={(event) => patchDraft({ type: event.target.value as GrowthNode['type'] })}>
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
                onChange={(event) => patchDraft({ description: event.target.value })}
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
                  onChange={(event) => patchDraft({ estimate: event.target.value })}
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
      {/*
        关系编辑器。**一个弹窗两种身份**:
        `relationId` 为空 = 建立一条新的(起点、终点、类型都还没提交);
        非空 = 改一条已经存在的边。

        合并成一个,是因为"新建"和"编辑"在这里要问的东西几乎一样 —— 分开写两份,
        就会出现"新建时能选前置、编辑时忘了禁用"这种两边不一致的漏洞。
      */}
      {dialog === 'relation' && (
        <Dialog title={relationId ? '编辑关系' : '建立关系'} onClose={closeRelationDialog}>
          <form
            className="node-form"
            onSubmit={(event) => {
              event.preventDefault();
              void (relationId ? submitRelationEdit() : submitRelationCreate());
            }}
          >
            {/* 方向那一句话,**建之前也要显示**(表单里两个下拉就是起点/终点),
                建之后更要显示:一条已经存在的边,用户第一件事是问"这是哪个方向的"。 */}
            <p className="relation-direction">
              {relationSentence(relationType, relationSource, relationTarget)}
            </p>

            {/* 起点与终点只在新建立的时候能选。已存在的边**不能改两端** ——
                后端那条 PATCH 只收 type 与 note,"改端点"等于删一条再建一条,
                而删除会丢掉这条边上已经写好的说明。这里不假装能改。 */}
            {!relationId && (
              <>
                <label>
                  {relationType === 'depends_on' ? '前置（先做）' : '起点'}
                  <select value={relationSource} onChange={(event) => patchDraft({ relationSource: event.target.value })}>
                    <option value="">请选择…</option>
                    {relationCandidates.map((node) => (
                      <option key={node.id} value={node.id} disabled={node.id === relationTarget}>{node.title || '（未命名）'}</option>
                    ))}
                  </select>
                </label>
                <label>
                  {relationType === 'depends_on' ? '后续（后做）' : '终点'}
                  <select value={relationTarget} onChange={(event) => patchDraft({ relationTarget: event.target.value })}>
                    <option value="">请选择…</option>
                    {relationCandidates.map((node) => (
                      <option key={node.id} value={node.id} disabled={node.id === relationSource}>{node.title || '（未命名）'}</option>
                    ))}
                  </select>
                </label>
              </>
            )}

            <label>
              关系类型
              <select value={relationType} onChange={(event) => patchDraft({ relationType: event.target.value as GrowthRelationType })}>
                <option value="related_to">相关（无向，不影响排期）</option>
                <option value="influences">影响（有向，不影响排期）</option>
                {/* 「前置」这一档在**编辑**一条非前置关系时是禁用的:后端不支持
                    在 depends_on 与另外两种之间换类型(见 `UpdateRelationRequest`)。
                    把禁用写在这里,而不是等用户点了保存再报错 —— 一个存不进去的
                    选项,不该看起来能选。 */}
                <option value="depends_on" disabled={Boolean(relationId) && relationType !== 'depends_on'}>
                  前置 → 后续（会改变排期）
                </option>
              </select>
            </label>

            {relationType === 'depends_on' ? (
              <p className="field-hint">
                「前置 → 后续」参与排期：后续任务不会排在前置完成之前。它没有说明这一栏
                —— 要记录为什么这么连，请另连一条「相关」或「影响」。
              </p>
            ) : (
              <>
                <label>
                  说明（可选）
                  <textarea
                    maxLength={200}
                    value={relationNote}
                    onChange={(event) => patchDraft({ relationNote: event.target.value })}
                    placeholder="为什么把这两个连起来？写一句，它会显示在线上"
                  />
                </label>
                {relationId && (
                  <p className="field-hint">
                    这一版不能在「前置」与另外两种之间换类型：前置关系存在排期那张表里，
                    换过去会丢掉这条边上已有的说明。要改性质，请新建一条再删掉这条。
                  </p>
                )}
              </>
            )}

            {/* 失败原因必须在这里说。它住在 `planError` 里,而这个弹窗是模态的 ——
                错误显示在别处等于让用户对着一个没反应的按钮反复点。循环依赖(409)、
                重复边(幂等,不会失败)、越权(404)、保存失败都在这里出现。 */}
            {planError && <p className="form-error" role="alert">{planError}</p>}

            <div className="relation-actions">
              <button className="primary-button" disabled={submitting || planSaving || (!relationId && (!relationSource || !relationTarget || relationSource === relationTarget))}>
                {submitting ? '正在保存…' : relationId ? '保存关系' : '建立关系'}
              </button>
              {relationId && (
                <button type="button" className="danger-button" disabled={submitting || planSaving} onClick={() => void deleteRelation()}>
                  <Trash2 size={14} />
                  删除这条关系
                </button>
              )}
            </div>
            <p className="field-hint">删除关系只断开这条线，两端的节点都还在。</p>
          </form>
        </Dialog>
      )}
      {dialog === 'files' && (
        <Dialog title={`${spaceTitle} · 空间文件`} onClose={() => patchDraft({ dialog: null })}>
          <SpaceFiles ownerId={spaceId} />
        </Dialog>
      )}
      {/* `detailNode` 而不是 `detailNodeId`:`spaceId` 那一层里找不到这个节点时就不画
          (它被删了、或者计划正在重取)。草稿仍然留着 —— 节点回来的时候,用户改了一半
          的正文还在,而这里不会闪一个"节点不存在"的空编辑器。 */}
      {detailNode && (
        <Dialog title={`编辑节点 · ${detailNode.title}`} onClose={closeDetailEditor}>
          <form className="node-form" onSubmit={(event) => { event.preventDefault(); updateNode(detailNode.id, isRealSpace
            ? { title: detailTitle.trim() || detailNode.title, description: detailDescription.trim() || undefined, priority: detailPriority, deadline: detailDeadline || null, estimateMinutes: parseEstimate(detailEstimate) }
            : { title: detailTitle.trim() || detailNode.title, description: detailDescription.trim() || undefined, priority: detailPriority, startDate: detailStart || undefined, endDate: detailEnd || undefined }); closeDetailEditor(); }}>
            <label>节点名称<input autoFocus value={detailTitle} maxLength={80} onChange={(event) => patchDraft({ detailTitle: event.target.value })} /></label>
            <label>详细说明<textarea value={detailDescription} maxLength={1000} onChange={(event) => patchDraft({ detailDescription: event.target.value })} placeholder="记录这个节点的目标、约束、判断和下一步…" /></label>
            <div className="node-editor-grid"><label>优先级<select value={detailPriority} onChange={(event) => patchDraft({ detailPriority: event.target.value as GrowthNode['priority'] })}><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></label>
              {/* 真实空间只给"截止时间"这一个日期 —— 后端有这个概念,别的没有。
                  把开始/结束日期也画出来、保存时又悄悄丢掉,用户会以为改了日期而
                  计划没动。排期(哪天做、做多久)在阶段 6。 */}
              {isRealSpace
                ? <>
                    <label>截止时间<input type="date" value={detailDeadline} onChange={(event) => patchDraft({ detailDeadline: event.target.value })} /></label>
                    {/* 预计工时是排期的输入:没有它,这个任务排不进任何一天,
                        「排期」和「今天」都会是空的,而链路上没有一处会报错。 */}
                    <label>
                      预计工时（分钟）
                      <input type="number" min={1} inputMode="numeric" value={detailEstimate} onChange={(event) => patchDraft({ detailEstimate: event.target.value })} placeholder="例如 90" />
                      <small className="field-hint">{estimateHint(detailEstimate)}</small>
                    </label>
                  </>
                : <><label>开始日期<input type="date" value={detailStart} onChange={(event) => patchDraft({ detailStart: event.target.value })} /></label><label>结束日期<input type="date" value={detailEnd} onChange={(event) => patchDraft({ detailEnd: event.target.value })} /></label></>}
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
