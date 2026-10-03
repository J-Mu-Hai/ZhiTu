/**
 * 后端接口的类型与调用。
 *
 * 字段名与后端契约逐字对应(后端用 `alias_generator=to_camel`,所以线上就是 camelCase)。
 * 后端的请求模型是 `extra="forbid"`:**多传一个字段就是 422**。所以这些类型不是
 * 注释,是硬边界 —— 前端对象不能整块丢进请求体,必须挑字段。
 */

import { API_BASE, ApiError, apiFetch, getToken } from './api';

// ---------------------------------------------------------------------------------
// 身份
// ---------------------------------------------------------------------------------

export interface UserProfile {
  id: string;
  email: string;
  displayName: string;
  timezone: string;
  school: string | null;
  major: string | null;
  year: string | null;
  rank: number | null;
  targetYear: number | null;
  targetGoal: string | null;
  bio: string | null;
  createdAt: string;
}

export interface AuthResult {
  token: string;
  expiresAt: string;
  absoluteExpiresAt: string;
  user: UserProfile;
}

export interface RegisterPayload {
  email: string;
  password: string;
  displayName: string;
  timezone: string;
}

/** 浏览器所在时区。取不到就退回东八区 —— 与后端默认值一致。 */
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
  } catch {
    return 'Asia/Shanghai';
  }
}

export function register(payload: RegisterPayload): Promise<AuthResult> {
  return apiFetch<AuthResult>('/api/auth/register', { method: 'POST', body: payload });
}

export function login(email: string, password: string): Promise<AuthResult> {
  // 跳过全局 401 处理:这里 401 的含义是"密码不对",不是"登录状态过期了"。
  // 让它去清令牌会把另一个标签页里正常的会话也踢掉。
  return apiFetch<AuthResult>('/api/auth/login', {
    method: 'POST',
    body: { email, password },
    skipUnauthorizedHandler: true,
  });
}

export function logout(): Promise<void> {
  // 令牌由 apiFetch 从存储里自动带上,不用传参。
  // 后端**没有 Authorization 也返回 204**,所以"令牌早就失效了还想登出"不会变成报错 ——
  // 要登出的对象已经登出了,目标已经达成。
  return apiFetch<void>('/api/auth/logout', {
    method: 'POST',
    skipUnauthorizedHandler: true,
  });
}

export function fetchMe(): Promise<UserProfile> {
  return apiFetch<UserProfile>('/api/users/me');
}

export type ProfilePatch = Partial<
  Pick<UserProfile, 'displayName' | 'school' | 'major' | 'year' | 'rank' | 'targetYear' | 'targetGoal' | 'bio'>
>;

export function updateMe(patch: ProfilePatch): Promise<UserProfile> {
  return apiFetch<UserProfile>('/api/users/me', { method: 'PATCH', body: patch });
}

// ---------------------------------------------------------------------------------
// 成长空间
// ---------------------------------------------------------------------------------

export interface WorkspaceSummary {
  id: string;
  title: string;
  intent: string;
  createdAt: string;
}

export interface WorkspaceCounts {
  nodes: number;
  conversations: number;
  proposals: number;
  scheduledSessions: number;
}

export interface WorkspaceDetail extends WorkspaceSummary {
  status: 'active' | 'archived';
  timezone: string;
  currentRevisionVersion: number;
  archivedAt: string | null;
  updatedAt: string;
  counts: WorkspaceCounts;
}

export interface RootNode {
  id: string;
  title: string;
  nodeType: string;
  status: string;
  depth: number;
  deadline: string | null;
}

export interface WorkspaceCreated {
  workspace: WorkspaceDetail;
  rootNode: RootNode;
}

export function listWorkspaces(includeArchived = false): Promise<WorkspaceSummary[]> {
  return apiFetch<WorkspaceSummary[]>(
    `/api/workspaces?includeArchived=${includeArchived ? 'true' : 'false'}`,
  );
}

export function createWorkspace(payload: { title: string; intent?: string; goal?: string }) {
  return apiFetch<WorkspaceCreated>('/api/workspaces', { method: 'POST', body: payload });
}

export function getWorkspace(id: string): Promise<WorkspaceDetail> {
  return apiFetch<WorkspaceDetail>(`/api/workspaces/${id}`);
}

export function updateWorkspace(
  id: string,
  patch: { title?: string; intent?: string; status?: 'active' | 'archived' },
): Promise<WorkspaceDetail> {
  return apiFetch<WorkspaceDetail>(`/api/workspaces/${id}`, { method: 'PATCH', body: patch });
}

// ---------------------------------------------------------------------------------
// 对话
// ---------------------------------------------------------------------------------

export type DegradedReason =
  | 'OPENJIUWEN_NOT_INSTALLED'
  | 'NO_API_KEY'
  | 'MODEL_TIMEOUT'
  | 'MODEL_OUTPUT_INVALID'
  | 'MODEL_AUTH_FAILED'
  | 'MODEL_RATE_LIMITED'
  | 'MODEL_UNAVAILABLE'
  | 'CIRCUIT_OPEN';

/**
 * 这一轮回复是谁生成的。
 *
 * `'scripted'` **是测试脚手架**,不是产品能力:只有 `AGENT_REASONER=script`
 * (要靠显式设的 `ZHITU_SCRIPTED_ACTIONS`)会产生它,产品里没有任何一条路径能落到
 * 这个值上。它在这里是为了让徽标**如实说**"脚本",而不是借用 `'direct_llm'`
 * 把一次脚本演示说成模型生成的。见后端 `db/models/enums.py::ModelSource`。
 */
export type ModelSource =
  | 'openjiuwen'
  | 'direct_llm'
  | 'rule_fallback'
  | 'unavailable'
  | 'scripted';

/**
 * 一次公开研究的结果。**闭集**,与后端 `ResearchView.status` 逐字对应。
 *
 * `null` 只为兼容更早的历史行,不是一种"结果"。
 */
export type ResearchStatus =
  | 'success'
  | 'cached'
  | 'unavailable'
  | 'blocked'
  | 'limited'
  | 'timeout'
  | 'failed';

/**
 * 一条**服务端验证过**的公开来源。
 *
 * 它不由模型生成:`sourceId` / `domain` 由服务端从 URL 派生,其余来自真实
 * provider 返回。所以模型无法凭空编出一条看起来像真的引用。
 */
export interface ResearchCitationView {
  /** 服务端从 URL 派生的稳定标识,同一来源在不同轮次里 id 相同。 */
  sourceId: string;
  title: string;
  url: string;
  domain: string;
  /** 提供方返回的摘录。可能为空 —— 它不影响这条来源是不是真的。 */
  excerpt: string | null;
  /** 发布日期。当前 provider 的 basic 深度不返回,通常为 null。 */
  publishedAt: string | null;
  /** 服务端抓到这条来源的时刻(ISO 字符串)。 */
  accessedAt: string;
  provider: 'tavily';
}

/**
 * 一条助手回复所依据的公开研究。
 *
 * `status` 回答"这一轮公开研究到底发生了什么",界面据此决定显示引用列表、
 * 还是如实的失败说明 —— **不能把"没查到"显示成"查过了"**。
 */
export interface ResearchView {
  status: ResearchStatus | null;
  /** 本轮是否尝试过公开研究(成功、缓存、限额、隐私拦截、失败都算)。 */
  consulted: boolean;
  /** 只有 `success` / `cached` 才非空。**不含搜索关键词。** */
  citations: ResearchCitationView[];
}

export interface MessageView {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  seq: number;
  createdAt: string;
  contextNodeId: string | null;
  proposalId: string | null;
  modelSource: ModelSource | null;
  degraded: boolean;
  degradedReason: DegradedReason | null;
  /** 这条回复依据的公开来源。null = 这一轮没查过公开研究。 */
  research: ResearchView | null;
}

export interface BriefView {
  version: number;
  goal: string | null;
  deadline: string | null;
  weeklyAvailableMinutes: number | null;
  currentLevel: string | null;
  successCriteria: string | null;
  constraints: string[];
  /** 还缺哪些规划条件。由**服务端**算,不是前端猜的。 */
  missing: string[];
}

export interface ConversationView {
  id: string | null;
  /** 最近的一批消息，按时间正序。更早的那些由 `truncated` 说明。 */
  messages: MessageView[];
  brief: BriefView;
  /** 更早的消息没有一起返回。为真时界面要说出来，见 ConversationPanel。 */
  truncated: boolean;
}

export interface SendMessageResponse {
  userMessage: MessageView;
  assistantMessage: MessageView;
  reply: string;
  source: ModelSource;
  degraded: boolean;
  degradedReason: DegradedReason | null;
  retryable: boolean;
  promptVersion: string;
  modelName: string | null;
  latencyMs: number | null;
  /** 与 `assistantMessage.research` 同一份。 */
  research: ResearchView | null;
  brief: BriefView;
  changedFields: string[];
  replayed: boolean;

  /**
   * 这一轮 AI 提的变更。**需要用户点"确认"才会写进计划。**
   *
   * `null` 有两种含义,靠 `proposalErrors` 区分:
   *   - `null` + 错误为空:模型这一轮没提变更(纯聊天或提问)。
   *   - `null` + 有错误:它提了,但被校验挡下了。界面必须如实说出来。
   * 把两者都显示成"什么都没有"的话,用户会以为 AI 没听懂,而其实它听懂了、
   * 只是提的东西不合法。
   */
  proposal: ProposalView | null;
  /** 校验失败的原因。空数组表示这次没有被拒绝的东西。 */
  proposalErrors: { code: string; message: string; ordinal?: number }[];

