'use client';
import { Suspense, createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode, type SetStateAction } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import type { AISettings, Conversation, FileAsset, GrowthNode, GrowthRelationType, JournalEntry, Message, PlanAction } from '@/types/growth';
import { dayNumber, todayInTimeZone } from './timeline';
import { PLACEHOLDER_ROOT_ID, emptyGrowth, planToGrowth } from './planProjection';
import { useAuth } from '@/features/auth/provider';
import type { AccountProfile } from '@/features/auth/types';
import { workspaceStorageKey } from './workspaces';
import * as backend from '@/lib/backend';
import type { RelationPayload } from '@/lib/backend';
import { ApiError, getToken } from '@/lib/api';

/**
 * 成长空间状态。
 *
 * ## 现在只有一种空间:真实空间
 *
 * **真实空间**(用户从"成长空间"里建的那个,id 是后端 UUID):对话完全走后端 ——
 * 历史读 `GET /api/workspaces/{id}/messages`,每一轮发 `POST .../messages`。
 * 计划图从 `GET /plan` 来(见 `planToGrowth`),`/plan` 是它的唯一来源。
 *
 * **示例空间已经整个删掉了。** 它曾经是"`workspace === 'primary'` 时才加载"的
 * 一份本地演示数据:整棵树、对话、随笔都在浏览器里,回复是写死的。
 * 它同时带来了四件事,删掉它一次解决四件:
 *
 * 1. localStorage 充当业务主存储 —— 于是"界面改了、库里没有"成了常态;
 * 2. 硬编码的四分类,和"用户自己建图"这个方向直接冲突;
 * 3. 假 AI 回复:文案里写着"本地演示建议",但用户看到的仍然是"AI 回答了我";
 * 4. 假 Today 观察段:它说"你今天课程安排比较满""我把科研任务降低到了一个",
 *    而知途从来没有拿到过用户的课表,也没替谁做过这个决定。
 *
 * 删掉之后这里剩下的东西只有一条规矩:**界面上的每一个数字都必须来自后端**。
 *
 * ## 这里做过、也必须继续避免的两件事
 *
 * 1. `send()` 在 `fetch` 失败后 `catch {}` 吞掉错误,然后根据几个中文关键词
 *    (推迟 / 太早 / 延后……)编一条回复。现在发送失败就是失败:
 *    出错的气泡 + 重试按钮。
 * 2. `readWorkspaces` 读的时候就写 —— 没有空间就伪造一个 `primary`。
 *    那不是"默认值",那是编数据。那个函数和它伪造的东西一起没了(没有空间就是
 *    没有空间,界面说"还没有空间"),但**这条规则还在**:读接口不写库。
 */

/**
 * 存在这台浏览器里的东西。
 *
 * **只有这两样。** 它们是"属于这台机器"的偏好,后端没有它们的位置 —— 用户在路径图上
 * 把某个阶段拖到了别处,换台机器不该看到同一个位置。计划、消息、提案都不在这里:
 * 计划由 `GET /plan` 提供,消息由 `GET .../messages` 提供。
 *
 * 上一版这里还装着整份计划图(`growth`/`journals`/`conversations`/`messages`/
 * `proposals`)—— 那是示例空间要整份存进 localStorage 才需要的形状。示例空间没了,
 * 那些字段就没有生产者了,留着只会让人以为"本地还存着一份计划"。
 */
type LocalPrefs = {
  settings: AISettings;
  positions: Record<string, { x: number; y: number }>;
};

/**
 * 当前打开的是哪个空间。
 *
 * `kind` 曾经是三种 —— `real` / `demo` / `none`,而且一开始是 `isReal: boolean`。
 * 那个布尔值正是"没选空间"落到示例空间那一支的原因:两种不同的情况共用一个布尔值,
 * 早晚会串。示例空间删掉之后 `demo` 这一档不存在了,但**保留 `none` 这一档**,
 * 因为它不是示例状态,是空状态。
 *
 * - `real`:后端里的空间,数据从 API 来。
 * - `none`:还没选。**空状态,不是示例状态。**
 */
export type SpaceInfo = {
  id: string;
  title: string;
  intent: string;
  kind: 'real' | 'none';
};

/** 还没选空间时的占位。它**不产生任何数据**,只是让组件有个形状可依赖。 */
const NO_SPACE: SpaceInfo = { id: 'none', title: '', intent: '', kind: 'none' };

/** 需要"当前空间"才有意义的页面。没有空间时这些页面会把人送回空间页。 */
const ROUTES_NEEDING_A_SPACE = ['/workbench', '/today', '/journal', '/conversations'];
const DEFAULT_SETTINGS: AISettings = { mode: '教练', frequency: '中', proactive: true, adjust: true, critique: true, rest: true };
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * 本地偏好(设置 + 画布位置)存哪个键。**按账户分。**
 *
 * 同一台机器上换账户登录,绝不能看见上一个账户的数据 —— 这条在示例空间还在的时候
 * 是"按账户分"和"共用一个键"两种做法并存(示例空间是所有人共用的那一份演示数据,
 * 所以刻意不按账户分)。示例空间删掉之后只剩一种做法:按账户分,没有例外。
 *
 * 写入和读取**必须**走同一个函数。分开写两处的时候很容易对不上,
 * 而对不上的表现是"改了没保存",很难查。
 */
