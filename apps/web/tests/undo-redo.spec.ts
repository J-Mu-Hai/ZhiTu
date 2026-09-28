import { expect, test, type Browser, type Page } from '@playwright/test';
import {
  TOKEN_KEY,
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  registerAccount,
  waitForRealPlan,
  type TestAccount,
} from './support/session';
import { selectNodeMenuItem } from './support/menu';

/**
 * 布局的撤销/重做:**一次拖拽 = 一步,撤销之后的那个位置会真的写回后端。**
 *
 * ## 这个文件钉的是哪几件事
 *
 * 撤销/重做最容易做浅的地方不是栈本身,而是它和**保存队列**的关系。位置天天在改,
 * 而保存是延迟 600 毫秒、串行发出去的 —— 如果"撤销"自己发一个 `PUT`,它就会和正在飞的
 * 那一趟抢着写,表现是**撤销之后刷新,节点又跳回拖到的位置**。所以这里每一条
 * "撤销了"的断言都分三层:
 *
 * 1. **画布上**那个 `transform`(用户看到的);
 * 2. **后端那一行**(`GET /layout`),"存下来了"的真值;
 * 3. **另开一个空 localStorage 的浏览器**读一次 —— 位置在 localStorage 里也有一份,
 *    所以只看同一个浏览器刷新会是一个自己骗自己的绿(见 `layout.spec.ts` 文件头)。
 *
 * ## 为什么"撤销第一次拖动"能验出实现对不对
 *
 * 一个节点第一次被拖动之前,位置表里**没有它的键**(位置是自动排布算出来的)。
 * 那时候最容易写出的实现是"撤销 = 把这个键删掉":本地看着完全正确(自动排布又把它
 * 摆回原处),但 `PUT /layout` **不删除未提交的行**,库里那一行还在 ——
 * 于是换台机器打开,节点又回到拖到的位置。所以这一条必须验到后端那一行,
 * 而"拖一次 → 撤一次 → 换台机器看"就是它的红证。
 *
 * ## 这里**不验**什么
 *
 * **业务撤销没有做,也不在这一批。** 撤销一个节点的位置不会让归档的东西回来、
 * 不会重建一行、不会产生计划版本 —— 文件里另有一条专门验这个(见最后一条测试)。
 * 建节点、改标题、建关系那些改动的撤销必须走受版本校验的补偿操作,那是另一件事
 * (任务书 §4.8),不要把它读成"撤销已经全面可用了"。
 *
 * `nodeAt` / `dragNode` / `openElsewhere` 与 `layout.spec.ts` 里那三个同名函数是
 * 同一个形状:两个文件各自持有一份,是因为那个文件里它们是局部函数(它的文件头解释了
 * 每一条的用意)。这里只保留这个文件用得到的断言。
 *
 * ## 每条测试自己写了 `test.slow()`
 *
 * 它们各自要走**两次整页加载 + 一个第二浏览器上下文**(注册账户、建空间、建节点、
 * 拖拽、撤销、再换个上下文读一次),实测一条 25~40 秒,而默认预算是 30 秒 ——
 * 撞上预算时的失败信息是 `apiRequestContext.fetch` 或 `page.goto` 超时,和要验的东西
 * 毫无关系。所以按 `playwright.config.ts` 里记下的那条口径办:`test.slow()` 放宽的是
 * **这一条测试的墙钟**(×3),断言上限仍然是 20 秒 —— 真正在乎的判据没有被放宽。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

interface StoredLayout {
  positions: { nodeId: string; x: number; y: number }[];
  viewports: { scopeNodeId: string; zoom: number; panX: number; panY: number }[];
}

function storedLayout(page: Page, token: string, workspaceId: string): Promise<StoredLayout> {
  return api<StoredLayout>(page, token, `/api/workspaces/${workspaceId}/layout`);
}

/** 一个空间、根目标下两个节点。两个都动得着,才分得出"被撤销的那一个"和"没事的那一个"。 */
async function scene(page: Page, prefix: string) {
  const account = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, account.token, '撤销验收空间');
  const root = (await getPlan(page, account.token, workspaceId)).nodes[0];
  const a = await createNode(page, account.token, workspaceId, { parentId: root.id, title: '被拖的那个' });
  const b = await createNode(page, account.token, workspaceId, { parentId: root.id, title: '旁边那个' });
  return { ...account, workspaceId, root: root.id, a, b };
}