  /**
   * **模型回答期间用户改了输入。**
   *
   * 为真时上面那段回复和那份分析都建立在一份已经过去的输入上。提案不会出现
   * (那些动作逐条落在 `proposalErrors` 里,码是 `INPUT_CHANGED`)。
   *
   * 界面必须拿它给下一步指引 —— 只说"这次没有提案"的话,用户会以为 AI 什么都没
   * 想出来,而真正该做的是点一下「根据最新内容重新分析」。
   */
  inputChanged: boolean;
}

export function getConversation(workspaceId: string): Promise<ConversationView> {
  return apiFetch<ConversationView>(`/api/workspaces/${workspaceId}/messages`);
}

export function sendMessage(
  workspaceId: string,
  payload: {
    content: string;
    clientMessageId?: string;
    contextNodeId?: string | null;
    currentView?: string | null;
    /**
     * 这次对话的作用范围起点 —— 用户在哪个子空间里。
     *
     * **与 `contextNodeId` 不是一回事**:那个是"我在说哪个节点",这个说的是
     * "这一轮允许改哪一片"。不传就是整个空间(旧行为)。传了一个后端认不出来的
     * 节点会得到 400,而不是被悄悄放宽成整个空间 —— 宁可报错也不越过用户划的线。
     */
    scopeRootId?: string | null;
  },
): Promise<SendMessageResponse> {
  return apiFetch<SendMessageResponse>(`/api/workspaces/${workspaceId}/messages`, {
    method: 'POST',
    body: payload,
  });
}

// ---------------------------------------------------------------------------------
// 问题节点
// ---------------------------------------------------------------------------------

/** 用户可以用什么方式回答一个问题。选项是加速器,不是限制。 */
export type QuestionResponseMode = 'single_select' | 'multi_select' | 'free_text' | 'mixed';

/**
 * 问题节点的生命周期。
 *
 * `answered` / `investigating` 都是“已处理但还没结束”的状态:前者是刚答完,
 * 后者是后续那一轮对话正在处理。模型失败时停在 `investigating` —— 答案与状态都已
 * 落库,刷新后读得回,不会丢。
 */
export type QuestionStatus =
  | 'pending'
  | 'answered'
  | 'investigating'
  | 'resolved'
  | 'archived';

export interface QuestionOption {
  id: string;
  label: string;
  /** 阶段 10:AI 的推荐项。前端据此明确标“推荐”。 */
  recommended: boolean;
}

export interface QuestionAnswer {
  selectedOptionIds: string[];
  customInput: string | null;
}

/**
 * 一个 AI 提出的问题。
 *
 * **它不是计划节点** —— 不进排期、不能当依赖、不计入任务统计。回答它也不等于
 * 同意任何计划变更:回答之后模型提的变更仍然是一份要用户点“确认,写入计划”的提案。
 */
export interface QuestionView {
  id: string;
  workspaceId: string;
  /** 从哪个节点聊出来的。节点归档后仍是原 id，但前端不应再渲染成可跳转的引用。 */
  sourceNodeId: string | null;
  sourceMessageId: string | null;
  /** 回答之后服务端靠它定位要重评的推理节点。 */
  reasoningNodeId: string | null;
  /**
   * 阶段 11:呈现方式。`conversation_intake` 只在对话区(橙色),**不投影成画布节点**;
   * `canvas_question` 才在画布上。
   */
  presentation: 'conversation_intake' | 'canvas_question';
  question: string;
  whyNow: string;
  /**
   * 阶段 10:提问前的**战略判断**。都是可审阅的结论,不是隐藏思维链。
   * 旧行 / 无法可信判断时为空字符串 —— 界面会显示“当前还不足以给出推荐”。
   */
  analysisSummary: string;
  recommendation: string;
  decisionImpact: string;
  /** 可选:哪些是假设、还需确认。 */
  confidenceNote: string | null;
  responseMode: QuestionResponseMode;
  options: QuestionOption[];
  allowCustomInput: boolean;
  status: QuestionStatus;
  answer: QuestionAnswer | null;
  /** 规划智能体重构 V1(P2):固定问题键(current_state / main_line / …)。null = 非 V1。 */
  v1Key: string | null;
  /** 模型对该问题的可审阅判断。null = 还没有判断。 */
  v1Analysis: V1NodeAnalysis | null;
  createdAt: string;
  updatedAt: string;
  answeredAt: string | null;
}

export interface QuestionListResponse {
  questions: QuestionView[];
  truncated: boolean;
}

export interface AnswerQuestionResponse {
  question: QuestionView;
  /** 回答之后那一轮对话的结果。重复提交(幂等)时为 `null`。 */
  turn: SendMessageResponse | null;
  replayed: boolean;
}

export interface QuestionActionResponse {
  question: QuestionView;
  replayed: boolean;
}

export function listQuestions(
  workspaceId: string,
  options: { includeDecided?: boolean } = {},
): Promise<QuestionListResponse> {
  const suffix = options.includeDecided ? '?includeDecided=true' : '';
  return apiFetch<QuestionListResponse>(`/api/workspaces/${workspaceId}/questions${suffix}`);
}

export function answerQuestion(
  workspaceId: string,
  questionId: string,
  payload: { selectedOptionIds: string[]; customInput?: string | null; clientAnswerId: string },
): Promise<AnswerQuestionResponse> {
  return apiFetch<AnswerQuestionResponse>(
    `/api/workspaces/${workspaceId}/questions/${questionId}/answer`,
    { method: 'POST', body: payload },
  );
}

function questionAction(
  workspaceId: string,
  questionId: string,
  action: 'skip' | 'later',
  clientActionId: string,
): Promise<QuestionActionResponse> {
  return apiFetch<QuestionActionResponse>(
    `/api/workspaces/${workspaceId}/questions/${questionId}/${action}`,
    { method: 'POST', body: { clientActionId } },
  );
}

export function skipQuestion(
  workspaceId: string,
  questionId: string,
  clientActionId: string,
): Promise<QuestionActionResponse> {
  return questionAction(workspaceId, questionId, 'skip', clientActionId);
}

export function deferQuestion(
  workspaceId: string,
  questionId: string,
  clientActionId: string,
): Promise<QuestionActionResponse> {
  return questionAction(workspaceId, questionId, 'later', clientActionId);
}

// ---------------------------------------------------------------------------------
// AI 分析
// ---------------------------------------------------------------------------------

/**
 * 一条分析的新鲜度。**由后端每次读的时候现算**,不是存在列里的标记。
 *
 * 只有两档。规范里提到的第三档"需要重新分析"不是另一种数据处境,它说的是
 * **过期之后该做什么** —— 界面在 `stale` 上显示「根据最新内容重新分析」就够了,
 * 不需要第三种枚举值。
 */
export type AnalysisFreshness = 'fresh' | 'stale';

/**
 * AI 对你写的内容做出的判断。
 *
 * **这不是用户说的话,也不是计划。** 它单独存一张表,是为了让"模型的一句猜测
 * 悄悄变成计划的前提"这件事没有路径可走 —— 这里的每一栏都只是判断,要变成
 * 计划必须走提案与确认。
 *
 * 七栏 + 可信度那句话。空数组表示模型这一栏没给内容,不表示"没有这一栏"。
 */
export interface AnalysisView {
  id: string;
  workspaceId: string;
  scopeRootId: string | null;
  focusNodeId: string | null;
  /**
   * 范围和焦点**现在**叫什么。节点被删掉之后退回分析当时记下的名字 ——
   * 后者是为了不让一条分析变成"关于(已删除)的分析"。
   */
  scopeRootTitle: string | null;
  focusNodeTitle: string | null;
  promptVersion: string | null;
  modelSource: ModelSource | null;
  createdAt: string;

  freshness: AnalysisFreshness;
  /**
   * 具体变了什么。每一条都是给人看的一句话。**空数组 + `stale` 不会同时出现**;
   * 但两档都会带上具体理由地说话,所以界面直接逐条显示即可,不必自己造句。
   *
   * 这一栏是"用户陈述"和"模型判断"之外的东西:它说的是**这份判断还成不成立**。
   */
  staleReasons: string[];
  /**
   * 这次分析只读到了范围的一部分时的说明。**与新鲜度无关** ——
   * 它说的是"这份判断覆盖多大范围",不是"它过期了"。
   */
  coverageNote: string | null;

  /** 用户陈述过的事实 */
  known: string[];
  /** 还缺什么 —— 模型接着要问的就是这些 */
  unknowns: string[];
  /** 依据 */
  evidence: string[];
  /** 模型自己的假设。**和"用户说的"必须分开显示** */
  assumptions: string[];
  /** 诊断 */
  diagnosis: string[];
  /** 可选的走法 */
  strategyOptions: string[];
  /** 风险。可以是"会排不开",但**绝不是"我已经调整了日程"** */
  risks: string[];

  /** 模型自己写的一句话可信度。不是算出来的分数 —— 一个 0.8 会被当成能比较的量。 */
  confidenceNote: string | null;
  /**
   * 这次判断的**正文**。七栏是索引,这里是内容 —— 一段完整的推理塞不进七栏各自
   * 400 字的形状里,而模型会迁就形状:塞不进去就不写了。§2.2。
   *
   * 它渲染在**七栏之上**(见 `NodeAnalysisPanel`):读一份判断的自然顺序是先看它
   * 怎么想的,再扫它列了什么。可能是 null(这一轮没给正文,或者更早的分析),
   * **null 与空串在界面上一视同仁**:都表示"这一条没有正文可显示"。
   */
  narrative: string | null;
}

export interface AnalysisListResponse {
  analyses: AnalysisView[];
  focusNodeId: string | null;
  scopeRootId: string | null;
  /** 这次返回为什么是这些。**空列表时界面必须用它说清是"没有"还是"没查到"。** */
  note: string;
}

/**
 * 某个节点(或整个空间)的分析记录,新的在前。
 *
 * `focusNodeId` 不给就是整个空间。**它不做"这个节点及其后代"的展开** ——
 * 那会把一条关于祖辈的分析混进子节点的列表。
 */
