import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { todayInTimeZone } from '../../src/features/growth/timeline';
import { DEMO_TODAY } from '../../src/mock/growth-state';

/**
 * 登录、调接口、进入示例空间 —— 每个端到端测试都要走的那几步。
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
 */

export const API_BASE = (process.env.API_BASE ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');

/** 令牌在 localStorage 里的键。见 `src/lib/api.ts`。 */
export const TOKEN_KEY = 'zhitu.auth.token.v1';

/**
 * 示例空间的 id。
 *
 * 保研那套演示数据还在(`src/mock/`),但它**不再是"没选空间时的默认值"** ——
 * 一个刚注册的账户看到系统替他建好的一份保研计划,是这一版明确要消灭的东西。
 * 现在它只能从"成长空间"页主动进入,地址上就是 `?workspace=primary`。
 *
 * 所以凡是"验的是那份演示内容"的测试,都得显式带上这个参数。它是产品的真实入口
 * (空间页那句"先看看示例空间"点下去就是这个地址),不是测试的私门。
 */
export const DEMO_WORKSPACE = 'primary';

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
 */
export async function registerAccount(
  page: Page,
  prefix = 'e2e',
  options: { navigate?: boolean } = {},
): Promise<TestAccount> {
  const email = `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@zhitu.test`;
  const password = 'playwright-password';
  const response = await page.request.post(`${API_BASE}/api/auth/register`, {
    data: { email, password, displayName: '端到端验收', timezone: 'Asia/Shanghai' },
  });
  if (!response.ok()) throw new Error(`注册失败:${response.status()} ${await response.text()}`);
  const body = (await response.json()) as { token: string; user: { id: string } };
  // localStorage 是**按来源**分家的:不在应用的来源上就写不进去。新开的页面还停在
  // `about:blank`,所以默认先打开 `/login` 再写令牌。浏览器已经在应用里时(测试中途
  // 换账户)那一次加载是白跑的,传 `navigate:false` 省掉它。
  if (options.navigate !== false) await page.goto('/login');
  await page.evaluate(
    ([key, value]) => localStorage.setItem(key, value),
    [TOKEN_KEY, body.token] as const,
  );
  return { token: body.token, userId: body.user.id, email, password };
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

/**
 * 把一个页面地址补成"在当前示例空间里打开它"。
 *
 * 六个主导航链接指向的是 `/journal`、`/conversations` 这样的裸地址,不带
 * `?workspace=`。在示例空间里点它们会**丢掉空间上下文**并被弹回空间页 ——
 * 因为示例空间不像真实空间那样记在 `zhitu.active.workspace.<用户>` 里,它只认
 * 地址栏上那个参数(见 `src/features/growth/provider.tsx` 的 WorkspaceRouter)。
 *
 * 所以凡是"跨页面走一遍"的测试,这里用显式地址代替点导航链接:验的是页面之间
 * 共享同一份状态,不是导航链接的 href 拼写。
 */
export function demoUrl(path: string, query: Record<string, string> = {}): string {
  const [pathname, search = ''] = path.split('?');
  const params = new URLSearchParams(search);
  params.set('workspace', DEMO_WORKSPACE);
  for (const [key, value] of Object.entries(query)) params.set(key, value);
  return `${pathname}?${params.toString()}`;
}

/** 进示例空间里的某一页。 */
export async function openDemoSpace(page: Page, path: string, query: Record<string, string> = {}): Promise<void> {
  await page.goto(demoUrl(path, query));
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

/** 断言画布上真的画出了一批节点 —— 它是"数据到了"最直接的证据。 */
export async function expectCanvasNodes(page: Page, ids: string[]): Promise<void> {
  await expect
    .poll(async () => (await page.locator('.react-flow__node').evaluateAll(
      (nodes) => nodes.map((node) => node.getAttribute('data-id') ?? '').sort(),
    )))
    .toEqual([...ids].sort());
}

/**
 * 一个种子日期,在**这一次运行里**实际会落在哪一天。
 *
 * 示例数据的日期是按 `DEMO_TODAY`(2026-09-16)排的常量,而产品在进示例空间时会
 * 把它们整体平移到"今天"(见 `provider.tsx` 的 `reanchorDemoDates`:今天 − 种子锚点,
 * 保持相对间隔)。所以测试里那些写死的 demo 日期**每过一天就整体后移一天** ——
 * 写成字面量(比如直接断言 `2026-10-18`)在写下的当天是对的,第二天起就是错的,
 * 而失败信息会指向"时间线拖动坏了",不是"日期常量过期了"。
 *
 * 这里用产品自己的 `todayInTimeZone()` 和种子锚点 `DEMO_TODAY` 算出这个位移,
 * 再把种子日期换算过去 —— 断言的对象没变(某个节点该在哪一天),
 * 变的是它不再是一个字面量。
 */
export function demoDate(seed: string): string {
  const day = 86400000;
  const shift = (Date.parse(`${todayInTimeZone()}T00:00:00Z`) - Date.parse(`${DEMO_TODAY}T00:00:00Z`)) / day;
  return new Date(Date.parse(`${seed}T00:00:00Z`) + shift * day).toISOString().slice(0, 10);
}
