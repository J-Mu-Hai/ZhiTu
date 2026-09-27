import { expect, test, type Browser, type Page } from '@playwright/test';
import {
  TOKEN_KEY,
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  enterSpace,
  getPlan,
  registerAccount,
  waitForRealPlan,
  type TestAccount,
} from './support/session';

/**
 * 布局落库:节点位置与每层视口,**从后端读写**。
 *
 * ## 这一批之前,布局是"这台机器的",不是"这个用户的"
 *
 * 位置只落 localStorage(换台机器看不到)、视口只活在 Provider 的内存里(刷新就没了),
 * 后端那两张表(`node_positions` / `scope_viewports`)与接口一直摆在那儿没人用。
 * 后果不是"少了个功能":用户在画布上摆好的图,**刷新之后会自己散开** —— 而界面上
 * 没有任何东西提示过这一点。
 *
 * ## 为什么每条用例都要另开一个浏览器上下文
 *
 * 位置在 localStorage 里**也有一份**(那是"这台机器上的偏好",留着是对的)。
 * 所以"刷新之后位置还在"这句话,在后端那一行根本没写成功时**也成立** ——
 * 它是一个自己骗自己的绿。真正能证明"落库了"的只有一件事:**一个空 localStorage
 * 的浏览器里,位置从后端回来了**。这个文件里几乎所有断言都建立在那上面。
 * (视口没有这个问题:它从来不落 localStorage。但一样走这条路验,少一种口径。)
 *
 * ## 断言分两层,缺一层就把话说过了头
 *
 * - **后端那一行**(`GET /layout`):"存下来了"的真值;
 * - **画布上那个 `transform`**:React Flow 真正画出来的值 —— 与产品自己存了什么无关。
 *   存了不等于用上了,这两件事必须分开看。
 *
 * ## 这个文件**不**重复后端已经验过的那些
 *
 * 整批拒绝(`NODE_NOT_FOUND`)、不删除未提交的行、按用户取、跨账号 404 ——
 * 都在 `backend/tests/test_relations_and_layout.py` 里。这里只验"用户看得见的那些":
 * 拖完/平移完在不在、换台机器在不在、存不上说不说、切空间串不串、自动 fit 顶不顶。
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

/** 一个空间、根目标下两个节点。第二个节点用来证明"只有被拖的那一个动了"。 */
async function scene(page: Page, prefix: string) {
  const account = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, account.token, '布局验收空间');
  const root = (await getPlan(page, account.token, workspaceId)).nodes[0];
  const a = await createNode(page, account.token, workspaceId, { parentId: root.id, title: '要拖的节点' });
  const b = await createNode(page, account.token, workspaceId, { parentId: root.id, title: '不动的那一个' });
  return { ...account, workspaceId, root: root.id, a, b };
}

/** 画布上那个节点**此刻**在哪儿。读的是 React Flow 写在 DOM 上的那个值。 */
async function nodeAt(page: Page, nodeId: string): Promise<{ x: number; y: number }> {
  const style = (await page.locator(`.react-flow__node[data-id="${nodeId}"]`).getAttribute('style')) ?? '';
  const found = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)/.exec(style);
  if (!found) throw new Error(`读不出这个节点的位置(style="${style}")`);
  return { x: Number(found[1]), y: Number(found[2]) };
}

/** 画布此刻的平移与缩放。同上,读的是浏览器里真正画出来的那个值。 */
async function viewportAt(page: Page): Promise<{ x: number; y: number; zoom: number }> {
  const style = (await page.locator('.react-flow__viewport').getAttribute('style')) ?? '';
  const found = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)\s*scale\(([\d.]+)\)/.exec(style);
  if (!found) throw new Error(`读不出视口(style="${style}")`);
  return { x: Number(found[1]), y: Number(found[2]), zoom: Number(found[3]) };
}

/** 两个数是不是"同一个位置"。浮点不做精确比较,但容差是几个像素 —— 不是"差不多就行"。 */
const close = (a: number, b: number, tolerance = 1.5) => Math.abs(a - b) < tolerance;