export function getAnalyses(
  workspaceId: string,
  params: { focusNodeId?: string; scopeRootId?: string; limit?: number } = {},
): Promise<AnalysisListResponse> {
  const parts: string[] = [];
  if (params.focusNodeId) parts.push(`focusNodeId=${params.focusNodeId}`);
  if (params.scopeRootId) parts.push(`scopeRootId=${params.scopeRootId}`);
  if (params.limit) parts.push(`limit=${params.limit}`);
  const suffix = parts.length ? `?${parts.join('&')}` : '';
  return apiFetch<AnalysisListResponse>(
    `/api/workspaces/${workspaceId}/analyses${suffix}`,
  );
}

/**
 * 「根据最新内容重新分析」。
 *
 * **返回的是整整一轮对话**(和 `sendMessage` 同一个形状),因为服务端走的就是同一条
 * 工作流:它会替用户说一句"根据最新内容重新分析一下这个节点",并把用户消息与助手回复
 * 都落进对话。所以调用方必须把这两条消息也放进对话里 —— 只把分析块刷新一下的话,
 * 对话与画布就成了两个真相。
 *
 * 那一句话**不在这里拼**:它是产品文案,写在服务层(`analysis_service.REANALYZE_MESSAGE`),
 * 前端再写一份的话,换个客户端(或以后加个快捷键)说的就是另一句了。
 */
export function refreshAnalysis(
  workspaceId: string,
  nodeId: string,
): Promise<SendMessageResponse> {
  return apiFetch<SendMessageResponse>(
    `/api/workspaces/${workspaceId}/nodes/${nodeId}/analysis/refresh`,
    { method: 'POST' },
  );
}

// ---------------------------------------------------------------------------------
// 计划
// ---------------------------------------------------------------------------------

/**
 * 一个计划节点。**没有"开始日期/结束日期"** —— 那是排期,在 `sessions` 里(阶段 6)。
 *
 * 这里只有 `deadline`(截止时间),那是用户/模型定的意图,不是排出来的安排。
 * 界面上把它画成一个点(截止于这天),不是一个区间 —— 把截止日当区间起点渲染,
 * 等于替用户编了一个他没说过的开始时间。
 */
export interface PlanNodePayload {
  id: string;
  parentId: string | null;
  title: string;
  description: string | null;
  acceptanceCriteria: string | null;
  nodeType: 'goal' | 'capability' | 'stage' | 'task' | 'milestone';
  /**
   * 用途轴。**与 `nodeType` 正交** —— "这是什么事"和"这件事要不要占日历"是两个问题。
   *
   * `information` 的信息主题不参与排期:没有工时、没有勾选、不进完成度分母,
   * 也不能作为前置依赖的端点。理由见后端 `db/models/enums.py::NodePurpose`。
   */
  purpose: 'planning' | 'information';
  /**
   * 规划层级(`strategy` / `phase` / `month` / `week` / `day`)。
   *
   * **`null` = 没指定** —— 存量节点、以及不需要层级的节点都是它。它只表达语义
   * 层级,不代表已排期。前端不得为一个 `null` 节点显示任何层级标签。
   */
  planningLevel: 'strategy' | 'phase' | 'month' | 'week' | 'day' | null;
  status: 'pending' | 'doing' | 'completed' | 'archived';
  priority: 'low' | 'medium' | 'high';
  estimateMinutes: number | null;
  deadline: string | null;
  depth: number;
  orderIndex: number;
  origin: 'user' | 'ai';
  completedAt: string | null;
  createdAt: string;
  /**
   * 节点**正文**的乐观锁版本号。改一次正文加一。
   *
   * 它和 `PlanPayload.revisionVersion` 是**两件事**:那个是整个空间的计划版本
   * (别人勾了一个任务它就会前进),这个只说"这一条正文被人改过没有"。用空间级版本号
   * 去挡正文保存的话,"另一个标签页勾掉了一个任务"会让正在写正文的人保存失败。
   */
  contentVersion: number;
  /** 规划智能体重构 V1(P2):固定分析容器键。null = 非 V1 节点。 */
  v1Key: string | null;
  /** 模型对该容器的可审阅判断。null = 还没有判断。 */
  v1Analysis: V1NodeAnalysis | null;
}

/**
 * 规划智能体重构 V1(P2):一个固定容器的**可审阅判断**。
 *
 * 分栏本身就是来源标签:`knownFacts` 是读到的,`assumptions` 是 AI 假设的,
 * `evidence` 是带来源的。**不存隐藏思维链。**
 */
export interface V1NodeAnalysis {
  judgment: string;
  knownFacts: string[];
  assumptions: string[];
  evidence: string[];
  importanceReason: string;
  uncertainty: 'low' | 'medium' | 'high';
  status: 'unexplored' | 'discussing' | 'resolved' | 'deferred';
  impactedNodeKeys: string[];
  discussionCount: number;
}

/** 规划智能体重构 V1(P2):战略路径草案。 */
export interface V1StrategyView {
  mainLine?: string;
  parallelLine?: string;
  deferOrAvoid?: string;
  riskControl?: string;
  tradeoff?: string;
  confirmed?: boolean;
}

export interface DependencyPayload {
  id: string;
  predecessorId: string;
  successorId: string;
  depType: string;
  lagDays: number;
}

/**
 * 画布上的一条边。**三种关系共用这一个形状。**
 *
 * `depends_on` 存在 `dependencies` 表里(它决定排期),另外两种存在 `node_relations`
 * 里。那是存储层的分叉,不该漏到这里来 —— 前端要是得先知道"这条边在哪个表",
 * 那它就得跟着那张表一起改。
 *
 * 方向一律是 `sourceId → targetId`(前置 → 后续),**任何一层都不反转**。
 */
export interface RelationPayload {
  id: string;
  relationType: 'depends_on' | 'related_to' | 'influences';
  sourceId: string;
  targetId: string;
  /** 用户写在这条边上的解释。`depends_on` 恒为 null —— 那张表没有这一列。 */
  note: string | null;
  /**
   * 这条边是谁建的。`depends_on` 恒为 null(同样是没有那一列),
   * **不要在前端补一个 'user'** —— 那会让 AI 连的前置看起来像用户自己连的。
   */
  origin: 'user' | 'ai' | null;
  /** 只有 `depends_on` 有;另外两种恒为 null。 */
  lagDays: number | null;
}

export interface CreateRelationRequest {
  sourceId: string;
  targetId: string;
  /**
   * **必填,服务端不猜。** 拖一条线默认连的是「相关」,绝不擅自解释成任务前置 ——
   * 那会改变排期,而用户只是把两个东西拖到一起。
   */
  relationType: string;
  note?: string | null;
}

export interface UpdateRelationRequest {
  relationType?: string;
  /** 传 null 是"清空说明",不传是"不改说明"。两者必须分得开。 */
  note?: string | null;
}

/**
 * 一个排期场次 —— "哪天做"的**唯一**表示。
 *
 * 一个 8 小时的任务有 8 行这个,而计划里始终只有一行节点。这不是实现细节,是产品
 * 规则:把「写文献综述」为了填满日历复制成 8 个同名任务,用户要勾 8 次完成。
 *
 * `startMinute` 为空是**一等状态**,不是缺失值:用户说过"每天大概两小时",没说过
 * "我 19:00 开始"。凭空指定一个时钟时间会让他看到一份自己没同意过的时间表。
 */
export interface ScheduledSessionPayload {
  id: string;
  nodeId: string;
  workspaceId: string;
  nodeTitle: string;
  scheduledDate: string;
  plannedMinutes: number;
  /** 缓冲也计入当天占用 —— "当天总量 ≤ 上限"这条不变量靠它才复核得出来。 */
  bufferMinutes: number;
  actualMinutes: number | null;
  seq: number;
  status: 'planned' | 'in_progress' | 'done' | 'skipped' | 'moved' | 'canceled';
  locked: boolean;
  lockReason: string | null;
  origin: 'scheduler' | 'user' | 'ai';
  startMinute: number | null;
  endMinute: number | null;
  completedAt: string | null;
}

export interface PlanPayload {
  workspaceId: string;
  revisionVersion: number;
  nodes: PlanNodePayload[];
  dependencies: DependencyPayload[];
  /**
   * 画布上要画的**全部**边,三种关系合在一起。
   *
   * 与 `dependencies` 并存不是重复:`dependencies` 是**排期读的那份**,这里是
   * **画的那份**。少了这一份,前端就得自己把两种边 join 起来,而"漏了一种"这件事
   * 没有任何东西会报错 —— 画布上只是少了几条线。
   */
  relations: RelationPayload[];
  brief: BriefView;
  /**
   * 排期场次。**已取消与已搬走的场次不在这里** —— 它们是墓碑,仍在库里,复盘时
   * 查得到,但"我的计划是什么"这个问题里不该出现一个已经不存在的安排。
   *
   * 空数组的含义是"还没排过期",不是"没有安排" —— 排期之前每个节点仍可能带着
   * 截止时间(那是意图,不是安排)。界面必须把两者分开说。
   */
  sessions: ScheduledSessionPayload[];
  totalNodes: number;
  completedNodes: number;
}

export interface NodeEditResult {
  node: PlanNodePayload;
  revisionVersion: number;
  deletedCount: number;
  removedDependencies: number;
  /**
   * 这一次删除**能不能拿回来**。
   *
   * 默认的 `DELETE /nodes/{id}` 是**归档**(可恢复),`?mode=delete` 才是彻底删除。
   * 界面必须按它说话:对一次彻底删除显示"删掉了,可以恢复"是一句错误的承诺。
   */
  restorable: boolean;
}

