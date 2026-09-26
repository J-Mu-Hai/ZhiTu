'use client';
import { Suspense, createContext, useCallback, useContext, useEffect, useMemo, useRef, useReducer, useState, type ReactNode } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { DEMO_TODAY, initialGrowth } from '@/mock/growth-state';
import { initialConversation } from '@/mock/conversations';
import type { AISettings, Conversation, FileAsset, GrowthNode, GrowthState, JournalEntry, Message, PlanAction, Proposal } from '@/types/growth';
import { historyConversations, initialJournals } from '@/mock/life';
import { collectNodeBranch, growthReducer, daysBetween, shiftDate } from './reducer';
import { todayInTimeZone } from './timeline';
import { emptyGrowth, planToGrowth } from './planProjection';
import { useAuth } from '@/features/auth/provider';
import type { AccountProfile } from '@/features/auth/types';
import { workspaceStorageKey } from './workspaces';
import * as backend from '@/lib/backend';
import { ApiError } from '@/lib/api';

/**
 * 成长空间状态。
 *
 * ## 这个文件现在有两半,界限必须清楚
 *
 * **真实空间**(用户从"成长空间"里建的那个,id 是后端 UUID):对话完全走后端 ——
 * 历史读 `GET /api/workspaces/{id}/messages`,每一轮发 `POST .../messages`。
 * 计划图仍然是前端本地状态(阶段 5 才接 `GET /plan`),但**新空间是空的**,
 * 只有一个根目标节点,标题取真实的空间名。
 *
 * **示例空间**(`workspace === 'primary'`,只能从空间页主动进入):还是原来那套
 * 本地演示数据,界面上标着"AI DEMO"。它不再是"没选空间时的默认值" ——
 * 没选空间会被送回空间页,因为一个刚注册的账户看到一份保研计划,
 * 会以为那是系统替他建的。
 *
 * ## 之前这里做了两件必须删掉的事
 *
 * 1. `send()` 在 `fetch` 失败后 `catch {}` 吞掉错误,然后根据几个中文关键词
 *    (推迟 / 太早 / 延后……)编一条回复。回复里甚至写着"这是本地演示建议",
 *    但用户看到的仍然是"AI 回答了我"。现在真实空间的发送失败就是失败:
 *    出错的气泡 + 重试按钮。
 * 2. `readWorkspaces` 读的时候就写 —— 没有空间就伪造一个 `primary`。
 *    那不是"默认值",那是编数据。
 */

type WorkspaceSnapshot = {
  growth: GrowthState;
  journals: JournalEntry[];
  conversations: Conversation[];
  settings: AISettings;
  messages: Message[];
  proposals: Proposal[];
  positions: Record<string, { x: number; y: number }>;
};

/**
 * 当前打开的是哪个空间。
 *
 * `kind` 是三种,不是一个布尔值 —— 因为一开始写成 `isReal: boolean` 的时候,
 * "还没有选空间"被塞进了 `isReal: false`,也就是**示例空间**那一支,
 * 于是没选空间就会看到示例数据。两种不同的情况共用一个布尔值,早晚会串。
 *
 * - `real`:后端里的空间,数据从 API 来。
 * - `demo`:示例空间,数据在浏览器里,界面上标着"示例"。
 * - `none`:还没选。**空状态,不是示例状态。**
 */
export type SpaceInfo = {
  id: string;
  title: string;
  intent: string;
  kind: 'real' | 'demo' | 'none';
};

const DEMO_SPACE: SpaceInfo = { id: 'primary', title: '示例空间', intent: '', kind: 'demo' };

/** 还没选空间时的占位。它**不产生任何数据**,只是让组件有个形状可依赖。 */
const NO_SPACE: SpaceInfo = { id: 'none', title: '', intent: '', kind: 'none' };

/** 需要"当前空间"才有意义的页面。没有空间时这些页面会把人送回空间页。 */
const ROUTES_NEEDING_A_SPACE = ['/workbench', '/today', '/journal', '/conversations'];
const DEFAULT_SETTINGS: AISettings = { mode: '教练', frequency: '中', proactive: true, adjust: true, critique: true, rest: true };
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/**
 * 本地计划快照存哪个键。
 *
 * 示例空间不按账户分:它是所有人共用的那一份演示数据,不属于任何一个账户,
 * 按账户分只会让"同一个示例空间在不同账户下长得不一样"。真实空间按账户分 ——
 * 同一台机器上换账户登录,绝不能看见上一个账户的计划。
 *
 * 写入和读取**必须**走同一个函数。分开写两处的时候很容易对不上,
 * 而对不上的表现是"改了没保存",很难查。
 *
 * **三种 kind 必须是三个键。** 以前这里是 `real ? 按账户 : 示例空间的键` ——
 * 于是"还没选空间"(`kind === 'none'`)和示例空间共用 `guest.primary`。而没选空间的
 * 时候画布上是一棵只有哨兵根节点的空树,那个空树被持久化到示例空间的键上,
 * 下一次进示例空间读到的就是它:`loadDemoSnapshot` 的守卫只看"有没有 goal 节点",
 * 空树正好有,于是守卫放行 —— **示例空间变成一片空白**,
 * 而界面上还写着"点击四个成长分类进入专属路径"。
 *
 * 触发条件比看上去宽:全新账户落在 `/spaces`,那一刻 `space` 就是 `none`,
 * 挂载即写入。所以只要新用户先看过空间列表,示例空间就没了。
 */
function storageKeyFor(user: AccountProfile | null, space: SpaceInfo): string {
  // 只有示例空间不按账户分:它是所有人共用的那一份演示数据,不属于任何一个账户,
  // 按账户分只会让"同一个示例空间在不同账户下长得不一样"。
  if (space.kind === 'demo') return workspaceStorageKey('guest', DEMO_SPACE.id);
  return workspaceStorageKey(user?.id ?? 'guest', space.id);
}
const exampleMessageIds = new Set(['m1', 'm2', 'm3', 'm4', 't1', 't2', 'c1', 'c2', 'f1', 'f2']);
const exampleConversationIds = new Set(['transformer', 'career', 'future']);
const exampleJournalIds = new Set(['journal-1', 'journal-2']);

/**
 * 今天的日期。
 *
 * 这里原来写死 `timeZone: 'Asia/Shanghai'`,而时间线那边用的是浏览器本地日期 ——
 * **同一个界面里有两个"今天"**。对国内用户两者恰好相同,所以这个分歧一直没被发现;
 * 出了东八区,随笔会记到"昨天"或"明天",而时间线上的"今天"在另一个位置。
 *
 * 客户端只有一个可靠的时区来源:浏览器。现在两边都走 `todayInTimeZone`。
 * (服务端记的是用户档案里的时区,那是它的真相;客户端要做的只是别自己另算一个。)
 */