/** 画布上那个节点**此刻**在哪儿 —— 读 React Flow 写在 DOM 上的值,不读我们自己的状态。 */
async function nodeAt(page: Page, nodeId: string): Promise<{ x: number; y: number }> {
  const style = (await page.locator(`.react-flow__node[data-id="${nodeId}"]`).getAttribute('style')) ?? '';
  const found = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/.exec(style);
  if (!found) throw new Error(`读不出这个节点的位置(style="${style}")`);
  return { x: Number(found[1]), y: Number(found[2]) };
}

const close = (a: number, b: number, tolerance = 1.5) => Math.abs(a - b) < tolerance;
const same = (a: { x: number; y: number }, b: { x: number; y: number }) => close(a.x, b.x) && close(a.y, b.y);

/**
 * 拖动一个节点。中间那几帧不是装饰(见 `layout.spec.ts`):一步跳到终点的话,
 * React Flow 可能只收到最后一次 `pointermove` 而不认为这是一次拖动。
 */
async function dragNode(page: Page, nodeId: string, dx: number, dy: number): Promise<void> {
  const node = page.locator(`.react-flow__node[data-id="${nodeId}"]`);
  const box = await node.boundingBox();
  if (!box) throw new Error('拖不动:这个节点不在画布上');
  const start = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  for (const ratio of [0.3, 0.6, 0.85]) await page.mouse.move(start.x + dx * ratio, start.y + dy * ratio);
  await page.mouse.move(start.x + dx, start.y + dy);
  await page.mouse.up();
  await expect(page.getByRole('dialog'), '拖一下节点把详情弹窗也打开了').toHaveCount(0);
}

/** 另开一个浏览器上下文(**空 localStorage**),同账户同空间 —— "换台机器打开"的替身。 */
async function openElsewhere(browser: Browser, account: TestAccount, url: string) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto('/login');
  await page.evaluate(([key, value]) => localStorage.setItem(key, value), [TOKEN_KEY, account.token] as const);
  await page.goto(url);
  await waitForRealPlan(page);
  return { close: () => context.close(), page };
}

/** 等初始 fit 跑完再动手,否则量到的视口/位置可能是它自己 fit 出来的那一份。 */
async function waitForInitialFit(page: Page): Promise<void> {
  await page.waitForTimeout(700);
}

/** 后端那一行里这个节点的位置(`null` = 还没有)。 */
async function storedAt(page: Page, token: string, workspaceId: string, nodeId: string) {
  const mine = (await storedLayout(page, token, workspaceId)).positions.find((item) => item.nodeId === nodeId);
  return mine ? { x: mine.x, y: mine.y } : null;
}

/** 工具栏上那两个按钮。**限定在工具栏里** —— 「归档」那个弹窗里也有别的同名按钮。 */
const toolbar = (page: Page) => page.locator('.space-floating-tools');