export interface NodePatch {
  title?: string;
  description?: string | null;
  acceptanceCriteria?: string | null;
  nodeType?: string;
  status?: string;
  priority?: string;
  estimateMinutes?: number | null;
  deadline?: string | null;
  /**
   * 正文的乐观锁:**你手上那一份是第几版**。不是要写的字段,是一个前置条件。
   *
   * 带上它,后端会拿它和库里那一版比对:对不上就 409 `CONCURRENCY_CONFLICT`,
   * 整条请求一个字都不写。**只有保存正文时才该带** —— 改标题/优先级/工时不带,
   * 因为那些字段是逐字段改的,对方动过标题不该让你的正文保存被拒。
   * 见 `backend/contracts/plan.py` 的 `UpdateNodeRequest.content_version`。
   */
  contentVersion?: number;
}

export function getPlan(workspaceId: string): Promise<PlanPayload> {
  return apiFetch<PlanPayload>(`/api/workspaces/${workspaceId}/plan`);
}

export function createNode(
  workspaceId: string,
  payload: {
    parentId: string;
    title: string;
    nodeType?: string;
    /** `information` 建出来的是主题/方向节点 —— 不进排期。默认 `planning`。 */
    purpose?: string;
    description?: string | null;
    priority?: string;
    estimateMinutes?: number | null;
    deadline?: string | null;
  },
): Promise<NodeEditResult> {
  return apiFetch<NodeEditResult>(`/api/workspaces/${workspaceId}/nodes`, {
    method: 'POST',
    body: payload,
  });
}

export function updateNode(
  workspaceId: string,
  nodeId: string,
  patch: NodePatch,
): Promise<NodeEditResult> {
  return apiFetch<NodeEditResult>(`/api/workspaces/${workspaceId}/nodes/${nodeId}`, {
    method: 'PATCH',
    body: patch,
  });
}

/**
 * 一个节点的**长正文**(「笔记」)。§2.2。
 *
 * ## 它为什么不在 `PlanNodePayload` 里
 *
 * 节点行上的 `description` 是**简述**(最多 300 码点,每次改计划都被快照进版本
 * 账本),这里最多 20,000 码点、不进版本账本。把它塞进计划载荷的后果是每次读计划、
 * 每次改计划、每一行版本记录都背着全部节点的全文 —— 而四个视图里没有一个需要正文。
 *
 * 所以它是**按需取**的:点开某个节点的详情时才 GET 这一条。
 */
export interface NotePayload {
  nodeId: string;
  body: string;
  /**
   * 笔记**自己**的乐观锁。**与 `PlanNodePayload.contentVersion` 是两个号** ——
   * 共用会变成"有人改了 300 字的简述 → 你 20,000 字的笔记保存失败"。
   *
   * 从来没有写过笔记的节点是 **0**(而不是 404):"这个节点还没有笔记"是完全正常的
   * 状态,用 404 表达它会把编辑器的初次加载变成一条错误路径。
   */
  contentVersion: number;
  updatedAt: string | null;
}

/** 读一个节点的长正文。**没写过返回空正文 + 第 0 版,不是 404。** */
export function getNodeNote(workspaceId: string, nodeId: string): Promise<NotePayload> {
  return apiFetch<NotePayload>(`/api/workspaces/${workspaceId}/nodes/${nodeId}/notes`);
}

/**
 * 整份覆盖一个节点的长正文。
 *
 * `expectedContentVersion` 是**前置条件,不是要写的字段**(与 `NodePatch.contentVersion`
 * 同一个设计):把 GET 到的那一版原样带回来,对不上就是 409,这次写入一个字都不落。
 *
 * 它**不返回 `revisionVersion`**(而节点编辑一定返回):笔记不是计划的一部分 ——
 * 带上一个不会因为这次写入而改变的版本号,调用方很容易读成"计划刚变了"。
 */
export function updateNodeNote(
  workspaceId: string,
  nodeId: string,
  payload: { body: string; expectedContentVersion?: number },
): Promise<{ note: NotePayload }> {
  return apiFetch<{ note: NotePayload }>(
    `/api/workspaces/${workspaceId}/nodes/${nodeId}/notes`,
    { method: 'PUT', body: payload },
  );
}

/**
 * 删一个节点**及其整棵子树**。默认是**归档**。
 *
 * ## 两个模式的区别只有一句话:以后能不能拿回来
 *
 * - `archive`(默认):用户点垃圾桶的那一下。边的行一行不动,`restoreNode` 能把这一支
 *   原样拿回来(连依赖、关系、排期一起)。
 * - `delete`:彻底删除,顺手物理删掉挂在上面的边,并且**不再接受恢复**。
 *
 * 名字刻意不叫 `softDelete` / `hardDelete`:两者在库里都是软删除(行都留着),
 * 区别只在 `purged_at` 那一列(见 `backend/services/node_service.py` 的那张表)。
 *
 * ## `removedDependencies` 在归档时恒为 0,而那不代表"什么都没删"
 *
 * 归档不动边,所以这个数字为 0 是**对的** —— 界面不要拿它当作"删干净了"的证据,
 * 判断删干净没有要看 `/plan` 里那个节点还在不在。
 */
export function deleteNode(
  workspaceId: string,
  nodeId: string,
  mode: 'archive' | 'delete' = 'archive',
): Promise<NodeEditResult> {
  return apiFetch<NodeEditResult>(
    `/api/workspaces/${workspaceId}/nodes/${nodeId}?mode=${mode}`,
    { method: 'DELETE' },
  );
}

/**
 * 归档(或彻底删除)**之前**,这一下会带走什么。
 *
 * 由后端算,不由前端拿本地那份计划推:手里那份可能是几分钟前的,而用户在确认框里
 * 看到的数字和实际发生的事对不上,比不给数字更糟 —— 他会照着那个数字做决定。
 */
export interface ArchiveImpact {
  nodeId: string;
  title: string;
  /** 会一起被收起来的后代数(不含自己)。 */
  descendants: number;
  /** 会从画布上消失的 `related_to` / `influences` 关系条数。 */
  relations: number;
  /** 会从画布上消失的「前置 → 后续」依赖条数。 */
  dependencies: number;
  /** 挂在这一支上的排期场次与分钟数。归档后它们不再出现在计划里,恢复时会原样回来。 */
  sessions: number;
  sessionMinutes: number;
  /** 其中日期已过、还标着"待做"的那几场 —— 恢复之后需要用户自己处理。 */
  overdueSessions: number;
}

export function getArchiveImpact(workspaceId: string, nodeId: string): Promise<ArchiveImpact> {
  return apiFetch<ArchiveImpact>(
    `/api/workspaces/${workspaceId}/nodes/${nodeId}/archive-impact`,
  );
}

/** 恢复之后超了每日上限的那一天。 */
export interface OverbookedDay {
  day: string;
  plannedMinutes: number;
  dailyCap: number;
  overBy: number;
}

/**
 * 一次恢复的结果。**排期那几个数字不是锦上添花。**
 *
 * 恢复会把归档期间冻结的场次一次性放回日历:它们可能已经过期,也可能和归档之后
 * 新排的挤在同一天。界面必须把"回来几场、过期几场、哪几天超了"说出来 ——
 * 静默恢复等于替用户交了一份他没看过的日程。
 */
export interface RestoreResult {
  node: PlanNodePayload;
  revisionVersion: number;
  restoredCount: number;
  restoredSessions: number;
  restoredMinutes: number;
  overdueSessions: number;
  overbookedDays: OverbookedDay[];
  /**
   * 跟着回来的关系与依赖条数。它们**没有被归档删掉过**(归档一行边都不动),
   * 所以这里说的是"重新可见",不是"重新创建" —— 界面用它解释"为什么线也回来了"。
   */
  relationsVisible: number;
}

/**
 * 把一个归档的节点**连同当时一起被归档的那一支**恢复回来。
 *
 * 三种失败各自有明确的话,界面要分开说(用户能做的事不一样):
 * `NODE_PURGED`(被彻底删除过,拿不回来了)、`PARENT_ARCHIVED`(先把上层恢复出来)、
 * `DEPENDENCY_CYCLE`(恢复之后会出现环,所以整体没有恢复)。
 */
export function restoreNode(workspaceId: string, nodeId: string): Promise<RestoreResult> {
  return apiFetch<RestoreResult>(`/api/workspaces/${workspaceId}/nodes/${nodeId}/restore`, {
    method: 'POST',
  });
}

/** 归档列表里的一行。 */
export interface ArchivedNode {
  node: PlanNodePayload;
  archivedAt: string;
  /** 这一次归档带走的子孙数。 */
  descendants: number;
  sessions: number;
  /** 能不能恢复。`false` 时原因在 `blockedReason` 里。 */
  restorable: boolean;
  /** `PARENT_ARCHIVED`(先恢复上层)或 `PARENT_PURGED`(上层被彻底删除了,回不来)。 */
  blockedReason: string | null;
}

/**
 * 这个空间里归档过什么,新的在前。
 *
 * **只列每一次归档的根** —— 同一批被带走的子孙不单独出现(点那一行的"恢复"会把它们
 * 一起带回来)。父节点在另一次归档里的那些仍然列出来,但 `restorable` 是 `false`,
 * 界面据此把按钮置灰并写明原因,而不是让用户点一下撞一句错误。
 */
export function listArchive(workspaceId: string): Promise<ArchivedNode[]> {
  return apiFetch<ArchivedNode[]>(`/api/workspaces/${workspaceId}/archive`);
}

export function addDependency(
  workspaceId: string,
  predecessorId: string,
  successorId: string,
): Promise<DependencyPayload> {
  return apiFetch<DependencyPayload>(`/api/workspaces/${workspaceId}/dependencies`, {
    method: 'POST',
    body: { predecessorId, successorId },
  });
}