/**
 * 拖动一个节点。
 *
 * 中间那几帧移动不是装饰:一步跳到终点的话,React Flow 可能只收到最后一次
 * `pointermove` 而**不认为这是一次拖动** —— 表现是"拖了,但位置没变",
 * 而这个失败长得像"选择器写错了"。(`relations.spec.ts` 里拖线那边同一个坑。)
 *
 * 最后那句是**前提检查**:拖动不该顺带打开节点详情。打开了的话,后面每一步都会
 * 被那个模态弹窗挡住,而失败信息会指向别处 —— 所以在这里就说清楚。
 */
async function dragNode(page: Page, nodeId: string, dx: number, dy: number): Promise<void> {
  const node = page.locator(`.react-flow__node[data-id="${nodeId}"]`);
  const box = await node.boundingBox();
  if (!box) throw new Error('拖不动:这个节点不在画布上');
  const start = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  for (const ratio of [0.3, 0.6, 0.85]) {
    await page.mouse.move(start.x + dx * ratio, start.y + dy * ratio);
  }
  await page.mouse.move(start.x + dx, start.y + dy);
  await page.mouse.up();
  await expect(page.getByRole('dialog'), '拖一下节点把详情弹窗也打开了').toHaveCount(0);
}

/**
 * 把画布拖开一段,让视口不等于初始的那一次 fit。
 *
 * 和 `canvas-stability.spec.ts` 里那个是同一件事(那边验的是"切视图回得来",
 * 这边验的是"存进了后端"),但**返回的是拖完之后 DOM 上的那个值**:
 * 这里不去算"我拖到哪儿了",而是问"画布现在显示的是什么,后端那一行是不是同一个数"。
 * 少一层换算,就少一处能把两个都算错的地方对上。
 */
async function panAway(page: Page): Promise<{ x: number; y: number; zoom: number }> {
  const before = await viewportAt(page);
  const box = await page.locator('.react-flow__pane').boundingBox();
  if (!box) throw new Error('画布没渲染出来,拖不动');
  // 从下缘中间起手:那里没有节点,也不会撞上左下角的缩略图和右下角的控件。
  await page.mouse.move(box.x + box.width / 2, box.y + box.height - 30);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 - 150, box.y + box.height - 120, { steps: 12 });
  await page.mouse.up();
  const after = await viewportAt(page);
  expect(after, '没能把画布拖开 —— 这条测试的前提没成立,别把它读成产品有问题').not.toEqual(before);
  return after;
}

/**
 * 另开一个浏览器上下文(**空的 localStorage**),用同一个账户进同一个空间。
 *
 * 这就是"换台机器打开"在测试里的替身,也是这个文件里唯一能把"落库了"和
 * "本地那份又读回来了"区分开的办法。见文件头。
 */
async function openElsewhere(
  browser: Browser,
  account: TestAccount,
  url: string,
): Promise<{ close: () => Promise<void>; page: Page }> {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto('/login');
  await page.evaluate(([key, value]) => localStorage.setItem(key, value), [TOKEN_KEY, account.token] as const);
  await page.goto(url);
  await waitForRealPlan(page);
  return { close: () => context.close(), page };
}

/**
 * 等那一次**初始 fit** 跑完再动手。
 *
 * 它不是"随便等一会儿":`PathView` 里那次定位是"计划到达 + 布局问过之后 180 毫秒",
 * 而那之前 ReactFlow 还会按 `fitView` 属性自己 fit 一次。在这个窗口里拖画布,
 * 量到的视口可能正好是它 fit 出来的那一份 —— 失败会指向产品,而问题在时序上。
 * 等过它,后面读到的每一个值都是"用户自己拖出来的"。
 */
async function waitForInitialFit(page: Page): Promise<void> {
  await page.waitForTimeout(700);
}

