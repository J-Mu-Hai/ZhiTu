import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { todayInTimeZone } from '../../src/features/growth/timeline';

/**
 * 登录、调接口、进空间 —— 每个端到端测试都要走的那几步。
 *
 * ## 为什么抽出来
 *
 * 账户从 localStorage 搬到后端之后,"打开工作台"从一步变成了三步:**有一个账户**、
 * **拿到令牌**、**选一个空间**。十二个文件各抄一份的话,后端契约一变(比如令牌存
 * 在哪个键下)就要改十二处 —— 而漏掉的那几处不会报错,它们会安静地停在登录页,
 * 长得像"选择器写错了"。所以这里只留一份,做法逐字来自 `plan-projection.spec.ts`
 * —— 那是迁移之后唯一被验证过的一套。
 *
 * ## 这里不做任何"替应用做决定"的事
 *
 * 注册走的是真的 `POST /api/auth/register`,令牌写进 localStorage 之后浏览器里的
 * 每个请求都带着它,由真正的鉴权路径把关;空间也是真的 `POST /api/workspaces`。
 * 测试**不伪造身份**,只是把真人会做的那几步做完。
 *
 * ## 示例空间已经删掉了,这里原来有四个辅助函数跟着一起删
 *
 * `DEMO_WORKSPACE` / `demoUrl` / `openDemoSpace` / `demoDate` 以及 `DEMO_TODAY`
 * 那个种子锚点 —— 它们服务的那个 `?workspace=primary` 演示空间整个没有了。
 * 现在**每个测试都在真实空间里搭自己的场景**:`createWorkspace` 建空间、
 * `api()` 建节点,看到的东西全部有后端那一行对应。这比读一份预置演示数据更慢,
 * 但它验的是产品而不是那份数据。
 */