export function removeDependency(
  workspaceId: string,
  predecessorId: string,
  successorId: string,
): Promise<void> {
  return apiFetch<void>(
    `/api/workspaces/${workspaceId}/dependencies?predecessorId=${predecessorId}&successorId=${successorId}`,
    { method: 'DELETE' },
  );
}

// ---------------------------------------------------------------------------------
// 画布上的关系与布局
//
// 关系那四个接口收的都是 `relation_id`,**不是一对节点** —— 三种边共用一个 id 空间,
// 前端不必记住"这条边在哪个表里"。依赖那两条(`addDependency`/`removeDependency`)
// 收的是一对节点,是排期那条路径的旧形状,新代码应当走这里。
// ---------------------------------------------------------------------------------
export function createRelation(
  workspaceId: string,
  payload: CreateRelationRequest,
): Promise<RelationPayload> {
  return apiFetch<RelationPayload>(`/api/workspaces/${workspaceId}/relations`, {
    method: 'POST',
    body: payload,
  });
}

export function updateRelation(
  workspaceId: string,
  relationId: string,
  patch: UpdateRelationRequest,
): Promise<RelationPayload> {
  return apiFetch<RelationPayload>(`/api/workspaces/${workspaceId}/relations/${relationId}`, {
    method: 'PATCH',
    body: patch,
  });
}

/** **删边不删节点。** 两端都还在,只是这条线没了。 */
export function removeRelation(workspaceId: string, relationId: string): Promise<void> {
  return apiFetch<void>(`/api/workspaces/${workspaceId}/relations/${relationId}`, {
    method: 'DELETE',
  });
}

/** 一个节点在画布上的位置。单位是画布坐标,不是屏幕像素 —— 不做任何换算。 */
export interface LayoutPositionPayload {
  nodeId: string;
  x: number;
  y: number;
}

/** 一个层级(总空间或某个子空间)的平移与缩放。`scopeNodeId` 就是那个层级的根节点。 */
export interface ScopeViewportPayload {
  scopeNodeId: string;
  zoom: number;
  panX: number;
  panY: number;
}

/**
 * 整份提交这一次看到的布局。
 *
 * **整份而不是逐条**:拖动一个节点会连续产生几十个中间位置,逐条发意味着几十次写入
 * 和几十个并发冲突。整份提交让"最后一次赢"成为语义本身,不必按时间戳仲裁。
 *
 * **它不删除没提交的行。** 位置按 (用户, 空间, 节点) 存,不按层级分;在某个子空间里
 * 做全量替换会删掉其他所有层级的位置,而界面看起来完全正常,直到用户返回上一层
 * 发现节点全叠在一起。
 */
export interface PutLayoutRequest {
  positions: LayoutPositionPayload[];
  viewports: ScopeViewportPayload[];
}

/**
 * 一个用户在一个空间里的全部布局。**按用户取,不按空间共享。**
 *
 * 保存布局**不产生计划版本** —— 位置是用户偏好,不是计划的一部分。复盘时"V7 改了什么
 * 把我的排期挪走了"这个问题,不该被几百次拖动淹没。
 */
export interface LayoutPayload {
  positions: LayoutPositionPayload[];
  viewports: ScopeViewportPayload[];
}

export function getLayout(workspaceId: string): Promise<LayoutPayload> {
  return apiFetch<LayoutPayload>(`/api/workspaces/${workspaceId}/layout`);
}

export function putLayout(
  workspaceId: string,
  payload: PutLayoutRequest,
): Promise<LayoutPayload> {
  return apiFetch<LayoutPayload>(`/api/workspaces/${workspaceId}/layout`, {
    method: 'PUT',
    body: payload,
  });
}

// ---------------------------------------------------------------------------------
// 排期
// ---------------------------------------------------------------------------------

/** 排期算法希望存在的一场。`sessionId` 为空表示这一场是**新加的**。 */
export interface PlannedSessionView {
  sessionId: string | null;
  nodeId: string;
  workspaceId: string;
  nodeTitle: string;
  scheduledDate: string;
  plannedMinutes: number;
  bufferMinutes: number;
  seq: number;
  startMinute: number | null;
  endMinute: number | null;
  origin: string;
  locked: boolean;
}

/**
 * 排不进去的那部分。**结构化的事实,不是一句"排不下"。**
 *
 * `bindingConstraint` 是"哪一道闸门卡住的",用户能做的三件事各自对应一个取值 ——
 * 给错约束的代价由用户承担(让他"少做点"而真正卡住的是锁定场次,他改完发现毫无变化)。
 */
export interface ScheduleGapView {
  workspaceId: string | null;
  nodeId: string | null;
  nodeTitle: string;
  unscheduledMinutes: number;
  reasonCode: string;
  bindingConstraint: string;
  detail: Record<string, unknown>;
}

/** 一条出路。`resolvesGap` 是**重跑一遍算出来的**,不是断言的。 */
export interface RecoveryOptionView {
  kind: string;
  label: string;
  description: string;
  resolvesGap: boolean;
  remainingUnscheduledMinutes: number;
  params: Record<string, unknown>;
}

export interface DailyLoadView {
  date: string;
  plannedMinutes: number;
  capacityMinutes: number;
  byWorkspace: Record<string, number>;
}

export interface ScheduleChurnView {
  moved: number;
  created: number;
  canceled: number;
  kept: number;
  /** 服务端拼好的一句话。措辞是这个产品的一部分,前端不重拼。 */
  description: string;
}

export interface SchedulePreviewResponse {
  /** 把这份结果写进库时要带上的门票。 */
  scheduleVersion: string;
  today: string;
  horizonDays: number;
  /**
   * 这次排期覆盖了哪些空间。**是"哪些"而不是"哪一个"** —— 时间池按人算,
   * 一份排期天然横跨这个账户的全部活动空间。界面必须如实说出来,否则用户在另一个
   * 空间里看到自己的任务被挪了日子,会以为系统乱动了他的计划。
   */
  scopeWorkspaceIds: string[];
  weeklyBudgetMinutes: number;
  sessions: PlannedSessionView[];
  gaps: ScheduleGapView[];
  options: RecoveryOptionView[];
  dailyLoad: DailyLoadView[];
  churn: ScheduleChurnView;
  totalPlannedMinutes: number;
  unscheduledMinutes: number;
  /** 恒为 false。排不下的部分一定在 `gaps` 里,不会安静地消失。 */
  truncated: boolean;
}

export interface ScheduleAppliedView {
  created: number;
  updated: number;
  moved: number;
  canceled: number;
  kept: number;
  workspaces: number;
  unscheduledMinutes: number;
}

export interface ScheduleApplyResponse {
  scheduleVersion: string;
  applied: ScheduleAppliedView;
  churn: ScheduleChurnView;
  replayed: boolean;
}

/**
 * 预览一份排期。**只读,一行都不写。**
 *
 * 路径是 workspace 级的(每个空间的界面都有那个按钮),但**效果是账号级的**:
 * 池子按人算,所以它会排上这个账户所有活动空间里的任务。
 *
 * 用 POST 而不是 GET:它是一次依赖"今天"的计算,不是一次读取。
 */
export function previewSchedule(workspaceId: string): Promise<SchedulePreviewResponse> {
  return apiFetch<SchedulePreviewResponse>(`/api/workspaces/${workspaceId}/schedule/preview`, {
    method: 'POST',
  });
}

/**
 * 应用一份排期。
 *
 * `scheduleVersion` 必须是**用户刚刚预览过的那一份**的版本号。中间隔着一次点击和
 * 一次往返,期间计划可能被改过(用户自己勾了完成、另一个标签页确认了一份提案)——
 * 对不上时后端返回 409,让用户重新预览,而不是照样写入一份他没看过的安排。
 *
 * `idempotencyKey` 每份预览生成一次并**在重试时复用同一个**。换一个键就等于告诉
 * 后端"这是另一次应用"。
 */
export function applySchedule(
  workspaceId: string,
  scheduleVersion: string,
  idempotencyKey: string,
): Promise<ScheduleApplyResponse> {
  return apiFetch<ScheduleApplyResponse>(`/api/workspaces/${workspaceId}/schedule/apply`, {
    method: 'POST',
    body: { scheduleVersion, idempotencyKey },
  });
}

// ---------------------------------------------------------------------------------
// 执行反馈 · 今天 · 提醒 · 按执行情况调整
// ---------------------------------------------------------------------------------

/**
 * 一次反馈的结果。闭集,四种对计划的含义**不一样**:
 *
 * - `completed` 这场完成
 * - `partial`   还在进行,做了一部分
 * - `skipped`   这场没做(冻结,排期不会再动它)
 * - `failed`    做了但没成 —— 这件事仍然欠着,所以这场**不算完成**
 */
export type ExecutionResult = 'completed' | 'partial' | 'skipped' | 'failed';

export interface RecordExecutionPayload {
  result: ExecutionResult;
  /** 调用方生成一次,**重试时复用同一个**。换键 = 告诉后端"这是另一次反馈"。 */
  idempotencyKey: string;
  startedAt?: string;
  endedAt?: string;
  actualMinutes?: number;
  /** 0.0 ~ 1.0。部分完成时给出"做到哪儿了"。 */
  completionRatio?: number;
  /** 为什么没做完 / 没做。用户说得出原因,复盘才可能给出有用的调整。 */
  delayReason?: string;
  userFeedback?: string;
}

export interface ExecutionRecordView {
  id: string;
  sessionId: string | null;
  nodeId: string;
  workspaceId: string;
  result: ExecutionResult;
  actualMinutes: number | null;
  completionRatio: number | null;
  startedAt: string | null;
  endedAt: string | null;
  delayReason: string | null;
  userFeedback: string | null;
  createdAt: string;
}