function getBeijingDate(now = new Date()): string {
  return todayInTimeZone(now);
}

function restoreExampleMarkers(snapshot: WorkspaceSnapshot): WorkspaceSnapshot {
  const markMessage = (message: Message): Message => exampleMessageIds.has(message.id) ? { ...message, isExample: true } : message;
  const markedConversations = snapshot.conversations.map(conversation => ({
    ...conversation,
    isExample: exampleConversationIds.has(conversation.id) || conversation.isExample,
    messages: conversation.messages.map(markMessage),
  }));
  const conversations = markedConversations.some(conversation => !conversation.isExample)
    ? markedConversations.filter(conversation => !conversation.isExample)
    : markedConversations;
  return {
    ...snapshot,
    messages: snapshot.messages.map(markMessage),
    conversations,
    journals: snapshot.journals.map(journal => {
      if (exampleJournalIds.has(journal.id)) return { ...journal, isExample: true };
      if (!journal.isExample && journal.date === DEMO_TODAY) return { ...journal, date: getBeijingDate() };
      return journal;
    }),
  };
}

/** 示例数据里带日期的字段 —— 见 `mock/growth-state.ts`,节点上没有别的时间字段。 */
const DEMO_DATE_FIELDS = ['startDate', 'endDate', 'scheduledDate'] as const;

/**
 * 把示例计划的日期整体平移到**真实的今天**。
 *
 * 示例数据是**写死在 `DEMO_TODAY`(2026-09-16)** 那一天的:那天有两项"今天做"的
 * 行动、一条排在那天的"联系导师",以及一个刚结束的学期开头。而时间线 / 今天页 /
 * 任务视图已经改成按真实日期过滤,种子节点的日期却没有跟着迁移 —— 于是示例空间的
 * "今天"**永远是空的**:`/today` 说"今天没有安排任务",任务视图的"今天"是 0 项,
 * 专注流程根本进不去。示例空间自己的主打演示路径就这样废掉了,而界面上什么都看不出来。
 *
 * **每次加载都从种子重算,绝不在存储值上累加。** 示例空间是会被写进 localStorage 的,
 * 如果只在建快照时平移一次,用户第二天再打开,那份快照仍然锚在昨天 —— 还是"今天
 * 恒空",只是晚一天出现。所以判据是"这个节点还是种子里的那一份吗":是,就按
 * `种子日期 + 天数差` 重算(幂等,算多少遍结果都一样);不是 —— 用户在示例空间里
 * 自己建的节点,本来就排在真实日期上 —— 一个字段都不碰。
 */
function reanchorDemoDates(growth: GrowthState, today: string): GrowthState {
  const shift = daysBetween(DEMO_TODAY, today);
  if (!Number.isFinite(shift) || shift === 0) return growth;
  return {
    ...growth,
    nodes: Object.fromEntries(Object.entries(growth.nodes).map(([id, node]) => {
      const seed = initialGrowth.nodes[id];
      if (!seed) return [id, node];
      const moved = { ...node };
      for (const field of DEMO_DATE_FIELDS) {
        const from = seed[field];
        if (from) moved[field] = shiftDate(from, shift);
      }
      return [id, moved];
    })),
  };
}

/** 后端的 `MessageView` -> 界面用的 `Message`。id 用后端的,不用本地新生成的 ——
 *  否则刷新后重新拉历史,同一条消息会以另一个 id 再出现一次。 */
function toMessage(view: backend.MessageView): Message {
  return {
    id: view.id,
    // 后端目前只产出 user / assistant。真出现 system 时按助手样式展示,总好过丢掉。
    role: view.role === 'user' ? 'user' : 'assistant',
    text: view.content,
    contextId: view.contextNodeId ?? undefined,
    proposalId: view.proposalId ?? undefined,
    source: view.modelSource ?? undefined,
    degraded: view.degraded,
    degradedReason: view.degradedReason,
  };
}

/** 示例空间:整份都在浏览器里,和上一版一致。 */
function demoSnapshot(): WorkspaceSnapshot {
  return {
    // 日期要锚在真实的今天 —— 否则这份演示数据一打开就是"今天什么都没有"。
    growth: reanchorDemoDates(clone(initialGrowth), getBeijingDate()),
    journals: clone(initialJournals),
    conversations: clone(historyConversations),
    settings: { ...DEFAULT_SETTINGS },
    messages: clone(initialConversation.messages),
    proposals: [],
    positions: {},
  };
}

/**
 * 还没有计划时的那份本地快照 —— 只给示例空间和"还没选空间"用。
 *
 * 后端建空间时就是只建一个根 goal 节点,所以这里显示一个节点不是"编了一份计划",
 * 而是**如实显示后端真实存在的那一行**。区别在于:以前的版本会把 24 个保研节点灌进
 * 任何一个新空间,那些节点在后端根本不存在。
 *
 * **真实空间的计划不再走这里了。** 阶段 5 接上了 `GET /plan`,真实空间读的是那份
 * 载荷(见 `planToGrowth`),节点 id 是真实 UUID —— 所以 `sendReal` 里那句
 * "UUID 才发" 的限制,现在只在计划还没拉到的那个窗口里才可能生效。
 *
 * 这个本地根节点的 id 是哨兵值 `goal`。它**只**代表"计划还没到",不代表任何后端
 * 事实,所以不能拿它去比较 —— 判断"是不是在根这一层"要用 `growth.goalId`
 * (真实空间里那是个 UUID)。`currentSpaceId` 会把它挡在前面,调用方通常看不到它。
 */
function emptySnapshot(space: SpaceInfo, fallbackTitle = ''): WorkspaceSnapshot {
  const goal: GrowthNode = {
    id: 'goal',
    title: space.title || fallbackTitle,
    description: space.intent || undefined,
    type: 'goal',
    status: 'pending',
    priority: 'high',
  };
  return {
    growth: {
      id: `growth-${space.id}`,
      title: space.title,
      goalId: 'goal',
      currentStageId: 'goal',
      nodes: { goal },
      edges: [],
    },
    journals: [],
    conversations: [],
    settings: { ...DEFAULT_SETTINGS },
    messages: [],
    proposals: [],
    positions: {},
  };
}