test('拖一下节点:位置进的是后端,换台机器打开还停在你放手的地方', async ({ page, browser }) => {
  const account = await scene(page, 'layout-drag');
  await page.goto(`/workbench?workspace=${account.workspaceId}`);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  const before = await nodeAt(page, account.a);
  await dragNode(page, account.a, 170, 200);
  const dragged = await nodeAt(page, account.a);
  expect(dragged, '拖完位置没变 —— 这条测试的前提没成立,别把它读成产品有问题').not.toEqual(before);

  // 真值是后端那一行。**画布上看起来对了不算证据** —— 位置在 localStorage 里也有
  // 一份,后端那一行没写成功时画布照样是对的(见文件头)。
  await expect
    .poll(async () => {
      const stored = (await storedLayout(page, account.token, account.workspaceId)).positions;
      const mine = stored.find((item) => item.nodeId === account.a);
      return mine ? { x: mine.x, y: mine.y } : null;
    }, { message: '拖动之后后端一直没收到这个节点的位置', timeout: 15000 })
    .not.toBeNull();

  const written = await storedLayout(page, account.token, account.workspaceId);
  const mine = written.positions.find((item) => item.nodeId === account.a)!;
  expect(close(mine.x, dragged.x), `库里那一行和画布上看到的位置对不上(${JSON.stringify(mine)} vs ${JSON.stringify(dragged)})`).toBe(true);
  expect(close(mine.y, dragged.y)).toBe(true);
  // 只有用户真的摆过的那一个在库里。**自动排布出来的位置不许上传**:那些位置随
  // 文字长短和窗口大小变,存下来只会让"我摆的"和"它算的"再也分不开。
  expect(written.positions.map((item) => item.nodeId)).toEqual([account.a]);

  const other = await openElsewhere(browser, account, `/workbench?workspace=${account.workspaceId}`);
  try {
    await expect
      .poll(async () => {
        const seen = await nodeAt(other.page, account.a);
        return close(seen.x, mine.x) && close(seen.y, mine.y);
      }, { message: '换了一个空的浏览器,节点没有回到库里的那个位置', timeout: 15000 })
      .toBe(true);
  } finally {
    await other.close();
  }
});

test('把画布拖开:视口进的是后端,换台机器打开还停在原处', async ({ page, browser }) => {
  const account = await scene(page, 'layout-viewport');
  await page.goto(`/workbench?workspace=${account.workspaceId}`);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  const panned = await panAway(page);
  // 等的是"库里那一行**就是画布上这个视口**",不是"库里已经有了一行"。
  // 后者会先被**初始 fit** 那一次满足(它也会存一行,而且比这次平移早),
  // 于是读到的是上一个视口,而失败信息指向这一次平移 —— 这条测试第一次就是这么红的:
  // 库里 `panX 176`,画布上 `26`,差的正好是这一次拖动的位移。
  const matchesCanvas = async () => {
    const mine = (await storedLayout(page, account.token, account.workspaceId)).viewports
      .find((item) => item.scopeNodeId === account.root);
    return Boolean(mine) && close(mine!.panX, panned.x) && close(mine!.panY, panned.y) && close(mine!.zoom, panned.zoom, 0.01);
  };
  await expect
    .poll(matchesCanvas, { message: '平移之后后端一直没收到这个视口', timeout: 15000 })
    .toBe(true);

  const written = await storedLayout(page, account.token, account.workspaceId);
  const mine = written.viewports.find((item) => item.scopeNodeId === account.root)!;
  expect(mine, '库里的视口不是记在当前这一层上的').toBeTruthy();
  expect(close(mine.panX, panned.x), `库里那一行和画布上看到的视口对不上(${JSON.stringify(mine)} vs ${JSON.stringify(panned)})`).toBe(true);
  expect(close(mine.panY, panned.y)).toBe(true);
  expect(close(mine.zoom, panned.zoom, 0.01)).toBe(true);

  const other = await openElsewhere(browser, account, `/workbench?workspace=${account.workspaceId}`);
  try {
    await expect
      .poll(async () => {
        const seen = await viewportAt(other.page);
        return close(seen.x, mine.panX) && close(seen.y, mine.panY) && close(seen.zoom, mine.zoom, 0.01);
      }, { message: '换了一个空的浏览器,画布没有回到库里的那个视口', timeout: 15000 })
      .toBe(true);
  } finally {
    await other.close();
  }
});