export interface RecordExecutionResponse {
  /**
   * 这条记录有没有真的落库。**成功时也是显式的 true** —— 库写不进去时后端返回
   * 503 + `saved: false`,不是静默切到内存。界面必须读它,不能只看状态码。
   */
  saved: boolean;
  record: ExecutionRecordView;
  /** 写完之后的**那一行场次**(库里的真相),不是客户端发上去的东西。 */
  session: ScheduledSessionPayload | null;
  replayed: boolean;
  /** 这个节点还剩几场没过、几场已完成。给界面一句"还剩 2 次"。 */
  nodeRemainingSessions: number;
  nodeCompletedSessions: number;
}

export function recordExecution(
  sessionId: string,
  payload: RecordExecutionPayload,
): Promise<RecordExecutionResponse> {
  return apiFetch<RecordExecutionResponse>(`/api/sessions/${sessionId}/executions`, {
    method: 'POST',
    body: payload,
  });
}

export interface TodayItemView {
  sessionId: string;
  nodeId: string;
  workspaceId: string;
  workspaceTitle: string;
  nodeTitle: string;
  plannedMinutes: number;
  bufferMinutes: number;
  seq: number;
  /** `MINUTES_ONLY` 模式下为 null —— 那是"某天多少分钟",不是"几点到几点"。 */
  startMinute: number | null;
  endMinute: number | null;
  status: string;
  locked: boolean;
  /** **`null` 表示没有任何记录,不表示没完成。** */
  result: ExecutionResult | null;
  actualMinutes: number | null;
  delayReason: string | null;
  /** 与 `result !== null` 同义,单独给出来是因为它更难被漏判成"假值 = 没做"。 */
  recorded: boolean;
}

export interface TodayWorkspaceView {
  workspaceId: string;
  title: string;
  items: TodayItemView[];
}

/** 一句**提问**,不是一句结论 —— 这场过去了而且我们不知道结果。 */
export interface CheckInQuestion {
  sessionId: string;
  nodeId: string;
  workspaceId: string;
  nodeTitle: string;
  scheduledDate: string;
  daysAgo: number;
  plannedMinutes: number;
  question: string;
}

/**
 * 跨空间的「今天」。**路径上没有 workspaceId** —— 用户问的是"我今天要做什么",
 * 不是"我这个空间今天要做什么"。逐空间看会让"两个空间各排了 60 分钟"看起来
 * 都来得及,而他只有两小时。
 */
export interface TodayResponse {
  today: string;
  timezone: string;
  workspaces: TodayWorkspaceView[];
  plannedMinutes: number;
  /** 只统计**有记录**的那些场次 —— 把没记录的算成 0 会让"今天投入了多久"在下午变成假数字。 */
  actualMinutes: number;
  itemCount: number;
  recordedCount: number;
  checkInQuestions: CheckInQuestion[];
  /** 服务端拼好的一句话,如实说明"没有记录"意味着什么。 */
  note: string;
}

export function fetchToday(): Promise<TodayResponse> {
  return apiFetch<TodayResponse>('/api/today');
}

export interface QuietHoursView {
  active: boolean;
  /** 当日分钟数。`fromMinute > toMinute` 表示这段跨过午夜(22:00 -> 08:00)。 */
  fromMinute: number;
  toMinute: number;
  description: string;
  source: string;
}

export interface ReminderView {
  key: string;
  /** `workspace_empty` / `plan_created` / `user_returned` / `repeated_skips` / `weekend` / `stage_completed`。 */
  kind: string;
  title: string;
  body: string;
  workspaceId: string | null;
  forDate: string;
}

/**
 * 当前该显示的提醒。**这是站内提醒,不是推送** —— 它只在用户打开界面时出现。
 */
export interface RemindersResponse {
  reminders: ReminderView[];
  quietHours: QuietHoursView;
  /** 被免打扰时段压住的条数。**如实说出来** —— 用户不该把"现在没有提醒"读成"一切正常"。 */
  suppressedCount: number;
  note: string;
}

export interface ReminderStateView {
  key: string;
  dismissed: boolean;
  snoozedUntil: string | null;
}

export function fetchReminders(): Promise<RemindersResponse> {
  return apiFetch<RemindersResponse>('/api/reminders');
}

/** 键放在请求体里 —— 它不是一行资源的 id,而是 `weekend:2026-W39` 这种复合标识。 */
export function dismissReminder(key: string): Promise<ReminderStateView> {
  return apiFetch<ReminderStateView>('/api/reminders/dismiss', { method: 'POST', body: { key } });
}

/** "稍后"是一个真实的承诺:到点它会重新出现,而不是被软化成"关掉"。 */
export function snoozeReminder(key: string, hours = 24): Promise<ReminderStateView> {
  return apiFetch<ReminderStateView>('/api/reminders/snooze', {
    method: 'POST',
    body: { key, hours },
  });
}

export interface DeviationView {
  code: string;
  workspaceId: string | null;
  nodeId: string | null;
  nodeTitle: string;
  sessionId: string | null;
  /** 服务端拼好的一句话,例如「周三那场 60 分钟的「变量与类型」还没有记录」。 */
  detail: string;
  /**
   * 这是一句提问(`true`)还是一句结论(`false`)。
   *
   * `true` = 我们**不知道**发生了什么(那场没有记录),它可以被问,不能被算作偏差。
   * `false` = 用户**说了**发生了什么(跳过/失败/部分),或者一件事客观上过期了。
   */
  isQuestion: boolean;
  daysAgo: number | null;
  facts: Record<string, unknown>;
}

export interface DeviationsResponse {
  deviations: DeviationView[];
  analyzedFor: string | null;
  /** 其中有多少条是**提问**。两类在界面上的措辞必须不同。 */
  questionCount: number;
  note: string;
}

/** 只是偏差事实 —— 不请模型,不花钱,模型挂了也照样正确。 */
export function fetchDeviations(workspaceId: string): Promise<DeviationsResponse> {
  return apiFetch<DeviationsResponse>(`/api/workspaces/${workspaceId}/deviations`);
}

export interface ReplanResponse {
  deviations: DeviationView[];
  /** 这一轮有没有真的请模型看过。没有偏差时是 false。 */
  consultedModel: boolean;
  proposal: ProposalView | null;
  /**
   * 模型提了变更但没通过校验时,逐条的问题。和 `proposal === null` 一起读:
   * 前者是"提了但不行",后者是"没提"。这两件事对用户意味着不同的东西。
   */
  proposalErrors: { code: string; message: string; ordinal?: number }[];
  source: string | null;
  degraded: boolean;
  degradedReason: string | null;
  retryable: boolean;
  /** **降级时它必须如实说明"这次没能给出调整方案"** —— 空白不等于"不需要调整"。 */
  message: string;
  analyzedFor: string | null;
}

/**
 * 按执行情况调整计划。
 *
 * 它走的是**和提案确认完全相同的路径** —— 校验、预览、确认事务一个都不少,返回的
 * `proposal` 要用户点确认才会写进去。`degraded` 为真时不会有提案,那是"这次没能给出
 * 方案",不是"系统认为不需要调整"。
 */
export function replan(workspaceId: string): Promise<ReplanResponse> {
  return apiFetch<ReplanResponse>(`/api/workspaces/${workspaceId}/replan`, { method: 'POST' });
}

/**
 * 约束 -> 中文名。取值来自后端 `scheduler/errors.py::BindingConstraint`,是个闭集。
 *
 * 界面必须点名"卡在哪",因为用户能做的三件事(少做点 / 延期 / 多投入)各自对应
 * 不同的约束 —— 不说清楚的话他只会盲点"增加投入",而那可能一点用都没有。
 *
 * 兜底分支返回原值而不是空串:后端将来加了新约束时,界面上会显示一个英文枚举名,
 * 那不好看,但比"什么都没说"强 —— 后者会让缺口看起来像系统的问题。
 */
export function bindingConstraintLabel(constraint: string): string {
  switch (constraint) {
    case 'DAILY_MAX':
      return '每天的时间上限';
    case 'WEEKLY_BUDGET':
      return '每周时间预算';
    case 'AVAILABILITY':
      return '你标出的可用时段';
    case 'DEADLINE':
      return '截止时间';
    case 'DEPENDENCY':
      return '前置任务的完成时间';
    case 'LOCKED_SESSIONS':
      return '你锁定的安排';
    case 'MIN_SESSION_SIZE':
      return '单场的最短时长';
    case 'NO_ESTIMATE':
      return '缺预计工时';
    case 'HORIZON':
      return '排期能算到的范围';
    default:
      return constraint;
  }
}

/** 缺口原因 -> 一句给人看的话。取值来自 `scheduler/errors.py::ScheduleErrorCode`。 */
export function gapReasonLabel(reason: string): string {
  switch (reason) {
    case 'NO_CAPACITY_BEFORE_DEADLINE':
      return '截止时间之前的日子都排满了';
    case 'DEADLINE_ALREADY_PASSED':
      return '截止时间已经过去';
    case 'DEPENDENCY_CHAIN_UNSATISFIABLE':
      return '前置任务排到了截止日之后';
    case 'LOCKED_SESSION_CONFLICT':
      return '和你锁定的安排撞上了';
    case 'NO_ESTIMATE':
      return '这件事没有填预计工时，不知道要做多久';
    case 'BELOW_MIN_SESSION':
      return '剩下的空隙放不下一场最小的安排';
    case 'HORIZON_EXHAUSTED':
      return '往后算的这段时间里没有空位了';
    default:
      return reason;
  }
}

// ---------------------------------------------------------------------------------
// 提案
// ---------------------------------------------------------------------------------

