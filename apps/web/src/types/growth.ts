import type { ResearchView } from '@/lib/backend';

export type Category = 'academic' | 'research' | 'experience' | 'personal';

/**
 * 一场排期 —— "哪天做多久"。**一个任务可以有很多场,而计划里始终只有一个节点。**
 *
 * 只为后端的真实场次存在。示例空间里没有这东西:那份演示数据里的
 * `startDate`/`endDate` 是"排出来的时段"的另一种画法,两者不会同时出现在一个
 * 节点上(`sessions` 有值时,`startDate`/`endDate` 就是从它算出来的)。
 */
export interface GrowthSession {
  id: string;
  date: string;
  plannedMinutes: number;
  /** 缓冲。它**计入当天占用** —— 界面上说"这天占了多少"必须把它算进去。 */
  bufferMinutes: number;
  seq: number;
  status: 'planned' | 'in_progress' | 'done' | 'skipped' | 'moved' | 'canceled';
  locked: boolean;
  startMinute: number | null;
}

export interface GrowthNode {
  id: string;
  title: string;
  description?: string;
  type: 'goal' | 'capability' | 'stage' | 'task' | 'milestone';
  /**
   * 用途轴。**与 `type` 正交**,不是它的第六个取值。
   *
   * `information` 是"主题 / 方向"节点:用户记下的一个情况,不占日历 —— 不显示完成
   * 勾选框、不进任务视图、不计入完成度(后端已经把它排除出 `totalNodes` /
   * `completedNodes`),也不能作为前置依赖的端点。
   *
   * 缺省当 `planning` 读:老数据与还没重取的载荷都没有这个字段。
   */
  purpose?: 'planning' | 'information';
  /**
   * 规划层级(`strategy` / `phase` / `month` / `week` / `day`)。
   *
   * 缺省表示"没指定" —— 存量节点、以及不需要层级的节点都是它。它只表达语义层级,
   * **不代表已排期**。没有值时界面不显示任何层级标签,避免凭空造出语义。
   */
  planningLevel?: 'strategy' | 'phase' | 'month' | 'week' | 'day';
  /**
   * 节点由谁创建。它不改变节点属于哪个 NodeSpace，只决定画布是否自动画出
   * 父子结构线：AI 生成的规划沿树生长，用户手动放下的节点先保持独立，等用户
   * 自己建立关系。
   */
  origin?: 'user' | 'ai';
  parentId?: string;
  category?: Category;
  /** 它所属的阶段(最近的 stage 祖先,没有就是根目标)。任务视图按它分组。 */
  stageId?: string;
  status: 'pending' | 'doing' | 'completed';
  priority: 'low' | 'medium' | 'high';
  /**
   * 时间线上这个节点占的区间。
   *
   * 两种来源,投影层保证只有一种会生效:
   * - 有 `sessions` 时,是这些场次的最早/最晚一天(**真实排出来的**)。
   * - 没有场次时,是 `deadline` 映出来的一个**点**(见下面 `deadline`)。
   */
  startDate?: string;
  endDate?: string;
  /** 最近一场还没做完的安排落在哪天。任务视图的"今天/本周"按它筛。 */
  scheduledDate?: string;
  /**
   * 截止日 —— 用户或模型定下的**意图**,不是排出来的安排。
   *
   * 它与 `sessions` 是两件事,不能互相顶替:截止日是"我要在这天之前做完",
   * 场次是"这几天做多久"。一个节点可以两者都有(排完期之后),
   * 也可以只有截止日(还没排期)。界面靠这个区分才能说出"截止 10.23"
   * 而不是一个光秃秃的 "10.23"(后者读起来像"这一天要做这件事")。
   */
  deadline?: string;
  /** 真实场次。为空/缺失表示这个节点还没排过期 —— 与"截止日已过"是两回事。 */
  sessions?: GrowthSession[];
  /**
   * 预计工时。**两个单位都留着,而且不是冗余。**
   *
   * `estimatedHours` 是给人看的("约 1.5 小时"),四舍五入过;`estimateMinutes` 是
   * 原样的那个数。真正的写入与排期用后者 —— 只用四舍五入过的那一份会引入漂移:
   * 填 100 分钟 → 投影成 1.7 小时 → 打开编辑器再存一次(什么都没改)就写回 102 分钟。
   */
  estimatedHours?: number;
  estimateMinutes?: number;
  timelineLevel?: 'major' | 'task' | 'action';
  /**
   * 正文的版本号(后端 `plan_nodes.content_version`),保存正文时原样带回做乐观锁。
   *
   * 可选是因为**它不该挡住保存**:投影拿不到它就说明这份格式不是真实的计划节点,
   * 而"某条路径上缺了一个号"的正确表现是照旧能存,不是把用户的正文扣下。
   *
   * 它必须跟着 `description` 一起走:用户在编辑器里改完正文按下保存时,要带的正是
   * "我读到的**那一版**" —— 而不是"现在最新的一版"。拿后者去比,锁就形同虚设。
   */
  contentVersion?: number;
}
export type Task = GrowthNode & { type: 'task' };
export type Milestone = GrowthNode & { type: 'milestone' };
/**
 * 画布上的一条边。**三种关系共用这一个形状** —— 它对应后端的 `RelationPayload`,
 * 而不是某一张表。
 *
 * `depends_on` 存在 `dependencies` 表里(它参与排期),`related_to` 与 `influences`
 * 存在 `node_relations` 表里。这个区分**不往上传**:画布只需要"有哪些线、连的是谁、
 * 什么类型、写了什么说明"。要判断"这条边会不会改变排期"的地方,判的是 `type`。
 */
