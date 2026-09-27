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
  Archive,
  ArrowUpRight,
  CheckSquare,
  FileText,
  Flag,
  Focus,
  FolderOpen,
  GitBranch,
  Plus,
  Redo2,
  RotateCcw,
  Trash2,
  Undo2,
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

/**
 * 焦点是不是在一个**能写字的地方**。
 *
 * `Ctrl+Z` 在正文、说明、日期、搜索框里的意思是"撤销我刚写的那几个字" —— 那是浏览器的
 * 本行工作,而画布的位置**不该**跟着动。用户在一个 textarea 里按撤销、结果画布上的节点
 * 跳回去了,这是最难解释的一种 bug:他撤销的是文字,却看到别的东西变了。
 *
 * `contenteditable` 也要算进来(`isContentEditable`),将来正文换成富文本编辑器时
 * 这一条就是唯一还拦着它的地方。
 */
function isTextEntry(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element || typeof element.tagName !== 'string') return false;
  if (element.isContentEditable) return true;
  const tag = element.tagName.toUpperCase();
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
}

function GrowthNodeComponent({ data, selected }: NodeProps<FlowNode>) {
  // 这里**不挂 `onDoubleClick`**,而"双击进子空间"这件事本身也已经没有了(步骤 4):
  // 单击开正文与详情,进子空间只走右上角那个箭头按钮(下面那个 `node-enter`)。
  // 两件事拆开之后,节点上就不该再有任何"看时间/看次数"的隐藏语义。
  const { enterSpace, askArchive } = useDemo();
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
          aria-label={`归档${node.title}及其子节点`}
          title={data.children > 0 ? `归档该节点及 ${data.children} 个直接子节点(可以恢复)` : '归档该节点(可以恢复)'}
          onClick={(event) => {
            event.stopPropagation();
            // **不直接删。** 先问一句"这一下会带走什么" —— 后代、关系、依赖、场次。
            // 那些数字由后端算(见 `askArchive`),不是从画布上这份计划里数的。
            askArchive(node.id);
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

/**
 * 正文停手多久才发一次保存。
 *
 * 与布局那一条(`provider.tsx` 的 `LAYOUT_SAVE_DEBOUNCE_MS = 600`)同一个量级,理由
 * 一样:太短会把一段话拆成十几次请求,太长会让"我改完就切走"丢掉最后那几秒。
 */
const BODY_SAVE_DEBOUNCE_MS = 700;

/**
 * 编辑器里那段正文此刻是什么状态。**这六种都要能显示出来** —— 少一种,用户就会
 * 在"没存上"的时候以为存上了。
 */
type BodyNote =
  | { kind: 'none' }
  | { kind: 'saving' }
  /** 真保存成功了,带一个时刻。**只有真拿到成功响应才会出现这一条。** */
  | { kind: 'saved'; at: string }
  | { kind: 'failed'; message: string }
  /** 别处改过同一段正文。带上服务端那一份,由用户决定留哪一段。 */
  | { kind: 'conflict'; serverBody: string; message: string };

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
    growth, selectedId, select, positions, commitNodeMove, spaceId, workspaceId, canvasKey, viewports, setScopeViewport,
    // `enterSpace` **不在这里取**:进入子空间只剩节点右上角那个按钮(在自己的
    // 子组件里取,见 `GrowthNodeComponent`)和面包屑。画布本身不再需要它 ——
    // 这是拆开单击语义之后顺带掉下来的一处:那条双击路径是它在这里唯一的用处。
    addNode, updateNode, saveNodeBody, addRelation, updateRelation, removeRelation,
    files, isRealSpace, planSaving, planLoading, planError, setPlanError,
    layoutReady, layoutError, retryLayoutSave,
    undoLayout, redoLayout, canUndo, canRedo, historyNote, setHistoryNote,
    archiveConfirm, closeArchiveConfirm, confirmArchive,
    archiveOpen, setArchiveOpen, archived, archiveListError, archiveNote, setArchiveNote,
    restoreArchived, restoringId,
  } = useDemo();
  const { fitView, setViewport } = useReactFlow();
  const nodesInitialized = useNodesInitialized();
  const fittedScope = useRef<string | null>(null);
  /** 正在被拖的那个节点**动手前**在哪。见 `onNodeDragStart` / `commitNodeMove`。 */
  const dragStart = useRef<{ id: string; position: { x: number; y: number } } | null>(null);

  /**
   * 画布上的撤销/重做快捷键。`Ctrl+Z` / `Ctrl+Shift+Z` / `Ctrl+Y`,Mac 上是 `Cmd`。
   *
   * 挂在 `window` 上而不是画布元素上:焦点几乎总在别处(刚点完按钮、刚点完节点),
   * 要求用户先点一下画布才能按快捷键,等于这个快捷键一半的时候不管用。
   *
   * ## 两处**放行**,一处不放行
   *
   * - 焦点在能写字的地方 → **放行给浏览器**(见 `isTextEntry`),自己一步也不动。
   * - 有模态弹窗开着 → 直接不管。焦点那时可能在弹窗里的某个按钮上(刚点过「保存节点」),
   *   而用户看着的是弹窗 —— 那时画布在背后悄悄动一下,他根本不会知道发生过什么。
   * - 其余情况:吃掉这一下(`preventDefault`),免得浏览器同时去撤销别的东西。
   */
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      const undo = key === 'z' && !event.shiftKey;
      const redo = (key === 'z' && event.shiftKey) || key === 'y';
      if (!undo && !redo) return;
      if (isTextEntry(event.target)) return;
      if (document.querySelector('dialog[open]')) return;
      event.preventDefault();
      if (undo) undoLayout(); else redoLayout();
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [undoLayout, redoLayout]);

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
  /* **这里原来有一个 `pendingOpen`(240 毫秒的双击窗口),已经删掉(步骤 4)。**
     它当时解决的问题是真的:单击开编辑器、双击进子空间,而浏览器双击会先发两次
     click,第一次 click 就把弹窗盖上去,第二次落在遮罩上,`dblclick` 永远不来。
     于是第一下点击先等 240 毫秒,窗口里来了 dblclick 就取消。

     但那个折中有它自己的代价,而且是用户能感觉到的:**每一次查看正文都要先等 240
     毫秒**,而且"双击"这条路径永远依赖一个计时器——点快了、点慢了、系统卡了一下,
     同一个动作会给出两种结果。任务书 §4.5 要的正是把这两件事拆开,不要让单击同时
     承担两件。

     现在:**单击 = 打开正文与详情(立刻),进入子空间只有两个入口** —— 节点右上角
     那个箭头按钮,和上面的面包屑。两者都是"按一下就知道会发生什么"的动作,
     不再需要任何计时器。 */
  const [measurements, setMeasurements] = useState<Record<string, { width: number; height: number }>>({});
  /**
   * **拖动中**的位置预览。节点画的是 `dragging[key] ?? positions[key] ?? 自动排布`。
   *
   * 它存在的理由:拖动过程中 `positions` 一个字节都不变(位置是拖完那一下才提交的),
   * 而画布会因为别的原因重渲染 —— 那时 ReactFlow 拿到的是旧的 `nodes`,节点会弹回原处。
   *
   * 它**必须**在拖动结束时被扔掉(`onNodeDragStop`),否则拖过一次的节点在画布上就永远
   * 看这一份,`positions` 的真值被挡在后面 —— 撤销会因此"按了没反应"。
   */
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

  /* ------------------------- 正文自动保存(步骤 4)的状态 ------------------------- */
  /** 编辑器里此刻的正文。**存的是 ref**:`flushBody` 是从定时器里被调用的,
   *  那时闭包里的 `detailDescription` 可能已经旧了一帧,而"发出去的必须是用户
   *  最后写下的那一段"。 */
  const detailDescriptionRef = useRef('');
  /** 手上这份正文是第几版(乐观锁)。打开编辑器时取,每次保存成功用**后端回来的**
   *  那个号覆盖。见 `openDetail` 与 `flushBody`。 */
  const bodyVersion = useRef<number | undefined>(undefined);
  /** 有没有一次保存在飞。用 ref 不用 state:它是"效果里的守卫",不参与渲染。 */
  const bodyInFlight = useRef(false);
  /** 上一次真保存成功的时刻。与 `bodyNote` 分开存,是因为状态要能回到"未保存"
   *  而这句话还得留着(用户想知道"我刚才那次到底存上没有")。 */
  const bodySavedAt = useRef<string | null>(null);
  const [bodyNote, setBodyNote] = useState<BodyNote>({ kind: 'none' });

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
    // `layoutReady` 那一项是步骤 3B 加的,补上这条注释里原来缺的一环:布局也会
    // **从后端**回来了(位置和视口)。不等它就 fit,等于拿一个默认视角盖掉用户上次
    // 摆好的位置 —— 而更糟的是那一次 fit 产生的视口会被当成"用户设的视口"存回去,
    // 把库里那一份也改掉。等它问过之后:有记忆的层级回到记忆里(下面的分支),
    // 没有记忆的才 fit。
    if (!nodesInitialized || planLoading || !layoutReady || fittedScope.current === spaceId) return;
    const timer = window.setTimeout(() => {
      fittedScope.current = spaceId;
      const remembered = viewports[spaceId];
      if (remembered) void setViewport(remembered, { duration: 0 });
      else void fitView({ padding: 0.18, maxZoom: 1, duration: 0 });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [nodesInitialized, planLoading, layoutReady, spaceId, measurements, fitView, setViewport, viewports]);

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
    // 打开的这一份是哪一版。**必须在打开的那一刻取**,不能等到保存的时候再去读
    // `detailNode.contentVersion`:那中间可能已经刷新过好几次计划,拿到的就是"最新
    // 那一版",而这个号正是用来发现"我手上这份旧了"的,它一"自动变新",锁就没了。
    bodyVersion.current = node.contentVersion;
    bodySavedAt.current = null;
    setBodyNote({ kind: 'none' });
  }

  /* ---------------------------------------------------------------------------
     正文的自动保存(步骤 4)
     ---------------------------------------------------------------------------
     「正文可靠保存」这句话拆开是三件事,缺一件这个功能就等于没有:

     1. **不用记得按保存。** 写两行字就切走,回来还在 —— 那才是"可靠"。所以正文
        边打边存(停手 700 毫秒发一次),而不是靠用户记得去点按钮。
     2. **存不上要看得见,而且草稿不许丢。** 失败时那段字**留在编辑器里**(它本来就
        在草稿存储里,见 `drafts.ts`),旁边写清"没存上"和一个能用的重试。
        **绝不显示"已保存"** —— 那是这个功能最容易犯、也最难被发现的错。
     3. **别人改过同一段正文时,不许悄悄盖掉。** 这就是 `contentVersion` 那个乐观锁:
        对不上就是 409,由用户决定留哪一段。见 `saveNodeBody`。

     状态用 ref + 一个 state 的组合:**"正在飞"和"冲突未决"必须是 ref**(效果里读,
     而且不参与渲染),"显示什么"是 state。冲突未决时**不自动重发** —— 否则它会
     一次又一次地撞同一堵墙,用户看到状态来回闪。
  --------------------------------------------------------------------------- */
  async function flushBody() {
    const node = detailNode;
    if (!node || bodyInFlight.current) return;
    const text = detailDescriptionRef.current;
    if (text === (node.description ?? '')) return;
    bodyInFlight.current = true;
    setBodyNote({ kind: 'saving' });
    const result = await saveNodeBody(node.id, text, bodyVersion.current);
    bodyInFlight.current = false;
    if (result.status === 'saved') {
      // **版本号用后端回来的那一个**:自己 `+1` 的话,只要有一次写入不是"正文 +1"
      // (比如别人先动过、或者某次编辑被别的路径合并了),本地那个号就会一路偏下去,
      // 之后每一次保存都 409。
      bodyVersion.current = result.contentVersion;
      bodySavedAt.current = new Date().toLocaleTimeString('zh-CN', { hour12: false });
      setBodyNote({ kind: 'saved', at: bodySavedAt.current });
      return;
    }
    if (result.status === 'conflict') {
      bodyVersion.current = result.serverVersion;
      setBodyNote({
        kind: 'conflict',
        serverBody: result.serverBody,
        message: result.message,
      });
      return;
    }
    setBodyNote({ kind: 'failed', message: result.message });
  }

  /**
   * 正文改动后停手 700 毫秒存一次。
   *
   * 依赖里**只有正文字和当前节点**:`bodyNote` 的状态变化不在依赖里,否则
   * "保存中 → 已保存"这一下会把这个效果再跑一遍,再排一个定时器 —— 存一次变成
   * 一直存。同理,`bodyInFlight` / `bodyNote` 的判定都在效果**里面**读 ref。
   */
  useEffect(() => {
    const node = detailNode;
    if (!node) return;
    detailDescriptionRef.current = detailDescription;
    if (bodyNote.kind === 'conflict') return;      // 冲突没解决之前不重发(见上)
    if (bodyInFlight.current) return;              // 上一次还在飞
    if (detailDescription === (node.description ?? '')) return;  // 没有差别
    const timer = window.setTimeout(() => { void flushBody(); }, BODY_SAVE_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 见上:`bodyNote` 只做守卫,不进依赖
  }, [detailDescription, detailNode]);

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
        <span>单击看正文与详情 · 右上角箭头进入子路径 · 拖动节点右侧圆点连线 · 点线可改关系</span>
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
        {/* 撤销/重做。**只管布局**,见 `layoutHistory.ts` —— 按钮的 `title` 里说清楚
            管的是什么,因为"这个撤销会不会把我的节点也弄没"是用户按下去之前会问的问题。
            没有东西可撤时**不只是禁用**:`title` 里说为什么不可用,否则一个灰按钮
            和"坏了"长得一样。 */}
        <button
          disabled={!canUndo}
          title={canUndo ? '撤销上一次移动节点（Ctrl+Z）' : '还没有可以撤销的移动'}
          aria-label="撤销"
          onClick={() => undoLayout()}
        >
          <Undo2 size={15} />
          撤销
        </button>
        <button
          disabled={!canRedo}
          title={canRedo ? '重做上一次移动（Ctrl+Shift+Z）' : '没有可以重做的移动'}
          aria-label="重做"
          onClick={() => redoLayout()}
        >
          <Redo2 size={15} />
          重做
        </button>
        <button onClick={() => patchDraft({ dialog: 'files' })}>
          <FolderOpen size={15} />
          空间文件 <small>{files.filter((file) => file.ownerId === spaceId).length || ''}</small>
        </button>
        {/* 归档的入口。**它不是一个"回收站图标"就够了的按钮**:用户点垃圾桶归档之后,
            要有一个明确的地方能找回来,而且要在那里看到"东西还在"。数字用徽标露出来,
            这样"我归档过东西"这件事不靠记忆。 */}
        <button
          // 这里**不清** `archiveNote`:归档那一下的结果("已归档「X」及其下面的 N 项,
          // 可以在这里恢复")就是在归档之后写的,而那时列表还没开 —— 清掉的话那句话
          // 永远没有出现过的一刻。留着它,点开列表的人第一眼看到的才是刚才做了什么。
          // 一条过期的提示由它自己的「知道了」和下一次归档/恢复清掉。
          onClick={() => setArchiveOpen(true)}
          title="看看这个空间归档过什么,把想留的恢复回来"
        >
          <Archive size={15} />
          归档 <small>{archived.length || ''}</small>
        </button>
        {/* 布局没存上。**拖动是可以悄悄失败的操作** —— 画面上节点就停在你放手的地方,
            而库里没有,刷新之后它回到原处,中间没有任何东西提示过你。所以这一行必须
            看得见,而且带一个**有用的**重试:载荷是点的那一刻现拼的,网络回来了、
            或者计划补上了,同一下就能成。 */}
        {layoutError && (
          <div className="layout-save-error" role="alert">
            <span>{layoutError}</span>
            <button onClick={retryLayoutSave}>重试</button>
          </div>
        )}
        {/* 「这一步里有东西已经不在了」。它**不是错误**(撤销本身成功了),所以用另一种
            颜色和 `role="status"`:用户需要知道的是"我这一步只恢复了一半,少的那部分
            在归档里,不在撤销里",而不是"我操作错了"。 */}
        {historyNote && (
          <div className="layout-history-note" role="status">
            <span>{historyNote}</span>
            <button onClick={() => setHistoryNote(null)}>知道了</button>
          </div>
        )}
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
        // 单击 = 打开正文与详情,**立刻开**。进入子空间不再挂在这里(见上面那段注释:
        // 拆开之后这条路上没有任何计时器,按下去发生什么是一定的)。
        onNodeClick={(_, node) => openDetail(node.data.object)}
        // 拖线建边。**在节点上拖,不在空白处拖** —— 空白处拖动是平移画布。
        onConnect={(connection) => {
          if (!connection.source || !connection.target) return;
          connectNodes(connection.source, connection.target);
        }}
        onEdgeClick={(_, edge) => {
          // 这个 handler 只收到关系边(父子连线由 `BranchEdge` 画,没有 onClick)。
          const relation = growth.edges.find((item) => item.id === edge.id);
          if (relation) openRelationEditor(relation);
        }}
        onPaneClick={() => { select(null); setSelectedEdgeId(null); }}
        onNodeDragStart={(_, node) => {
          // 拖动开始那一刻它在哪儿。撤销要把它摆回**这一帧**,而不是"删掉它的位置" ——
          // 节点第一次被拖动时位置表里没有它,删键只在本地看着对,刷新就会被库里那份
          // 拉回拖到的位置。见 `commitNodeMove`。
          dragStart.current = { id: node.id, position: { x: node.position.x, y: node.position.y } };
        }}
        onNodesChange={(changes) => {
          for (const change of changes) {
            if (change.type === 'position' && change.position && change.dragging) {
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
        onNodeDragStop={(_, node) => {
          // **一次拖拽 = 一步历史。** 中途那几十帧在 `onNodesChange` 里只进 `dragging`
          // (预览),一行历史都不占 —— 逐帧记的话,按一次撤销只往回挪一个像素。
          const moved = { [`${spaceId}:${node.id}`]: node.position };
          const started = dragStart.current;
          dragStart.current = null;
          // `before` 只有拖动的**起点**知道。没拿到(理论上不会)就不记这一步:
          // 记一条"撤销了但没动"的账比不记更坏。
          commitNodeMove(moved, started?.id === node.id ? { [`${spaceId}:${node.id}`]: started.position } : {});
          // **拖完把预览扔掉。** `dragging` 只是"拖动中"的那一份(画节点用的是
          // `dragging[key] ?? positions[key] ?? 自动排布`),而它以前从来不清 ——
          // 于是拖过一次之后,这个节点在画布上永远看 `dragging` 那一份,**位置的
          // 真值(`positions`)被它挡住**。从前看不出来,是因为拖完两者恰好相等;
          // 撤销让它们分开了,表现是"撤销之后画布一动不动"(位置变了,画的是旧的那一份)。
          const key = `${spaceId}:${node.id}`;
          setDragging((old) => {
            if (!(key in old)) return old;
            const next = { ...old };
            delete next[key];
            return next;
          });
        }}
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
          <form className="node-form" onSubmit={(event) => { event.preventDefault();
            // **这个按钮不再写 `description`。** 正文有自己的保存路径(带上乐观锁、
            // 边打边存,见 `flushBody`)—— 两边都写的话,这里发出去的那一份不带版本号,
            // 恰好就是"悄悄盖掉别人刚写的正文"那条路。一个字段只有一个写入口。
            updateNode(detailNode.id, isRealSpace
            ? { title: detailTitle.trim() || detailNode.title, priority: detailPriority, deadline: detailDeadline || null, estimateMinutes: parseEstimate(detailEstimate) }
            : { title: detailTitle.trim() || detailNode.title, priority: detailPriority, startDate: detailStart || undefined, endDate: detailEnd || undefined }); closeDetailEditor(); }}>
            <label>节点名称<input autoFocus value={detailTitle} maxLength={80} onChange={(event) => patchDraft({ detailTitle: event.target.value })} /></label>
            {/* 正文:边打边存。下面那一行状态是**真实结果**,不是"我发过一次请求" ——
                见 `flushBody`。 */}
            <label>
              详细说明
              <textarea value={detailDescription} maxLength={1000} onChange={(event) => patchDraft({ detailDescription: event.target.value })} placeholder="记录这个节点的目标、约束、判断和下一步…" />
            </label>
            {isRealSpace && (
              <p className={`body-save-note is-${bodyNote.kind}`} role="status">
                {bodyNote.kind === 'saving' && '正在保存正文…'}
                {bodyNote.kind === 'saved' && `正文已保存（${bodyNote.at}）`}
                {bodyNote.kind === 'failed' && (
                  <>
                    正文没有保存上:{bodyNote.message}
                    <button type="button" onClick={() => { setBodyNote({ kind: 'none' }); void flushBody(); }}>重试</button>
                  </>
                )}
                {bodyNote.kind === 'conflict' && (
                  <>
                    <b>这段正文在别处被改过了,所以这次没有写进去。</b>
                    {bodyNote.serverBody
                      ? <>库里现在是:「{bodyNote.serverBody}」</>
                      : <>库里那份这一步没读到 —— 可以先用你这份覆盖,或者关掉重开再看一眼。</>}
                  </>
                )}
                {bodyNote.kind === 'none' && (detailDescription === (detailNode.description ?? '')
                  ? '正文与库里一致。'
                  : '正文有改动,停手后会自动保存。')}
              </p>
            )}
            {isRealSpace && bodyNote.kind === 'conflict' && (
              <div className="body-conflict-actions">
                {/* **两条路都摆出来,而且都不静默。** 覆盖是用户明确选的(它拿的是
                    库里此刻那一版做前置条件,所以"覆盖"也仍然是一致性写入);
                    放弃则是把库里那份读回编辑器。哪一种都不该由我们替他挑。 */}
                <button type="button" onClick={() => { setBodyNote({ kind: 'none' }); void flushBody(); }}>
                  用我这份覆盖
                </button>
                {Boolean(bodyNote.serverBody) && (
                  <button
                    type="button"
                    onClick={() => { patchDraft({ detailDescription: bodyNote.serverBody }); setBodyNote({ kind: 'none' }); }}
                  >
                    放弃我的改动,载入库里那份
                  </button>
                )}
              </div>
            )}
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
            <p>{isRealSpace ? '正文边写边存（停手即存）；下面这个按钮保存名称、优先级与时间，会产生一个新的计划版本。进入子路径请用节点右上角的箭头。' : '在这里保存的说明会保留在当前成长空间。'}</p>
            {isRealSpace && planError && <p className="form-error" role="alert">{planError}</p>}
            <button className="primary-button" disabled={!detailTitle.trim() || planSaving}>{planSaving ? '保存中…' : '保存节点'}</button>
          </form>
        </Dialog>
      )}
      {/* 归档确认。**先给数字,再给按钮。**
          这一张是这一批的核心:在这之前,用户点垃圾桶的那一下会直接毁掉一整支,
          而他在按下去之前看不到"会带走什么",按完之后也没有任何地方能把东西拿回来。
          数字来自 `GET /archive-impact`,由后端算 —— 不是从画布上这份计划里数的。 */}
      {archiveConfirm && (
        <Dialog title={`归档「${archiveConfirm.title}」？`} onClose={closeArchiveConfirm}>
          <div className="archive-panel">
            {archiveConfirm.loading && <p className="archive-hint">正在算这一下会带走什么…</p>}
            {archiveConfirm.error && (
              <>
                <p className="form-error" role="alert">
                  {archiveConfirm.error}
                </p>
                <p className="archive-hint">
                  {/* 不给"照样删"这条退路:这一批的全部意义就是"点之前知道代价"。
                      取不到数字还让按下去,等于又回到"闭着眼睛删"。 */}
                  数字取不到就先不归档 —— 少了它,你按下去的时候是不知道会带走什么的。
                </p>
              </>
            )}
            {archiveConfirm.impact && (
              <>
                <p className="archive-hint">
                  归档之后这一支从计划里消失,但<b>可以</b>在「归档」里恢复。
                </p>
                <ul className="archive-impact">
                  <li>
                    后代 <b>{archiveConfirm.impact.descendants}</b> 个
                    {archiveConfirm.impact.descendants === 0 && '（它自己没有下级）'}
                  </li>
                  <li>
                    关系 <b>{archiveConfirm.impact.relations}</b> 条、前置 <b>{archiveConfirm.impact.dependencies}</b> 条
                  </li>
                  <li>
                    排期 <b>{archiveConfirm.impact.sessions}</b> 场（共 {archiveConfirm.impact.sessionMinutes} 分钟）
                    {archiveConfirm.impact.overdueSessions > 0 && (
                      <span className="archive-warn">，其中 {archiveConfirm.impact.overdueSessions} 场已经过期</span>
                    )}
                  </li>
                </ul>
                <p className="archive-hint">
                  恢复时那几场会<b>原样</b>放回日历,不重排 —— 已经过期的那几场需要你自己安排。
                </p>
                <div className="archive-actions">
                  <button type="button" onClick={closeArchiveConfirm}>
                    取消
                  </button>
                  <button className="primary-button" type="button" disabled={planSaving} onClick={() => void confirmArchive()}>
                    {planSaving ? '归档中…' : '归档'}
                  </button>
                </div>
              </>
            )}
          </div>
        </Dialog>
      )}
      {/* 归档列表。**恢复的入口就在这里** —— 没有它,"可恢复"只是一句空话:
          用户归档之后找不到任何地方能把东西拿回来。
          父节点还在另一次归档里的那几行**仍然列出来**,但按钮置灰并写明原因 ——
          藏起来会让用户以为它被删了,而他其实还能先恢复上层。 */}
      {archiveOpen && (
        <Dialog title={`${spaceTitle} · 归档`} onClose={() => setArchiveOpen(false)}>
          <div className="archive-panel">
            {archiveNote && (
              <p className="archive-note" role="status">
                {archiveNote}
                <button type="button" className="archive-dismiss" onClick={() => setArchiveNote(null)}>
                  知道了
                </button>
              </p>
            )}
            {archiveListError && <p className="form-error" role="alert">{archiveListError}</p>}
            {!archiveListError && archived.length === 0 && (
              <p className="archive-hint">这个空间里还没有归档过东西。删掉一个节点时它会被收进这里,而不是消失。</p>
            )}
            <ul className="archive-list">
              {archived.map((entry) => (
                <li key={entry.node.id} className={entry.restorable ? '' : 'is-blocked'}>
                  <div className="archive-row-head">
                    <strong>{entry.node.title}</strong>
                    <small>{entry.archivedAt.slice(0, 10)} 归档</small>
                  </div>
                  <p className="archive-row-meta">
                    {entry.descendants > 0 && `后代 ${entry.descendants} 个 · `}
                    {entry.sessions > 0 ? `排期 ${entry.sessions} 场 · ` : ''}
                    连着的线会一起回来
                  </p>
                  {entry.restorable ? (
                    <button
                      type="button"
                      disabled={restoringId !== null}
                      onClick={() => void restoreArchived(entry.node.id)}
                    >
                      <RotateCcw size={13} />
                      {restoringId === entry.node.id ? '恢复中…' : '恢复'}
                    </button>
                  ) : (
                    <p className="archive-blocked">
                      {/* 两种挡的原因给的是两句不同的话,因为用户能做的事不一样:
                          一种是"先恢复上层",一种是"没有别的办法"。 */}
                      {entry.blockedReason === 'PARENT_PURGED'
                        ? '它的上层被彻底删除了,这一项恢复不了。'
                        : '它的上层还在归档里,先把上层恢复出来。'}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          </div>
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
