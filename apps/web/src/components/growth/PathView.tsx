'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Background,
  BaseEdge,
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
  Crosshair,
  FileText,
  Flag,
  FolderOpen,
  GitBranch,
  MoreHorizontal,
  Plus,
  Redo2,
  RotateCcw,
  Trash2,
  Undo2,
} from 'lucide-react';
import { NodeAnalysisPanel } from '@/components/growth/NodeAnalysisPanel';
import { ContextMenu, type ContextMenuState } from '@/components/ui/ContextMenu';
import { Dialog } from '@/components/ui/Dialog';
import { AmbientGlow } from '@/components/ui/AmbientGlow';
import { CREATE_KINDS, useCanvasDraft, type CreateKind } from '@/features/growth/drafts';
import { planningLevelLabel } from '@/features/growth/planningLevel';
import {
  MAX_DESCRIPTION_CODEPOINTS,
  MAX_NOTE_CODEPOINTS,
  codePointLength,
  isDescriptionExempt,
} from '@/lib/codepoints';
import { useDemo } from '@/features/growth/provider';
import { useMobileLayout } from '@/lib/media';
import type { GrowthEdge, GrowthNode, GrowthRelationType } from '@/types/growth';
import { SpaceFiles } from './SpaceFiles';
import { CanvasQuestionNodeComponent, QuestionInteractionContext, type CanvasQuestionDraft, type QuestionFlowNode, type QuestionInteraction } from './CanvasQuestionNode';
import { ReasoningNodeComponent, type ReasoningFlowNode } from './ReasoningNode';
import type { ReasoningNodeView } from '@/lib/backend';

type GrowthFlowData = {
  object: GrowthNode;
  root: boolean;
  children: number;
  files: number;
  vertical: boolean;
  /**
   * 打开这个节点的右键菜单。
   *
   * 为什么走 `data` 而不是让节点组件自己拿一份菜单状态:菜单要夹紧在**画布容器**里
   * (见 `ContextMenu` 的说明),而容器是 `Canvas` 才知道的东西。节点组件自己渲染菜单
   * 的话,它得先想办法找到那个容器,而 `data` 这条路本来就是 ReactFlow 给子组件传
   * 上下文的方式。
   *
   * `trigger` 是"关掉之后焦点还给谁"—— 用键盘(`Shift+F10`)或点「更多」打开时给按钮,
   * 鼠标右键时给 `null`(点右键的位置本来就没有焦点可言)。
   */
  onMore: (node: GrowthNode, trigger: HTMLElement | null) => void;
  /**
   * 这个节点**刚刚**被建出来。只有它为真才播那一次创建动画。
   *
   * 为什么不能写成"新节点就播":下面那份 memo 会因为拖动、选中、文件数变化等
   * 各种原因整张重算,挂在"节点存在"上的动画会让**全部**节点一起重新淡入一次
   * (规范 4.1 最后一句点名禁止)。所以标记只在 `addNode` 真的返回了一个节点时
   * 挂上,并且由动画自己的结束事件摘掉。
   */
  created: boolean;
  /** 创建动画播完了。参数是节点 id —— 摘标记前要确认摘的还是同一个。 */
  onCreatedEnd: (id: string) => void;
};
type GrowthFlowNode = Node<GrowthFlowData, 'growth'>;
type FlowNode = GrowthFlowNode | QuestionFlowNode | ReasoningFlowNode;

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

/**
 * 这一次指针事件是不是落在**真正的空白画布**上。
 *
 * ## 为什么不能写 `closest('.react-flow__pane')`
 *
 * 那个写法在本仓的 React Flow 版本里**是错的,而且错得看不出来**。实测的 DOM 结构是:
 *
 * ```
 * div.react-flow__pane
 *   div.react-flow__viewport
 *     div.react-flow__nodes
 *       div.react-flow__node      <- 节点在这里面
 * ```
 *
 * 也就是说节点、边、以及我们自己那些浮层**全都是 `.react-flow__pane` 的后代** ——
 * `closest` 对它们一律返回真。于是"双击节点不会建节点"这句话不成立:双击一张卡片
 * 会弹出一个新建表单,而它看起来像"这个产品偶尔会乱建东西"。
 *
 * ## 判据是"目标**就是**那一层",而 React Flow 自己也是这么判的
 *
 * 它内部的 `wrapHandler` 是 `if (event.target !== containerRef.current) return;` ——
 * 同一个意思。空白处双击/右键时目标是 pane 元素本身(背景那层 SVG 是
 * `pointer-events: none`,不会把事件截走);点中任何东西时目标是那件东西。
 * 所以这一条**按构造**排除了节点、边、连接点、工具栏、缩略图、菜单与编辑器,
 * 而不是靠一份要维护的排除名单。
 *
 * 将来若升级 React Flow 且它的类名变了,这里会**整体失效**(什么都建不出来),
 * 而不是悄悄放开 —— 那种失败方向是对的,端到端验收也会当场红。
 */
function onEmptyPane(event: { target: EventTarget | null }): boolean {
  const target = event.target as HTMLElement | null;
  return Boolean(target?.classList?.contains('react-flow__pane'));
}