/**
 * 真实空间的本地缓存。**只存两样东西:画布上手动拖过的位置、AI 设置。**
 *
 * 这两样是"属于这台浏览器"的偏好,后端没有它们的位置 —— 用户在路径图上把某个
 * 阶段拖到了别处,换台机器不该看到同一个位置。
 *
 * 计划、消息、提案**都不在这里**:
 *
 * - 计划由 `GET /plan` 提供(见 `growth`)。
 * - 消息由 `GET .../messages` 提供(见历史加载那段)。
 *
 * 上一版把整份计划图也写进 localStorage,于是刷新后的顺序是"先画出本地那份 →
 * 后端的数据到了再替换"。用户看到的是计划"闪了一下变了",而如果两份不一致,
 * 他会以为自己刚才的改动丢了。两个真相同时存在时,总有一个时刻在显示错的那个。
 */
function loadRealSnapshot(user: AccountProfile | null, space: SpaceInfo): WorkspaceSnapshot {
  const fallback = emptySnapshot(space);
  if (!user || typeof window === 'undefined') return fallback;
  try {
    const stored = localStorage.getItem(storageKeyFor(user, space));
    if (!stored) return fallback;
    const parsed = JSON.parse(stored) as Partial<WorkspaceSnapshot>;
    return {
      // 计划不从本地恢复 —— 见上面那段。这里的空树只是个形状,不是一份计划。
      growth: fallback.growth,
      journals: [],
      conversations: [],
      settings: parsed.settings ?? fallback.settings,
      messages: [],
      proposals: [],
      positions: parsed.positions ?? {},
    };
  } catch {
    return fallback;
  }
}

function loadDemoSnapshot(): WorkspaceSnapshot {
  const fallback = demoSnapshot();
  if (typeof window === 'undefined') return fallback;
  try {
    const stored = localStorage.getItem(storageKeyFor(null, DEMO_SPACE));
    if (!stored) return fallback;
    const parsed = JSON.parse(stored) as Partial<WorkspaceSnapshot>;
    // 守卫要能认出"这确实是示例空间自己存下来的那一份"。
    //
    // 以前这里只问"有没有 goal 节点" —— 而**任何**一份空快照都有 goal 节点
    // (哨兵根),所以别人的数据能冒充示例数据通过守卫。按 `id` 认:
    // 示例数据是 `growth-1`(`mock/growth-state.ts`),空树是 `growth-none`,
    // 真实空间是 `growth-<uuid>`。认出来不对就退回内置的那一份,
    // 并且**把被污染的键清掉** —— 否则每次进示例空间都要再判一次,
    // 而用户明明什么都没做错。
    if (parsed.growth?.id !== initialGrowth.id) {
      localStorage.removeItem(storageKeyFor(null, DEMO_SPACE));
      return fallback;
    }
    return restoreExampleMarkers({
      // 存下来的那份可能锚在昨天(用户昨天打开过) —— 从种子重算一次,
      // 而不是信任存储值。见 `reanchorDemoDates`。
      growth: reanchorDemoDates(parsed.growth, getBeijingDate()),
      journals: parsed.journals ?? fallback.journals,
      conversations: parsed.conversations ?? fallback.conversations,
      settings: parsed.settings ?? fallback.settings,
      messages: parsed.messages ?? fallback.messages,
      proposals: parsed.proposals ?? [],
      positions: parsed.positions ?? {},
    });
  } catch {
    return fallback;
  }
}