test('存不上的时候要说出来,而且重试真的能成', async ({ page }) => {
  const account = await scene(page, 'layout-failed');
  await page.goto(`/workbench?workspace=${account.workspaceId}`);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  // 先等"这一次打开"该存的都存完(初始 fit 记下的那个视口),再掐 PUT。
  // 不等的话掐到的可能是那一次保存 —— 于是"没存上"从拖动之前就开始了,
  // 而后面每一句都会指向拖动,整条测试在验一件别的事。
  await expect
    .poll(async () => (await storedLayout(page, account.token, account.workspaceId)).viewports.length,
      { message: '打开空间之后初始视口没有存进后端,这条测试的前提没成立', timeout: 15000 })
    .toBeGreaterThan(0);

  // 只掐 PUT。GET 是读,掐了它验的就变成另一件事了。
  const layoutUrl = /\/api\/workspaces\/[0-9a-fA-F-]{36}\/layout$/;
  await page.route(layoutUrl, async (route) => {
    if (route.request().method() === 'PUT') await route.abort();
    else await route.continue();
  });

  await dragNode(page, account.a, 150, 160);
  const dragged = await nodeAt(page, account.a);

  // 600 毫秒防抖之后那次保存失败,**必须有一行说出来** —— 拖动是可以悄悄失败的操作:
  // 画面上节点就停在你放手的地方,而库里没有。用 `.layout-save-error` 定位而不是
  // 按文案:这一行说的话会改,而"它就是布局那一行"不会。
  const notice = page.locator('.layout-save-error');
  await expect(notice, '布局没存上,界面上却什么都没说').toBeVisible({ timeout: 15000 });
  await expect(notice.locator('span'), '那一行是空的 —— 说了等于没说').toContainText(/连不上|没保存上/);
  expect((await storedLayout(page, account.token, account.workspaceId)).positions, '这条测试要的是"没存上",可它存上了').toEqual([]);

  await page.unroute(layoutUrl);
  await notice.getByRole('button', { name: '重试' }).click();

  // 重试要**能把这一份补上去**:载荷是点的那一刻现拼的(见 provider 里
  // `retryLayoutSave`),所以重试是这个按钮唯一有意义的行为 —— 不是让它转一圈。
  await expect
    .poll(async () => {
      const mine = (await storedLayout(page, account.token, account.workspaceId)).positions.find((item) => item.nodeId === account.a);
      return mine ? close(mine.x, dragged.x) && close(mine.y, dragged.y) : false;
    }, { message: '重试之后位置还是没有进库', timeout: 15000 })
    .toBe(true);
  await expect(notice, '重试成功之后那一行还挂着').toHaveCount(0);
});

test('拖动之后马上切空间:位置进的是刚才那个空间,新空间一个字节都没写', async ({ page }) => {
  const account = await scene(page, 'layout-switch');
  const otherSpace = await createWorkspace(page, account.token, '布局验收二号空间');
  await page.goto(`/workbench?workspace=${account.workspaceId}`);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  const before = await nodeAt(page, account.a);
  await dragNode(page, account.a, 160, 150);
  const dragged = await nodeAt(page, account.a);
  expect(dragged).not.toEqual(before);

  // **不等那 600 毫秒**,直接走。这正是"待发的那一份必须跟着它自己的空间走"那一刻:
  // 定时器还活着,而画布(和它的 Provider)马上就要被换掉。
  await page.locator('.top-navigation nav a', { hasText: '成长空间' }).click();
  await expect(page).toHaveURL(/\/spaces$/);
  await page.locator('.space-card', { hasText: '布局验收二号空间' }).getByRole('button', { name: '进入工作台' }).click();
  await waitForRealPlan(page);

  // 等过那 600 毫秒再加上一次往返的时间 —— 要漏的话,这个窗口足够它漏出来。
  await page.waitForTimeout(1500);

  const mine = (await storedLayout(page, account.token, account.workspaceId)).positions.find((item) => item.nodeId === account.a);
  expect(mine, '切走之后,刚才那个空间的位置丢了 —— 那一次拖动白拖了').toBeTruthy();
  expect(close(mine!.x, dragged.x) && close(mine!.y, dragged.y), `存进刚才那个空间的位置不是用户放手的那个(${JSON.stringify(mine)} vs ${JSON.stringify(dragged)})`).toBe(true);

  const leaked = await storedLayout(page, account.token, otherSpace);
  expect(leaked.positions, '上一个空间的位置写进了新空间').toEqual([]);
  expect(leaked.positions.find((item) => item.nodeId === account.a), '新空间里出现了另一个空间的节点').toBeUndefined();
});