test('连续拖两次:撤销一次回上一步,再撤销回最初那一帧,而且库里也回去了', async ({ page, browser }) => {
  test.slow();
  const account = await scene(page, 'undo-twice');
  const url = `/workbench?workspace=${account.workspaceId}`;
  await page.goto(url);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  const before = await getPlan(page, account.token, account.workspaceId);
  const p0 = await nodeAt(page, account.a);

  // --- 两次拖拽 = 两步 ---------------------------------------------------------
  await dragNode(page, account.a, 140, 90);
  const p1 = await nodeAt(page, account.a);
  expect(same(p1, p0), '第一次拖完位置没变 —— 这条测试的前提没成立,别读成产品有问题').toBe(false);
  await dragNode(page, account.a, 90, 130);
  const p2 = await nodeAt(page, account.a);
  expect(same(p2, p1), '第二次拖完位置没变 —— 前提没成立').toBe(false);

  // 逐帧不入历史:一次拖拽中途会经过几十个坐标,如果每一帧都算一步,用户按一次撤销
  // 只会把节点往回挪一个像素 —— 这里两次拖拽只该有**两步**可撤。
  const undoButton = toolbar(page).getByRole('button', { name: '撤销' });
  const redoButton = toolbar(page).getByRole('button', { name: '重做' });
  await expect(redoButton, '还没撤销过就有重做的东西').toBeDisabled();

  // --- 撤销一次:回到第一次拖完的位置 -----------------------------------------
  await undoButton.click();
  await expect.poll(() => nodeAt(page, account.a).then((now) => same(now, p1)), {
    message: '撤销一次之后画布没有回到第一次拖完的位置',
  }).toBe(true);
  await expect.poll(() => storedAt(page, account.token, account.workspaceId, account.a).then((now) => now !== null && same(now, p1)), {
    message: '撤销之后后端那一行不是撤销后的位置',
    timeout: 15000,
  }).toBe(true);
  await expect(toolbar(page).getByRole('button', { name: '重做' }), '撤销过之后重做该能用了').toBeEnabled();

  // --- 再撤销一次:回到最初那一帧,**而且这一次是真的写进了库** -----------------
  //
  // 这一条就是文件头说的那个红证:如果"撤销"实现成"把这一键删掉",画布上看着完全对
  // (自动排布又把它摆回原处),而库里那一行还停在 p1 —— 换台机器打开就露出来。
  await undoButton.click();
  await expect.poll(() => nodeAt(page, account.a).then((now) => same(now, p0)), {
    message: '撤销两次之后画布没有回到最初那一帧',
  }).toBe(true);
  await expect.poll(() => storedAt(page, account.token, account.workspaceId, account.a).then((now) => now !== null && same(now, p0)), {
    message: '撤销两次之后后端那一行没有回到最初那一帧 —— 画布对了不等于库里对了',
    timeout: 15000,
  }).toBe(true);
  await expect(toolbar(page).getByRole('button', { name: '撤销' }), '两步都撤完了,撤销该灰了').toBeDisabled();

  // --- 换台机器打开:撤到的位置还在 -------------------------------------------
  const other = await openElsewhere(browser, account, url);
  try {
    await expect.poll(() => nodeAt(other.page, account.a).then((now) => same(now, p0)), {
      message: '换台机器打开之后,节点不在撤销到的位置上',
    }).toBe(true);
  } finally {
    await other.close();
  }

  // --- 撤销布局**没有**动计划 --------------------------------------------------
  //
  // 拖动与撤销都只走 `PUT /layout`,而它不产生计划版本。这一条防的是"撤销顺手改了
  // 计划"这种听起来荒唐、但一旦有了通用撤销框架就很容易发生的事。
  const after = await getPlan(page, account.token, account.workspaceId);
  expect(after.revisionVersion, '撤销布局不该产生新的计划版本').toBe(before.revisionVersion);
  expect(after.nodes.map((item) => item.id).sort(), '撤销布局不该增删节点').toEqual(before.nodes.map((item) => item.id).sort());
});

test('撤销再重做:回到拖动后的位置,库里也跟上', async ({ page }) => {
  test.slow();
  const account = await scene(page, 'redo');
  await page.goto(`/workbench?workspace=${account.workspaceId}`);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  const p0 = await nodeAt(page, account.a);
  await dragNode(page, account.a, 150, 110);
  const p1 = await nodeAt(page, account.a);
  expect(same(p1, p0), '拖完位置没变 —— 前提没成立').toBe(false);

  // 快捷键与按钮是**同一个入口**的两条路:`Ctrl+Z` 撤销,`Ctrl+Shift+Z` 重做。
  await page.keyboard.press('Control+z');
  await expect.poll(() => nodeAt(page, account.a).then((now) => same(now, p0)), {
    message: '按了 Ctrl+Z 但画布没有回到拖动前',
  }).toBe(true);

  await page.keyboard.press('Control+Shift+z');
  await expect.poll(() => nodeAt(page, account.a).then((now) => same(now, p1)), {
    message: '按了 Ctrl+Shift+Z 但画布没有回到拖动后的位置',
  }).toBe(true);
  await expect.poll(() => storedAt(page, account.token, account.workspaceId, account.a).then((now) => now !== null && same(now, p1)), {
    message: '重做之后后端那一行不是重做后的位置',
    timeout: 15000,
  }).toBe(true);
  await expect(toolbar(page).getByRole('button', { name: '重做' })).toBeDisabled();

  // 重做分支作废:撤销一步之后**又拖了一次**,那一步就不该还能重做 ——
  // 否则"重做"会把用户带到一个他从没走过的状态上(刚拖的那个节点回到原点)。
  await page.keyboard.press('Control+z');
  await expect.poll(() => nodeAt(page, account.a).then((now) => same(now, p0))).toBe(true);
  await dragNode(page, account.a, -60, 40);
  await expect(toolbar(page).getByRole('button', { name: '重做' }), '新的一次拖动之后,旧的重做分支必须作废').toBeDisabled();
});