function useWorkspaceState(user: AccountProfile | null, space: SpaceInfo) {
  const [seed] = useState(() => {
    if (space.kind === 'real') return loadRealSnapshot(user, space);
    if (space.kind === 'demo') return loadDemoSnapshot();
    // 还没选空间:空。**不是**示例数据 —— 这是这一版最容易搞错的地方。
    return emptySnapshot(space, user?.targetGoal || '还没有选择成长空间');
  });

  // 示例空间的计划在浏览器里,由 reducer 维护。
  const [demoGrowth, dispatch] = useReducer(growthReducer, seed.growth);

  // 真实空间的计划**在后端**。这一份是它的投影,只读。
  const [plan, setPlan] = useState<backend.PlanPayload | null>(null);
  const [planLoading, setPlanLoading] = useState(space.kind === 'real');
  const [planError, setPlanError] = useState<string | null>(null);
  const [planSaving, setPlanSaving] = useState(false);
  const isReal = space.kind === 'real';

  /**
   * 当前这份计划。
   *
   * **真实空间的 `growth` 是算出来的,不是存起来的。** 这是"一个状态,多个视图"
   * 在前端的落点:路径图、时间线、任务列表读的都是它,而它的唯一来源是 `/plan`。
   * 上一版把计划存在 localStorage 里并让 reducer 就地改,于是"界面上改了、后端不知道"
   * 成了常态 —— 而刷新之后那个改动会消失,用户只会觉得这个产品记不住东西。
   *
   * 计划还没拉到(或拉失败)时给一个只有根目标的空树,而不是留在上一份上:
   * 显示一份**可能已经过时**的计划比显示"正在读"更糟。
   */
  const growth = useMemo(
    () =>
      isReal ? (plan ? planToGrowth(plan, space.title) : emptyGrowth(space.title, space.intent)) : demoGrowth,
    [isReal, plan, space.title, space.intent, demoGrowth],
  );

  const [spaceId, setSpaceId] = useState('goal');

  /**
   * 当前显示的空间 id —— **保证指得到节点**。
   *
   * `spaceId` 是一个指针,而它有两种落后于 `growth` 的时机:
   *
   * 1. 计划刚从后端回来。那一帧 `growth` 已经从 `emptyGrowth`(根 id 是哨兵值
   *    `'goal'`)换成真实 UUID 的树,而 `spaceId` 还是 `'goal'`。
   * 2. 换空间 / 删掉当前空间所在的分支。
   *
   * 落后的那一帧里 `growth.nodes[spaceId]` 是 `undefined`。以前调用方直接把它当节点
   * 用(`growth.nodes[spaceId].title`、`add(growth.nodes[spaceId])`),于是**打开任何
   * 一个真实空间,工作台都会白屏** —— React 抛 "Cannot read properties of undefined",
   * 页面上是一句 "Application error: a client-side exception has occurred"。
   * 靠 effect 去同步 `spaceId` 救不了:effect 在渲染**之后**才跑,而崩溃发生在渲染中。
   *
   * 所以对外给的不是那个指针,而是它指向的结果:指针有效就用指针,无效就回落到根目标
   * (计划非空时它一定在)。`spaceId`/`setSpaceId` 退居为"用户点进了哪一个"的内部状态,
   * 只有 `enterSpace` 和删除后的回退会改它。
   */
  const currentSpaceId = growth.nodes[spaceId] ? spaceId : growth.goalId;

  const [files, setFiles] = useState<FileAsset[]>([]);
  const objectUrls = useRef(new Set<string>());
  useEffect(() => () => { objectUrls.current.forEach(url => URL.revokeObjectURL(url)); }, []);
  const [journals, setJournals] = useState<JournalEntry[]>(seed.journals);
  const [conversations, setConversations] = useState(seed.conversations);
  const [settings, setSettings] = useState<AISettings>(seed.settings);
  const [focus, setFocus] = useState({ nodeId: 'attention', seconds: 0, running: false });
  useEffect(() => { if (!focus.running) return; const timer = setInterval(() => setFocus(f => ({ ...f, seconds: f.seconds + 1 })), 1000); return () => clearInterval(timer); }, [focus.running]);
  const [selectedId, select] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>(seed.messages);
  const [proposals, setProposals] = useState<Proposal[]>(seed.proposals);
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>(seed.positions);
  const [impact, setImpact] = useState(false);
  const [previewProposalId, setPreviewProposalId] = useState<string | null>(null);

  // 对话状态。真实空间才用:加载中 / 发送中 / 这一轮的错误 / 还缺哪些规划条件。
  const [historyLoading, setHistoryLoading] = useState(space.kind === 'real');
  // 更早的消息没有随这次请求返回。界面据此说明"上面还有",而不是让对话从
  // 一句没头没尾的话开始。
  const [messagesTruncated, setMessagesTruncated] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [retryable, setRetryable] = useState(false);
  const [brief, setBrief] = useState<backend.BriefView | null>(null);
  const [lastFailed, setLastFailed] = useState<{ clientMessageId: string; text: string } | null>(null);

  /**
   * 后端里的提案。**和上面那个本地 `proposals` 不是一回事。**
   *
   * 本地那套是示例空间的演示数据(`{nodeId, originalStart, actions: PlanAction[]}`),
   * 描述的是"把某个节点的日期挪一挪"。后端的提案描述的是"AI 想对计划做这些变更",
   * 是一份带校验结果的变更集。硬塞进同一个类型只会让两边都变得说不清楚。
   */
  const [remoteProposals, setRemoteProposals] = useState<backend.ProposalView[]>([]);
  /**
   * 这一轮被校验挡下来的变更。**必须显示出来。**
   *
   * 不显示的话,用户看到的是"AI 回复了我一段话,但计划什么都没变",而真实原因是
   * 它提的变更不合法。他会以为是自己没说清楚,于是换个说法再说一遍 —— 而问题
   * 不在他说的话上。
   */
  const [proposalErrors, setProposalErrors] = useState<backend.SendMessageResponse['proposalErrors']>([]);
  const [deciding, setDeciding] = useState(false);
  /** 「按执行情况调整」的状态。`message` 是给用户看的那句话,成败都有。 */
  const [replanState, setReplanState] = useState<{ busy: boolean; message: string | null; degraded: boolean }>(
    { busy: false, message: null, degraded: false },
  );

  // 示例空间:整份状态都在浏览器里,照旧整个存下来。
  useEffect(() => {
    // "还没选空间"不写。它不是一份状态,是一份占位 —— 存下来只会污染别的键
    // (见 `storageKeyFor`)。
    if (!user || isReal || space.kind === 'none') return;
    const snapshot: WorkspaceSnapshot = {
      growth: demoGrowth, journals, conversations, settings, messages, proposals, positions,
    };
    localStorage.setItem(storageKeyFor(user, space), JSON.stringify(snapshot));
  }, [conversations, demoGrowth, isReal, journals, messages, positions, proposals, settings, user, space]);

  // 真实空间:只存属于浏览器的两样(见 `loadRealSnapshot`)。计划与消息都在后端。
  useEffect(() => {
    if (!user || !isReal) return;
    localStorage.setItem(storageKeyFor(user, space), JSON.stringify({ settings, positions }));
  }, [isReal, positions, settings, user, space]);

  // 计划。**这就是真实空间里计划的唯一来源。**
  useEffect(() => {
    if (!isReal) return;
    let cancelled = false;
    setPlanLoading(true);
    backend.getPlan(space.id)
      .then(view => {
        if (cancelled) return;
        setPlan(view);
        setBrief(view.brief);
        setPlanError(null);
      })
      .catch(cause => {
        if (cancelled) return;
        // 读不到计划时**不保留上一份**。显示一份可能已经过时的计划,比显示
        // "没读到"更糟 —— 用户会照着它做判断。
        setPlan(null);
        setPlanError(cause instanceof ApiError ? cause.message : '读取计划失败。');
      })
      .finally(() => { if (!cancelled) setPlanLoading(false); });
    return () => { cancelled = true; };
  }, [isReal, space.id]);

  // 当前正在看哪一层。根目标 id 只在计划**第一次到达**时从 `goal` 变成真实 UUID,
  // 之后每次写入后的重新拉取都不会变 —— 所以这个 effect 不会把用户从他已经
  // 进入的那个阶段里弹出来。
  useEffect(() => {
    if (growth.goalId) setSpaceId(growth.goalId);
  }, [growth.goalId]);

  const refreshProposals = useCallback(async () => {
    setRemoteProposals(await backend.listProposals(space.id));
  }, [space.id]);

  // 提案。和计划、历史一样,每次打开空间都重新拉 —— 上一轮没处理完的那一份,
  // 关掉标签页再回来还应该在,否则用户会以为它丢了。
  useEffect(() => {
    if (!isReal) return;
    let cancelled = false;
    backend.listProposals(space.id)
      .then(list => { if (!cancelled) setRemoteProposals(list); })
      .catch(() => { if (!cancelled) setRemoteProposals([]); });
    return () => { cancelled = true; };
  }, [isReal, space.id]);

  // 历史。后端是唯一真相,所以每次打开真实空间都重新拉一次。
  useEffect(() => {
    if (space.kind !== 'real') return;
    let cancelled = false;
    setHistoryLoading(true);
    backend.getConversation(space.id)
      .then(view => {
        if (cancelled) return;
        setMessages(view.messages.map(toMessage));
        setBrief(view.brief);
        setMessagesTruncated(view.truncated);
      })
      .catch(cause => {
        if (cancelled) return;
        setSendError(cause instanceof ApiError ? cause.message : '读取对话历史失败。');
      })
      .finally(() => { if (!cancelled) setHistoryLoading(false); });
    return () => { cancelled = true; };
  }, [space.id, space.kind]);

  function previewProposal(id: string) {
    const proposal = proposals.find(p => p.id === id && p.status === 'pending');
    if (!proposal) return;
    // 回到根那一层,再选中被改动的节点 —— 它在根空间里才看得见。
    setSpaceId(growth.goalId); select(proposal.nodeId); setPreviewProposalId(id); setImpact(true);
  }
  function enterSpace(id: string) { if (!growth.nodes[id]) return; setSpaceId(id); select(id); }
  function updatePlanMeta(title: string, targetYear: number, school: string, major: string) {
    dispatch({ type: 'UPDATE_PLAN_META', title, description: `${targetYear} · ${school} · ${major}` });
  }
  /**
   * 重新拉整份计划。
   *
   * 写入之后**不就地打补丁**,而是重取。理由是删一个节点会连带删掉整棵子树、
   * 清掉挂在它们上面的依赖,改一个节点会推动版本号 —— 在客户端复刻这套级联
   * 等于把服务端的语义抄一遍。抄写的版本早晚会和原件不一致,而不一致的那一刻
   * 界面上显示的东西就是错的,且不会有任何东西报错。一次往返换这个,值得。
   */
  const refreshPlan = useCallback(async () => {
    const fresh = await backend.getPlan(space.id);
    setPlan(fresh);
    setBrief(fresh.brief);
  }, [space.id]);

  /**
   * 真实空间的一次写入。失败**不吞**:把错误放进 `planError` 让界面显示出来。
   *
   * 吞掉的话,用户点了"完成"、界面没有任何反应、也没有任何提示 —— 他会以为
   * 自己没点中,于是再点一次。而真正发生的是写入失败了。
   */
  async function mutatePlan<T>(action: () => Promise<T>): Promise<T | null> {
    if (!isReal) return null;
    setPlanSaving(true);
    setPlanError(null);
    try {
      const result = await action();
      await refreshPlan();
      return result;
    } catch (cause) {
      setPlanError(cause instanceof ApiError ? cause.message : '保存失败,请重试。');
      return null;
    } finally {
      setPlanSaving(false);
    }
  }

  function updateNode(nodeId: string, patch: Extract<PlanAction, { type: 'UPDATE_NODE' }>['patch']) {
    if (isReal) {
      // 界面的 patch 里有 `startDate`/`endDate`,后端**没有这两个字段** ——
      // 排期是阶段 6 的事。静默丢掉它们是不行的(用户改了日期、界面说保存成功、
      // 而计划没动),所以这里显式只传后端认识的字段,日期那部分由调用方
      // (路径图的节点编辑器)按真实能力决定显示什么。
      void mutatePlan(() => backend.updateNode(space.id, nodeId, {
        title: patch.title,
        description: patch.description ?? null,
        priority: patch.priority,
        status: patch.status,
        deadline: patch.deadline === undefined ? undefined : patch.deadline || null,
        // **预计工时是排期的输入,不是装饰。** 少了它的叶子节点在排期里会变成一条
        // "这个任务没有工时"的缺口,一场都不会被排出来 —— 于是「今天」永远是空的,
        // 用户看不到任何可以勾的东西。所以这个字段必须能从这里发出去。
        estimateMinutes: patch.estimateMinutes === undefined ? undefined : patch.estimateMinutes,
      }));
      return;
    }
    dispatch({ type: 'UPDATE_NODE', nodeId, patch });
  }

  function setNodeStatus(nodeId: string, status: GrowthNode['status']) {
    if (isReal) {
      void mutatePlan(() => backend.updateNode(space.id, nodeId, { status }));
      return;
    }
    dispatch({ type: 'UPDATE_STATUS', nodeId, status });
  }

  /**
   * 新建一个节点。**返回它到底成没成。**
   *
   * 真实节点的 id 由后端生成,所以这里给不出 id(调用方也不需要)。但**必须给得出
   * "成没成"**:创建弹窗以前提交完就无条件关掉,而失败时错误进的是 `planError` ——
   * 显示在详情弹窗里,创建弹窗根本看不到。用户看到的是"弹窗关了,树上多了一片吗?
   * 没有" —— 于是他再点一次,再失败一次。
   */
  async function addNode(
    title: string,
    type: GrowthNode['type'] = 'task',
    description = '',
    estimateMinutes?: number | null,
  ): Promise<boolean> {
    if (!title.trim()) return false;
    if (isReal) {
      const created = await mutatePlan(() => backend.createNode(space.id, {
        parentId: currentSpaceId,
        title: title.trim(),
        nodeType: type,
        description: description.trim() || null,
        // 建的时候就能填工时 —— 建完再去详情里补,是"先创建一份排不进去的东西,
        // 再回来修"的两步路,而排期读的正是这个字段。
        estimateMinutes: estimateMinutes ?? null,
      }));
      return created !== null;
    }
    const parent = growth.nodes[currentSpaceId]; const id = crypto.randomUUID();
    // 示例空间里新建的节点排到今天 —— 用户是**现在**加的它。以前排到 `DEMO_TODAY`,
    // 也就是一个写死的过去日期,新加的叶子会立刻出现在时间线的三个月前。
    const today = todayInTimeZone();
    dispatch({ type: 'CREATE_NODE', node: { id, title: title.trim(), description: description.trim() || undefined, type, parentId: currentSpaceId, category: parent.category ?? 'personal', stageId: growth.currentStageId, status: 'pending', priority: 'medium', startDate: today, endDate: today, scheduledDate: today } });
    select(id); return true;
  }

  function deleteNode(nodeId: string) {
    const node = growth.nodes[nodeId];
    if (!node || nodeId === growth.goalId) return;

    if (isReal) {
      // 本地的选中态与画布位置可以立刻清掉:它们不依赖后端是否成功,而且
      // 保留一个指向"正在被删的节点"的选中态会让详情面板闪一下空白。
      // **计划本身不动** —— 以 `refreshPlan` 回来的那份为准。
      if (selectedId === nodeId) select(null);
      setPositions(old => Object.fromEntries(Object.entries(old).filter(([key]) => !key.endsWith(`:${nodeId}`))));
      void mutatePlan(() => backend.deleteNode(space.id, nodeId));
      return;
    }

    const branch = collectNodeBranch(growth, nodeId);
    const parentId = node.parentId && growth.nodes[node.parentId] ? node.parentId : growth.goalId;
    dispatch({ type: 'DELETE_NODE', nodeId });
    if (selectedId && branch.has(selectedId)) select(null);
    if (branch.has(currentSpaceId)) { setSpaceId(parentId); select(parentId); }
    if (branch.has(focus.nodeId)) setFocus(old => ({ ...old, nodeId: parentId, running: false }));
    setPositions(old => Object.fromEntries(Object.entries(old).filter(([key]) => !key.split(':').some(id => branch.has(id)))));
    setFiles(old => old.filter(asset => {
      if (!branch.has(asset.ownerId)) return true;
      URL.revokeObjectURL(asset.url); objectUrls.current.delete(asset.url); return false;
    }));
    setProposals(old => old.filter(proposal => !branch.has(proposal.nodeId)));
    setPreviewProposalId(null); setImpact(false);
    setJournals(old => old.map(journal => ({ ...journal, linkedNodeIds: journal.linkedNodeIds.filter(id => !branch.has(id)) })));
    setConversations(old => old.map(conversation => ({ ...conversation, linkedNodeIds: conversation.linkedNodeIds.filter(id => !branch.has(id)) })));
    setMessages(old => old.map(message => message.contextId && branch.has(message.contextId)
      ? { ...message, contextId: undefined, proposalId: undefined }
      : message));
  }
  function addFiles(ownerId: string, incoming: File[]) {
    const assets = incoming.map(file => { const url = URL.createObjectURL(file); objectUrls.current.add(url); return { id: crypto.randomUUID(), ownerId, name: file.name, size: file.size, mime: file.type, url, file }; });
    setFiles(old => [...old, ...assets]);
  }
  function removeFile(id: string) { const asset = files.find(f => f.id === id); if (asset) { URL.revokeObjectURL(asset.url); objectUrls.current.delete(asset.url); } setFiles(old => old.filter(f => f.id !== id)); }
  function publishJournal(content: string, tags: string[], linkedNodeIds: string[], images: File[]) {
    const id = crypto.randomUUID(); setJournals(old => [{ id, content: content.trim(), tags, linkedNodeIds, date: getBeijingDate() }, ...old]); addFiles(id, images);
  }
  function sendHistory(id: string, text: string) {
    if (id === 'admission') { void send(text); return; }
    setConversations(old => old.map(c => c.id !== id ? c : { ...c, messages: [...c.messages,
      { id: crypto.randomUUID(), role: 'user', text },
      { id: crypto.randomUUID(), role: 'assistant', text: `我们可以继续聊「${c.title}」。先把你最在意的问题变成一个小行动，再回到关联空间安排它。\n\n这是示例空间的本地回复，不是模型生成的。` },
    ] }));
  }
  const append = (message: Omit<Message, 'id'>) => setMessages(old => [...old, { ...message, id: crypto.randomUUID() }]);

  /**
   * 计划变更的统一入口。
   *
   * ## 真实空间里,这里**不再**什么都接
   *
   * 上一版这个函数无条件 `dispatch(action)`,把变更写进本地 reducer。接上后端之后
   * 那样做就成了一句谎话:界面上的任务勾上了、后端不知道,刷新就退回去。
   *
   * 所以按动作分流:
   *
   * - `UPDATE_STATUS`(勾选完成)→ **直接保存**。这是用户明确的动作,属于产品
   *   规定里"用户自己勾选完成可以直接保存"的那一类,不经过提案。
   * - `UPDATE_TIME`(在时间线上拖日期)→ **如实说不支持**。它需要的是"排期"
   *   (哪天做、做多久),而后端现在只有 `deadline`(截止日)——两个不同的东西。
   *   把它当截止日写进去会静默改掉用户设的截止时间,而界面上的说辞是"调整了安排"。
   *   排期在阶段 6 接入,那时这个方法会真的有东西可写。
   * - `UPDATE_PLAN_META` 与其余本地动作只在示例空间里走 reducer。
   */
  function apply(action: PlanAction) {
    if (isReal) {
      if (action.type === 'UPDATE_STATUS') { setNodeStatus(action.nodeId, action.status); return; }
      if (action.type === 'UPDATE_TIME') {
        setPlanError('现在还不能直接拖日期改安排 —— 计划里只有截止时间,还没有排出来的具体时段。');
        return;
      }
      return;
    }
    dispatch(action);
    if (action.type === 'UPDATE_TIME') {
      setProposals(old => old.map(p => p.nodeId === action.nodeId && p.status === 'pending' ? { ...p, status: 'outdated' } : p));
      const node = growth.nodes[action.nodeId];
      if (node.id === 'project' && action.startDate >= '2026-12-01' && action.startDate <= '2027-01-15') {
        const id = crypto.randomUUID();
        setProposals(old => [...old, { id, nodeId: node.id, originalStart: action.startDate, status: 'pending', actions: [{ type: 'UPDATE_TIME', nodeId: node.id, startDate: '2027-01-18', endDate: '2027-02-28' }] }]);
        append({ role: 'assistant', contextId: node.id, proposalId: id, text: `你把「科研项目」调整到了 ${Number(action.startDate.slice(5, 7))} 月。\n\n这可能与期末复习阶段产生时间冲突。\n\n我建议保留 10 月的导师联系，但将正式科研项目调整到寒假。` });
      } else append({ role: 'assistant', contextId: node.id, text: `已将「${node.title}」调整为 ${action.startDate} 至 ${action.endDate}。路径、时间线和任务会同步读取这次变更。` });
    }
  }
  async function accept(id: string) {
    const proposal = proposals.find(p => p.id === id);
    if (!proposal || proposal.status !== 'pending') return;
    proposal.actions.forEach(dispatch);
    setProposals(old => old.map(p => p.id === id ? { ...p, status: 'accepted' } : p.nodeId === proposal.nodeId && p.status === 'pending' ? { ...p, status: 'outdated' } : p));
    setImpact(false);
    const change = proposal.actions.find(a => a.type === 'UPDATE_TIME');
    append({ role: 'assistant', text: change ? `已将「${growth.nodes[proposal.nodeId].title}」安排在 ${change.startDate} 至 ${change.endDate}。其他安排保持不变，路径、时间线和任务已同步。` : '已接受调整。', contextId: proposal.nodeId });
  }

  /**
   * 幂等键:每份提案一个,生成一次之后**重试复用同一个**。
   *
   * 每次点击都新生成一个的话,用户双击"确认"就是两次不同的请求 —— 后端两道闸门
   * (守卫写 + 幂等台账)本来就是为了挡住这件事,而一个每次都变的键会让它们都失效。
   */
  const decisionKeys = useRef(new Map<string, string>());
  function keyFor(proposalId: string): string {
    const existing = decisionKeys.current.get(proposalId);
    if (existing) return existing;
    const generated = crypto.randomUUID();
    decisionKeys.current.set(proposalId, generated);
    return generated;
  }

  /**
   * 确认一份提案 —— **用户点了按钮才走到这里,模型不能替用户调**。
   *
   * 成功后重新拉计划与提案。计划那一步是关键:确认真正改变了节点树,而界面上
   * 那棵树是从 `/plan` 算出来的,不重拉的话用户会看到"确认成功了但什么都没变"。
   */
  async function confirmRemote(proposalId: string): Promise<boolean> {
    setDeciding(true);
    setPlanError(null);
    try {
      await backend.confirmProposal(space.id, proposalId, keyFor(proposalId));
      await Promise.all([refreshPlan(), refreshProposals()]);
      return true;
    } catch (cause) {
      // 失败的原因必须原样告诉用户:**计划在提案生成后被人改过**(409
      // STALE_BASE_REVISION)和"网络断了"要做的事完全不同 —— 前者要重新生成,
      // 后者重试就行。后端给的 message 本来就是写给用户看的中文,直接用。
      setPlanError(cause instanceof ApiError ? cause.message : '确认失败,请重试。');
      await refreshProposals().catch(() => undefined);
      return false;
    } finally {
      setDeciding(false);
    }
  }

  async function rejectRemote(proposalId: string): Promise<boolean> {
    setDeciding(true);
    setPlanError(null);
    try {
      await backend.rejectProposal(space.id, proposalId);
      await refreshProposals();
      return true;
    } catch (cause) {
      setPlanError(cause instanceof ApiError ? cause.message : '操作失败,请重试。');
      return false;
    } finally {
      setDeciding(false);
    }
  }

  /**
   * 按最近的执行情况请 AI 提一份调整方案。
   *
   * **它不写任何东西。** 后端把偏差事实交给模型,模型提的变更走的是**和对话里
   * 完全相同的**校验与落库路径 —— 落成一份 `validated` 的提案,等用户点确认。
   * 所以这里做完只要重拉提案列表,不需要重拉计划:计划还没变。
   *
   * `message` 无论成败都会带回来,而且**降级时它说的是"这次没能给出调整方案"**,
   * 不是"系统认为不需要调整"。这两句话在界面上的区别就是这个字段存在的理由 ——
   * 前者要用户重试,后者要用户放心,把它们显示成同一句空话是最坏的结果。
   */
  async function replan() {
    if (replanState.busy) return;
    setReplanState({ busy: true, message: null, degraded: false });
    try {
      const result = await backend.replan(space.id);
      await refreshProposals();
      setReplanState({ busy: false, message: result.message, degraded: result.degraded });
    } catch (cause) {
      setReplanState({
        busy: false,
        message: cause instanceof ApiError ? cause.message : '调整失败,请重试。',
        degraded: true,
      });
    }
  }

  /**
   * 真实空间的一轮对话。
   *
   * `clientMessageId` 由调用方给,重试时**必须**用同一个 —— 后端靠它去重,
   * 换一个就等于告诉后端"这是一条新消息",网络超时重发就会变成两条。
   */
  async function sendReal(text: string, clientMessageId: string) {
    setSending(true);
    setSendError(null);
    setRetryable(false);
    // 上一轮的校验错误不再"当前"。留在屏幕上的话,用户发完新的一句会以为
    // **这一轮**又没通过 —— 而它其实是上一轮的陈旧内容。
    setProposalErrors([]);

    // 用户的话先上屏。后端是先落库再调模型的,所以这条气泡背后的那一行一定存在,
    // 哪怕模型超时 —— 这正是"模型失败不能丢掉用户输入"在界面上的样子。
    const optimisticId = `pending-${clientMessageId}`;
    const optimistic: Message = {
      id: optimisticId,
      role: 'user',
      text,
      contextId: selectedId ?? undefined,
      pending: true,
    };
    setMessages(old => [...old.filter(m => m.id !== optimisticId), optimistic]);

    try {
      const result = await backend.sendMessage(space.id, {
        content: text,
        clientMessageId,
        // 只有真实的节点 UUID 才发。本地计划图里的 id(`goal`)后端不认识,
        // 发过去是 422。阶段 5 本地节点都换成真实 id 之后,这个判断自然就总为真。
        contextNodeId: selectedId && UUID_RE.test(selectedId) ? selectedId : null,
        currentView: 'workbench',
      });
      setMessages(old => [
        ...old.filter(m => m.id !== optimisticId),
        toMessage(result.userMessage),
        toMessage(result.assistantMessage),
      ]);
      setBrief(result.brief);
      setProposalErrors(result.proposalErrors);
      setLastFailed(null);
      // 这一轮可能产出了一份新的待确认提案。重新拉一次,而不是把 `result.proposal`
      // 塞进数组:后者会和"打开空间时那一份"用两套代码维护同一个列表。
      await refreshProposals();
    } catch (cause) {
      const error = cause instanceof ApiError ? cause : null;
      setMessages(old => old.map(m => m.id === optimisticId ? { ...m, pending: false, failed: true } : m));
      setSendError(error?.message ?? '发送失败。');
      setRetryable(error ? error.retryable : true);
      setLastFailed({ clientMessageId, text });
    } finally {
      setSending(false);
    }
  }

  /** 示例空间的一轮对话。本地生成,**界面上标着"示例"**,不假装是模型。 */
  function sendDemo(text: string) {
    const node = selectedId ? growth.nodes[selectedId] : null;
    append({ role: 'user', text, contextId: selectedId ?? undefined });
    if (node?.startDate && node.endDate && /推迟|太早|晚一点|延后|往后/.test(text)) {
      const requestedFebruary = /2\s*月|二月/.test(text);
      const startDate = requestedFebruary && `${node.startDate.slice(0,4)}-02-01` > node.startDate
        ? `${node.startDate.slice(0,4)}-02-01`
        : requestedFebruary ? `${Number(node.startDate.slice(0,4)) + 1}-02-01` : shiftDate(node.startDate, 30);
      const endDate = shiftDate(node.endDate, daysBetween(node.startDate, startDate));
      const id = crypto.randomUUID();
      setProposals(old => [...old.map(p => p.nodeId === node.id && p.status === 'pending' ? { ...p, status: 'outdated' as const } : p), { id, nodeId: node.id, originalStart: node.startDate!, status: 'pending', actions: [{ type: 'UPDATE_TIME', nodeId: node.id, startDate, endDate }] }]);
      append({ role: 'assistant', contextId: node.id, proposalId: id, text: `可以先预览把「${node.title}」推迟到 ${startDate} 的安排，持续时间保持不变。\n\n${node.id === 'project' ? '科研启动延后可能压缩后续论文产出和夏令营准备时间，建议提前保留导师沟通与文献阅读。' : '延后可能压缩后续安排的准备时间，建议确认相关节点是否需要同步调整。'}其他任务暂不变动。\n\n这是示例空间的本地建议，不是模型生成的；查看影响不会修改计划，接受后才会更新。` });
      return;
    }
    append({ role: 'assistant', contextId: selectedId ?? undefined, text: node
      ? `我们可以围绕「${node.title}」继续梳理。${node.description || '先确定一个足够小、可以开始的行动。'}\n\n这是示例空间的本地回复，不是模型生成的。想看真实的 AI 对话，请在「成长空间」里新建一个空间。`
      : '当前是示例空间。想看真实的 AI 对话，请在「成长空间」里新建一个空间 —— 示例空间里的回复是本地写好的，不会经过模型。' });
  }

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || sending) return;
    // 只有真实空间能发。示例空间走本地演示,还没选空间的话没有地方可发 ——
    // 静默丢掉比编一句回复好;界面在这种状态下本来就会把人送回空间页。
    if (space.kind === 'demo') { sendDemo(trimmed); return; }
    if (space.kind !== 'real') return;
    await sendReal(trimmed, crypto.randomUUID());
  }

  /** 重试上一轮。复用同一个 `clientMessageId`,所以不会重复落库。 */
  async function retry() {
    if (!lastFailed || sending) return;
    setMessages(old => old.filter(m => !m.failed));
    await sendReal(lastFailed.text, lastFailed.clientMessageId);
  }

  return { growth, workspaceId: space.id, isRealSpace: space.kind === 'real', apply, selectedId, select, messages, proposals, accept, send, retry, sending, sendError, retryable, brief, historyLoading, messagesTruncated, positions, setPositions, impact, setImpact, previewProposalId, previewProposal,
    // 计划。`revisionVersion` 是"你眼前这份是第几版" —— 界面上比对提案的
    // `baseRevisionVersion` 用它,能在发请求**之前**发现"你看的那份已经旧了"。
    plan, planLoading, planError, planSaving, setPlanError, revisionVersion: plan?.revisionVersion ?? 0,
    // `refreshPlan` 也对外给出去:排期应用之后要重拉计划,而那条路径在组件里
    // (它还要显示"这次动了多少场"),不该为了统一而塞进 `mutatePlan` 的通用错误处理。
    refreshPlan,
    // 提案
    remoteProposals, proposalErrors, deciding, confirmRemote, rejectRemote,
    replan, replanState,
    // 对外给的是**算出来**的那个(见 `currentSpaceId`)。调用方拿它去
    // `growth.nodes[spaceId]` 是安全的,这是这个字段的契约。
    spaceId: currentSpaceId, enterSpace, updatePlanMeta, updateNode, setNodeStatus, addNode, deleteNode, files, addFiles, removeFile, journals, publishJournal, conversations, setConversations, sendHistory, settings, setSettings, focus, setFocus };
}
const Context = createContext<ReturnType<typeof useWorkspaceState> | null>(null);