export type GrowthRelationType = 'depends_on' | 'related_to' | 'influences';

export interface GrowthEdge {
  id: string;
  /**
   * 方向是 `source -> target`。对 `depends_on` 来说就是**前置 -> 后续**,与后端
   * `DependencyPayload.predecessor_id -> successor_id` 同向 —— 全仓只有这一个方向
   * 约定,前端不做反转(见 `planProjection.ts`)。
   */
  source: string;
  target: string;
  type: GrowthRelationType;
  /**
   * 用户写在这条边上的说明。
   *
   * `depends_on` 恒为 `undefined` —— `dependencies` 表没有说明列,后端会在带说明
   * 创建前置关系时直接拒绝(而不是把那句话丢掉)。所以界面上"给前置关系写说明"
   * 应当是不可用的,不是一个存不进去的输入框。
   */
  note?: string;
  /** 只有 `depends_on` 有:前置完成后还要等几天。 */
  lagDays?: number;
}
export interface GrowthState {
  id: string;
  title: string;
  goalId: string;
  currentStageId: string;
  nodes: Record<string, GrowthNode>;
  edges: GrowthEdge[];
}
export type PlanAction =
  | { type: 'CREATE_NODE'; node: GrowthNode }
  | { type: 'DELETE_NODE'; nodeId: string }
  | { type: 'UPDATE_STATUS'; nodeId: string; status: GrowthNode['status'] }
  // `deadline`(截止日)和 `estimateMinutes`(预计工时)是真节点上真实的字段;
  // `startDate`/`endDate` 是示例数据里的排出来的时段。并存不是冗余 —— 两种空间编辑
  // 的字段不同,而这条 action 是共用入口,provider 按空间类型决定把哪些字段发出去。
  //
  // 两个字段都收 `null` 是为了表达"清掉它"(后端就是这么表示的),reducer 会把
  // `null` 归一成 `undefined` —— 不归的话 `null` 会顺着 `...patch` 渗进视图模型,
  // 而所有 `node.x && ...` 的判断仍然"碰巧"是对的,只在类型检查器里报错。
  //
  // 单位:后端的 `estimate_minutes` 是**分钟**,示例数据的 `estimatedHours` 是**小时**。
  // 两个名字都留在这里,是因为它们真的不是同一个字段,换算是调用方的事。
  | { type: 'UPDATE_NODE'; nodeId: string; patch: Partial<Pick<GrowthNode, 'title' | 'description' | 'priority' | 'startDate' | 'endDate' | 'status' | 'estimatedHours'>> & { deadline?: string | null; estimateMinutes?: number | null } }
  | { type: 'UPDATE_TIME'; nodeId: string; startDate: string; endDate: string }
  | { type: 'UPDATE_PLAN_META'; title: string; description: string };
export interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  contextId?: string;
  proposalId?: string;
  /**
   * 这条回复是谁生成的(`direct_llm` / `rule_fallback` / …)。
   *
   * 历史消息也带这个字段 —— 用户往上翻的时候,应该看得出哪几句是模型不可用那段时间
   * 留下的。只有最新一条带来源、往上翻就看不出来,等于把那段经历藏起来了。
   */
  source?: string;
  degraded?: boolean;
  degradedReason?: string | null;
  /**
   * 这条回复依据的**服务端验证过的**公开来源。
   *
   * 模型无法写入它 —— 来源来自真实的 `research_public` 工具执行。界面据此
   * 渲染可点击的引用;`research` 不存在表示这一轮没查过。
   */
  research?: ResearchView;
  /** 用户消息已发出、助手还没回。本地占位,不落库。 */
  pending?: boolean;
  /** 这一轮失败了。界面显示错误与重试,**不显示一句编出来的 AI 回复**。 */
  failed?: boolean;
}
export interface Conversation { id: string; title: string; linkedNodeIds: string[]; messages: Message[]; tags?: string[] }
export interface User { id: string; name: string; major: string; year: string; rank: number; targetYear: number }
export interface JournalEntry { id: string; content: string; date: string; linkedNodeIds: string[]; tags?: string[] }
export interface FileAsset { id: string; ownerId: string; name: string; size: number; mime: string; url: string; file: File }
export interface AISettings { mode: string; frequency: string; proactive: boolean; adjust: boolean; critique: boolean; rest: boolean }
export interface Proposal { id: string; nodeId: string; status: 'pending' | 'accepted' | 'outdated'; originalStart: string; actions: PlanAction[]; remote?: boolean }