function GrowthNodeComponent({ id, data, selected }: NodeProps<GrowthFlowNode>) {
  // 这里**不挂 `onDoubleClick`**,而"双击进子空间"这件事本身也已经没有了(步骤 4):
  // 单击开正文与详情,进子空间只走右上角那个箭头按钮(下面那个 `node-enter`)。
  // 两件事拆开之后,节点上就不该再有任何"看时间/看次数"的隐藏语义。
  const { enterSpace } = useDemo();
  const node = data.object;
  const sourcePosition = data.vertical ? Position.Bottom : Position.Right;
  const targetPosition = data.vertical ? Position.Top : Position.Left;
  /** 信息主题不画任务勾选框 —— 它没有"做完"这回事(见 `GrowthNode.purpose`)。 */
  const isInformation = node.purpose === 'information';

  return (
    <div
      className={`growth-node ${data.root ? 'goal' : node.type} ${isInformation ? 'is-information' : ''} ${node.category ?? ''} ${selected ? 'is-selected' : ''} ${node.status === 'completed' ? 'is-complete' : ''} ${data.created ? 'is-created' : ''}`}
      // 建出来之后只播一次。按**动画名**判断,而不是"有动画结束就叫一次":
      // 这个元素里将来多一条装饰动画时,不写名字的版本会顺手多摘一次标记,
      // 于是创建动画就再也不会被摘掉。
      onAnimationEnd={(event) => {
        if (event.animationName === 'node-create') data.onCreatedEnd(id);
      }}
    >
      {!data.root && <Handle type="target" position={targetPosition} />}
      <div className="node-heading">
        <span className="node-marker" aria-hidden="true">
          {data.root ? <Flag size={21} /> : node.type === 'task' && !isInformation
            ? (node.status === 'completed' ? <CheckSquare size={14} /> : <span className="node-task-box" />)
            : <span className="node-title-dot" />}
        </span>
        <strong className="node-title">{node.title}</strong>
        {node.planningLevel && (
          <span className={`node-level node-level-${node.planningLevel}`}>
            {planningLevelLabel(node.planningLevel)}
          </span>
        )}
      </div>
      {(node.description || data.root) && <p className="node-description">{node.description || '根目标'}</p>}
      {node.status === 'doing' && <span className="node-doing" />}
      {/*
        节点上原来常驻一个垃圾桶。**它现在收进菜单里了**(§9.1.1:低频操作收进菜单,
        让画布更整洁),这个「更多」按钮是那个菜单的入口 —— 也是**触屏唯一的入口**:
        触屏没有右键,`@media (hover: none)` 下它恒显(见 dark-theme.css)。

        归档那一下没有丢,只是挪了一层:菜单里点「归档」走的还是同一个
        `askArchive`(先问"这一下会带走什么"),不是直接删。

        **根目标上也有这个按钮**,虽然它那颗垃圾桶原来是有的、后来又不渲染了。
        理由不是"对称":根目标的归档是**禁用且写明原因**的(§9.1.1),而那句话
        得有个地方能读出来。少了这个按钮,根目标上唯一能读到原因的路是**右键** ——
        触屏没有右键,键盘也没有等价入口,于是那句"根目标不能归档"对触屏和键盘
        用户根本不存在,他们看到的只是"这个节点没有更多操作"。
      */}
      <button
        className="node-more nodrag nopan"
        aria-haspopup="menu"
        aria-label={`${node.title}的更多操作`}
        title="更多操作（右键这个节点也可以）"
        onClick={(event) => {
          event.stopPropagation();
          // 菜单的落点由 `onMore` 那一侧从按钮的 rect 算(见 `openNodeMenu`)——
          // 键盘那条路(`Shift+F10`)传的是同一个元素,所以两条路落在同一个地方。
          data.onMore(node, event.currentTarget);
        }}
        onKeyDown={(event) => {
          // `Shift+F10` 是"打开右键菜单"的标准键盘操作。不接的话,键盘用户拿不到
          // 这个菜单里的任何一项(归档、进入子路径),而那些是这里才有的功能。
          if (event.key === 'F10' && event.shiftKey) {
            event.preventDefault();
            event.stopPropagation();
            data.onMore(node, event.currentTarget);
          }
        }}
      >
        <MoreHorizontal size={13} />
      </button>
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
 * 这一段说明此刻是不是**长到不该发出去**。
 *
 * 上限是一条**条件规则**,不是一把尺子(见 `lib/codepoints.ts` 与后端
 * `contracts/plan.py` 的 `description_length_error`):库里那一份已经超过 300 码点的
 * 说明继续想写多长写多长 —— 那些是上限出现之前用户唯一的表达方式。
 *
 * 所以第二个参数是**库里那一份**,不是输入框里那个数。拿输入框判的话,用户把一个
 * 豁免节点的正文删到 200 字,界面就翻脸说"还可以写 300 字",而服务端那边仍然豁免它。
 */
function descriptionTooLong(text: string, stored: string | null | undefined): boolean {
  return codePointLength(text) > MAX_DESCRIPTION_CODEPOINTS && !isDescriptionExempt(stored);
}

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

const nodeTypes = { growth: GrowthNodeComponent, question: CanvasQuestionNodeComponent, reasoning: ReasoningNodeComponent };

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
  /*
   * 这条线是不是**与当前 hover / 拖动的那一个节点直接相连**。
   *
   * 判断放在这里而不是交给 CSS:CSS 选不到"与 hover 的那个节点相连的边"。而这里
   * 拿到的是真实的 `source`/`target` id,不是标题 —— 同名节点不会亮错。值由
   * `Canvas` 那份 memo 按 hovered/dragging 的 id 算好传进来。
   */
  const focused = props.data?.__focus === true;
  /**
   * 这一条是不是刚建出来的那条,正在播一次绘制动画。
   *
   * 绘制靠 `pathLength=1` 把整条路径归一化成长度 1,再把 dasharray 设成 1、
   * dashoffset 从 1 走到 0(`motion.css` 里的 `edge-draw`)。动画期间这条边自己的
   * 虚线样式要让开 —— 所以下面 `strokeDasharray` 在绘制时**不写内联值**,由类提供。
   * 方向就是数据里的 source → target,没有翻转任何东西。
   */
  const drawing = props.data?.__drawn === true;
  const onDrawnEnd = props.data?.onDrawnEnd as ((id: string) => void) | undefined;
  const [path, labelX, labelY] = getBezierPath({
    sourceX: props.sourceX, sourceY: props.sourceY, sourcePosition: props.sourcePosition,
    targetX: props.targetX, targetY: props.targetY, targetPosition: props.targetPosition,
    curvature: 0.32,
  });
  return (
    <BaseEdge
      id={props.id}
      path={path}
      className={drawing ? 'is-drawing' : undefined}
      pathLength={drawing ? 1 : undefined}
      onAnimationEnd={drawing ? () => onDrawnEnd?.(props.id) : undefined}
      // 点得中比好看要紧:线只有一两像素宽,而"点这条线"是打开关系编辑器的入口。
      interactionWidth={24}
      style={{
        stroke: look.color,
        // 选中最粗;其次是与当前节点直接相连的那一条。**其余的一律保持原样** ——
        // 规范 4.2 要的是"只有相关的线变清晰",不是"别的线一起变暗"。
        strokeWidth: props.selected ? look.width + 1 : focused ? look.width + 0.7 : look.width,
        strokeDasharray: drawing ? undefined : look.dash,
        opacity: props.selected ? 1 : focused ? 1 : 0.9,
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

/** 仅 UI 的问题锚定虚线:表示“这个问题由此处引出”。**不是业务关系。** */
function QuestionAnchorEdge(props: EdgeProps) {
  const [path] = getSmoothStepPath({
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    targetX: props.targetX,
    targetY: props.targetY,
    sourcePosition: props.sourcePosition,
    targetPosition: props.targetPosition,
    borderRadius: 20,
    offset: 18,
  });
  return (
    <BaseEdge
      id={props.id}
      path={path}
      style={{ strokeDasharray: '6 4', stroke: '#c7a06e', strokeWidth: 1.2, opacity: 0.75 }}
    />
  );
}

/** 推理地图的边。**不是业务关系** —— 不参与排期、不可编辑、不可删除。 */
function ReasoningLinkEdge(props: EdgeProps) {
  const [path] = getSmoothStepPath({
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    targetX: props.targetX,
    targetY: props.targetY,
    sourcePosition: props.sourcePosition,
    targetPosition: props.targetPosition,
    borderRadius: 16,
    offset: 16,
  });
  return (
    <BaseEdge
      id={props.id}
      path={path}
      style={{ strokeDasharray: '4 4', stroke: '#8a7fb0', strokeWidth: 1.2, opacity: 0.7 }}
    />
  );
}

/**
 * 讨论锚定线:目标根节点 → 顶层推理节点的**纯 UI** 虚线。
 *
 * 它回答的是"这些讨论维度是从哪个目标长出来的"。它**不是** `depends_on`、
 * 不是 `related_to`、不是任何一张业务表里的一行 —— 不参与排期、不可编辑、
 * 不可删除,也永远不会被写成业务关系。所以画法刻意与关系边不同:更细、更淡、
 * 虚线间距更大,一眼看得出它是"归属说明",不是"用户建的业务线"。
 */
function ReasoningAnchorEdge(props: EdgeProps) {
  const [path] = getSmoothStepPath({
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    targetX: props.targetX,
    targetY: props.targetY,
    sourcePosition: props.sourcePosition,
    targetPosition: props.targetPosition,
    borderRadius: 22,
    offset: 20,
  });
  return (
    <BaseEdge
      id={props.id}
      path={path}
      style={{ strokeDasharray: '3 5', stroke: '#8a7fb0', strokeWidth: 1, opacity: 0.45 }}
    />
  );
}

const edgeTypes = { branch: BranchEdge, relation: RelationEdge, questionAnchor: QuestionAnchorEdge, reasoningLink: ReasoningLinkEdge, reasoningAnchor: ReasoningAnchorEdge };

/**
 * `__zhituCanvasLifecycle` 最多留多少条。理由见 `Canvas` 里那个 effect 的说明。
 *
 * 它是**给测试看的**,所以这个数只对测试有意义;`canvas-stability.spec.ts` 里那条
 * "不许重挂载"的用例会检查"没数到上限"(数到上限那条断言就恒真了)。
 * 改这里的话,顺手看一眼那个用例里写着的同一个数。
 */
const CANVAS_LIFECYCLE_LIMIT = 50;

/**
 * 推理地图节点的详情面板。
 *
 * **用户原文与 Agent 摘要分区显示。** 保存只改标题与原文(服务端会锁定改过的标题),
 * 其余字段由 Agent 维护。暂缓/标记完成只改推理地图的节点状态,不碰业务计划。
 */
function ReasoningDetail({
  node,
  onClose,
  onAgentTurn,
  onEdit,
}: {
  node: ReasoningNodeView;
  onClose: () => void;
  onAgentTurn: (payload: { trigger: 'node_selected' | 'user_message' | 'strategy_confirmation'; reasoningHandle: string; message?: string }) => void;
  onEdit: (patch: { title?: string; userDescription?: string; status?: string }) => void;
}) {
  const [title, setTitle] = useState(node.title);
  const [body, setBody] = useState(node.userDescription ?? '');
  const [discuss, setDiscuss] = useState('');
  useEffect(() => {
    setTitle(node.title);
    setBody(node.userDescription ?? '');
  }, [node.id, node.title, node.userDescription]);

  return (
    <div className="reasoning-detail" role="dialog" aria-label={`推理节点:${node.title}`}>
      <header>
        <span className="eyebrow">目标推理 · {node.handle}</span>
        <button type="button" aria-label="关闭推理详情" onClick={onClose}>×</button>
      </header>
      <label className="rd-field">
        <span>维度标题</span>
        <input value={title} onChange={event => setTitle(event.target.value)} aria-label="维度标题" />
      </label>
      {node.summary && (
        <p className="rd-summary">
          <span className="rd-label">AI 摘要</span>
          {node.summary}
        </p>
      )}
      <label className="rd-field">
        <span>你的说明（原文，AI 不会覆盖）</span>
        <textarea
          value={body}
          onChange={event => setBody(event.target.value)}
          aria-label="你的说明"
          placeholder="写下你对这个维度的想法…"
        />
      </label>
      {node.evidence.length > 0 && (
        <p className="rd-evidence"><span className="rd-label">依据</span>{node.evidence.join('；')}</p>
      )}
      <p className="rd-meta">优先级 {node.priority} · 状态 {node.status}</p>
      <div className="rd-actions">
        <button type="button" onClick={() => onEdit({ title, userDescription: body })}>保存</button>
        <button type="button" onClick={() => onAgentTurn({ trigger: 'node_selected', reasoningHandle: node.handle })}>自动分析</button>
        <button type="button" onClick={() => onAgentTurn({ trigger: 'node_selected', reasoningHandle: node.handle, message: '展开这个维度' })}>展开</button>
        <button type="button" onClick={() => onEdit({ status: 'paused' })}>暂缓</button>
        <button type="button" onClick={() => onEdit({ status: 'resolved' })}>标记完成</button>
        {node.nodeType === 'route' && (
          <button type="button" onClick={() => onAgentTurn({ trigger: 'strategy_confirmation', reasoningHandle: node.handle })}>
            确认这条战略
          </button>
        )}
      </div>
      <div className="rd-discuss">
        <input value={discuss} onChange={event => setDiscuss(event.target.value)} placeholder="围绕它讨论一句…" aria-label="讨论内容" />
        <button
          type="button"
          disabled={!discuss.trim()}
          onClick={() => {
            onAgentTurn({ trigger: 'user_message', reasoningHandle: node.handle, message: discuss.trim() });
            setDiscuss('');
          }}
        >
          发送
        </button>
      </div>
    </div>
  );
}

function Canvas() {
  const {
    growth, selectedId, select, positions, commitNodeMove, questionPositions, commitQuestionMove, spaceId, workspaceId, canvasKey, viewports, setScopeViewport,
    // `enterSpace` / `askArchive` 又回到这里了:**节点的右键菜单**是它们的第三个入口
    // (前两个是节点上那个箭头按钮和工具栏)。它们都在**画布这一层**打开菜单,所以
    // 取在这里;节点子组件里那一份只管自己那个箭头。
    enterSpace, askArchive,
    addNode, updateNode, saveNodeBody, addRelation, updateRelation, removeRelation,
    // 长正文(笔记)。它**不是**节点的一个字段:两万字按需取、整份存,与自己一条
    // 版本线 —— 见 `lib/backend.ts` 的 `NotePayload`。
    loadNodeNote, saveNodeNote,
    files, isRealSpace, planSaving, planLoading, planError, setPlanError,
    layoutReady, layoutError, retryLayoutSave,
    undoLayout, redoLayout, canUndo, canRedo, historyNote, setHistoryNote,
    archiveConfirm, closeArchiveConfirm, confirmArchive,
    archiveOpen, setArchiveOpen, archived, archiveListError, archiveNote, setArchiveNote,
    restoreArchived, restoringId,
    questions, submitAnswer, dismissQuestion, postponeQuestion, questionFocus,
    reasoning, reasoningLoading, ensureReasoningMap, agentTurn, editReasoningNode,
    refineStrategy, refining,
  } = useDemo();
  const { fitView, setViewport, screenToFlowPosition } = useReactFlow();
  /*
   * 手机那一档的画布参数要跟别处不一样(见下面 fit 那段和 `fitViewOptions`)。
   * 用的是同一个断点源(`lib/media.ts`),和 `Workbench` 收起面板用的是同一个值 ——
   * 两处各自写一个 `matchMedia` 迟早会漂。
   */
  const narrowScreen = useMobileLayout();
  const nodesInitialized = useNodesInitialized();
  const fittedScope = useRef<string | null>(null);
  /** 正在被拖的那个节点**动手前**在哪。见 `onNodeDragStart` / `commitNodeMove`。 */
  const dragStart = useRef<{ id: string; position: { x: number; y: number } } | null>(null);

  /*
   * ----------------------------- 生命感的三个标记(批次 A)
   *
   * 三个都是"刚刚发生了什么"的一次性标记,而且**每一个都有一个真实的来源**:
   *
   * | 标记 | 谁来点亮 | 谁来熄灭 |
   * | --- | --- | --- |
   * | `createdId` | `addNode` 返回了节点 | 动画自己的 `animationend` |
   * | `drawnEdgeId` | `addRelation` 返回了那条边 | 同一个 |
   * | `hoveredId` | 指针真的进了这个节点 | 指针离开 |
   *
   * 没有任何一个是"等一会儿就当作发生了"。规范第 10 节禁止的正是那种写法:
   * 用动画掩盖等待、失败或降级。
   */
  const [createdId, setCreatedId] = useState<string | null>(null);
  const [drawnEdgeId, setDrawnEdgeId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  /** 摘标记时确认摘的还是同一个 —— 连着建两个节点时,先建那个的结束事件不该把后一个的标记摘掉。 */
  const clearCreated = useCallback((id: string) => setCreatedId((current) => (current === id ? null : current)), []);
  const clearDrawn = useCallback((id: string) => setDrawnEdgeId((current) => (current === id ? null : current)), []);
  /*
   * 换一层空间就把两个标记扔掉。
   *
   * 不扔会怎样:在一个层级建了一条两端都在别处的边(那条边这一层不画,所以动画没播),
   * 用户再走进能看见它的层级时,它会**突然自己画一遍** —— 看起来像刚刚发生了什么,
   * 其实什么都没发生。
   */
  useEffect(() => {
    setCreatedId(null);
    setDrawnEdgeId(null);
  }, [spaceId]);

  /*
   * 平移画布时给 `<html>` 挂一个类,让 Dock 的阴影/底色略降、环境光暂停
   * (规范 6.2 与 9.2)。
   *
   * **不走 React state**:平移每秒会产生几十次事件,而这两件事的性质是"页面上的
   * 一个开关",与 React 的渲染无关。走 state 的话每次平移都要把这一层重算一遍,
   * 换来的只是同一个类名。
   */
  const panningRef = useRef(false);
  const setPanning = useCallback((on: boolean) => {
    if (panningRef.current === on) return;
    panningRef.current = on;
    document.documentElement.classList.toggle('is-canvas-panning', on);
  }, []);
  // 卸载时一定要摘掉:`is-canvas-panning` 挂在 `<html>` 上,不跟着组件走。
  useEffect(() => () => {
    panningRef.current = false;
    document.documentElement.classList.remove('is-canvas-panning');
  }, []);

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
  /**
   * **拖动中**的问题节点位置预览。
   *
   * 与业务节点的 `dragging` 分开:业务那份会进布局历史与落库(经 `commitNodeMove`),
   * 而问题节点是 UI-only 投影 —— 拖它不得触碰业务位置、历史或后端。分开之后也顺带
   * 避免了“拖问题节点把整张业务图重算一遍”。
   */
  const [questionDragging, setQuestionDragging] = useState<Record<string, { x: number; y: number }>>({});
  /** 打开的是哪个推理节点(用会话内短记号)。详情面板里显示原文与 Agent 摘要。 */
  const [openReasoningHandle, setOpenReasoningHandle] = useState<string | null>(null);
  const handleOpenReasoning = useCallback(
    (node: ReasoningNodeView) => setOpenReasoningHandle(node.handle),
    [],
  );
  const openReasoningNode = reasoning?.nodes.find((item) => item.handle === openReasoningHandle) ?? null;
  const [submitting, setSubmitting] = useState(false);
  /**
   * 当前选中的**边**。与节点的 `selectedId` 是两回事:`selectedId` 决定"聚焦所选"
   * 和节点高亮,这个只决定哪条线是加粗的。
   *
   * 它不进草稿存储:选中态不是"用户打了一半的输入",卸载丢掉没有任何损失 ——
   * 和 `selectedId` 一个待遇。
   */
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  //: 画布上被“指着”的问题节点(来自点击,或来自右侧“定位到画布”)。这是纯 UI 状态。
  const [focusedQuestionId, setFocusedQuestionId] = useState<string | null>(null);
  const lastQuestionFocusNonce = useRef<number>(-1);
  //: 每个问题节点的输入草稿。放在这里而不是节点组件里 —— React Flow 重建节点时
  //: 组件局部 state 会被清空(点了选项按钮又变灰)。
  const [questionDrafts, setQuestionDrafts] = useState<Record<string, CanvasQuestionDraft>>({});

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

  /* ------------------------- 长正文(笔记)的状态 -------------------------
     与上面那一组**同构,但是另一套**:另一份文本、另一个版本号、另一条保存路径。
     它不能和正文共用一套状态 —— 共用会让"正文正在保存"和"长正文正在保存"变成
     屏幕上的同一句话,而用户改的是两个框里的东西。
     -------------------------------------------------------------------- */
  /** 编辑器里此刻的长正文。理由与 `detailDescriptionRef` 一样:`flushNote` 从定时器里被调用。 */
  const noteBodyRef = useRef('');
  /** 手上这份长正文是第几版(笔记自己的乐观锁)。见 `lib/backend.ts` 的 `NotePayload`。 */
  const noteVersion = useRef<number | undefined>(undefined);
  /** 库里那一份长正文此刻是什么 —— 用来判"有没有真的改动",以及冲突时那句话。 */
  const noteServerBody = useRef('');
  const noteInFlight = useRef(false);
  const [noteNote, setNoteNote] = useState<BodyNote>({ kind: 'none' });
  /**
   * 上面那三样(正文 ref、版本号、库里那一份)**现在属于哪个节点**。`null` 是
   * "不属于任何一个" —— 编辑器关着,或者这一份还在路上。
   *
   * **为什么必须单独有一个,而不是拿 `draft.noteNodeId` 充当**:`flushNote` 是从
   * 定时器里被调用的,那时闭包里的 `draft` 可能已经旧了。少了这个守卫,"用户在 A 上
   * 打了字、700 毫秒内切到 B"就会把 **B 的正文存到 A 头上** —— 两个节点都还在、
   * 界面也不报错,是一笔查不出来的账。
   */
  const noteOwnerRef = useRef<string | null>(null);
  /** 编辑器此刻开着哪个节点。同样是为了让 `flushNote` 在定时器里有一个"现在"可读。 */
  const detailNodeIdRef = useRef<string | null>(null);
  /** 读到第几次。重试按钮把它加一,下面那个效果就再读一遍。 */
  const [noteReload, setNoteReload] = useState(0);
  /**
   * 读长正文失败了。**失败时不给编辑** —— 一个空白的编辑器配上一句"没读到"会让
   * 用户以为自己没写过,然后把他新写的存上去,而那可能是对服务端那一份的覆盖。
   * 所以这一条是"编辑器先别出现",不是"当作空的"。
   */
  const [noteLoadError, setNoteLoadError] = useState<string | null>(null);

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
    dialog, title, description, createKind, estimate, createPosition,
    detailNodeId, detailTitle, detailDescription,
    noteNodeId, noteBody,
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
  const closeCreateDialog = () => patchDraft({
    dialog: null, title: '', description: '', createKind: 'action', estimate: '', createPosition: null,
  });
  const closeDetailEditor = () => patchDraft({
    detailNodeId: null, detailTitle: '', detailDescription: '',
    // 长正文那一份也一起丢。**不清的话**下一次打开别的节点时,`noteNodeId` 会对不上
    // 而显示"正在读取"直到那一份回来 —— 不难看,但把一个"关掉=不写了"的动作留在
    // 了半途。它已经存过的部分在库里,这里丢的只是编辑器里那一份。
    noteNodeId: null, noteBody: '',
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
  /**
   * 画布上**到底有没有可见内容**。
   *
   * 它**不等于**"这一层有没有业务子节点"。问题地图(reasoning)与问题节点(question)
   * 也是画布内容 —— 只看 `direct.length` 的话,一个刚由目标推理拉起整张地图、但还没有
   * 任何计划子节点的空间会错误地显示“这里，还可以长出更多可能”,而画布上明明已经有
   * 内容。三者都不存在时才显示空态;一旦地图存在,空态**完全不渲染**,不占画布位置。
   */
  const visibleQuestions = questions.filter((item) => item.status !== 'archived').length;
  const canvasHasContent =
    direct.length > 0 || (reasoning?.nodes.length ?? 0) > 0 || visibleQuestions > 0;
  // 对话框标题用。真实空间的根节点标题可能是空的(建空间时用户只填了空间名),
  // 那时退回空间名 —— 对话框上写着「在「」中新建节点」是一句废话。
  const spaceTitle = growth.nodes[spaceId]?.title || growth.title;

  /**
   * 节点组件拿到的 `onMore`,以及它转手的那个真正的开关。
   *
   * 为什么要绕一个 ref:`onMore` 要进下面那个 `useMemo` 的 `data`,而给 `useMemo`
   * 的依赖加上 `openNodeMenu` 会让它**每次渲染都重算** —— `openNodeMenu` 用到
   * `enterSpace` / `askArchive`,那两个是 provider 每次渲染都新建的普通函数,身份
   * 永远不稳定。而那份 memo 走的是整张图的节点与边,不值当为它每帧重算。
   *
   * ref 里的那一份在**每次渲染**时被重新赋值(见 `openNodeMenu` 之后那行),
   * 所以 `handleMore` 调到的永远是最新的闭包,但它自己的身份是恒定的。
   *
   * 这两个声明必须在 `useMemo` **之前**:`const` 有暂时性死区,放到后面的话
   * 这里引用它会直接抛 `TS2448`,而不是悄悄退化成一个过期闭包。
   */
  const openNodeMenuRef = useRef<((node: GrowthNode, trigger: HTMLElement | null, point?: { x: number; y: number }) => void) | null>(null);
  const handleMore = useCallback((node: GrowthNode, trigger: HTMLElement | null) => {
    openNodeMenuRef.current?.(node, trigger);
  }, []);

  /*
   * 问题节点的动作走**稳定的包装函数**。
   *
   * `submitAnswer` / `dismissQuestion` / `postponeQuestion` / `select` 都是 provider
   * 每次渲染新建的普通函数。它们直接进下面 `useMemo` 的依赖,会让整张图在**任何**
   * provider 重渲染时重算 —— 节点对象一批批换新,而 React Flow 对“换了对象”的节点
   * 会重置测量,于是“测量 → 重渲染 → 再测量”的循环就来了(节点持续闪烁)。
   * 用 ref 里那一份最新的闭包包一层,包装函数身份恒定,图只在真正的数据变化时重算。
   */
  const questionActionsRef = useRef({ submitAnswer, dismissQuestion, postponeQuestion, select });
  questionActionsRef.current = { submitAnswer, dismissQuestion, postponeQuestion, select };
  const handleQuestionSubmit = useCallback(
    (id: string, payload: { selectedOptionIds: string[]; customInput?: string | null }) =>
      questionActionsRef.current.submitAnswer(id, payload),
    [],
  );
  const handleQuestionSkip = useCallback(
    (id: string) => questionActionsRef.current.dismissQuestion(id),
    [],
  );
  const handleQuestionLater = useCallback(
    (id: string) => questionActionsRef.current.postponeQuestion(id),
    [],
  );
  const handleQuestionLocate = useCallback(
    (sourceNodeId: string) => questionActionsRef.current.select(sourceNodeId),
    [],
  );
  const setQuestionDraft = useCallback((questionId: string, next: CanvasQuestionDraft) => {
    setQuestionDrafts((prev) => ({ ...prev, [questionId]: next }));
  }, []);

  /**
   * 问题交互的 context 值。**草稿变了只重渲染消费它的那几张问题卡**,不重建整张
   * 节点数组 —— 这是闪烁修复的一半(另一半是下面 memo 的稳定依赖与 `measured`)。
   */
  const questionInteraction = useMemo<QuestionInteraction>(
    () => ({
      drafts: questionDrafts,
      onDraftChange: setQuestionDraft,
      onSubmit: handleQuestionSubmit,
      onSkip: handleQuestionSkip,
      onLater: handleQuestionLater,
      onLocateSource: handleQuestionLocate,
    }),
    [
      questionDrafts,
      setQuestionDraft,
      handleQuestionSubmit,
      handleQuestionSkip,
      handleQuestionLater,
      handleQuestionLocate,
    ],
  );

  const { nodes, edges: baseEdges } = useMemo(() => {
    /*
     * **节点投影重建计数(只给端到端测试看)。**
     *
     * 鼠标在节点上进进出出**不该**重建这份投影 —— 重建会让 React Flow 重新测量,
     * 画布上就是那阵闪烁。这个计数是“悬停 10 次投影不重建”那句话的证据:它数的是
     * **这份 memo 真的跑了几次**,不是从 DOM 上猜。写在 `window` 上而不是组件里,
     * 重挂载也不会把证据清掉(与 `__zhituCanvasLifecycle` 同一个理由)。
     *
     * 它不参与任何产品逻辑,所以 SSR 下直接跳过。
     */
    if (typeof window !== 'undefined') {
      const holder = window as unknown as { __zhituNodeProjectionBuilds?: number };
      holder.__zhituNodeProjectionBuilds = (holder.__zhituNodeProjectionBuilds ?? 0) + 1;
    }
    const nextNodes: FlowNode[] = [];
    const nextEdges: Edge[] = [];
    const all = Object.values(growth.nodes);
    const directChildren = all.filter((node) => node.parentId === spaceId);
    const vertical = false;

    /*
     * 节点投影**不读 hover**。`hoveredId` / `dragging` 只用来决定哪些线要变清晰,
     * 而那只该改**边**、不该重建节点对象 —— 节点对象一换,React Flow 就要重新测量、
     * 重排,鼠标一进一出就把整张图抖一遍。所以"谁被指着"这件事挪到了下面那份
     * 只算边的 `edges` memo 里。
     */

    // 兜底:`spaceId` 的契约是"一定指得到一个节点"(见 provider 里的 `currentSpaceId`)。
    // 万一将来这个契约被破坏,**这里不画比整个页面崩掉好** —— 在渲染中抛异常会把整棵
    // React 树卸掉,用户看到的是 "Application error: a client-side exception has
    // occurred",连左边导航都没了。少画一个节点难看,但那个状态是可用的、可导航的,
    // 而且节点数对不上的断言会立刻指出问题。
    if (!growth.nodes[spaceId]) return { nodes: [], edges: [] };

    //: 每个真实节点**本次画在哪**。问题节点靠它做锚定(见下面那些投影)。
    const positionById: Record<string, { x: number; y: number }> = {};

    function add(node: GrowthNode, x: number, y: number, root = false) {
      const key = `${spaceId}:${node.id}`;
      const placed = dragging[key] ?? positions[key] ?? { x, y };
      positionById[node.id] = placed;
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
          onMore: handleMore,
          created: node.id === createdId,
          onCreatedEnd: clearCreated,
        },
        position: dragging[key] ?? positions[key] ?? { x, y },
        selected: selectedId === node.id,
        ariaLabel: node.title,
      });
    }
    function connect(parent: string, node: GrowthNode) {
      // 父子连线也跟着 hover/拖动变清晰一点点 —— 它同样是"与这个节点相关的线"。
      // 默认值一个字没改:不相关的线看起来和以前完全一样。
      //
      // 这里只写**基础**宽度;hover/拖动时那 +0.7 由下面只算边的 `edges` memo
      // 叠上去。hover 不重建这张投影,是这一轮修复的一半。
      const base = selectedId === node.id ? 1.8 : 1.35;
      nextEdges.push({
        id: `${parent}-${node.id}`,
        source: parent,
        target: node.id,
        type: 'branch',
        data: { __baseWidth: base },
        style: {
          stroke: colors[node.category ?? 'academic'],
          opacity: 1,
          strokeWidth: base,
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
      // `parentId` 仍然保存“这个节点属于当前 NodeSpace”，但不再被误读成“用户
      // 已经建立了一条线”。手动放下的节点先独立；AI 生成的规划结构仍自动生长。
      if (node.origin !== 'user') connect(spaceId, node);
      let descendantY = cursorY;
      descendants.forEach((child) => {
        add(child, childX + width(node) + 170, descendantY);
        if (child.origin !== 'user') connect(node.id, child);
        descendantY += height(child) + 32;
      });
      cursorY += blockHeight + 56;
    });
    const rootCenter = centers.length ? (centers[0] + centers[centers.length - 1]) / 2 : 90;
    add(root, 0, rootCenter - height(root) / 2, true);

    // ---- 问题节点(纯投影) --------------------------------------------------
    // 来源:`agent_questions` 的 `questions`(provider 从 `GET /questions` 拉)。
    // 它们**不是** `plan_nodes`:不参与排期/依赖/统计,也不写进任何关系表。
    // 锚定到源节点;源节点缺失或未加载时锚到当前空间根。同锚点的多个问题按
    // `createdAt` 确定性排序 + 固定偏移,避免堆叠、保证刷新前后位置稳定。
    const orderedQuestions = [...questions].sort((a, b) =>
      a.createdAt === b.createdAt ? a.id.localeCompare(b.id) : a.createdAt.localeCompare(b.createdAt),
    );
    const primaryQuestionId = orderedQuestions.find((item) => item.status === 'pending')?.id ?? null;
    const anchorCounts: Record<string, number> = {};
    orderedQuestions.forEach((item) => {
      if (item.status === 'archived') return;
      const anchorId = item.sourceNodeId && positionById[item.sourceNodeId] ? item.sourceNodeId : spaceId;
      const anchor = positionById[anchorId] ?? { x: 0, y: 0 };
      const index = anchorCounts[anchorId] ?? 0;
      anchorCounts[anchorId] = index + 1;
      const nodeId = `question:${item.id}`;
      const positionKey = `${spaceId}:${nodeId}`;
      // 用户拖过就听用户的(UI-only 位置表);否则给一个确定性的扇出位置,
      // 保证同一锚点下多个问题不堆叠、刷新前后一致。
      const fallback = {
        x: anchor.x + 40 + (index % 2) * 300,
        y: anchor.y + 150 + Math.floor(index / 2) * 210,
      };
      const placed = questionDragging[positionKey] ?? questionPositions[positionKey] ?? fallback;
      nextNodes.push({
        id: nodeId,
        type: 'question',
        // **可自由拖动**,但它仍是 UI 投影:拖动只写 UI-only 位置表,不碰业务图谱。
        draggable: true,
        connectable: false,
        deletable: false,
        selectable: true,
        // 已测量过就带上,避免 React Flow 把“没有 measured 的新对象”当成未测量而反复重测。
        measured: measurements[nodeId],
        ariaLabel: item.question,
        position: placed,
        data: {
          questionId: item.id,
          question: item,
          isPrimary: item.id === primaryQuestionId,
          isFocused: focusedQuestionId === item.id,
        },
      });
      // 仅 UI 的锚定虚线:**不是 NodeRelation、不是 dependency**,不写任何表。
      nextEdges.push({
        id: `question-anchor:${item.id}`,
        source: anchorId,
        target: nodeId,
        type: 'questionAnchor',
        className: 'question-anchor-edge',
        // 不可选、不可删、不可重新连接、不进任何保存。
        selectable: false,
        deletable: false,
        reconnectable: false,
        focusable: false,
        zIndex: 0,
      });
    });

    // ---- 目标推理地图(纯 UI 投影,不是业务节点) ----------------------------
    // 它和 `growth`、`question` 是三种不同的节点类型。位置是确定性算出来的 ——
    // 推理节点不进业务位置表,刷新后按同一规则重建。
    if (reasoning && reasoning.nodes.length > 0) {
      const anchor = positionById[spaceId] ?? { x: 0, y: 0 };
      const reasoningPos: Record<string, { x: number; y: number }> = {};
      const primaries = reasoning.nodes.filter((item) => !item.parentHandle);
      primaries.forEach((item, index) => {
        reasoningPos[item.handle] = { x: anchor.x + index * 300, y: anchor.y + 430 };
      });
      for (const item of reasoning.nodes) {
        if (!item.parentHandle) continue;
        const parent = reasoningPos[item.parentHandle] ?? { x: anchor.x, y: anchor.y + 430 };
        const siblings = reasoning.nodes.filter((node) => node.parentHandle === item.parentHandle);
        const index = siblings.indexOf(item);
        reasoningPos[item.handle] = { x: parent.x + (index + 1) * 220, y: parent.y + 160 };
      }
      for (const item of reasoning.nodes) {
        const nodeId = `reasoning:${item.handle}`;
        nextNodes.push({
          id: nodeId,
          type: 'reasoning',
          draggable: false,
          connectable: false,
          deletable: false,
          selectable: true,
          // 已测量过就带上,避免 React Flow 把“没有 measured 的新对象”当成未测量
          // 而反复重测 —— 那是悬停/重建时闪烁的直接机制(与问题节点同一条)。
          measured: measurements[nodeId],
          ariaLabel: item.title,
          position: reasoningPos[item.handle] ?? { x: anchor.x, y: anchor.y + 430 },
          data: {
            node: item,
            isFocus: item.handle === reasoning.focusHandle,
          },
        });
      }
      // 讨论锚定线:每个**顶层**推理节点(没有 `parentHandle` 的)都与当前目标
      // 根节点连一条 UI-only 虚线,表达"这一层讨论是从这个目标长出来的"。
      // 它继承的是绘制语义,不是业务语义:不进 NodeRelation、不是 depends_on、
      // 不写任何表、不参与排期(见 `ReasoningAnchorEdge` 与验收用例)。
      for (const item of primaries) {
        nextEdges.push({
          id: `reasoning-anchor:${item.handle}`,
          source: spaceId,
          target: `reasoning:${item.handle}`,
          type: 'reasoningAnchor',
          className: 'reasoning-anchor-edge',
          // 不可选、不可删、不可重新连接、不可聚焦;压在节点与真实关系之下。
          selectable: false,
          deletable: false,
          reconnectable: false,
          focusable: false,
          zIndex: 0,
        });
      }
      for (const link of reasoning.links) {
        if (!reasoningPos[link.sourceHandle] || !reasoningPos[link.targetHandle]) continue;
        nextEdges.push({
          id: `reasoning-link:${link.id}`,
          source: `reasoning:${link.sourceHandle}`,
          target: `reasoning:${link.targetHandle}`,
          type: 'reasoningLink',
          selectable: false,
          deletable: false,
          reconnectable: false,
          focusable: false,
        });
      }
    }

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
        data: {
          kind: edge.type,
          note: edge.note,
          __drawn: edge.id === drawnEdgeId,
          onDrawnEnd: clearDrawn,
        },
        selected: selectedEdgeId === edge.id,
        // 箭头挂在这里,不在 `RelationEdge` 里 —— 见那边的注释。
        markerEnd: look.arrow
          ? { type: MarkerType.ArrowClosed, color: look.color, width: 15, height: 15 }
          : undefined,
      });
    });
    return { nodes: nextNodes, edges: nextEdges };
  }, [growth, spaceId, isRootSpace, selectedId, selectedEdgeId, positions, dragging, questionPositions, questionDragging, files, measurements, handleMore, createdId, drawnEdgeId, clearCreated, clearDrawn, questions, focusedQuestionId, reasoning]);

  /*
   * hover / 拖动的高亮**只作用在边对象上**。
   *
   * 上面那份 memo 产出的是稳定的节点投影:只要数据没变,`nodes` 里每个对象的
   * id、position、measured、data 回调都不动 —— 鼠标在节点上进进出出不会重建它们,
   * React Flow 也就不需要重新测量/重排(那正是"悬停闪烁"的来源)。
   *
   * 这里只按 `focusId` 复制**被指着的线**那一两条,其余边原样返回。于是:节点
   * 对象一个不换,相关线高亮的能力一点没少。
   */
  const edges = useMemo(() => {
    /*
     * 与上面那个投影计数配对：hover 真的到达 React 时，这份**只算边**的 memo 会跑。
     * 验收里同时看两个数 —— 边在动、节点投影不动，才能证明解耦真的生效。
     */
    if (typeof window !== 'undefined') {
      const holder = window as unknown as { __zhituEdgeFocusBuilds?: number };
      holder.__zhituEdgeFocusBuilds = (holder.__zhituEdgeFocusBuilds ?? 0) + 1;
    }
    // 拖动优先:拖的时候指针可能已经不在节点上了,而用户关心的还是手里这一个。
    // 键是 `${spaceId}:${nodeId}`(见 `onNodesChange`),**按真实 id 判断**,
    // 不是按标题。
    const draggingKey = Object.keys(dragging).find((key) => key.startsWith(`${spaceId}:`));
    const focusId = draggingKey ? draggingKey.slice(spaceId.length + 1) : hoveredId;
    if (focusId === null) return baseEdges;
    return baseEdges.map((edge) => {
      if (edge.type === 'branch') {
        if (edge.source !== focusId && edge.target !== focusId) return edge;
        const base = (edge.data as { __baseWidth?: number } | undefined)?.__baseWidth ?? 1.35;
        return { ...edge, style: { ...edge.style, strokeWidth: base + 0.7 } };
      }
      if (edge.type === 'relation') {
        if (edge.source !== focusId && edge.target !== focusId) return edge;
        return { ...edge, data: { ...edge.data, __focus: true } };
      }
      return edge;
    });
  }, [baseEdges, dragging, spaceId, hoveredId]);

  // 右侧「定位到画布」:等 question 节点投影出来之后 fit 一次,并选中它。
  useEffect(() => {
    if (!questionFocus) return;
    const targetId = `question:${questionFocus.id}`;
    if (!nodes.some((node) => node.id === targetId)) return;
    if (lastQuestionFocusNonce.current === questionFocus.nonce) return;
    lastQuestionFocusNonce.current = questionFocus.nonce;
    setFocusedQuestionId(questionFocus.id);
    void fitView({ nodes: [{ id: targetId }], duration: 260, maxZoom: 1.5, padding: 0.6 });
  }, [questionFocus, nodes, fitView]);

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
      // `maxZoom` 1 → 1.2 与 `padding` 0.18 → 0.12 一起,解决的是"画布没有视觉中心":
      // 三个节点的小图本来会被放大填满画布,被 `maxZoom: 1` 卡住 —— 于是它缩在
      // 1022×950 的中间一小团。**不是节点长得小,是它被禁止长大。**
      //
      // 1.2 是权衡后的上限,不是随手取的:缩放控件一次是 `scaleBy(1/1.2)`,
      // `canvas-create.spec.ts` 用"连缩两次之后 zoom < 0.95"来验缩放真的生效 ——
      // 1.2 / 1.44 = 0.833,留得下余量;再往上(比如 1.35)那一档只剩 1 个百分点,
      // 而且放大会把文字一起拉毛。
      //
      // 手机那一档 `padding` 放到 0.2:这个数是**视口宽度的比例**,390 下 12% 两边
      // 各留 47px,而节点自己有 220~240 的 `min-width` —— 树一宽就顶到画布边上,
      // "打开就有一棵挤在边框上的图"。0.2 两边各留 78px,根节点完整、四周有呼吸。
      else void fitView({ padding: narrowScreen ? 0.2 : 0.12, maxZoom: 1.2, duration: 0 });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [nodesInitialized, planLoading, layoutReady, spaceId, measurements, fitView, setViewport, viewports, narrowScreen]);

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
  // 每次渲染都记一次"编辑器开着哪个节点"。`flushNote` 从定时器里被调用,而定时器的
  // 闭包停在**排它那一帧**上 —— 没有这一行,它就不知道用户已经切走了(见 `noteOwnerRef`)。
  detailNodeIdRef.current = detailNodeId;

  /* ------------------- 三个长度上限此刻的状态(只用于界面提示) -------------------
     真正的执行在服务端;这里的三行是为了让用户**在发出去之前**就知道自己写长了。
     三个各算各的,因为它们是三条不同的规则:
     - 创建表单:新建的节点一律受限 —— 它还没有库里那一份,谈不上豁免。
     - 详情里的说明:受不受限看**库里那一份**(见 `descriptionTooLong`)。
     - 长正文:一条固定的 20,000,与上面那条无关。
     ------------------------------------------------------------------------ */
  const createDescriptionOver = codePointLength(description) > MAX_DESCRIPTION_CODEPOINTS;
  const detailDescriptionOver = descriptionTooLong(detailDescription, detailNode?.description);
  const noteOver = codePointLength(noteBody) > MAX_NOTE_CODEPOINTS;
  /**
   * 这一层上一次看到的视口。
   *
   * 有记忆时**不用 ReactFlow 自带的初次 fit**(下面那个 `fitView` 属性):它会在挂载
   * 那一刻先 fit 一次,而我们那个 effect 稍后再跳回记忆里的位置 —— 用户会看到画面
   * 先跳一下再回来。少这一次跳动,也让"视口是哪来的"只有一个答案。
   */
  const rememberedViewport = viewports[spaceId];

  /* ---------------------------------------------------------------------------
     右键菜单(§9.1.1)
     ---------------------------------------------------------------------------
     两处 `preventDefault`,而且**只有这两处**:

     - `onNodeContextMenu` / `onPaneContextMenu` 是 ReactFlow 给的回调,它们只在
       画布与节点上触发。**不挂全局 `contextmenu` 监听** —— 那样详情弹窗里的正文
       textarea、说明输入框都会被吃掉原生菜单,而"选中一段字然后用系统菜单查一下"
       是这两个框里最正常的操作之一。

     `paneMenu` 里的**不清 `selectedId`**:用户在空白处右键是想在那儿建个东西,
     不是想取消选中另一个节点。清掉的话菜单关掉之后画布上没有选中项,他会以为
     自己刚才那一下把选中弄丢了。
  --------------------------------------------------------------------------- */
  const [nodeMenu, setNodeMenu] = useState<ContextMenuState | null>(null);
  const [paneMenu, setPaneMenu] = useState<ContextMenuState | null>(null);
  /** 菜单挂靠哪个元素夹紧 —— 画布那一层,见 `ContextMenu` 的 `boundary`。 */
  const canvasRef = useRef<HTMLDivElement>(null);

  /**
   * 打开一个节点的菜单。
   *
   * `trigger` 给了就按**那个元素的 rect** 定位(点「更多」按钮、或键盘 `Shift+F10`),
   * 没给就用鼠标的 `clientX/clientY`(右键)。两条路都落在"用户刚才指的那个东西"旁边。
   *
   * **菜单里的操作对象是传进来的这个 `node`,不是 `selectedId`。** 差别是可测的:
   * 先点选 A、再右键 B,菜单上那一项必须作用于 **B**。读 `selectedId` 的话它会去归档 A
   * —— 而用户在菜单上看到的名字是 B,于是"我删了 B,结果 A 没了"。
   */
  function openNodeMenu(node: GrowthNode, trigger: HTMLElement | null, point?: { x: number; y: number }) {
    const at = point ?? (() => {
      const rect = trigger?.getBoundingClientRect();
      return rect ? { x: rect.right, y: rect.bottom } : { x: 0, y: 0 };
    })();
    const isRoot = node.id === growth.goalId;
    setPaneMenu(null);
    setNodeMenu({
      x: at.x,
      y: at.y,
      restoreFocusTo: trigger,
      items: [
        // 根目标上不摆「进入子路径」:根目标的子空间**就是**你现在看的这一层
        // (`spaceId === growth.goalId`),点下去什么都不会发生。一份菜单里摆一项
        // 按了没反应的,和根目标归档原来那句静默 return 是同一种毛病。
        ...(isRoot ? [] : [{
          key: 'enter',
          label: '进入子路径',
          icon: <ArrowUpRight size={13} />,
          onSelect: () => enterSpace(node.id),
        }]),
        {
          key: 'focus',
          label: '聚焦这个节点',
          icon: <Crosshair size={13} />,
          onSelect: () => { void fitView({ nodes: [{ id: node.id }], duration: 220, maxZoom: 1.2, padding: 0.8 }); },
        },
        {
          key: 'archive',
          label: '归档(可以恢复)',
          icon: <Trash2 size={13} />,
          // 根目标删不掉。**禁用要说原因** —— 只给一个灰项的话,用户没有任何办法
          // 知道为什么。以前这里是**静默 return**(见 `provider.askArchive`),那
          // 更糟:菜单点下去什么都不会发生,看起来像坏了。
          disabled: isRoot,
          disabledReason: isRoot ? '根目标不能归档' : undefined,
          onSelect: () => askArchive(node.id),
        },
      ],
    });
  }

  // 每次渲染都把**最新的那一份**放进 ref —— `handleMore` 只认这个 ref,
  // 而它自己的身份是恒定的(见上面那段说明)。这一行必须在每次渲染都跑,
  // 所以它是一条普通的赋值语句,不在任何回调或条件里。
  openNodeMenuRef.current = openNodeMenu;

  function openDetail(node: GrowthNode) {
    select(node.id);
    patchDraft({
      detailNodeId: node.id,
      detailTitle: node.title,
      detailDescription: node.description ?? '',
    });
    // 打开的这一份是哪一版。**必须在打开的那一刻取**,不能等到保存的时候再去读
    // `detailNode.contentVersion`:那中间可能已经刷新过好几次计划,拿到的就是"最新
    // 那一版",而这个号正是用来发现"我手上这份旧了"的,它一"自动变新",锁就没了。
    bodyVersion.current = node.contentVersion;
    bodySavedAt.current = null;
    setBodyNote({ kind: 'none' });
    // 长正文那一份**不在这里赋值**:它不在 `/plan` 载荷里,要按需去取(见下面那个
    // 效果)。这里只把状态清干净 —— 下一次渲染时 `noteNodeId` 与 `detailNodeId`
    // 对不上,于是编辑器显示"正在读取",而不是上一个节点的正文顶着这个节点的标题。
    setNoteNote({ kind: 'none' });
    setNoteLoadError(null);
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
    // 超限不自动存。理由与 `flushNote` 里那一条一样:服务端会拒(400),而拒了之后
    // 每 700 毫秒再撞一次,屏幕上只会来回闪"正在保存正文"。豁免的那一类不受这条约束
    // —— 判据是**库里那一份**(见 `isDescriptionExempt`)。
    if (descriptionTooLong(text, node.description)) return;
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
    if (descriptionTooLong(detailDescription, node.description)) return;  // 超限,见 `flushBody`
    const timer = window.setTimeout(() => { void flushBody(); }, BODY_SAVE_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 见上:`bodyNote` 只做守卫,不进依赖
  }, [detailDescription, detailNode]);

  /* ---------------------------------------------------------------------------
     长正文(笔记)的读取与保存
     ---------------------------------------------------------------------------
     与上面那段**同构,但读的那一步不一样**。正文住在计划里,打开编辑器时它已经在手上
     (`node.description`);长正文不在 `/plan` 载荷里(两万字会让每一次读计划都背着它),
     所以打开一个节点要**按需去读一次**。于是这里多出一种状态:**还没读完** ——
     那段时间屏幕上不能显示任何字,显示上一个节点那一份是这里最坏的一种错
     (标题已经是 B,字还是 A)。

     三件事的边界:
     - **读** 失败不当成空:`noteLoadError` 让编辑器先不出现(理由见那个 state)。
     - **写** 仍是停手 700 毫秒一次,带笔记自己的版本号 —— 它与正文那条锁互不牵连,
       所以改一次 300 字的说明不会让两万字的笔记存不上(见 `saveNodeNote`)。
     - **冲突** 回读的也是笔记那一份,而不是整份计划。
  --------------------------------------------------------------------------- */
  useEffect(() => {
    if (!isRealSpace || !detailNodeId) return;
    // **先把归属清掉**,再开始读:`noteBodyRef` 里现在装的还是上一个节点的字,
    // 而"正在读"这段时间任何一次 `flushNote` 都必须什么都不做。
    noteOwnerRef.current = null;
    let cancelled = false;
    void (async () => {
      const fresh = await loadNodeNote(detailNodeId);
      if (cancelled) return;                     // 用户已经切到别的节点/关掉了
      if (!fresh) {
        setNoteLoadError('这一份长正文没读到。');
        return;
      }
      setNoteLoadError(null);
      noteVersion.current = fresh.contentVersion;  // 没写过时是 0,见 `loadNodeNote`
      noteServerBody.current = fresh.body;
      noteOwnerRef.current = detailNodeId;
      // **以库里那一份为准,不做"本地那份还没存,先留着"的合并。** 合并会让手上这份
      // 的正文是旧的那一段、版本号却是新的那个 —— 而版本号正是用来发现这件事的。
      // 与本文件对正文做的(`openDetail` 直接取 `node.description`)是同一件事。
      patchDraft({ noteNodeId: detailNodeId, noteBody: fresh.body });
      setNoteNote({ kind: 'none' });
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `patchDraft` 跟着草稿键走,键变时这个效果本来就该重跑
  }, [detailNodeId, noteReload, isRealSpace]);

  async function flushNote() {
    const nodeId = noteOwnerRef.current;
    // 编辑器里那份**属于**这个节点、**且**用户还开在这个节点上 —— 两个条件缺一不可。
    // 前者挡"刚切过去、新的那份还在路上",后者挡"定时器排下之后用户就切走了"。
    if (!nodeId || nodeId !== detailNodeIdRef.current || noteInFlight.current) return;
    const text = noteBodyRef.current;
    if (text === noteServerBody.current) return;
    // 超限就不自动存。服务端会拒(400),而拒了之后每 700 毫秒再撞一次同一堵墙,
    // 屏幕上那行状态会来回闪 —— 用户看到的是"一直在保存",不是"太长了,存不进去"。
    if (codePointLength(text) > MAX_NOTE_CODEPOINTS) return;
    noteInFlight.current = true;
    setNoteNote({ kind: 'saving' });
    const result = await saveNodeNote(nodeId, text, noteVersion.current);
    noteInFlight.current = false;
    if (result.status === 'saved') {
      // 版本号用**后端回来的那一个**,理由与 `flushBody` 同:自己 `+1` 的话,
      // 只要有一次写入不是"笔记 +1",本地那个号就会一路偏下去。
      noteVersion.current = result.contentVersion;
      noteServerBody.current = text;
      setNoteNote({ kind: 'saved', at: new Date().toLocaleTimeString('zh-CN', { hour12: false }) });
      return;
    }
    if (result.status === 'conflict') {
      noteVersion.current = result.serverVersion;
      setNoteNote({ kind: 'conflict', serverBody: result.serverBody, message: result.message });
      return;
    }
    setNoteNote({ kind: 'failed', message: result.message });
  }

  /** 长正文改动后停手 700 毫秒存一次。守卫与依赖的理由同上面那段正文。 */
  useEffect(() => {
    if (!isRealSpace) return;
    const node = detailNode;
    // `noteNodeId` 而不是 `noteBody`:编辑器里那份是**哪个节点**的,由它说了算。
    // 对不上就是"还没读完",那时候一个定时器都不该排。
    if (!node || noteNodeId !== node.id) return;
    noteBodyRef.current = noteBody;
    if (noteNote.kind === 'conflict') return;
    if (noteInFlight.current) return;
    if (codePointLength(noteBody) > MAX_NOTE_CODEPOINTS) return;
    if (noteBody === noteServerBody.current) return;
    const timer = window.setTimeout(() => { void flushNote(); }, BODY_SAVE_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 见上:`noteNote` 只做守卫,不进依赖
  }, [noteBody, noteNodeId, detailNode, isRealSpace]);

  /**
   * 这一层能连的节点 —— **就是画布上看得见的那些**,不是这个空间里的全部节点。
   *
   * 见画边那里的注释:连到别的层级去的边在当前这一层看不见(有一头不在场)。
   * 让用户在表单里选中一个画布上没有的节点,得到的是"我建了,但图上找不到" ——
   * 不如把选择范围收成与眼睛看到的一致。跨层关系是步骤 4 的事(那时每层都能看到
   * 挂在自己下面的东西),这一批不含。
   */
  const relationCandidates = nodes.flatMap((node) => (node.type === 'growth' ? [node.data.object] : []));
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
    // 真实的那条边回来了 —— 让它从 source 画到 target 一次(280ms),然后回到稳定。
    // 用的是**后端返回的** id 与两端:`related_to` 是无向的,库里按 UUID 排过序,
    // 所以画出来的方向以后端那一份为准,不是用户拖动的那一头。
    setDrawnEdgeId(created.id);
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
    <div className={`path-canvas ${isRootSpace ? 'root-path' : 'leaf-path'}`} ref={canvasRef}>
      {/* 画布这一块大面积空背景上的环境光晕。它在这一层是 `position:absolute`,
          理由见 `AmbientGlow` 与 `motion.css` 里那段说明。 */}
      <AmbientGlow />
      <div className="space-floating-tools">
        {/* 提示里必须写清"怎么连线" —— 拖线这件事没有任何别的入口在教。
            也说明**点线**能打开编辑器:线很细,不提示的话没人会去点它。

            **双击建节点与右键菜单也必须写在这里。** 这两件事在界面上没有任何
            可见的形状(空白处没有按钮,节点上的垃圾桶还刚被收进了菜单),不教
            就真的没人会发现 —— 而"用户不知道能双击"和"这个功能没做"在他那里
            是同一件事。 */}
        <button disabled={!canUndo} title={canUndo ? '撤销上一次移动节点（Ctrl+Z）' : '还没有可以撤销的移动'} aria-label="撤销" onClick={() => undoLayout()}><Undo2 size={15}/></button>
        <button disabled={!canRedo} title={canRedo ? '重做上一次移动（Ctrl+Shift+Z）' : '没有可以重做的移动'} aria-label="重做" onClick={() => redoLayout()}><Redo2 size={15}/></button>
        <button onClick={() => patchDraft({ dialog: 'files' })}><FolderOpen size={15}/>空间文件 <small>{files.filter(file => file.ownerId === spaceId).length || ''}</small></button>
        <details className="canvas-tools-menu"><summary aria-label="更多空间操作"><MoreHorizontal size={17}/>{archived.length ? <small>{archived.length}</small> : null}</summary><div className="canvas-tools-popover" onClick={event => { if ((event.target as HTMLElement).closest('button:not(:disabled)')) event.currentTarget.closest('details')?.removeAttribute('open'); }}>
          <p>双击空白处也可以新建节点，右键节点可以打开节点操作。</p>
          <button disabled={!canCreate} title={canCreate ? undefined : '正在读取计划…'} onClick={() => { setPlanError(null); patchDraft({ dialog: 'node' }); }}><Plus size={15}/>{createLabel}</button>
          <button disabled={!canCreate || relationCandidates.length < 2} title={relationCandidates.length < 2 ? '这一层至少要有两个节点才能连关系' : undefined} onClick={() => openRelationForm()}><GitBranch size={15}/>建立关系</button>
          <button onClick={() => setArchiveOpen(true)} title="看看这个空间归档过什么,把想留的恢复回来"><Archive size={15}/>归档 <small>{archived.length || ''}</small></button>
        </div></details>
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
      <QuestionInteractionContext.Provider value={questionInteraction}>
      <ReactFlow<FlowNode>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        fitView={!rememberedViewport}
        // 与上面那个 `fitView` 调用**必须同值**:同一个初次视角有两条入口 ——
        // 有记忆时走上面那段 effect,没有记忆时走 ReactFlow 自带的这个属性。
        // 只改一处会让"第一次进这个空间"和"清掉记忆再进"长得不一样,
        // 而那种不一致最容易被当成随机故障。
        fitViewOptions={{ padding: narrowScreen ? 0.2 : 0.12, maxZoom: 1.2 }}
        zoomOnDoubleClick={false}
        minZoom={0.25}
        maxZoom={1.7}
        // 单击 = 打开正文与详情,**立刻开**。进入子空间不再挂在这里(见上面那段注释:
        // 拆开之后这条路上没有任何计时器,按下去发生什么是一定的)。
        onNodeClick={(_, node) => {
          if (node.type === 'question') {
            setFocusedQuestionId(node.data.questionId);
            return;
          }
          if (node.type === 'reasoning') {
            handleOpenReasoning(node.data.node);
            return;
          }
          openDetail(node.data.object);
        }}
        /*
         * 指针进出节点。只记 id —— 一个字符串。让整张图按 hover 重算一次的代价
         * 是有的,所以它必须换来看得见的东西:与这个节点直接相连的线在 140ms 内
         * 变清晰(见 `focusId`)。不相关的一条都不动。
         */
        onNodeMouseEnter={(_, node) => setHoveredId(node.id)}
        onNodeMouseLeave={() => setHoveredId(null)}
        // 拖线建边。**在节点上拖,不在空白处拖** —— 空白处拖动是平移画布。
        onConnect={(connection) => {
          if (!connection.source || !connection.target) return;
          connectNodes(connection.source, connection.target);
        }}
        onEdgeClick={(_, edge) => {
          // 问题锚定虚线不是业务关系:点它不该打开关系编辑器。
          if (edge.id.startsWith('question-anchor:')) return;
          // 讨论锚定线同样不是业务关系 —— 它连不上任何 `growth.edges` 里的行,
          // 但先挡一道,免得将来 id 形式变了之后被误当业务线。
          if (edge.id.startsWith('reasoning-anchor:')) return;
          // 这个 handler 只收到关系边(父子连线由 `BranchEdge` 画,没有 onClick)。
          const relation = growth.edges.find((item) => item.id === edge.id);
          if (relation) openRelationEditor(relation);
        }}
        onPaneClick={() => { select(null); setSelectedEdgeId(null); setFocusedQuestionId(null); }}
        /*
          `screenToFlowPosition` —— **不手搓** `(clientX - rect.left - panX) / zoom`。
          手搓的那一份在缩放不为 1、或者画布有 padding 的时候就会偏,而偏差是
          按比例放大的:用户缩到 0.5 倍双击一下,节点落在离指针两倍远的地方。
          库的这个函数本来就处理了 `transform`,没有理由自己再推一遍。
        */
        onDoubleClick={(event) => {
          // 与工具栏那个按钮**同一个闸门**。用 `isRealSpace` 是不够的:计划还没到的时候
          // `spaceId` 是哨兵值 `'goal'`,那时建节点发出去就是 422(见 `canCreate`)。
          if (!canCreate || dialog !== null) return;
          // **正向判定**:双击必须落在画布空白处。写成"排除节点、排除边、排除工具栏"
          // 那种名单是维护不完的 —— 加一个浮层就漏一个。判据见 `onEmptyPane`:
          // 目标是"那一层本身"才算,于是节点、边、连接点、工具栏、菜单、编辑器
          // 全都按构造不满足。
          if (!onEmptyPane(event)) return;
          setPlanError(null);
          const point = screenToFlowPosition({ x: event.clientX, y: event.clientY });
          // 建出来的是**当前层级的同级主题**节点,不是新建一个独立工作空间 ——
          // 所以父节点是 `spaceId`(当前这一层),不是根目标。
          patchDraft({ dialog: 'node', createKind: 'topic', createPosition: point });
        }}
        onNodeContextMenu={(event, node) => {
          event.preventDefault();
          // 问题节点没有业务右键菜单。
          if (node.type !== 'growth') return;
          openNodeMenu(node.data.object, null, { x: event.clientX, y: event.clientY });
        }}
        onPaneContextMenu={(event) => {
          event.preventDefault();
          // **这个回调不只在空白处触发。** 实测:右键工具栏那一条也会走到这里
          // —— 因为工具栏是画在 `.react-flow__pane` **里面**的(见 `onEmptyPane`)。
          // 所以这里必须自己再判一次,否则用户在某个按钮上右键会得到一个
          // 悬在工具栏上的"在这里新建节点"。
          if (!onEmptyPane(event)) return;
          setPlanError(null);
          // 流坐标**在开菜单这一步就算好**,不在 `onSelect` 里算 —— 后者是在菜单已经
          // 开着、可能过了几秒之后才跑的,那时用户若顺手滚过滚轮,算出来的位置就不是
          // 他右键的那一点了。
          const point = screenToFlowPosition({ x: event.clientX, y: event.clientY });
          setNodeMenu(null);
          setPaneMenu({
            x: event.clientX,
            y: event.clientY,
            restoreFocusTo: null,
            items: [
              {
                key: 'create',
                label: '在这里新建节点',
                icon: <Plus size={13} />,
                onSelect: () => {
                  // **不清 `selectedId`** —— 右键空白处不是"取消选中"。
                  patchDraft({ dialog: 'node', createKind: 'topic', createPosition: point });
                },
              },
            ],
          });
        }}
        onNodeDragStart={(_, node) => {
          // 拖动开始那一刻它在哪儿。撤销要把它摆回**这一帧**,而不是"删掉它的位置" ——
          // 节点第一次被拖动时位置表里没有它,删键只在本地看着对,刷新就会被库里那份
          // 拉回拖到的位置。见 `commitNodeMove`。
          dragStart.current = { id: node.id, position: { x: node.position.x, y: node.position.y } };
        }}
        onNodesChange={(changes) => {
          for (const change of changes) {
            if (change.type === 'position' && change.position && change.dragging) {
              const key = `${spaceId}:${change.id}`;
              // 问题节点是 UI-only:拖动只进它自己的预览表,不进业务那份。
              if (change.id.startsWith('question:')) {
                setQuestionDragging((old) => ({ ...old, [key]: change.position! }));
              } else {
                setDragging((old) => ({ ...old, [key]: change.position! }));
              }
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
          const key = `${spaceId}:${node.id}`;
          const started = dragStart.current;
          dragStart.current = null;
          if (node.type === 'question') {
            // **UI-only 位置。** 不记历史、不排布局落库、不碰后端 —— 问题节点不是
            // 业务节点,拖它只是“我把这张卡放在这里”。
            commitQuestionMove(key, { x: node.position.x, y: node.position.y });
          } else {
            // **一次拖拽 = 一步历史。** 中途那几十帧在 `onNodesChange` 里只进 `dragging`
            // (预览),一行历史都不占 —— 逐帧记的话,按一次撤销只往回挪一个像素。
            const moved = { [key]: node.position };
            // `before` 只有拖动的**起点**知道。没拿到(理论上不会)就不记这一步:记一条
            // "撤销了但没动"的账比不记更坏。
            commitNodeMove(moved, started?.id === node.id ? { [key]: started.position } : {});
          }
          // **拖完把预览扔掉。** 预览只是“拖动中”的那一份(画节点用的是
          // `dragging[key] ?? positions[key] ?? 自动排布`),拖完不清的话位置的
          // 真值会被它挡住 —— 撤销因此“按了没反应”。
          if (node.type === 'question') {
            setQuestionDragging((old) => {
              if (!(key in old)) return old;
              const next = { ...old };
              delete next[key];
              return next;
            });
          } else {
            setDragging((old) => {
              if (!(key in old)) return old;
              const next = { ...old };
              delete next[key];
              return next;
            });
          }
        }}
        onMoveStart={() => setPanning(true)}
        onMoveEnd={(_, viewport) => {
          // 先摘掉平移标记,**在下面那个早退之前** —— 否则"初始定位还没跑完"的
          // 那几次平移会让这个类一直挂在 `<html>` 上,Dock 从此一直是淡的。
          setPanning(false);
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
          // 问题节点没有 `data.object`(它不是业务 GrowthNode)—— 直接取会抛。
          // 这里先判类型,再判可取到的 object,最后才回退到默认色。
          nodeColor={(node) => {
            if (node.type === 'question') return '#c7a06e';
            const object = (node.data as Partial<GrowthFlowData> | undefined)?.object;
            return colors[object?.category ?? 'academic'];
          }}
          maskColor="rgba(238,244,244,.78)"
          pannable
          zoomable
        />
      </ReactFlow>
      </QuestionInteractionContext.Provider>
      {openReasoningNode && (
        <ReasoningDetail
          node={openReasoningNode}
          onClose={() => setOpenReasoningHandle(null)}
          onAgentTurn={(payload) => { void agentTurn(payload); }}
          onEdit={(patch) => { void editReasoningNode(openReasoningNode.id, patch); }}
        />
      )}
      {(reasoningLoading || reasoning?.status === 'failed') && (
        <div className="reasoning-status" role="status">
          {reasoningLoading ? '正在梳理问题地图…' : '问题地图这次没有梳理成'}
          {!reasoningLoading && reasoning?.status === 'failed' && (
            <button type="button" onClick={() => { void ensureReasoningMap({ retry: true }); }}>重试</button>
          )}
        </div>
      )}
      {reasoning?.phase === 'execution_planning' && (
        <button
          className="reasoning-refine"
          type="button"
          disabled={refining}
          onClick={() => { void refineStrategy(); }}
        >
          {refining ? '正在细化…' : '细化第一阶段'}
        </button>
      )}
      {!canvasHasContent && (
        <div className="empty-space-note">
          <span>这里，还可以长出更多可能。</span>
          <button onClick={() => { setPlanError(null); patchDraft({ dialog: 'node' }); }}>
            <Plus size={14} />
            {isRootSpace ? '添加第一个子节点' : '添加第一片树叶'}
          </button>
        </div>
      )}
      {/*
        两个菜单都渲染在这里(而不是在节点组件里),因为定位要按**画布容器**夹紧,
        而容器是这一层才知道的。同一时刻只可能开一个 —— 打开任一个都会把另一个关掉。
      */}
      {nodeMenu && (
        <ContextMenu state={nodeMenu} onClose={() => setNodeMenu(null)} boundary={canvasRef.current} />
      )}
      {paneMenu && (
        <ContextMenu state={paneMenu} onClose={() => setPaneMenu(null)} boundary={canvasRef.current} />
      )}
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
              const kind = CREATE_KINDS[createKind];
              const created = await addNode({
                title,
                nodeType: kind.nodeType,
                purpose: kind.purpose,
                description,
                // 信息主题**不带工时** —— 它不进排期,工时对它没有意义(§4.1)。
                // 界面上那个输入框对它是隐藏的,这里再挡一次:用户先填了工时、
                // 再把类型改成「主题 / 方向」,那一串数字还在草稿里,不该跟着提交上去。
                estimateMinutes: kind.purpose === 'planning' ? parseEstimate(estimate) : null,
                position: createPosition,
              });
              setSubmitting(false);
              if (!created) return;
              // **动画的唯一触发点就在这一行之后。** `created` 是 `addNode` 真的
              // 返回了一个节点 —— 也就是说库里已经有了它。失败那条路(`!created`)
              // 在上面就返回了,什么都不会播。
              setCreatedId(created.id);
              // 建成了才丢草稿:这一份已经变成库里的节点了。
              closeCreateDialog();
              // **双击那条路不再自动 fit。** 用户刚刚亲手指定了位置,把画面重新摆一遍
              // 等于把他自己那一下抹掉 —— 而且他双击之后节点就在眼前,不需要"带我去看"。
              // 工具栏那条路仍然 fit:那里没有指定过位置,节点可能落在视野之外。
              if (!createPosition) {
                setTimeout(() => void fitView({ duration: 200, padding: 0.2, maxZoom: 1 }), 80);
              }
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
            {/*
              选择器问的是**"你要加的是什么"**,不是"节点类型"。
              每一项映射到一对 `(purpose, nodeType)`(见 `CREATE_KINDS`)——
              用户不需要知道这两个后端字段的区别,而不问用途的后果是:
              他建了一个"主题",然后在那里填工时、看见勾选框、最后发现它排不进日历。
            */}
            <label>
              {isRootSpace ? '节点类型' : '树叶类型'}
              <select
                value={createKind}
                onChange={(event) => patchDraft({ createKind: event.target.value as CreateKind })}
              >
                {(Object.keys(CREATE_KINDS) as CreateKind[]).map((key) => (
                  <option key={key} value={key}>{CREATE_KINDS[key].label}</option>
                ))}
              </select>
              <small className="field-hint">{CREATE_KINDS[createKind].purpose === 'information'
                ? '记下来的一件事或一个方向,不排进日历。'
                : '要去做的一件事,可以排进日历。'}</small>
            </label>
            <label>
              {isRootSpace ? '节点说明（可选）' : '树叶说明'}
              {/* **没有 `maxLength`。** 浏览器那个属性数的是 UTF-16 码元,一个 emoji
                  会被它数成两个 —— 于是它会在服务端本来接受的输入上先拦住,而且用户
                  看不见(输入框里的计数说"满了",服务端那边还有一半余量)。上限在
                  服务端,这里只是**提前告知**,见下面的计数。 */}
              <textarea
                value={description}
                onChange={(event) => patchDraft({ description: event.target.value })}
                placeholder="写清楚这片树叶要积累什么、下一步做什么"
              />
              <small className={`body-count${createDescriptionOver ? ' is-over' : ''}`}>
                {createDescriptionOver
                  ? `说明最多 300 个字（按 Unicode 码点计），这一份有 ${codePointLength(description)} 个。`
                  : `${codePointLength(description)}/${MAX_DESCRIPTION_CODEPOINTS}`}
              </small>
            </label>
            {/* 预计工时。**只有真实空间问它** —— 示例空间没有排期算法,问了也
                没有东西会用它,而一个填了却没有下文的输入框是在骗人。
                放在这里而不是"建完再说",是因为排期读的正是这个字段:一个没有
                工时的任务排不进任何一天。

                **信息主题根本不问。** 它不是"暂时留空",是这个问题对那一类节点
                不成立 —— 给一个不进日历的东西问"要做多久",用户会开始怀疑
                自己刚才选的是什么。 */}
            {isRealSpace && CREATE_KINDS[createKind].purpose === 'planning' && (
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
            {/* 超限时**按钮就按不下去**,而不是发出去等一个 400 —— 那一下的观感是
                "点了没反应",而原因写在上面那行计数里。 */}
            <button className="primary-button" disabled={!title.trim() || submitting || planSaving || createDescriptionOver}>
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
            updateNode(detailNode.id, { title: detailTitle.trim() || detailNode.title }); closeDetailEditor(); }}>
            <label>节点名称<input autoFocus value={detailTitle} maxLength={80} onChange={(event) => patchDraft({ detailTitle: event.target.value })} /></label>
            {detailNode.planningLevel && (
              <p className="node-level-detail">规划层级:{planningLevelLabel(detailNode.planningLevel)}</p>
            )}
            {/* 正文:边打边存。下面那一行状态是**真实结果**,不是"我发过一次请求" ——
                见 `flushBody`。 */}
            <label>
              详细说明
              {/* **没有 `maxLength`**,理由与创建表单那一处同。 */}
              <textarea value={detailDescription} onChange={(event) => patchDraft({ detailDescription: event.target.value })} placeholder="记录这个节点的目标、约束、判断和下一步…" />
              {/* 两种话分开说(见 `isDescriptionExempt`):受限的报数,存量已经超过
                  300 的那一类**如实说它不受限** —— 装作受限会让用户以为必须删掉自己
                  写了很久的东西,而服务端其实照收。 */}
              <small className={`body-count${detailDescriptionOver ? ' is-over' : ''}`}>
                {isDescriptionExempt(detailNode.description)
                  ? `${codePointLength(detailDescription)} 字（这份说明超过 300，不受上限约束）`
                  : detailDescriptionOver
                    ? `说明最多 300 个字（按 Unicode 码点计），这一份有 ${codePointLength(detailDescription)} 个。`
                    : `${codePointLength(detailDescription)}/${MAX_DESCRIPTION_CODEPOINTS}`}
              </small>
            </label>
            {isRealSpace && (
              /* 这一段是**铺开写的一串 `? :`**,不是一串 `&&`。顺序在这里就是语义:

                 **超限 > 保存结果 > 相不相符。** 超限不是一种保存结果,它是"这段话
                 根本发不出去"—— 而状态机里没有"太长"这一档(它不该有:它是一个
                 拒绝发出去的理由,不是一个后端回来的答复),所以它**必须**排在
                 "已保存"前面。排在后面的话,用户把一个刚存成功的说明改到 400 字,
                 屏幕上会一直挂着「正文已保存」,而那段多出来的字永远也存不进去。
                 那是这个界面最容易骗到人的一句话。

                 唯一排在它前面的是**冲突未决**:那一条带着两个按钮,说的是"你手上
                 这份和库里那份不一样,你选一个",而"太长了"只是"现在发不出去"。 */
              <p className={`body-save-note is-${detailDescriptionOver ? 'failed' : bodyNote.kind}`} role="status">
                {bodyNote.kind === 'conflict' ? (
                  <>
                    <b>这段正文在别处被改过了,所以这次没有写进去。</b>
                    {bodyNote.serverBody
                      ? <>库里现在是:「{bodyNote.serverBody}」</>
                      : <>库里那份这一步没读到 —— 可以先用你这份覆盖,或者关掉重开再看一眼。</>}
                  </>
                ) : detailDescriptionOver
                  ? `这段说明超过了 ${MAX_DESCRIPTION_CODEPOINTS} 个字，这样存不进去。把它改短一些才会自动保存。`
                  : bodyNote.kind === 'saving' ? '正在保存正文…'
                    : bodyNote.kind === 'saved' ? `正文已保存（${bodyNote.at}）`
                      : bodyNote.kind === 'failed' ? (
                        <>
                          正文没有保存上:{bodyNote.message}
                          <button type="button" onClick={() => { setBodyNote({ kind: 'none' }); void flushBody(); }}>重试</button>
                        </>
                      )
                        : detailDescription === (detailNode.description ?? '')
                          ? '正文与库里一致。'
                          : '正文有改动,停手后会自动保存。'}
              </p>
            )}
            {isRealSpace && bodyNote.kind === 'conflict' && (
              <div className="body-conflict-actions">
                {/* **两条路都摆出来,而且都不静默。** 覆盖是用户明确选的,但它**不是**
                    "无条件写入":`flushBody` 带的是冲突那一刻从库里读回来的
                    `contentVersion`,所以哪怕点了覆盖,只要这中间又有人写过,它还是 409、
                    还是回到这一屏(有用例专门钉住这一下,见 `node-body.spec.ts` 的
                    「点了覆盖之后又有人改,它还是不肯写」)。放弃则是把库里那份读回编辑器。
                    哪一种都不该由我们替他挑 —— 文案也照这个说:覆盖的是**草稿**,
                    不是"我这份",它要经过同一道版本校验。 */}
                <button type="button" onClick={() => { setBodyNote({ kind: 'none' }); void flushBody(); }}>
                  用我的草稿覆盖
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
            {/* ------------------------------------------------------------------
                长正文(笔记)。**它不是这个节点的一个字段。**
                单独一张表、单独一条版本锁(见后端 `note_service` 与
                `lib/backend.ts` 的 `NotePayload`),所以它有自己的编辑器、自己的
                保存状态、自己的冲突两个按钮 —— 改一次 300 字的说明不该让两万字的
                正文保存失败,反过来也一样。
                ------------------------------------------------------------------ */}
            {isRealSpace && (
              /* `<label>` 而不是 `<div>`:那一行标题要是这个 textarea 的**可访问名字**。
                 写成兄弟节点的话,读屏软件念到输入框时只会说"多行文本框" —— 而这个
                 弹窗里有两个多行文本框,用户分不清自己在哪一个里面。 */
              <label className="note-field">
                <span className="note-field-head">
                  <span>长正文（笔记）</span>
                  <small className={`body-count${noteOver ? ' is-over' : ''}`}>
                    {codePointLength(noteBody)}/{MAX_NOTE_CODEPOINTS}
                  </small>
                </span>
                {noteLoadError ? (
                  /* **读不到就不给编辑。** 摆一个空编辑器在这儿,用户会以为自己没写过,
                     然后把他新写的存上去 —— 而服务端可能有一份,那一下就是覆盖。 */
                  <span className="note-save-note is-failed">
                    {noteLoadError}
                    <button type="button" onClick={() => setNoteReload((times) => times + 1)}>重试</button>
                  </span>
                ) : noteNodeId === detailNode.id ? (
                  <textarea
                    value={noteBody}
                    onChange={(event) => patchDraft({ noteBody: event.target.value })}
                    placeholder="资料、引文、长一点的思考都可以写在这里，最多两万字，停手就存。"
                  />
                ) : (
                  /* 归属对不上 = 这一份还在路上。这段时间**一个字都不显示** ——
                     显示上一个节点那份是这里最坏的一种错(标题已经是 B,字还是 A)。 */
                  <span className="note-save-note">正在读取这一份长正文…</span>
                )}
                {/* 顺序与上面那段说明**逐条相同**,理由也在那里写了:超限不是一种
                    保存结果,它是"这段话发不出去",所以要排在"已保存"前面 —— 否则
                    一份存过、随后又被写到超长的正文会一直挂着「长正文已保存」。
                    这里的 `role="status"` 挪到了 `<span>` 上,因为 `<label>` 里面
                    不能再嵌一块会被读屏单独念出来的区域名(`<p>` 会)。 */}
                {!noteLoadError && (
                  /* **类名与上面那段说明的不一样**(`note-save-note` 而不是
                     `body-save-note`)。同一个类名会让"这段正文的保存状态"变成一个
                     可能指两个元素的说法 —— 而既有的用例里 `.body-save-note` 指的是
                     说明那一段(见 `node-body.spec.ts`),它们会立刻变成 strict mode
                     违规。样式上两者共用一套(见 `canvas-polish.css`),所以这里区分的
                     是**语义**,不是长相。 */
                  <span
                    className={`note-save-note is-${noteOver ? 'failed' : noteNote.kind}`}
                    role="status"
                  >
                    {noteNote.kind === 'conflict' ? (
                      <>
                        <b>这份长正文在别处被改过了,所以这次没有写进去。</b>
                        {noteNote.serverBody
                          ? <>库里现在是:「{noteNote.serverBody}」</>
                          : <>库里那份这一步没读到 —— 可以先用你这份覆盖,或者关掉重开再看一眼。</>}
                      </>
                    ) : noteOver
                      ? `超过 ${MAX_NOTE_CODEPOINTS} 个字了，这样存不进去。把这一份改短一些才会自动保存。`
                      : noteNote.kind === 'saving' ? '正在保存长正文…'
                        : noteNote.kind === 'saved' ? `长正文已保存（${noteNote.at}）`
                          : noteNote.kind === 'failed' ? (
                            <>
                              长正文没有保存上:{noteNote.message}
                              <button type="button" onClick={() => { setNoteNote({ kind: 'none' }); void flushNote(); }}>重试</button>
                            </>
                          )
                            : noteBody === noteServerBody.current
                              ? '长正文与库里一致。'
                              : '长正文有改动,停手后会自动保存。'}
                  </span>
                )}
                {!noteLoadError && noteNote.kind === 'conflict' && (
                  <span className="body-conflict-actions">
                    {/* 与正文那一组同形同义:覆盖带的也是**冲突那一刻**从库里读回来的
                        版本号,所以它不是"无条件写入";放弃则是把库里那份读回编辑器。
                        读回来的同时要把 `noteServerBody` 一起更新 —— 不更新的话,
                        下面那个自动保存效果会认为"与库里不一致",转头又存一遍。 */}
                    <button type="button" onClick={() => { setNoteNote({ kind: 'none' }); void flushNote(); }}>
                      用我的草稿覆盖
                    </button>
                    {Boolean(noteNote.serverBody) && (
                      <button
                        type="button"
                        onClick={() => {
                          patchDraft({ noteBody: noteNote.serverBody });
                          noteServerBody.current = noteNote.serverBody;
                          setNoteNote({ kind: 'none' });
                        }}
                      >
                        放弃我的改动,载入库里那份
                      </button>
                    )}
                  </span>
                )}
              </label>
            )}
            <p>{isRealSpace ? '正文与长正文会在停手后自动保存；下面的按钮只保存节点名称。进入子路径请用节点右上角的箭头。' : '在这里保存的说明会保留在当前成长空间。'}</p>
            {isRealSpace && planError && <p className="form-error" role="alert">{planError}</p>}
            {/* 详情里**也要**有一个归档入口(§9.1.1:「节点详情中保留可发现的『更多/归档』入口」)。
                它的必要性不在"多一个入口",而在**右键菜单在触屏上不存在** —— 节点上那个
                「更多」按钮是触屏的入口,而这个按钮在**弹窗里**,弹窗打开时节点已经被盖住了。
                要把一个节点收起来的人,正看着它的资料时应该就能做到,不用先关掉弹窗。 */}
            <div className="node-form-actions">
              <button className="primary-button" disabled={!detailTitle.trim() || planSaving}>{planSaving ? '保存中…' : '保存节点'}</button>
              {isRealSpace && (
                <>
                  <button
                    type="button"
                    className="danger-button"
                    disabled={detailNode.id === growth.goalId}
                    title={detailNode.id === growth.goalId ? '根目标不能归档' : '收起来,以后可以在「归档」里恢复'}
                    onClick={() => { closeDetailEditor(); askArchive(detailNode.id); }}
                  >
                    <Trash2 size={13} />
                    归档
                  </button>
                  {/*
                    原因要**印出来**,不能只写在那句 `title` 上。
                    这里是个原生 `disabled` 按钮,而原生禁用的按钮**不触发鼠标事件、
                    也没法聚焦** —— 于是那句 title 在 Chrome 上根本弹不出来,屏幕阅读器
                    也读不到。结果就是"一个灰掉的按钮,用户没有任何办法知道为什么",
                    和菜单里那条禁用项要写 `disabledReason` 是同一件事。
                  */}
                  {detailNode.id === growth.goalId && (
                    <small className="field-hint">根目标不能归档</small>
                  )}
                </>
              )}
            </div>
          </form>
          {/* AI 分析。**在表单外面** —— 它不是这个节点的一个字段,提交那个按钮
              跟它没有关系;混进 `<form>` 里会让"保存节点"的含义变得含糊。
              `refreshToken` 换一个值就重读一次:正文保存成功时徽标必须当场变,
              而"变没变"是服务端现算的,不能让本地猜。 */}
          {/* 两个保存成功都要让面板重读一次:说明与长正文**都是**分析的输入
              (笔记那一条还进了 `input_snapshot`,见后端 `services/input_snapshot.py`),
              所以任何一边刚存上,那个"分析是不是过时了"的徽标都可能已经变了。 */}
          <NodeAnalysisPanel
            nodeId={detailNode.id}
            refreshToken={bodyNote.kind === 'saved' ? bodyNote.at
              : noteNote.kind === 'saved' ? noteNote.at : ''}
          />
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