export interface ProposalItemView {
  ordinal: number;
  op: string;
  summary: string;
  /** `create_node` 这类新建的项,在提案内部用的临时名(`n1`)。 */
  localId: string | null;
  /** 改动指向的节点。服务端从 `localId` 映射回来的真 UUID。 */
  targetNodeId: string | null;
  targetTitle: string | null;
  /** 将被写入的字段原样。界面上可以展开看。 */
  payload: Record<string, unknown>;
  /** `delete_node` 会连带删掉的子节点数量。 */
  affectedChildren: number;
  /**
   * 这一条本来是「新建」,被**合并**成了「补充已有节点」——值是那句"合并到哪里去了"
   * 的标题,`null` 表示没有被改写。§2.5 / §4.4。
   *
   * **界面必须把它说出来。** 悄悄把一条"新建"改成"更新"是违反 §7.2 的:用户以为
   * AI 给他加了一个新节点,实际发生的是它改了一个旧节点 —— 两者在画布上的样子完全
   * 不同,而这个确认框是用户唯一能拦住它的地方。
   */
  coalescedFrom: string | null;
}

export interface ProposalView {
  id: string;
  status: 'validated' | 'pending_confirmation' | 'applied' | 'rejected' | 'stale' | 'failed';
  baseRevisionVersion: number;
  triggerType: string;
  itemCount: number;
  items: ProposalItemView[];
  reasoning: string | null;
  /** 生成时就算好的展示信息(每个 op 动了什么)。服务端生成,前端只显示。 */
  changeSummary: Record<string, unknown>;
  createdAt: string;
  /** 过期时间。过了之后确认会被拒(`PROPOSAL_EXPIRED`)。 */
  expiresAt: string | null;
  decidedAt: string | null;
}

export interface AppliedChangeView {
  nodesCreated: number;
  nodesUpdated: number;
  nodesDeleted: number;
  /**
   * 被写进**长正文(笔记)**的节点数。
   *
   * **不能与 `nodesUpdated` 相加** —— 一个节点可能既改了说明、又补了笔记,两个
   * 数字里各算一次。它们是"这几类写入各碰到了几个节点",不是一份划分。
   */
  notesUpdated: number;
  dependenciesAdded: number;
  dependenciesRemoved: number;
  /**
   * 被写进 `node_relations` 的「相关 / 影响」边条数。
   *
   * 与 `dependenciesAdded` 是两件事:那些写 `dependencies` 并参与排期,
   * 这些只是画布上的说明,不改排期结果。
   */
  relationsAdded: number;
  revisionVersion: number;
}

export interface ConfirmProposalResponse {
  proposal: ProposalView;
  applied: AppliedChangeView;
  /** 这次响应是不是重放之前那一次的。用户双击"确认"时第二次是 true。 */
  replayed: boolean;
}

/**
 * 确认一份提案 —— 产品里最重的一次写入。
 *
 * `idempotencyKey` 由调用方生成一次并**在重试时复用同一个**。换一个键就等于告诉
 * 后端"这是另一次确认",网络超时重发会变成写两遍。
 */
export function confirmProposal(
  workspaceId: string,
  proposalId: string,
  idempotencyKey: string,
): Promise<ConfirmProposalResponse> {
  return apiFetch<ConfirmProposalResponse>(
    `/api/workspaces/${workspaceId}/proposals/${proposalId}/confirm`,
    { method: 'POST', body: { idempotencyKey } },
  );
}

export function listProposals(workspaceId: string): Promise<ProposalView[]> {
  return apiFetch<ProposalView[]>(`/api/workspaces/${workspaceId}/proposals`);
}

export function rejectProposal(
  workspaceId: string,
  proposalId: string,
  reason?: string,
): Promise<ProposalView> {
  return apiFetch<ProposalView>(
    `/api/workspaces/${workspaceId}/proposals/${proposalId}/reject`,
    { method: 'POST', body: reason ? { reason } : {} },
  );
}

// ---------------------------------------------------------------------------------
// 展示用的文案
// ---------------------------------------------------------------------------------

/** 这一轮的回复是谁生成的。界面上必须显示出来 —— 见 backend/contracts/conversation.py。 */
export function sourceLabel(source: string): string {
  switch (source) {
    case 'openjiuwen':
      return 'AI 规划 · openJiuwen';
    case 'direct_llm':
      return 'AI 规划 · DeepSeek';
    case 'rule_fallback':
      return '本地规则 · 模型不可用';
    case 'scripted':
      // 说"脚本"就够了:这一条只可能来自隔离栈里的 `AGENT_REASONER=script`。
      // 写成和别的模式一样的一句话(比如"AI 规划 · 本地脚本")就把它伪装成了
      // 一次真实的模型调用 —— 那正是这个枚举要防的事。
      return '测试脚手架 · 脚本回放';
    default:
      return '模型不可用';
  }
}

/**
 * 降级原因 -> 给用户看的一句话。
 *
 * 参数类型故意放宽成 `string | null`:后端将来加了新的 `DegradedReason`,
 * 前端这里**不该因此编译不过** —— 那样最坏的结果是线上白屏,而不是少一句提示。
 * 兜底分支返回空串,徽标退化成只显示"本地规则 · 模型不可用",信息量小但不错。
 */
export function degradedHint(reason: string | null | undefined): string {
  switch (reason) {
    case 'NO_API_KEY':
      return '还没有配置模型密钥,现在只能回答固定问题。';
    case 'OPENJIUWEN_NOT_INSTALLED':
      return 'openJiuwen 未安装,当前回落到直连模型。';
    case 'MODEL_TIMEOUT':
      return '模型响应超时。';
    case 'MODEL_OUTPUT_INVALID':
      return '模型这次的回答没能解析出结果。';
    case 'MODEL_AUTH_FAILED':
      return '模型密钥无效或已过期。';
    case 'MODEL_RATE_LIMITED':
      return '模型服务限流了。';
    case 'MODEL_UNAVAILABLE':
      return '连不上模型服务。';
    case 'CIRCUIT_OPEN':
      return '连续失败次数过多,已暂时停止调用。';
    default:
      return '';
  }
}

/** 简报字段 -> 中文名。用于"已记下:每周 4 小时"这类反馈。 */
export function fieldLabel(field: string): string {
  switch (field) {
    case 'goal':
      return '目标';
    case 'deadline':
      return '截止时间';
    case 'weekly_available_minutes':
      return '每周可投入';
    case 'current_level':
      return '当前水平';
    case 'success_criteria':
      return '验收标准';
    case 'constraints':
      return '约束';
    default:
      return field;
  }
}

// ---------------------------------------------------------------------------------
// 目标推理地图(阶段 7)
// ---------------------------------------------------------------------------------

/** 显式 Agent turn 的触发来源。**闭集**,服务端按它选阶段与动作。 */
export type AgentTurnTrigger =
  | 'space_entered'
  | 'user_message'
  | 'node_selected'
  | 'question_answered'
  | 'strategy_confirmation'
  | 'regenerate_roadmap'
  | 'progress_update'
  | 'execution_planning'
  | 'retry';

/**
 * 推理地图上的一个节点。
 *
 * **它不是 GrowthNode**:不进排期、任务统计、依赖或执行记录。`userDescription`
 * 是用户原文(前端可编辑),`summary` 是 Agent 维护的摘要 —— 两者必须分区显示,
 * Agent 不会覆盖用户字段。
 */
export interface ReasoningNodeView {
  id: string;
  handle: string;
  parentHandle: string | null;
  linkedPlanNodeId: string | null;
  title: string;
  summary: string | null;
  userDescription: string | null;
  nodeType: string;
  status: string;
  nextAction: string;
  importance: number;
  uncertainty: number;
  urgency: number;
  impact: number;
  confidence: number;
  /** 可解释启发式现算,不是概率。 */
  priority: number;
  rationale: string | null;
  assumptions: string[];
  evidence: string[];
  /** 阶段 8:路线 / 阶段的粗粒度时间带、成果物、通过标准。 */
  timeframe: string | null;
  deliverable: string | null;
  passCriteria: string | null;
  /** 阶段 11:结构化时间架构。dated 用 startDate/endDate;relative 用 startWeek/endWeek。 */
  timeframeKind: 'dated' | 'relative' | null;
  startWeek: number | null;
  endWeek: number | null;
  startDate: string | null;
  endDate: string | null;
  source: string;
  version: number;
  updatedAt: string;
  /**
   * 规划智能体重构 V1:画布角色(`group` / `analysis` / `strategy`)。
   * null = 非 V1 节点(老地图 / V0.1)。
   */
  v1Kind: string | null;
  /** V1 固定标识(`current_state` / `true_intent` / …)。null = 非 V1。 */
  v1Key: string | null;
  /** 该分析节点当前唯一待确认的一件事。null = 不再需要追问。 */
  v1Question: string | null;
}

export interface ReasoningLinkView {
  id: string;
  sourceHandle: string;
  targetHandle: string;
  linkType: string;
  note: string | null;
}

/** 阶段 12:对话式战略 intake 当前等回答的那一条。**不是问题实体。** */
export interface PendingIntakeView {
  messageId: string | null;
  question: string;
  decisionScope: string;
  whyThisMatters: string;
  quickReplies: string[];
}

/** V0.1 时间轴上的一个投影项(阶段 / 里程碑 / 截止 / 成果)。 */
export interface V01TimelineItemView {
  id: string;
  title: string;
  kind: 'phase' | 'milestone' | 'deadline' | 'deliverable';
  startWeek: number | null;
  endWeek: number | null;
  startDate: string | null;
  endDate: string | null;
  goal: string;
  deliverable: string;
  completionCriteria: string;
  status: 'draft' | 'planned';
  planNodeId: string | null;
}