test('保存请求还没回来就撤销:库里最后留下的是撤销后那一份,不会被旧请求盖掉', async ({ page }) => {
  test.slow();
  const account = await scene(page, 'undo-inflight');
  await page.goto(`/workbench?workspace=${account.workspaceId}`);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  // **只慢拖完之后的那一趟**(2 秒),后面的不慢。要重现的是"旧请求最后落地",
  // 而"旧"的那一趟正是拖动之后**先发**的那一个 —— 它还在飞的时候撤销发生了。
  //
  // 为什么必须让它比后发的那趟**慢**:如果所有请求一起慢 1.2 秒,先发的还是先到,
  // 时间顺序不会反过来,任何实现都是绿的 —— 那样这条测试就只是个摆设。
  //
  // 记 `putCount` 而不是"第几趟",是因为打开页面时自动 fit 也会写一次视口
  // (`setScopeViewport` → 同一条保存队列),它会占掉第一趟。
  let slowNextPut = false;
  await page.route('**/api/workspaces/*/layout', async (route) => {
    if (route.request().method() !== 'PUT') return route.fallback();
    if (slowNextPut) {
      slowNextPut = false;
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    await route.continue();
  });

  const p0 = await nodeAt(page, account.a);
  slowNextPut = true;
  await dragNode(page, account.a, 160, 120);
  const p1 = await nodeAt(page, account.a);
  expect(same(p1, p0), '拖完位置没变 —— 前提没成立').toBe(false);

  // 等过 600 毫秒那次防抖:这一趟 PUT 已经在飞了(它还要 2 秒才回来)。
  await page.waitForTimeout(900);
  await page.keyboard.press('Control+z');
  await expect.poll(() => nodeAt(page, account.a).then((now) => same(now, p0)), {
    message: '撤销之后画布没有回到拖动前',
  }).toBe(true);

  // 落定:先是撤销后那一份到达后端……
  await expect.poll(() => storedAt(page, account.token, account.workspaceId, account.a).then((now) => now !== null && same(now, p0)), {
    message: '撤销之后库里一直没变成撤销后的位置',
    timeout: 20000,
  }).toBe(true);
  // ……然后**再等一会儿**,确认没有一趟更旧的请求在后头把它盖回去。
  await page.waitForTimeout(2000);
  const settled = await storedAt(page, account.token, account.workspaceId, account.a);
  expect(settled !== null && same(settled, p0), `等了两秒之后库里的位置又变了(${JSON.stringify(settled)})`).toBe(true);
});

test('跨空间:另一个空间没有可撤销的东西,而切回来看到的还是已保存的布局', async ({ page }) => {
  test.slow();
  const account = await scene(page, 'undo-scope');
  const urlA = `/workbench?workspace=${account.workspaceId}`;
  await page.goto(urlA);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  await dragNode(page, account.a, 150, 100);
  const p1 = await nodeAt(page, account.a);
  await expect(toolbar(page).getByRole('button', { name: '撤销' })).toBeEnabled();
  // 先等它落库,免得后面分不清"切空间丢的是历史还是布局"。
  await expect.poll(() => storedAt(page, account.token, account.workspaceId, account.a).then((now) => now !== null && same(now, p1)), {
    timeout: 15000,
  }).toBe(true);

  // --- 另一个空间:自己的历史,和上一个空间没关系 -----------------------------
  const otherId = await createWorkspace(page, account.token, '另一个空间');
  const otherRoot = (await getPlan(page, account.token, otherId)).nodes[0];
  await page.goto(`/workbench?workspace=${otherId}`);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  await expect(
    toolbar(page).getByRole('button', { name: '撤销' }),
    '刚打开的空间里出现了上一个空间的撤销记录',
  ).toBeDisabled();
  const otherP0 = await nodeAt(page, otherRoot.id);
  await dragNode(page, otherRoot.id, 120, 80);
  await expect(toolbar(page).getByRole('button', { name: '撤销' }), '这个空间自己拖过了,应该能撤').toBeEnabled();
  await page.keyboard.press('Control+z');
  await expect.poll(() => nodeAt(page, otherRoot.id).then((now) => same(now, otherP0)), {
    message: '另一个空间里撤销没有生效',
  }).toBe(true);

  // --- 切回第一个空间:历史清了(与刷新同等对待),**但布局不能清** -----------
  await page.goto(urlA);
  await waitForRealPlan(page);
  await waitForInitialFit(page);
  await expect(
    toolbar(page).getByRole('button', { name: '撤销' }),
    '切走再切回来时历史还留着 —— 这一版允许它清掉(和刷新一样),但不该是"另一个空间带过来的"',
  ).toBeDisabled();
  await expect.poll(() => nodeAt(page, account.a).then((now) => same(now, p1)), {
    message: '切回这个空间之后,已经保存的布局被清掉了 —— 清历史不能连布局一起清',
  }).toBe(true);
});

test('在输入框里按撤销:文字归浏览器,画布一步都不动', async ({ page }) => {
  test.slow();
  const account = await scene(page, 'undo-typing');
  await page.goto(`/workbench?workspace=${account.workspaceId}`);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  const p0 = await nodeAt(page, account.a);
  await dragNode(page, account.a, 140, 100);
  const p1 = await nodeAt(page, account.a);

  // --- 一、对话框里的正文(详细说明) -----------------------------------------
  await page.locator(`.react-flow__node[data-id="${account.a}"]`).click();
  const editor = page.getByRole('dialog');
  await expect(editor.getByRole('heading')).toContainText('编辑节点');
  const description = editor.getByPlaceholder('记录这个节点的目标、约束、判断和下一步…');
  await description.click();
  // 用**真实按键**敲进去,不用 `fill()`:后者是直接赋值的,浏览器不会把它放进自己的
  // 撤销栈里,于是这一条测试会因为"文字没被撤销"而红 —— 而那和产品没关系。
  await description.pressSequentially('weekend-only');
  await expect(description).toHaveValue('weekend-only');

  await page.keyboard.press('Control+z');
  // 文字那一边归浏览器(它自己有撤销栈)。这里断言的是"这一下没有白按",
  // 同时**不是**在断言它的实现细节(撤成什么样由浏览器决定)。
  await expect(description, '在输入框里按撤销,文字一点都没动 —— 这一下大概被画布吃掉了').not.toHaveValue('weekend-only');
  // ……而画布上的节点**必须一动不动**:用户在正文里撤销的是文字,不是布局。
  expect(same(await nodeAt(page, account.a), p1), '在正文里按撤销,画布上的节点动了').toBe(true);

  await editor.getByRole('button', { name: '关闭弹窗' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);

  // --- 二、不在弹窗里的输入框(和 AI 说话的框) -------------------------------
  //
  // 上一条里弹窗本身也会让快捷键绕开画布(`PathView.tsx` 那个 effect 的第二条),
  // 所以真正验"焦点在输入框里"的是这一条:这个框不在任何 dialog 里。
  const composer = page.getByLabel('给 AI 的消息');
  await composer.click();
  await composer.pressSequentially('read-first');
  await expect(composer).toHaveValue('read-first');
  await page.keyboard.press('Control+z');
  expect(same(await nodeAt(page, account.a), p1), '在和 AI 的输入框里按撤销,画布上的节点动了').toBe(true);

  // --- 三、那一步历史**没有被吃掉** -------------------------------------------
  //
  // 上面两次 Ctrl+Z 如果被画布接住了,历史就已经被消费掉、节点也早就跳回去了
  // (上面那条断言会先红)。这一条是它的正证:离开输入框之后那一步还在,按下去仍然管用。
  //
  // 点画布上的一块空白,把焦点从输入框里拿出来。**原来点的是左上角那 20×20** ——
  // 那个角落现在浮着空间路径(它是左上角的浮动组件),点上去落在它身上,
  // 而这一条要的是"点到画布上"。所以往画布里面挪一点(仍然是空白:左边那一列
  // 在这个空间里没有节点)。
  await page.locator('.react-flow__pane').click({ position: { x: 60, y: 220 } });
  // 顺手确认焦点真的离开了 —— 不然下一步就变成"在输入框里按撤销",
  // 而那正是上面那一条验的事,两条测试会变成同一条。
  await expect(composer).not.toBeFocused();
  await toolbar(page).getByRole('button', { name: '撤销' }).click();
  await expect.poll(() => nodeAt(page, account.a).then((now) => same(now, p0)), {
    message: '撤销那一步在输入框里被吃掉了 —— 离开输入框后按撤销,节点没有回到拖动前',
  }).toBe(true);
});

test('撤销一个已经归档的节点:它不会回来,跳过这件事会说清楚', async ({ page }) => {
  test.slow();
  const account = await scene(page, 'undo-archived');
  await page.goto(`/workbench?workspace=${account.workspaceId}`);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  const b0 = await nodeAt(page, account.b);
  // 第一步:动 b。第二步:动 a。然后把 a 归档,再撤销 —— 撤的正是 a 那一步。
  await dragNode(page, account.b, 130, 90);
  const b1 = await nodeAt(page, account.b);
  await dragNode(page, account.a, 150, 120);

  // 归档这一步 §9.1.1 之后在菜单里(`.node-more` →「归档(可以恢复)」)。
  // 这条测试真正钉的是"撤销不该把归档过的节点带回来",入口怎么变与它无关。
  await selectNodeMenuItem(page, account.a, '归档');
  const confirm = page.getByRole('dialog');
  await expect(confirm.getByRole('heading')).toContainText('归档');
  await confirm.getByRole('button', { name: '归档' }).click();
  await expect.poll(async () => (await page.locator('.react-flow__node').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-id')))).includes(account.a), {
    message: '归档之后那个节点还在画布上',
  }).toBe(false);

  // --- 撤销那一步:被归档的节点**不许**借着撤销回来 --------------------------
  await toolbar(page).getByRole('button', { name: '撤销' }).click();
  await expect(
    toolbar(page).locator('.layout-history-note'),
    '撤销了涉及已归档节点的那一步,却没有说明',
  ).toContainText('已经不在这个空间里');
  await expect(
    page.locator(`.react-flow__node[data-id="${account.a}"]`),
    '撤销布局把一个已经归档的节点带回画布了 —— 布局撤销不能恢复业务数据',
  ).toHaveCount(0);

  // 库里也一样:它还躺在归档里,好好待着,没被这一步撤销"顺手恢复"。
  const plan = await getPlan(page, account.token, account.workspaceId);
  expect(plan.nodes.map((item) => item.id), '归档的节点被撤销带回了计划里').not.toContain(account.a);
  // 归档列表是一行的数组,每行的 id 埋在 `node` 里(`ArchivedNodePayload`)——
  // 列表里出现的是**每一批归档的根**,不是被带走的每一个。
  const archived = await api<{ node: { id: string } }[]>(
    page, account.token, `/api/workspaces/${account.workspaceId}/archive`,
  );
  expect(archived.map((item) => item.node.id), '归档列表里找不到它了 —— 它去哪了?').toContain(account.a);

  // --- 跳过不等于卡住:再撤一步,上一步(b 的位置)照常回来 -------------------
  await toolbar(page).getByRole('button', { name: '撤销' }).click();
  await expect.poll(() => nodeAt(page, account.b).then((now) => same(now, b0)), {
    message: '跳过那一步之后,再撤销一步就不动了 —— 被归档的节点把后面的撤销卡住了',
  }).toBe(true);
  await expect.poll(() => storedAt(page, account.token, account.workspaceId, account.b).then((now) => now !== null && same(now, b0)), {
    message: 'b 的位置没有写回后端',
    timeout: 15000,
  }).toBe(true);
  await expect(toolbar(page).locator('.layout-history-note'), '这一步没有跳过的东西,提示该收回去').toHaveCount(0);
  expect(b1, 'b 的位置本来就该和最初不一样,否则这条测试白测').not.toEqual(b0);
});
