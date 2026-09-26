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
}
export type Task = GrowthNode & { type: 'task' };
export type Milestone = GrowthNode & { type: 'milestone' };
export interface GrowthEdge { id: string; source: string; target: string; type: 'dependency' | 'support' }
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