test('后端记着的那份布局,不会被自动 fit 顶掉', async ({ page, browser }) => {
  const account = await scene(page, 'layout-fit');
  // 先往后端写一份**fit 算不出来**的布局:平移到画面外的一角、缩放到 0.55
  // (自动 fit 的缩放是 1,而且它把内容摆在画布中间)。这样"被顶掉了"和
  // "回到了库里那份"长得完全不一样,断言才不会两头都对。
  await api(page, account.token, `/api/workspaces/${account.workspaceId}/layout`, {
    method: 'PUT',
    data: {
      positions: [{ nodeId: account.a, x: 640, y: 420 }],
      viewports: [{ scopeNodeId: account.root, zoom: 0.55, panX: 137, panY: 91 }],
    },
  });

  const other = await openElsewhere(browser, account, `/workbench?workspace=${account.workspaceId}`);
  try {
    // 等过那次初始 fit(计划到达 + 布局问过之后 180 毫秒)。它要是把视口顶掉了,
    // 这里读到的就是它 fit 出来的值。
    await other.page.waitForTimeout(1200);
    const seen = await viewportAt(other.page);
    expect(close(seen.x, 137) && close(seen.y, 91), `视口被顶掉了,现在是 ${JSON.stringify(seen)}`).toBe(true);
    expect(close(seen.zoom, 0.55, 0.01), `缩放被顶掉了,现在是 ${JSON.stringify(seen)}`).toBe(true);
    const node = await nodeAt(other.page, account.a);
    expect(close(node.x, 640) && close(node.y, 420), `节点位置被自动排布顶掉了,现在是 ${JSON.stringify(node)}`).toBe(true);
  } finally {
    await other.close();
  }
});

test('子空间里那个节点摆在哪,是从库里按"它属于哪一层"读回来的', async ({ page, browser }) => {
  const account = await scene(page, 'layout-nested');
  // `node_positions` 一行只装得下一个位置(`(用户, 空间, 节点)`),而同一层里
  // 还有别的节点共用同一张表 —— 一张"哪个节点属于哪一层"的对照表并不存在,
  // 是**从计划里的 `parentId` 推出来的**。这一条验的就是那一步推导:
  // 推错的话,子空间里的节点会拿着库里另一层的那一份位置,或者干脆没有位置。
  const leaf = await createNode(page, account.token, account.workspaceId, {
    parentId: account.a,
    title: '子空间里的那一个',
  });
  await api(page, account.token, `/api/workspaces/${account.workspaceId}/layout`, {
    method: 'PUT',
    data: { positions: [{ nodeId: leaf, x: 700, y: 500 }], viewports: [] },
  });

  const other = await openElsewhere(browser, account, `/workbench?workspace=${account.workspaceId}`);
  try {
    // 进入子空间 —— 位置是**在那一层画出来的**,和根那一层看到的不是同一张图。
    await enterSpace(other.page, account.a);
    await expect
      .poll(async () => (await other.page.locator('.react-flow__node').evaluateAll(
        (nodes) => nodes.map((node) => node.getAttribute('data-id') ?? '').sort(),
      )), { message: '进入子空间之后没看到那一层的节点', timeout: 15000 })
      .toContain(leaf);

    const node = await nodeAt(other.page, leaf);
    expect(close(node.x, 700) && close(node.y, 500), `子空间里的位置不是库里那一份(自动排布会摆在别处):现在是 ${JSON.stringify(node)}`).toBe(true);
  } finally {
    await other.close();
  }
});