/** 当前目标推理地图。读接口与 agent turn 都返回这一份。 */
export interface GoalReasoningView {
  workspaceId: string;
  sessionId: string | null;
  rootPlanNodeId: string | null;
  phase: string;
  turnAction: string;
  status: string;
  mapVersion: number;
  focusHandle: string | null;
  focusReasoningNodeId: string | null;
  focusReason: string | null;
  inputVersion: string | null;
  strategyProposalId: string | null;
  /** 阶段 11:intake 进度(已问几个关键问题 / 上限)。 */
  intakeQuestionsAsked: number;
  intakeQuestionLimit: number;
  /** 阶段 12:当前正在等回答的 intake 关键问题。null = 不在等回答。 */
  pendingIntake: PendingIntakeView | null;
  /** 规划智能体 V0.1 的工作流阶段。null = 非 V0.1。 */
  workflowStage: string | null;
  /** V0.1 阶段一当前等回答的核心问题(2–4 个)。 */
  discoveryQuestions: string[];
  /** V0.1 时间线投影(唯一权威来源)。非 V0.1 一律为空。 */
  v01Timeline: V01TimelineItemView[];
  /** V0.1 待确认的时间线提案 id。null = 没有待确认草案。 */
  v01TimelineProposalId: string | null;
  /**
   * 规划智能体重构 V1(P1)阶段一档位:`initial_thinking` / `goal_reframe` /
   * `factor_analysis` / `strategy_draft`。null = 非 V1(老空间 / V0.1)。
   */
  v1Stage: string | null;
  /** 首轮回答后的整体判断(可审阅结论,不含隐藏思维链)。 */
  v1Judgment: string | null;
  /** 当前唯一需要回答的全局关键问题。null = 不等待全局回答。 */
  v1Question: string | null;
  /** 当前焦点容器键(模型选出的最值得讨论的一项)。 */
  v1FocusKey: string | null;
  /** 为什么这个焦点比其他未知项更能改变路线。 */
  v1FocusReason: string | null;
  /** 战略路径草案。null = 还没形成。 */
  v1Strategy: V1StrategyView | null;
  /** V1 模型回合状态:`idle` / `running` / `failed`。 */
  v1Status: string | null;
  /** 上一次 V1 模型回合失败的可读原因。 */
  v1Error: string | null;
  /** P5:是否允许导出决策审计记录(后端 `AGENT_AUDIT_EXPORT`)。 */
  v1AuditExportEnabled: boolean;
  /** 阶段 11:时间架构里的日期是否已校准。false = 只有相对周,不伪造日历日期。 */
  datesCalibrated: boolean;
  exploredAt: string | null;
  lastEvaluatedAt: string | null;
  nodes: ReasoningNodeView[];
  links: ReasoningLinkView[];
  error: string | null;
}

export interface AgentTurnRequest {
  trigger: AgentTurnTrigger;
  selectedNodeId?: string | null;
  reasoningHandle?: string | null;
  message?: string | null;
  idempotencyKey: string;
  contextVersion?: number | null;
}

export interface AgentTurnResponse {
  reasoning: GoalReasoningView;
  message: MessageView | null;
  question: QuestionView | null;
  replayed: boolean;
  degraded: boolean;
  degradedReason: DegradedReason | null;
  retryable: boolean;
  changed: boolean;
  /** 战略确认提案没通过校验时的逐条原因。 */
  proposalErrors: { code: string; message: string; ordinal?: number }[];
}

export interface UpdateReasoningNodeRequest {
  title?: string;
  userDescription?: string;
  status?: string;
}

export function getReasoningMap(workspaceId: string): Promise<GoalReasoningView> {
  return apiFetch<GoalReasoningView>(`/api/workspaces/${workspaceId}/reasoning`);
}

export function runAgentTurn(
  workspaceId: string,
  payload: AgentTurnRequest,
): Promise<AgentTurnResponse> {
  return apiFetch<AgentTurnResponse>(`/api/workspaces/${workspaceId}/agent/turn`, {
    method: 'POST',
    body: payload,
  });
}

export function updateReasoningNode(
  workspaceId: string,
  nodeId: string,
  payload: UpdateReasoningNodeRequest,
): Promise<GoalReasoningView> {
  return apiFetch<GoalReasoningView>(
    `/api/workspaces/${workspaceId}/reasoning/nodes/${nodeId}`,
    { method: 'PATCH', body: payload },
  );
}

/** 规划智能体重构 V1(P4):生成本周计划 + 下周预览(落成待确认提案)。 */
export function generateV1Weekly(workspaceId: string): Promise<AgentTurnResponse> {
  return apiFetch<AgentTurnResponse>(
    `/api/workspaces/${workspaceId}/agent/v1/weekly/generate`,
    { method: 'POST', body: {} },
  );
}

/** 规划智能体重构 V1(P4):把本周计划拆成少量工作日工作块(落成待确认提案)。 */
export function generateV1Daily(workspaceId: string): Promise<AgentTurnResponse> {
  return apiFetch<AgentTurnResponse>(
    `/api/workspaces/${workspaceId}/agent/v1/daily/generate`,
    { method: 'POST', body: {} },
  );
}

/**
 * 规划智能体重构 V1(P5):下载决策审计记录(Markdown / JSON)。
 *
 * 直接取原始响应并触发浏览器下载 —— 不能用 `apiFetch`(它只解析 JSON,而 Markdown
 * 不是 JSON,且我们要的是**文件**而不是内存对象)。
 */
export async function downloadV1Audit(
  workspaceId: string,
  format: 'markdown' | 'json',
): Promise<void> {
  const token = getToken();
  const response = await fetch(
    `${API_BASE}/api/workspaces/${workspaceId}/agent/v1/audit-export?format=${format}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (!response.ok) {
    let message = `导出失败（${response.status}）。`;
    try {
      const body = (await response.json()) as { error?: { message?: string } };
      message = body.error?.message ?? message;
    } catch {
      /* 响应不是 JSON:用上面的默认文案。 */
    }
    throw new ApiError(response.status, 'AUDIT_EXPORT_FAILED', message);
  }
  const blob = await response.blob();
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const matched = /filename="([^"]+)"/.exec(disposition);
  const filename = matched?.[1] ?? `agent-audit.${format === 'json' ? 'json' : 'md'}`;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/** 规划智能体重构 V1(P4):周末回顾入口 —— 汇总完成度并准备未来重规划草案。 */
export function reviewV1(workspaceId: string): Promise<AgentTurnResponse> {
  return apiFetch<AgentTurnResponse>(`/api/workspaces/${workspaceId}/agent/v1/review`, {
    method: 'POST',
    body: {},
  });
}

/** 规划智能体重构 V1(P2):确认战略逻辑,并生成待确认的粗时间架构(P3)。
 *
 * 服务端返回的是 `AgentTurnResponse`(含消息与最新地图);这里只把地图交给调用方。 */
export function confirmV1Strategy(workspaceId: string): Promise<GoalReasoningView> {
  return apiFetch<AgentTurnResponse>(`/api/workspaces/${workspaceId}/agent/v1/strategy/confirm`, {
    method: 'POST',
    body: {},
  }).then(response => response.reasoning);
}

/** 细化已确认战略。**只有已确认战略存在时**服务端才接受。 */
export function refineStrategy(workspaceId: string): Promise<SendMessageResponse> {
  return apiFetch<SendMessageResponse>(`/api/workspaces/${workspaceId}/agent/strategy/refine`, {
    method: 'POST',
    body: {},
  });
}

// ---------------------------------------------------------------------------------
// Agent 运行轨迹(本地诊断,阶段 9)
//
// **这不是思维链。** 返回的每一步都来自服务端真实执行边界;`summary` 只含工具名、
// 状态与条数,`safeSummary` 来自服务端闭集。契约里没有任何字段能承载 prompt、
// 用户原文或密钥。见后端 `contracts/trace.py`。
// ---------------------------------------------------------------------------------

/** 一步真实执行。`unavailable` 只用于加轨迹字段之前的历史行。 */
export type AgentTraceStep =
  | 'queued'
  | 'resolving_context'
  | 'waiting_model'
  | 'running_tool'
  | 'retrying'
  | 'validating_output'
  | 'persisting'
  | 'completed'
  | 'failed'
  | 'timed_out'
  | 'cancelled'
  | 'unavailable';

export type AgentTraceStatus =
  | 'running'
  | 'completed'
  | 'failed'
  | 'timed_out'
  | 'cancelled'
  | 'unavailable';

/** 一次只读工具调用的**脱敏**摘要。 */
export interface AgentTraceToolView {
  toolName: string;
  status: 'ok' | 'error' | 'rejected';
  durationMs: number | null;
  summary: string;
}

/** 最近一次 Agent turn 的运行轨迹。按时间倒序返回。 */
export interface AgentTraceTurnView {
  id: string;
  shortId: string;
  trigger: string;
  triggerLabel: string;
  currentStep: AgentTraceStep;
  stepLabel: string;
  status: AgentTraceStatus;
  terminal: boolean;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
  /** 真实的状态转移序列(步骤名)。复制诊断摘要用它。 */
  steps: AgentTraceStep[];
  lastProgressAt: string | null;
  waitingSeconds: number | null;
  waitingTooLong: boolean;
  attempt: number;
  terminalCode: string | null;
  safeSummary: string | null;
  tools: AgentTraceToolView[];
  retryable: boolean;
}

export interface AgentTraceView {
  workspaceId: string;
  workspaceShortId: string;
  enabled: boolean;
  generatedAt: string;
  limit: number;
  turns: AgentTraceTurnView[];
}

/**
 * 取最近若干次 Agent turn 的脱敏轨迹。
 *
 * 开关关闭时服务端返回 404 `TRACE_DISABLED` —— 调用方据此把入口整块藏起来,
 * 而不是显示一个坏掉的诊断面板。
 */
export function getAgentTrace(workspaceId: string, limit = 20): Promise<AgentTraceView> {
  return apiFetch<AgentTraceView>(`/api/workspaces/${workspaceId}/agent/trace?limit=${limit}`);
}