export const API_BASE = (process.env.API_BASE ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');

/** 令牌在 localStorage 里的键。见 `src/lib/api.ts`。 */
export const TOKEN_KEY = 'zhitu.auth.token.v1';

export interface TestAccount {
  token: string;
  /** 后端里的用户 UUID。它决定 `zhitu.active.workspace.<id>` 这类本地键的名字。 */
  userId: string;
  email: string;
  password: string;
}

/**
 * 后端不在时**失败,而不是跳过**。
 *
 * 账户、空间、计划、对话都在后端。后端不在时这些测试验的东西根本不存在,
 * 跳过只会让"界面和后端对不上"在没人注意的时候悄悄回来。
 */
export async function assertBackendRunning(request: APIRequestContext): Promise<void> {
  const health = await request.get(`${API_BASE}/health`).catch(() => null);
  if (!health?.ok()) {
    throw new Error(
      `后端没有在 ${API_BASE} 上运行。这些测试验的是"界面画出来的 == 后端存着的",` +
        `没有后端就没有可比的真值。先启动它:参见 README 的本地开发一节。`,
    );
  }
}

/**
 * 建一个账户,返回它的令牌,并把令牌写进浏览器的 localStorage。
 *
 * 邮箱带时间戳:测试会往开发库 `data/zhitu_dev.db` 里真的写一行用户,用一个固定
 * 邮箱的话第二次跑就是"这个邮箱已经注册过了"。
 *
 * `signIn: false` 只建账户、**不碰这个浏览器** —— 给"同一个浏览器里换账户"那类
 * 测试备一个人:那边要的是一个真实存在于后端的第二个账户,而这台机器上现在
 * 登录的必须还是第一个人。写令牌和注册本身是两件事,只是以前默认捆在一起。
 */
export async function registerAccount(
  page: Page,
  prefix = 'e2e',
  options: { navigate?: boolean; signIn?: boolean } = {},
): Promise<TestAccount> {
  const email = `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@zhitu.test`;
  const password = 'playwright-password';
  const response = await page.request.post(`${API_BASE}/api/auth/register`, {
    data: { email, password, displayName: '端到端验收', timezone: 'Asia/Shanghai' },
  });
  if (!response.ok()) throw new Error(`注册失败:${response.status()} ${await response.text()}`);
  const body = (await response.json()) as { token: string; user: { id: string } };
  const account = { token: body.token, userId: body.user.id, email, password };
  if (options.signIn === false) return account;
  // localStorage 是**按来源**分家的:不在应用的来源上就写不进去。新开的页面还停在
  // `about:blank`,所以默认先打开 `/login` 再写令牌。浏览器已经在应用里时(测试中途
  // 换账户)那一次加载是白跑的,传 `navigate:false` 省掉它。
  if (options.navigate !== false) await page.goto('/login');
  await page.evaluate(
    ([key, value]) => localStorage.setItem(key, value),
    [TOKEN_KEY, body.token] as const,
  );
  return account;
}

/**
 * 带令牌调一个接口,拿真值。
 *
 * 失败时把响应体一起抛出去。只说"500"的话,看到的人还得自己重跑一遍才知道原因。
 */
export async function api<T>(
  page: Page,
  token: string,
  path: string,
  init?: { method?: string; data?: unknown },
): Promise<T> {
  const response = await page.request.fetch(`${API_BASE}${path}`, {
    method: init?.method ?? 'GET',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: init?.data,
  });
  if (!response.ok()) {
    throw new Error(`${init?.method ?? 'GET'} ${path} -> ${response.status()} ${await response.text()}`);
  }
  return (await response.json()) as T;
}

/** 建一个空间,返回它的 id。 */
export async function createWorkspace(page: Page, token: string, title: string, intent = ''): Promise<string> {
  const created = await api<{ workspace: { id: string } }>(page, token, '/api/workspaces', {
    method: 'POST',
    data: { title, intent },
  });
  return created.workspace.id;
}

/** `/plan` 里一条节点。只声明测试真的会读的几个字段。 */
export interface PlanNode {
  id: string;
  parentId: string | null;
  title: string;
  nodeType: string;
  deadline: string | null;
  status: string;
  /**
   * 预计工时(分钟)。**正文保存不许动它** —— 它抹掉的话,那条任务会突然排不进
   * 任何一天,而排期与「今天」都不会报错,只是安静地少了一件事(见 `node-body.spec.ts`)。
   */
  estimateMinutes: number | null;
  /** 正文。`null` 是"没写过",与空串不是一回事(后端用 `None` 表示没写过)。 */
  description: string | null;
  /**
   * 正文的乐观锁(步骤 4)。**测试要读它才能造出真正的冲突** —— 拿一个旧号去写,
   * 而不是"随便写个 0 看看会不会炸"。
   */
  contentVersion: number;
}
/** 计划里一条边。`/plan` 会把它一并返回,画布的连线就是从这儿投影出来的。 */
export interface PlanDependency {
  id: string;
  predecessorId: string;
  successorId: string;
  depType: string;
  lagDays: number;
}

/**
 * `/plan` 里的一条边 —— 三种关系共用这一个形状(见 `lib/backend.ts::RelationPayload`)。
 *
 * 它和 `PlanDependency` **不是一回事**:后者只有"前置"那一种,是排期读的东西;
 * 前者是画布画的东西。同一条前置关系会同时出现在两份里。
 */
export interface PlanRelation {
  id: string;
  relationType: 'depends_on' | 'related_to' | 'influences';
  sourceId: string;
  targetId: string;
  note: string | null;
  origin: 'user' | 'ai' | null;
  lagDays: number | null;
}

export interface PlanPayload {
  nodes: PlanNode[];
  totalNodes: number;
  revisionVersion: number;
  /** 前置 → 后续。删掉一个节点时,挂在它上面的边必须一起消失,否则库里会留下悬空边。 */
  dependencies: PlanDependency[];
  /**
   * 画布上所有的边,三种类型一起。**包含 `dependencies` 的那一份。**
   *
   * 界面上的连线读的是这一份(见 `planProjection.ts`),所以"画布上有没有这条线"
   * 的真值在这里,而不是在 `dependencies` 里 —— 一条 `related_to` 在后者里根本不存在。
   */
  relations: PlanRelation[];
}

/** 读一份计划。**整个空间**,不是当前这一层 —— 进入子空间不影响它读回什么。 */
export async function getPlan(page: Page, token: string, workspaceId: string): Promise<PlanPayload> {
  return api<PlanPayload>(page, token, `/api/workspaces/${workspaceId}/plan`);
}

/**
 * 建一条"前者完成后才能开始后者"。
 *
 * **走接口而不是界面**,理由和 `createNode` 一样:它是给别的测试搭场景用的。
 * 凡是验"界面上画不画得出这条线"的测试,就**不该**用它 —— 那要走 `PathView` 上的
 * 表单或拖线(见 `tests/relations.spec.ts`),否则测的是"接口能写"而不是"用户能连"。
 */
export async function addDependency(
  page: Page,
  token: string,
  workspaceId: string,
  predecessorId: string,
  successorId: string,
): Promise<void> {
  await api(page, token, `/api/workspaces/${workspaceId}/dependencies`, {
    method: 'POST',
    data: { predecessorId, successorId },
  });
}

/**
 * 连一条 `related_to`(普通关联,不参与排期)。
 *
 * 和 `addDependency` 同一个理由:**给别的测试搭场景用**。验"界面上画不画得出这条线"
 * 的测试要走 `PathView` 的拖线或"建立关系"表单(见 `tests/relations.spec.ts`),
 * 否则测的是"接口能写"而不是"用户能连"。
 *
 * `relationType` 是**必填**的,服务端不做"没写就是前置"的猜测(见
 * `contracts/plan.py::CreateRelationRequest`)—— 所以这里也不给它默认值。
 */
export async function addRelation(
  page: Page,
  token: string,
  workspaceId: string,
  sourceId: string,
  targetId: string,
  relationType: 'related_to' | 'influences' = 'related_to',
): Promise<void> {
  await api(page, token, `/api/workspaces/${workspaceId}/relations`, {
    method: 'POST',
    data: { sourceId, targetId, relationType },
  });
}

/**
 * 拿**接口**建一个节点,返回它的 id。
 *
 * 为什么用接口而不是点界面:凡是要验"后端有什么、界面画不画得出来"的测试,
 * 节点就必须先真的在后端。从界面建的话,建失败时测试会因为另一个原因
 * (按钮没生效)而失败,分不清到底哪一环坏了。
 */
export async function createNode(
  page: Page,
  token: string,
  workspaceId: string,
  data: { parentId: string; title: string; nodeType?: string; deadline?: string; description?: string; estimateMinutes?: number },
): Promise<string> {
  const created = await api<{ node: PlanNode }>(page, token, `/api/workspaces/${workspaceId}/nodes`, {
    method: 'POST',
    data,
  });
  return created.node.id;
}

/**
 * 等到计划真的从后端到达。
 *
 * 计划到达之前,真实空间画出来的是一棵只有根目标的**占位**树,根节点的 id 是哨兵值
 * `'goal'`(见 `planProjection.emptyGrowth`)。那个状态下界面还改不了数据 —— 新建节点
 * 是禁用的,因为 `POST /nodes` 要一个真 UUID。不先等这一步,测试是在一个"还不能操作"
 * 的界面上点按钮,失败原因会指向选择器而不是它真正的原因。
 */
export async function waitForRealPlan(page: Page): Promise<void> {
  await expect
    .poll(async () => {
      const ids = await renderedNodeIds(page);
      return ids.length > 0 && !ids.includes('goal');
    }, { message: '计划没有从后端到达:画布上还是那个哨兵根节点' })
    .toBe(true);
}

/** 画布上真正画出来的节点 id。ReactFlow 不加虚拟化参数时每个节点都在 DOM 里。 */
export async function renderedNodeIds(page: Page): Promise<string[]> {
  return (await page.locator('.react-flow__node').evaluateAll(
    (nodes) => nodes.map((node) => node.getAttribute('data-id') ?? ''),
  )).sort();
}

/**
 * 打开一个空间里的某一页。
 *
 * 六个主导航链接指向的是 `/journal`、`/conversations` 这样的裸地址,不带
 * `?workspace=` —— 它们靠 `zhitu.active.workspace.<用户>` 那个本地键找回上下文。
 * 所以跨页面走一遍有两种写法:点导航链接(验的是链接本身),或者显式带上参数
 * (验的是页面之间共享同一份状态)。这个函数是后一种。
 */
export function spaceUrl(path: string, workspaceId: string, query: Record<string, string> = {}): string {
  const [pathname, search = ''] = path.split('?');
  const params = new URLSearchParams(search);
  params.set('workspace', workspaceId);
  for (const [key, value] of Object.entries(query)) params.set(key, value);
  return `${pathname}?${params.toString()}`;
}

/** 进某个空间里的某一页。 */
export async function openSpacePage(page: Page, path: string, workspaceId: string, query: Record<string, string> = {}): Promise<void> {
  await page.goto(spaceUrl(path, workspaceId, query));
}

/**
 * 点一下,并等它的效果出现;效果没出现就再点一下。
 *
 * 为什么需要:**可见不等于点得动**。整页加载之后,Playwright 一看到按钮露出来就会点,
 * 但那一刻事件处理可能还没挂上(dev server 现场编译、同时跑满好几个浏览器的时候尤其
 * 明显) —— 这一下就白点了:按钮还在,该出现的表单永远不开。产品本身没问题(人手点
 * 第二次就开了),但它报出来的样子是"某个元素 20 秒都不出现",长得像产品坏了。
 *
 * 所以这里不猜时间,而是**验效果**:效果出现就停,没出现就再点一次。
 * (效果必须是"点了会发生什么",不能是普通可见性 —— 否则第一次就满足了。)
 */
export async function clickUntilVisible(page: Page, trigger: Locator, effect: Locator): Promise<void> {
  await expect(async () => {
    await trigger.click();
    await expect(effect).toBeVisible({ timeout: 3000 });
  }).toPass({ timeout: 15000 });
}

/**
 * 进入某个节点的子空间。
 *
 * ## 为什么是点那个箭头按钮,而不是双击
 *
 * 2026-09-27(步骤 4)之前,进子空间靠**双击节点**。那件事和"单击打开正文与详情"
 * 挤在同一个手势上,只能靠一个 240 毫秒的计时器分开 —— 于是每一次查看正文都要先
 * 等 240 毫秒,而双击会不会被认出来取决于手速。现在两件事拆开了:单击看正文,
 * 进子空间走节点右上角那个箭头(`.node-enter`,它从 3A 那一批起就在)。
 *
 * 所以测试里也该走那条路 —— **不要**在这里偷偷用 `dblclick`:那个手势已经不是
 * 产品的入口了,留着它等于测一条用户走不了的路。
 */
export async function enterSpace(page: Page, nodeId: string): Promise<void> {
  const node = page.locator(`.react-flow__node[data-id="${nodeId}"]`);
  await node.scrollIntoViewIfNeeded();
  await node.hover();
  // "进去了"的证据:**这个节点变成了当前这一层的根**(`PathView` 里
  // `growth.nodes[spaceId]` 就是画出来那个 `goal`)—— 它原来是一张普通卡片。
  // 用它当效果,而不是"按钮还在不在":后者点之前就成立(见 `clickUntilVisible`
  // 那段注释里"效果必须是点了会发生什么")。
  const becameTheRoot = page.locator(`.react-flow__node[data-id="${nodeId}"] .growth-node.goal`);
  await clickUntilVisible(page, node.locator('.node-enter'), becameTheRoot);
}

/** 断言画布上真的画出了一批节点 —— 它是"数据到了"最直接的证据。 */
export async function expectCanvasNodes(page: Page, ids: string[]): Promise<void> {
  await expect
    .poll(async () => (await page.locator('.react-flow__node').evaluateAll(
      (nodes) => nodes.map((node) => node.getAttribute('data-id') ?? '').sort(),
    )))
    .toEqual([...ids].sort());
}

/**
 * 算一份排期,并把这一份**应用**下去。
 *
 * 排期是"哪几天做多久"的写入路径 —— 它和截止时间是两件事(见 `types/growth.ts` 里
 * `GrowthNode.deadline` 那段)。界面上对应的是工作台的「排期」:先看一份草案,确认了
 * 才写。这里走的是同一条路,只是把"看"和"确认"压成一步。
 *
 * **必须带 `estimateMinutes` 的节点才排得进去**:没有工时的节点会变成 `NoEstimate`
 * 缺口,一行场次都不会产生 —— 而那时候测试失败的样子是"今天这一页什么都没有",
 * 看起来像页面坏了。
 *
 * 返回的是**数据库回报的真实笔数**(`applied.created`),不是算法说它想写多少。
 */
export async function scheduleEverything(
  page: Page,
  token: string,
  workspaceId: string,
): Promise<{ created: number; totalPlannedMinutes: number }> {
  const preview = await api<{ scheduleVersion: string; totalPlannedMinutes: number }>(
    page,
    token,
    `/api/workspaces/${workspaceId}/schedule/preview`,
    { method: 'POST' },
  );
  const applied = await api<{ applied: { created: number } }>(
    page,
    token,
    `/api/workspaces/${workspaceId}/schedule/apply`,
    {
      method: 'POST',
      data: { scheduleVersion: preview.scheduleVersion, idempotencyKey: `e2e-schedule-${crypto.randomUUID()}` },
    },
  );
  return { created: applied.applied.created, totalPlannedMinutes: preview.totalPlannedMinutes };
}

/** 「今天」这一页读到的一条安排。跨**全部活动空间** —— 路径上没有空间参数。 */
export interface TodayItem {
  sessionId: string;
  nodeId: string;
  nodeTitle: string;
  workspaceTitle: string;
  plannedMinutes: number;
  status: string;
  result: string | null;
  recorded: boolean;
  actualMinutes: number | null;
}

/** 读一份「今天」的真值。界面上的勾是乐观更新,这一份才是库里那一行。 */
export async function getToday(
  page: Page,
  token: string,
): Promise<{ items: TodayItem[]; itemCount: number; recordedCount: number; plannedMinutes: number }> {
  const today = await api<{
    workspaces: { items: TodayItem[] }[];
    itemCount: number;
    recordedCount: number;
    plannedMinutes: number;
  }>(page, token, '/api/today');
  return {
    items: today.workspaces.flatMap((workspace) => workspace.items),
    itemCount: today.itemCount,
    recordedCount: today.recordedCount,
    plannedMinutes: today.plannedMinutes,
  };
}

/**
 * 相对今天第 `offset` 天的日期(`YYYY-MM-DD`,UTC 口径)。
 *
 * 测试里建节点时用它算截止日,而**不能写字面量** —— 一个写死 `2026-10-18` 的断言
 * 在写下的当天是对的,第二天起就是错的,而失败信息会指向"时间线坏了",不是
 * "日期常量过期了"。这里和产品自己算日期用的是同一套口径(`todayInTimeZone`)。
 */
export function dayOffset(offset: number, from = new Date()): string {
  const base = Date.parse(`${todayInTimeZone(from)}T00:00:00Z`);
  return new Date(base + offset * 86400000).toISOString().slice(0, 10);
}