function storageKeyFor(user: AccountProfile | null, space: SpaceInfo): string {
  return workspaceStorageKey(user?.id ?? 'guest', space.id);
}

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

/**
 * 读这台浏览器上存着的那两样偏好。
 *
 * 读失败(JSON 坏了、存的是上一版的形状)就退回默认值 —— **不抛错**。
 * 偏好读不出来不该让整个工作台打不开:位置和设置都能重新设一次,计划不能重来,
 * 而计划在后端,不依赖这里。
 *
 * 兼容性:上一版往同一个键里存的是整份快照(带 `growth`/`messages`/…)。
 * 这里只取 `settings` 和 `positions`,多出来的字段自然被忽略,所以老数据不会炸。
 */
function loadLocalPrefs(user: AccountProfile | null, space: SpaceInfo): LocalPrefs {
  const fallback: LocalPrefs = { settings: { ...DEFAULT_SETTINGS }, positions: {} };
  if (!user || typeof window === 'undefined') return fallback;
  try {
    const stored = localStorage.getItem(storageKeyFor(user, space));
    if (!stored) return fallback;
    const parsed = JSON.parse(stored) as Partial<LocalPrefs>;
    return { settings: parsed.settings ?? fallback.settings, positions: parsed.positions ?? {} };
  } catch {
    return fallback;
  }
}

/**
 * 攒多久再发一次布局。
 *
 * 拖动一次会连续产生几十个位置,而整份提交的语义已经是"最后一次赢"
 * (见 `PutLayoutRequest` 的注释)—— 中间那些不必上路。600 毫秒是"用户停手了"的
 * 一个大致刻度:比一次拖动的间隔长,比人再去点别的东西快。
 */
const LAYOUT_SAVE_DEBOUNCE_MS = 600;

/** 两份位置表是不是一模一样。见 `setPositions` 里为什么需要它。 */
function samePositions(
  a: Record<string, { x: number; y: number }>,
  b: Record<string, { x: number; y: number }>,
): boolean {
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every(key => b[key]?.x === a[key]?.x && b[key]?.y === a[key]?.y);
}