/**
 * 决定当前打开哪个空间。
 *
 * 顺序是:`?workspace=` 参数 -> 上次打开的空间(本地记住的)-> 都没有就是"还没有选"。
 *
 * **没有"默认空间"这一步**。上一版默认落到 `primary`(示例空间),于是一个刚注册、
 * 一个空间都没有的账户,第一次打开工作台看到的是 24 个别人的保研节点。
 * 现在这种情况会把人送回空间页 —— 那里有"创建一个"的引导,那是诚实的。
 *
 * ## 这里最容易写错的一件事:别把整棵树挡住
 *
 * 我第一版写的是「没有 space 就渲染加载屏」。那是错的:这个 Provider 包着
 * **整个应用**,包括登录页和空间页。于是没有空间时,登录页也被换成了加载屏 ——
 * 用户看到一个永远转不完的圈,连登录框都出不来。
 *
 * 正确的分法是三种情况各走各的:
 *   - 没登录 -> 直接放行(登录页只需要 useAuth)。
 *   - 有空间 -> 正常挂载。
 *   - 没空间 -> **照样挂载**,只是内容是空的。只有"必须有空间才有意义"的那几个
 *     页面会被送回空间页;空间页本身照常渲染它的"创建一个"引导。
 */
function WorkspaceRouter({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const requested = params.get('workspace');
  const userId = user?.id ?? 'guest';
  const activeKey = `zhitu.active.workspace.${userId}`;
  const [space, setSpace] = useState<SpaceInfo | null>(null);

  useEffect(() => {
    if (!user) { setSpace(null); return; }
    // 示例空间要显式进入(`?workspace=primary`),不会被当成默认值。
    if (requested === DEMO_SPACE.id) { setSpace(DEMO_SPACE); return; }
    const id = requested ?? (typeof window === 'undefined' ? null : localStorage.getItem(activeKey));
    if (!id) {
      setSpace(null);
      // 只有需要空间的页面才跳走。空间页、我的页在这里没有空间也讲得通。
      if (ROUTES_NEEDING_A_SPACE.some(route => pathname.startsWith(route))) router.replace('/spaces');
      return;
    }
    let cancelled = false;
    backend.getWorkspace(id)
      .then(detail => {
        if (cancelled) return;
        localStorage.setItem(activeKey, id);
        setSpace({ id: detail.id, title: detail.title, intent: detail.intent, kind: 'real' });
      })
      .catch(cause => {
        if (cancelled) return;
        // 空间不存在、或者不是这个账户的。两种情况都清掉记住的 id ——
        // 留着一个打不开的 id,每次进工作台都会再失败一次。
        localStorage.removeItem(activeKey);
        setSpace(null);
        if (ROUTES_NEEDING_A_SPACE.some(route => pathname.startsWith(route))) {
          router.replace(`/spaces?unopened=${encodeURIComponent(id)}`);
        } else {
          console.warn('打不开这个成长空间:', cause instanceof ApiError ? cause.message : cause);
        }
      });
    return () => { cancelled = true; };
  }, [activeKey, pathname, requested, router, user]);

  // 没登录:不挂 Provider。**"没登录就没有成长空间数据"是这个应用该有的不变量** ——
  // 任何在未登录时调用 useDemo() 的组件都会立刻抛错,而不是拿到一份编出来的数据。
  if (!user) return <>{children}</>;

  // key 里带上空间 id:换空间就是换一份状态,不复用上一个空间的 reducer。
  return (
    <WorkspaceStateProvider key={space?.id ?? NO_SPACE.id} user={user} space={space ?? NO_SPACE}>
      {children}
    </WorkspaceStateProvider>
  );
}

function WorkspaceStateProvider({ children, user, space }: { children: ReactNode; user: AccountProfile | null; space: SpaceInfo }) {
  const value = useWorkspaceState(user, space);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function DemoProvider({ children }: { children: ReactNode }) {
  return <Suspense fallback={<div className="loading">正在打开成长空间…</div>}><WorkspaceRouter>{children}</WorkspaceRouter></Suspense>;
}
export function useDemo() { const context = useContext(Context); if (!context) throw new Error('DemoProvider missing'); return context; }