function useWorkspaceState(user: AccountProfile | null, space: SpaceInfo) {
  const [seed] = useState(() => loadLocalPrefs(user, space));

  // 真实空间的计划**在后端**。这一份是它的投影,只读。
  const [plan, setPlan] = useState<backend.PlanPayload | null>(null);
  const [planLoading, setPlanLoading] = useState(space.kind === 'real');
  const [planError, setPlanError] = useState<string | null>(null);
  const [planSaving, setPlanSaving] = useState(false);
  const isReal = space.kind === 'real';

  /**
   * 当前这份计划。
   *
   * **`growth` 是算出来的,不是存起来的。** 这是"一个状态,多个视图"在前端的落点:
   * 路径图、时间线、任务列表读的都是它,而它的唯一来源是 `/plan`。
   * 上一版把计划存在 localStorage 里并让 reducer 就地改,于是"界面上改了、后端不知道"
   * 成了常态 —— 而刷新之后那个改动会消失,用户只会觉得这个产品记不住东西。
   *
   * 计划还没拉到(或拉失败)时给一个只有根目标的空树,而不是留在上一份上:
   * 显示一份**可能已经过时**的计划比显示"正在读"更糟。
   */
  const growth = useMemo(
    () => (plan ? planToGrowth(plan, space.title) : emptyGrowth(space.title, space.intent)),
    [plan, space.title, space.intent],
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

  /**
   * 画布子树的 `key`。**它和 `currentSpaceId` 只差一件事,而那一件事是刻意的。**
   *
   * `key` 的作用是"换一层就换一份状态":用户进了子空间,画布上那个还没提交的弹窗、
   * ReactFlow 内部那份平移缩放,都不该跟着过去。这个隔离要留着。
   *
   * 但**哨兵值不算"换了一层"**。计划第一次到达之前,`currentSpaceId` 是
   * `PLACEHOLDER_ROOT_ID`;计划一到位,它变成根节点的真实 UUID —— 那一刻画布子树被
   * 重建一次,而用户什么都没做。今天这一下丢不掉东西(那时"新建节点"还是禁用的,
   * 见 `PathView` 里的 `canCreate`),但它是**每一次打开工作台都要交的一笔账**:
   * ReactFlow 内部的视口、以及将来会长在这棵子树上的正文编辑器,全都跟着重建。
   *
   * 所以:该换层级时照换(指针指向了别处),读数抖一下时不换(退回哨兵值)。
   * 计划读失败时 `growth.goalId` 本身也会退回哨兵值,那时这里跟着退回 —— 那是同一层,
   * 只是这一次没读到,不该被当成用户换了层级。
   */
  const canvasKey = spaceId === PLACEHOLDER_ROOT_ID ? growth.goalId : spaceId;

  /**
   * 每个层级各自的视口(平移 + 缩放)。
   *
   * **按空间分开** —— 它在 Provider 里,而 Provider 的 `key` 是空间 id,
   * 所以换空间天然拿不到上一个空间的那一份。
   *
   * 它已经**不只是内存里的一份了**:2026-09-27 起会写进后端的 `scope_viewports`
   * (见下面"布局落库"那一段,`setScopeViewport` 也搬到了那里)。
   * 落 localStorage 仍然不做 —— 节点集合一变(补了节点、删了子树),上一次的视口
   * 就可能框住一片空白,而"打开是一张空图"比"重新 fit 一次"更难解释。
   * 跨设备那一份走后端,理由不同:那是用户在**另一台机器上**摆过的位置,不是这台
   * 机器的本地状态。
   */
  const [viewports, setViewportsRaw] = useState<Record<string, { x: number; y: number; zoom: number }>>({});
  // 时间线的那一份形状不同(`start` 是"从第几天开始看",`density` 是每天多少像素),
  // 所以不塞进上面那个表。它的初始值以前算在 `TimelineView` 里,现在挪到这里 ——
  // 不然切一次视图回去,时间线就跳回今天。
  const [timelineViewport, setTimelineViewport] = useState(() => ({ start: dayNumber(todayInTimeZone()) - 25, density: 4 }));

  const [files, setFiles] = useState<FileAsset[]>([]);
  const objectUrls = useRef(new Set<string>());
  useEffect(() => () => { objectUrls.current.forEach(url => URL.revokeObjectURL(url)); }, []);
  // 随笔、会话、消息、提案都**从空开始**。它们曾经从示例空间的种子里来;
  // 现在消息由 `GET .../messages` 填(见历史加载那段),随笔和会话是本地功能
  // (见 `publishJournal` / `setConversations`)。
  const [journals, setJournals] = useState<JournalEntry[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [settings, setSettings] = useState<AISettings>(seed.settings);
  const [focus, setFocus] = useState({ nodeId: '', seconds: 0, running: false });
  useEffect(() => { if (!focus.running) return; const timer = setInterval(() => setFocus(f => ({ ...f, seconds: f.seconds + 1 })), 1000); return () => clearInterval(timer); }, [focus.running]);
  const [selectedId, select] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [positions, setPositionsRaw] = useState<Record<string, { x: number; y: number }>>(seed.positions);

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
   * 提案。**只有后端这一份。**
   *
   * 这里曾经有两个同名的东西:一个本地的 `proposals`(示例空间的演示数据,
   * `{nodeId, originalStart, actions: PlanAction[]}`,描述"把某个节点的日期挪一挪"),
   * 和一个后端的 `remoteProposals`(AI 想对计划做哪些变更,带校验结果的变更集)。
   * 两者名字像、含义完全不同,所以后端的那个一直带着 `remote` 前缀。
   *
   * 示例空间删掉之后本地那份没有生产者了,`remote` 这个前缀也就失去了对照物 ——
   * 但**名字保留**:它在这份文件里到处出现,而改名是纯噪音的改动,不值得混在
   * 删除示例空间这一次里做。
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

  // 只存属于浏览器的两样(见 `LocalPrefs`)。计划与消息都在后端,不在这里。
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
  //
  // **哨兵值不算"用户换了层级"。** 计划读失败时 `growth.goalId` 会退回
  // `PLACEHOLDER_ROOT_ID`,这里如果照跟,用户会从他正待着的子空间里被弹回根 ——
  // 而"这一次没读到计划"根本不是他的操作。今天这条退回只发生在换空间/整页加载那
  // 几种本来就会重建整棵子树的时刻,所以还看不出后果;等步骤 3 给计划加上重试,
  // 它就会变成"每次后端抖一下,正在写的正文被弹走一次"。
  useEffect(() => {
    if (growth.goalId && growth.goalId !== PLACEHOLDER_ROOT_ID) setSpaceId(growth.goalId);
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

  function enterSpace(id: string) { if (!growth.nodes[id]) return; setSpaceId(id); select(id); }
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

  // ---------------------------------------------------------------------------------
  // 布局落库(步骤 3B)
  // ---------------------------------------------------------------------------------
  /**
   * 画布布局(**节点位置 + 每层视口**)的读写。
   *
   * 这一段要同时守住四件事,它们互相拉扯,所以放在一处、一次说清:
   *
   * 1. **串行**:同一时刻只允许一个 PUT 在飞。两个同时在飞时,先发的那个可能后落地
   *    —— 用户把节点拖到 A、再拖到 B,界面上一路都对,刷新却看到它在 A,
   *    而他明明看到过 B。
   * 2. **合并**:拖动会连续产生几十个中间位置,而整份提交的语义已经是"最后一次赢"
   *    (见 `PutLayoutRequest`)。中间那些不上路:攒一小会儿,只发最后一份。
   * 3. **按空间隔离**:切空间时,上一个空间还没发出去的那一份**绝不能**写进新空间。
   *    做法是让待发的数据留在闭包里 —— 定时器和它捕获的 `space.id` 是一起被丢掉的,
   *    新空间拿到的是新挂载的一套 ref。**卸载时故意不取消那个定时器**:它属于上一个
   *    空间,数据也只该进上一个空间,而"拖动之后立刻切走"正是必须存住的场景之一。
   * 4. **失败要看得见**:写不成就把话说明白,并留一个**能用**的重试。不自动重试 ——
   *    自动重试会把"后端一直存不上"变成"界面一切正常、刷新就丢"。
   */
  const [layoutError, setLayoutError] = useState<string | null>(null);
  /**
   * 后端那份布局**问过一次了**(成功、失败都算)。
   *
   * 它是给 `PathView` 的自动 fit 用的:后端那份还没回来就 fit,等于用一个默认视角
   * 盖掉用户上次摆好的位置 —— 而且那一次 fit 会被当成"用户的视口"存回去,
   * 把库里的那一份也改掉。见 `PathView` 里那个 effect 的第一行。
   */
  const [layoutReady, setLayoutReady] = useState(!isReal);
  /** 读回来、还没用上的那一份。见下面那个合并 effect。 */
  const [serverLayout, setServerLayout] = useState<backend.LayoutPayload | null>(null);

  /** 有改动还没写进去。 */
  const dirty = useRef(false);
  /** 有一个 PUT 正在飞。**它就是"串行"的全部实现。** */
  const inFlight = useRef(false);
  const saveTimer = useRef<number | null>(null);
  /** 攒改动时用的是哪个账户的令牌。见 `flushLayout` 开头那道判断。 */
  const pendingToken = useRef<string | null>(null);
  /**
   * 用户**这一次会话里自己动过**布局。
   *
   * 后端那份回来得比用户的手慢时(计划还没到、或者用户手快),它就不能再盖上来:
   * 用户刚把节点拖到某个地方,没有理由拿一份更旧的位置把他按回去。
   */
  const layoutTouched = useRef(false);

  /**
   * 最新的这三份东西,**给回调读**。
   *
   * 保存是异步的:定时器、PUT、失败后的重试都不在渲染里跑,而闭包捕获到的是
   * "这次渲染开始时"的那一份。镜像保证了无论在哪一刻拼载荷,拼的都是最新的一份
   * (这也正是"合并未发出的更新"的实现方式:重试用的是重试那一刻的数据,
   * 不是失败那一刻的)。
   */
  const positionsRef = useRef(positions);
  const viewportsRef = useRef(viewports);
  const growthRef = useRef(growth);
  positionsRef.current = positions;
  viewportsRef.current = viewports;
  growthRef.current = growth;

  /**
   * 把本地这份布局拼成一次提交的载荷。**两个筛子,缺一个都会出事。**
   *
   * - **哨兵值不上路**:计划还没从后端到的时候占位树的根是 `PLACEHOLDER_ROOT_ID`
   *   (`'goal'`),而画布是活的 —— 用户在那个窗口里拖一下,位置表里就会留下
   *   `goal:goal` 这样的键。`PUT /layout` 收的 `node_id` 是 UUID,发过去是 422。
   * - **计划里已经没有的节点不上路**:`put_layout` 对不认识的 id 是**整批拒绝**
   *   (见 `layout_service.py`),一个已经删掉的节点留在本地位置表里,会让此后
   *   每一次保存都失败 —— 而失败信息和"拖动"看起来毫无关系。
   *
   * 还有一件不那么显然的:同一个节点可能有两份位置。`node_positions` 一行只装得下
   * 一个(`(用户, 空间, 节点)` 唯一),而画布上同一个节点在**两个层级**里都画得出来
   * ——作为某一层的子节点,和作为它自己那一层的根——各自的键是 `看到的那一层:节点`。
   * 取它的**归属层级**那一份(`${parentId ?? 自己}:${自己}`,根目标归自己):
   * 那一份回答的是"这个节点在这个空间里在哪",另一份只是这次会话里的一瞥。
   */
  const layoutPayload = useCallback((): backend.PutLayoutRequest => {
    const known = growthRef.current.nodes;
    const canonical = new Map<string, { x: number; y: number }>();
    for (const [key, value] of Object.entries(positionsRef.current)) {
      const nodeId = key.slice(key.indexOf(':') + 1);
      const node = known[nodeId];
      if (!node || !UUID_RE.test(nodeId)) continue;
      const home = `${node.parentId ?? nodeId}:${nodeId}`;
      if (!canonical.has(nodeId) || key === home) canonical.set(nodeId, value);
    }
    return {
      positions: [...canonical].map(([nodeId, value]) => ({ nodeId, x: value.x, y: value.y })),
      viewports: Object.entries(viewportsRef.current)
        .filter(([scopeId]) => Boolean(known[scopeId]) && UUID_RE.test(scopeId))
        .map(([scopeNodeId, viewport]) => ({ scopeNodeId, zoom: viewport.zoom, panX: viewport.x, panY: viewport.y })),
    };
  }, []);

  /**
   * 把攒着的那一份写进后端。**永远只有一趟在飞**(`inFlight`),写完之后如果又攒了
   * 新的,用**最新的一份**再发一次 —— 这一趟在飞的时候发生的事不会丢,也不会
   * 和它抢着写。
   */
  const flushLayout = useCallback(async () => {
    if (!isReal || inFlight.current) return;
    // 账户换过了就**不发**。待发的这一份产生在上一个账户的画布上,而"退出登录"到
    // "下一个人登进来"之间那个定时器还活着(上面说了为什么故意不取消它)。令牌就是
    // 账户的凭据:它变了,这一份就该作废 —— 而不是拿新账户的身份去写旧空间。
    if (pendingToken.current !== getToken()) { dirty.current = false; return; }
    inFlight.current = true;
    // 「这个节点不在这个空间里」这种失败只修一次,不空转(见下面 catch)。
    let repaired = false;
    try {
      while (dirty.current) {
        dirty.current = false;
        try {
          await backend.putLayout(space.id, layoutPayload());
          setLayoutError(null);
        } catch (cause) {
          dirty.current = true;   // 没写成就还是脏的
          setLayoutError(cause instanceof ApiError ? cause.message : '布局没保存上。');
          if (!repaired && cause instanceof ApiError && cause.code === 'NODE_NOT_FOUND') {
            // 后端是**整批拒绝**的,而这里最常见的原因是本地这份计划落后了(刚删掉的
            // 那个节点还在这边的位置表里)。那样的话重试发出去的是同一份载荷,点几次
            // 都一样 —— 所以先把计划拉回来再试一次;还不行就停,把错误留在界面上。
            repaired = true;
            await refreshPlan().catch(() => {});
            continue;
          }
          break;
        }
      }
    } finally {
      inFlight.current = false;
    }
  }, [isReal, layoutPayload, refreshPlan, space.id]);

  /** 记下"布局脏了",并安排一次延迟保存。 */
  const scheduleLayoutSave = useCallback(() => {
    dirty.current = true;
    pendingToken.current = getToken();
    if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => { saveTimer.current = null; void flushLayout(); }, LAYOUT_SAVE_DEBOUNCE_MS);
  }, [flushLayout]);

  /**
   * 用户点「重试」。**载荷是这一刻现拼的**,所以它比失败那一次新 ——
   * 网络回来了、或者计划补上了,同一下就能成。这也是为什么这个按钮不看
   * `ApiError.retryable`:那个判断说的是"同样的请求再发一次有没有意义"。
   */
  const retryLayoutSave = useCallback(() => {
    pendingToken.current = getToken();
    dirty.current = true;
    void flushLayout();
  }, [flushLayout]);

  /**
   * 节点位置。**值没变就不算改动。**
   *
   * 这一条不是省事:拖动结束时 ReactFlow 会连着报几次,而"点了但没挪动"也会报一次。
   * 不比较的话,那些都不会产生新的位置,却会各排一次保存 —— 库里那一行一个字节
   * 都不会变,而界面上"刚才那下到底存上没有"变成了一个没有答案的问题。
   */
  const setPositions = useCallback((updater: SetStateAction<Record<string, { x: number; y: number }>>) => {
    const next = typeof updater === 'function' ? updater(positionsRef.current) : updater;
    if (next === positionsRef.current || samePositions(next, positionsRef.current)) return;
    // 立刻更新镜像:同一串连续改动里,后一次读到的是前一次的结果(而不是上一次渲染的)。
    positionsRef.current = next;
    setPositionsRaw(next);
    layoutTouched.current = true;
    scheduleLayoutSave();
  }, [scheduleLayoutSave]);

  /**
   * 一个层级的视口。同样的道理:**值没变就不算改动**。
   *
   * 这里不比较的代价更大:我们自己在"回到记忆里的位置"时会 `setViewport` 一次,
   * ReactFlow 随后就用同一个视口喊 `onMoveEnd` —— 于是每一次"回到记忆位置"都会变成
   * 一次新的保存,而它写进去的和库里已经有的一模一样。
   *
   * 顺带说明**自动 fit 也会走到这里**(它下一步就是 `onMoveEnd`):所以打开一个空间
   * 本身也会留下一行"这一层我上次看到的是这个样子"。那是有意的 —— 下次进来回到那儿,
   * 而不是每次都被重新摆一遍;节点变化之后那个视口可能显得空,用户拖一下就覆盖了。
   */
  const setScopeViewport = useCallback((scopeId: string, viewport: { x: number; y: number; zoom: number }) => {
    const old = viewportsRef.current[scopeId];
    if (old && old.x === viewport.x && old.y === viewport.y && old.zoom === viewport.zoom) return;
    const next = { ...viewportsRef.current, [scopeId]: viewport };
    viewportsRef.current = next;
    setViewportsRaw(next);
    layoutTouched.current = true;
    scheduleLayoutSave();
  }, [scheduleLayoutSave]);

  // 后端那份布局。**读失败不报错**:位置退回 localStorage 那份(视口没有),画布照样
  // 能用,下一次保存会把这份推上去 —— 这不是用户做错了什么,不该给他一行红字。
  // 但 `layoutReady` 必须翻成 true,否则自动 fit 会一直不跑,画布停在默认视角。
  useEffect(() => {
    if (!isReal) { setLayoutReady(true); return; }
    let cancelled = false;
    setLayoutReady(false);
    backend.getLayout(space.id)
      .then(payload => { if (!cancelled) setServerLayout(payload); })
      .catch(() => { if (!cancelled) setServerLayout(null); })
      .finally(() => { if (!cancelled) setLayoutReady(true); });
    return () => { cancelled = true; };
  }, [isReal, space.id]);

  /**
   * 把读回来的那一份合进本地。**计划没到就不合** —— 位置的键要从计划里的
   * `parentId` 推出来(`${归属层级}:${节点}`),而计划没到时画布上是一棵占位树,
   * 拿它去认节点会把整份布局丢光。
   */
  useEffect(() => {
    if (!serverLayout || !plan) return;
    // 只用一次。留着的话,此后每一次计划重取(改一个节点、删一个节点都会重取)都会
    // 把这份旧布局再盖回来 —— 把用户刚拖过的位置按回去。
    setServerLayout(null);
    if (layoutTouched.current) return;
    const known = growth.nodes;
    const mergedPositions = { ...positionsRef.current };
    for (const item of serverLayout.positions) {
      const node = known[item.nodeId];
      if (!node) continue;
      mergedPositions[`${node.parentId ?? item.nodeId}:${item.nodeId}`] = { x: item.x, y: item.y };
    }
    positionsRef.current = mergedPositions;
    setPositionsRaw(mergedPositions);
    const mergedViewports = { ...viewportsRef.current };
    for (const item of serverLayout.viewports) {
      if (!known[item.scopeNodeId]) continue;
      mergedViewports[item.scopeNodeId] = { x: item.panX, y: item.panY, zoom: item.zoom };
    }
    viewportsRef.current = mergedViewports;
    setViewportsRaw(mergedViewports);
  }, [serverLayout, plan, growth]);

  function updateNode(nodeId: string, patch: Extract<PlanAction, { type: 'UPDATE_NODE' }>['patch']) {
    if (!isReal) return;
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
  }

  function setNodeStatus(nodeId: string, status: GrowthNode['status']) {
    if (!isReal) return;
    void mutatePlan(() => backend.updateNode(space.id, nodeId, { status }));
  }

  /**
   * 新建一个节点。**返回它到底成没成。**
   *
   * 节点的 id 由后端生成,所以这里给不出 id(调用方也不需要)。但**必须给得出
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
    if (!title.trim() || !isReal) return false;
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

  /**
   * 连一条边。**返回后端真正存下来的那一条**(失败是 `null`)。
   *
   * 为什么不返回 boolean:后端会把 `related_to` 的两端按 UUID 排序归一(它是无向的,
   * 存哪一头在前由 id 决定,不由用户拖动的方向决定),而且同一条边连两次是**幂等**
   * 的 —— 回来的是既有那一条。所以"库里现在这条边的 id 和类型是什么"只有后端知道,
   * 而调用方紧接着就要用这个 id 打开关系编辑器。
   */
  async function addRelation(
    sourceId: string,
    targetId: string,
    type: GrowthRelationType,
    note?: string,
  ): Promise<RelationPayload | null> {
    if (!isReal) return null;
    return await mutatePlan(() => backend.createRelation(space.id, {
      sourceId, targetId, relationType: type, note: note?.trim() || null,
    }));
  }

  /** 改一条边的类型或说明。**只传要改的字段** —— 后端按"没传"与"传了 null"区分。 */
  async function updateRelation(
    relationId: string,
    patch: { relationType?: GrowthRelationType; note?: string | null },
  ): Promise<boolean> {
    if (!isReal) return false;
    const updated = await mutatePlan(() => backend.updateRelation(space.id, relationId, patch));
    return updated !== null;
  }

  /** 删一条边。**它不碰任何节点** —— 节点是节点,线是线。 */
  async function removeRelation(relationId: string): Promise<boolean> {
    if (!isReal) return false;
    const removed = await mutatePlan(() => backend.removeRelation(space.id, relationId));
    return removed !== null;
  }

  // ---------------------------------------------------------------------------------
  // 归档与恢复(步骤 3C)
  //
  // 垃圾桶那一下**不再直接写库**。用户点它之前要先看到"这一下会带走什么",点之后
  // 也要能看到"东西还在,可以拿回来" —— 所以这里分成三件事:
  //
  // 1. `askArchive`:只**读**一次影响范围,把确认框撑起来。没有它就没有"点之前知道代价"。
  // 2. `confirmArchive`:那一次真的写入(默认归档)。
  // 3. `refreshArchive` / `restoreArchived`:归档列表与恢复。
  //
  // ## 为什么这三个弹窗的状态**不**进 `drafts.ts`
  //
  // 草稿存储装的是"用户打了一半的输入"(见那个文件的头)。这三样里一个字都没有:
  // 影响范围是后端算的只读数字,列表是后端的状态,确认框上只有两个按钮。把它们塞进
  // 草稿反而有害 —— 那份数字会被当成"用户的东西"跨视图留着,而它已经旧了。
  // 切一次视图回来重新问一次后端,拿到的才是对的。
  //
  // ## 影响范围**取不到就不给归档**
  //
  // 退路是"照删,不显示数字" —— 那正是这一批要消灭的东西:用户在不知道代价的情况下
  // 按下去,而这批的全部意义就是"点之前知道"。所以取不到就把话说明白、留一个重试,
  // 归档那一下按不动。
  const [archiveConfirm, setArchiveConfirm] = useState<{
    nodeId: string;
    title: string;
    impact: backend.ArchiveImpact | null;
    loading: boolean;
    error: string | null;
  } | null>(null);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archived, setArchived] = useState<backend.ArchivedNode[]>([]);
  const [archiveListError, setArchiveListError] = useState<string | null>(null);
  const [archiveNote, setArchiveNote] = useState<string | null>(null);
  /** 正在恢复哪一行(按钮转圈用),以及有没有一个恢复请求在飞。 */
  const [restoringId, setRestoringId] = useState<string | null>(null);

  const refreshArchive = useCallback(async () => {
    if (!isReal) { setArchived([]); return; }
    try {
      setArchived(await backend.listArchive(space.id));
      setArchiveListError(null);
    } catch (cause) {
      setArchiveListError(cause instanceof ApiError ? cause.message : '读不到归档列表。');
    }
  }, [isReal, space.id]);

  // 挂载与换空间时读一次:工具栏上那个数字不能等到用户点开才准。
  useEffect(() => { void refreshArchive(); }, [refreshArchive]);

  async function askArchive(nodeId: string) {
    const node = growth.nodes[nodeId];
    if (!node || nodeId === growth.goalId || !isReal) return;
    setArchiveNote(null);
    setArchiveConfirm({ nodeId, title: node.title, impact: null, loading: true, error: null });
    try {
      const impact = await backend.getArchiveImpact(space.id, nodeId);
      // 读回来的时候用户可能已经关掉它、或者点了另一个节点。只认当前这一个。
      setArchiveConfirm(current =>
        current && current.nodeId === nodeId
          ? { ...current, impact, loading: false }
          : current,
      );
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : '算不出这一下会带走什么。';
      setArchiveConfirm(current =>
        current && current.nodeId === nodeId
          ? { ...current, loading: false, error: message }
          : current,
      );
    }
  }

  function closeArchiveConfirm() { setArchiveConfirm(null); }

  async function confirmArchive() {
    const target = archiveConfirm;
    if (!target?.impact) return;
    const nodeId = target.nodeId;
    setArchiveConfirm(null);
    // 本地的选中态与画布位置可以立刻清掉:它们不依赖后端是否成功,而且
    // 保留一个指向"正在被归档的节点"的选中态会让详情面板闪一下空白。
    // **计划本身不动** —— 以 `refreshPlan` 回来的那份为准。
    if (selectedId === nodeId) select(null);
    // 这一下走**原始 setter**,不排保存。两个理由:
    //
    // 1. 后端读布局时本来就会滤掉已删节点的位置行(`load_layout`),这一下清理纯粹是
    //    本地的,没有要告诉后端的东西。
    // 2. 排了反而危险:删除请求刚发出去、计划还没重取回来那一刻,本地这份计划里
    //    **还有**这个节点,于是那次保存会把它的位置一起提交上去 —— 而 `put_layout`
    //    对不认识的 id 是整批拒绝的,用户会收到一行和"归档"看不出关系的保存失败。
    const remaining = Object.fromEntries(Object.entries(positionsRef.current).filter(([key]) => !key.endsWith(`:${nodeId}`)));
    positionsRef.current = remaining;
    setPositionsRaw(remaining);
    const result = await mutatePlan(() => backend.deleteNode(space.id, nodeId, 'archive'));
    if (result) {
      setArchiveNote(
        `已归档「${target.title}」${target.impact.descendants > 0 ? `及其下面的 ${target.impact.descendants} 项` : ''},` +
        '可以在这里恢复。',
      );
      await refreshArchive();
    }
  }

  /** 把一条归档恢复回来,并**如实报告**排期那边会怎么样(回来几场、过期几场、哪天超了)。 */
  async function restoreArchived(nodeId: string): Promise<boolean> {
    if (!isReal || restoringId) return false;
    setRestoringId(nodeId);
    setArchiveNote(null);
    const result = await mutatePlan(() => backend.restoreNode(space.id, nodeId));
    setRestoringId(null);
    if (!result) return false;
    await refreshArchive();
    const lines = [`已恢复「${result.node.title}」${result.restoredCount > 1 ? `及其下面的 ${result.restoredCount - 1} 项` : ''}。`];
    if (result.relationsVisible > 0) lines.push(`${result.relationsVisible} 条线跟着回来了。`);
    if (result.restoredSessions > 0) {
      lines.push(`回来 ${result.restoredSessions} 场排期(共 ${result.restoredMinutes} 分钟)。`);
    }
    if (result.overdueSessions > 0) {
      lines.push(`其中 ${result.overdueSessions} 场已经过期,需要你自己安排。`);
    }
    // **没有自动重排。** 所以这里要说清"哪一天超了",而不是替用户挪走那几场。
    for (const day of result.overbookedDays) {
      lines.push(`${day.day} 排了 ${day.plannedMinutes} 分钟,超过每天 ${day.dailyCap} 分钟的上限。`);
    }
    setArchiveNote(lines.join(' '));
    return true;
  }
  function addFiles(ownerId: string, incoming: File[]) {
    const assets = incoming.map(file => { const url = URL.createObjectURL(file); objectUrls.current.add(url); return { id: crypto.randomUUID(), ownerId, name: file.name, size: file.size, mime: file.type, url, file }; });
    setFiles(old => [...old, ...assets]);
  }
  function removeFile(id: string) { const asset = files.find(f => f.id === id); if (asset) { URL.revokeObjectURL(asset.url); objectUrls.current.delete(asset.url); } setFiles(old => old.filter(f => f.id !== id)); }
  function publishJournal(content: string, tags: string[], linkedNodeIds: string[], images: File[]) {
    const id = crypto.randomUUID(); setJournals(old => [{ id, content: content.trim(), tags, linkedNodeIds, date: getBeijingDate() }, ...old]); addFiles(id, images);
  }

  /**
   * 计划变更的统一入口。
   *
   * ## 这里**不接所有动作**
   *
   * 上一版这个函数无条件 `dispatch(action)`,把变更写进本地 reducer。接上后端之后
   * 那样做就成了一句谎话:界面上的任务勾上了、后端不知道,刷新就退回去。
   *
   * 所以按动作分流,只放行**后端真的能写**的那一个:
   *
   * - `UPDATE_STATUS`(勾选完成)→ **直接保存**。这是用户明确的动作,属于产品
   *   规定里"用户自己勾选完成可以直接保存"的那一类,不经过提案。
   * - `UPDATE_TIME`(在时间线上拖日期)→ **如实说不支持**。它需要的是"排期"
   *   (哪天做、做多久),而后端现在只有 `deadline`(截止日)——两个不同的东西。
   *   把它当截止日写进去会静默改掉用户设的截止时间,而界面上的说辞是"调整了安排"。
   *   排期在阶段 6 接入,那时这个方法会真的有东西可写。
   * - 其余动作**什么都不做**,并且如实说。它们以前由 reducer 就地改本地计划,
   *   而本地那份计划已经没有了 —— 现在是静默无效(界面说改了、其实没改),
   *   所以这里显式报错,和 `UPDATE_TIME` 一个待遇。
   */
  function apply(action: PlanAction) {
    if (!isReal) return;
    if (action.type === 'UPDATE_STATUS') { setNodeStatus(action.nodeId, action.status); return; }
    if (action.type === 'UPDATE_TIME') {
      setPlanError('现在还不能直接拖日期改安排 —— 计划里只有截止时间,还没有排出来的具体时段。');
      return;
    }
    setPlanError('这个改动现在还不能保存到计划里。');
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

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || sending) return;
    // 只有真实空间能发 —— 消息的真相在后端,没有别的地方可以发。
    // 还没选空间时静默丢掉比编一句回复好;界面在这种状态下本来就会把人送回空间页。
    if (space.kind !== 'real') return;
    await sendReal(trimmed, crypto.randomUUID());
  }

  /** 重试上一轮。复用同一个 `clientMessageId`,所以不会重复落库。 */
  async function retry() {
    if (!lastFailed || sending) return;
    setMessages(old => old.filter(m => !m.failed));
    await sendReal(lastFailed.text, lastFailed.clientMessageId);
  }

  return { growth, workspaceId: space.id, isRealSpace: space.kind === 'real', apply, selectedId, select, messages, send, retry, sending, sendError, retryable, brief, historyLoading, messagesTruncated, positions, setPositions,
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
    spaceId: currentSpaceId,
    // 画布子树的 key。**与 `spaceId` 不是同一个东西**,别拿它去查节点 —— 见 `canvasKey`。
    canvasKey,
    // 视口(用户偏好,不进版本账)。画布按层级存,时间线一份。
    viewports, setScopeViewport, timelineViewport, setTimelineViewport,
    // 布局落库的那三样。`layoutReady` 是"后端那份问过了",自动 fit 要等它;
    // `layoutError` 与 `retryLayoutSave` 是保存失败时界面上那一行和那个按钮。
    layoutReady, layoutError, retryLayoutSave,
    enterSpace, updateNode, setNodeStatus, addNode,
    // 归档与恢复。`deleteNode` 那个直接写库的入口**改名成了 `askArchive`** ——
    // 名字换掉是有意的:它的语义从"删"变成了"先问一句",留着旧名字会让下一个改动
    // 的人以为它还是原来那件事。
    askArchive, archiveConfirm, closeArchiveConfirm, confirmArchive,
    archiveOpen, setArchiveOpen, archived, archiveListError, archiveNote, setArchiveNote,
    restoreArchived, restoringId, refreshArchive,
    // 关系。三种边共用这三个入口(后端也是同一组)—— 分成 dependsOn / relatedTo
    // 两套 API 会让调用方先知道"这条边在哪个表里",而那正是接口层要挡掉的事。
    addRelation, updateRelation, removeRelation,
    files, addFiles, removeFile, journals, publishJournal, conversations, setConversations, settings, setSettings, focus, setFocus };
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
    // `?workspace=` 必须是一个真实空间 id。它以前还兼着"进入示例空间"这一档
    // (`?workspace=primary`),那一档已经没有了 —— 现在它就是一个 id,打不开
    // 就走下面的失败分支,不会退回任何演示内容。
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
