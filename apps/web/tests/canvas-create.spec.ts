/**
 * §9.1.1 的鼠标交互:双击空白处建节点、右键开菜单、归档前先问。
 *
 * ## 这个文件在防什么
 *
 * 这一批把三个**手势**接上了画布。手势类的东西有一个共同的失败方式:它在
 * 开发者的鼠标下是对的,在别人的鼠标下不对 —— 因为"我双击的那一点"和
 * "节点落在的那一点"之间隔着一次**坐标变换**(画布平移了、缩放不是 1、
 * 容器有内边距)。所以下面验收 1 的期望值是**测试自己按变换的定义算出来的**,
 * 不是从产品那里问来的:
 *
 *     flow = (client - 容器左上角 - 平移) / 缩放
 *
 * 这正是产品**不该**手写的那条公式(它必须用 `screenToFlowPosition`)——
 * 两边各算一遍,一个写错了不会互相抵消。
 *
 * ## 另外三条各自防的东西
 *
 * - **验收 3**:取消与失败都不能留下痕迹。判据是**请求数**(`page.on('request')`)
 *   和后端里的节点集合,不是"弹窗关掉了"—— 后者在一个"关了弹窗但已经建好了"的
 *   实现上照样成立。
 * - **验收 4**:菜单的作用对象是**被右键的那一个**,不是当前选中项。这一条只有在
 *   "先选中 A、再右键 B"的时候才分得出来 —— 两者的 id 必须不同。
 * - **验收 5**:归档前那句"这一下会带走什么"取不到时,不许放行。一个"拿不到数字
 *   就当 0"的实现在用户眼里是"它说只影响 0 个后代",而实际上要收走的是一整支。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  API_BASE,
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  enterSpace,
  getPlan,
  openSpacePage,
  registerAccount,
  renderedNodeIds,
  waitForRealPlan,
} from './support/session';
import { openNodeMenu } from './support/menu';
import { pointOnEdge } from './support/edge';

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

// ---------------------------------------------------------------------------------
// 坐标:三个函数,和 `layout.spec.ts` 里那些是同一个形状(那边的是局部函数)
// ---------------------------------------------------------------------------------

/** 画布上那个节点**此刻**在哪儿(流坐标)。读的是 React Flow 写在 DOM 上的值。 */
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

/**
 * **独立算一遍**屏幕上这一点对应的流坐标。
 *
 * 这就是产品的 `screenToFlowPosition` 必须给出的那个答案。手写在这里是**故意的**:
 * 产品的实现用了库函数,测试的期望值用变换的定义 —— 两条不同的路得到同一个数,
 * 才说明那个数是坐标变换本身的性质,而不是两边共享了同一个错误。
 */
async function expectedFlowPoint(page: Page, client: { x: number; y: number }) {
  const viewport = await viewportAt(page);
  const rect = await page.locator('.react-flow').boundingBox();
  if (!rect) throw new Error('读不出画布容器');
  return {
    x: (client.x - rect.x - viewport.x) / viewport.zoom,
    y: (client.y - rect.y - viewport.y) / viewport.zoom,
  };
}

const close = (a: number, b: number, tolerance = 2) => Math.abs(a - b) < tolerance;

/** 后端那一行布局。与 `layout.spec.ts` 的 `storedLayout` 同一个口径。 */
interface StoredLayout {
  positions: { nodeId: string; x: number; y: number }[];
  viewports: { scopeNodeId: string; zoom: number; panX: number; panY: number }[];
}

function storedLayout(page: Page, token: string, workspaceId: string): Promise<StoredLayout> {
  return api<StoredLayout>(page, token, `/api/workspaces/${workspaceId}/layout`);
}

/** 等初始 fit 跑完 —— 它自己会改一次视口,不等的话量到的是它 fit 出来的那一份。 */
const waitForInitialFit = (page: Page) => page.waitForTimeout(700);

/** 把画布拖开一段,让视口不等于初始那一次 fit(与 `layout.spec.ts` 同一个动作)。 */
async function panAway(page: Page): Promise<void> {
  const before = await viewportAt(page);
  const box = await page.locator('.react-flow__pane').boundingBox();
  if (!box) throw new Error('画布没渲染出来,拖不动');
  await page.mouse.move(box.x + box.width / 2, box.y + box.height - 30);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 - 150, box.y + box.height - 120, { steps: 12 });
  await page.mouse.up();
  const after = await viewportAt(page);
  expect(after, '没能把画布拖开 —— 这条测试的前提没成立').not.toEqual(before);
}

/**
 * 找一个**真的是空白**的点。
 *
 * 不写死一个偏移量:节点在哪儿取决于自动排布,而"我以为那块是空的"这种假设
 * 会在某次布局改动之后变成"双击到了一个节点上",失败信息却指向别处。
 * 判据用的是浏览器自己的命中测试,和产品那个 `onEmptyPane` 是同一个事实。
 */
async function emptySpot(page: Page): Promise<{ x: number; y: number }> {
  const box = await page.locator('.react-flow__pane').boundingBox();
  if (!box) throw new Error('画布没渲染出来');
  for (const [fx, fy] of [[0.62, 0.74], [0.3, 0.78], [0.78, 0.28], [0.5, 0.86], [0.35, 0.3]]) {
    const x = box.x + box.width * fx;
    const y = box.y + box.height * fy;
    const isPane = await page.evaluate(
      ([px, py]) => (document.elementFromPoint(px, py) as HTMLElement | null)?.classList.contains('react-flow__pane') ?? false,
      [x, y] as const,
    );
    if (isPane) return { x, y };
  }
  throw new Error('画布上找不到一块空白 —— 这条测试的前提不成立');
}

/** 点那个新建表单的提交按钮(根层叫「新建节点」,子层叫「添加树叶」)。 */
const submitButton = (page: Page) =>
  page.getByRole('dialog').getByRole('button', { name: /新建节点|添加树叶/ });

/** 表单里的名称输入框。两层用不同的 label,所以按"名称"这个共同词找。 */
const titleField = (page: Page) => page.getByRole('dialog').getByLabel(/名称/);

async function createByDoubleClick(page: Page, title: string): Promise<{ x: number; y: number }> {
  const spot = await emptySpot(page);
  const expected = await expectedFlowPoint(page, spot);
  await page.mouse.dblclick(spot.x, spot.y);
  await expect(page.getByRole('dialog')).toBeVisible();
  await titleField(page).fill(title);
  await submitButton(page).click();
  await expect(page.getByRole('dialog'), '建完之后表单该收起来').toHaveCount(0);
  return expected;
}

/** 叶子标题找节点 id。 */
async function nodeIdByTitle(page: Page, token: string, workspaceId: string, title: string): Promise<string> {
  const node = (await getPlan(page, token, workspaceId)).nodes.find((item) => item.title === title);
  if (!node) throw new Error(`计划里没有标题为「${title}」的节点 —— 它没建成`);
  return node.id;
}

// ---------------------------------------------------------------------------------
// 验收 1:位置准,而且落在**当前层级**
// ---------------------------------------------------------------------------------
test('平移并缩放之后双击，节点落在指针那一格，刷新后还在；每一层都成立', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'create-position');
  const workspaceId = await createWorkspace(page, token, '双击新建验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  // **先把视口弄乱。** 不平移不缩放的话,"位置准"这件事在 zoom=1、pan=初始 的
  // 特例下自动成立 —— 而 §9.1.1 要的正是"缩放平移后位置仍准确"。
  await panAway(page);
  await page.locator('.react-flow__controls-zoomout').click();
  await page.locator('.react-flow__controls-zoomout').click();
  const viewport = await viewportAt(page);
  expect(viewport.zoom, '没缩上 —— 这条测试的前提没成立').toBeLessThan(0.95);

  let scope = root.id;
  for (const depth of [1, 2]) {
    const title = `第 ${depth} 层主题`;
    const expected = await createByDoubleClick(page, title);

    const created = await nodeIdByTitle(page, token, workspaceId, title);
    // **层级归属**:双击建的是**当前这一层**的同级主题,不是新的独立空间。
    expect(
      (await getPlan(page, token, workspaceId)).nodes.find((n) => n.id === created)!.parentId,
      `第 ${depth} 层建出来的节点挂错了父节点`,
    ).toBe(scope);

    // 位置:和测试自己算出来的那一个数比,容差是几个像素。
    const placed = await nodeAt(page, created);
    expect(close(placed.x, expected.x), `x 偏了:落在 ${placed.x},应当是 ${expected.x}`).toBe(true);
    expect(close(placed.y, expected.y), `y 偏了:落在 ${placed.y},应当是 ${expected.y}`).toBe(true);

    // 先钉"后端那一行也有它",再谈刷新。位置写入有 600 毫秒防抖
    // (`LAYOUT_SAVE_DEBOUNCE_MS`):建完立刻刷新的话,**还没发出去的那一份会连同
    // 定时器一起被丢掉** —— 那样红的是一条自己没等的测试,不是产品没存。
    // 等它落库,顺手把"刷新后仍在"从"画布上看着对"升级成"后端真的存了"。
    await expect
      .poll(
        async () => {
          const row = (await storedLayout(page, token, workspaceId)).positions.find((item) => item.nodeId === created);
          return row ? close(row.x, expected.x) && close(row.y, expected.y) : false;
        },
        { message: `位置没落进后端布局(${JSON.stringify(expected)})` },
      )
      .toBe(true);

    /*
     * 刷新之前先把本地那份偏好快照删掉。
     *
     * 位置除了后端那一行,还会顺手落进 localStorage(`storageKeyFor`)。留着它的话,
     * 下面"刷新后还在"可能只是**本地缓存**还在 —— 那样这条断言就没有在验后端那一份。
     * 前缀只覆盖画布偏好,认证令牌不在里面,所以登录状态不受影响。
     */
    await page.evaluate(() => {
      for (const key of Object.keys(localStorage)) {
        if (key.startsWith('zhitu.workspace.')) localStorage.removeItem(key);
      }
    });

    // 刷新之后还在 —— 位置是**写进后端布局**的,不是画布上的一次性排布。
    await page.reload();
    await waitForRealPlan(page);
    await waitForInitialFit(page);

    /*
     * 刷新会回到空间的**根**那一层:当前停在哪个子空间不进 URL(`enterSpace` 只是换一个
     * React state),所以节点要回到**它归属的那一层**才画得出归属的那份位置。
     *
     * 画布按 `${看到的那一层}:${节点}` 取位置(见 `PathView` 的 `add`),而后端存的是
     * **归属**那一份(`${parentId}:${nodeId}`)。看的就是它父节点那一层时,两个键相同 ——
     * 所以第 1 层不用做任何事,第 2 层要先走回去。
     */
    if (scope !== root.id) {
      await enterSpace(page, scope);
      await waitForInitialFit(page);
    }

    const afterReload = await nodeAt(page, created);
    expect(
      close(afterReload.x, expected.x) && close(afterReload.y, expected.y),
      `刷新之后位置变了:${JSON.stringify(afterReload)} vs ${JSON.stringify(expected)}`,
    ).toBe(true);
    expect(
      (await getPlan(page, token, workspaceId)).nodes.find((n) => n.id === created)!.parentId,
      '刷新之后父节点变了',
    ).toBe(scope);

    // 进下一层:它成为那一层的根,再双击一次。
    scope = created;
    await enterSpace(page, created);
    await waitForInitialFit(page);
    await expect.poll(() => renderedNodeIds(page)).toEqual([created]);
  }
});

// ---------------------------------------------------------------------------------
// 验收 2:不该建的地方一个都不建(结构性的,不靠排除名单)
// ---------------------------------------------------------------------------------
test('双击节点、边与工具栏都不建节点；只有空白处才建', async ({ page }) => {
  const { token } = await registerAccount(page, 'create-negative');
  const workspaceId = await createWorkspace(page, token, '双击不误建空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  const a = await createNode(page, token, workspaceId, { parentId: root.id, title: '先做的', nodeType: 'task' });
  const b = await createNode(page, token, workspaceId, { parentId: root.id, title: '后做的', nodeType: 'task' });
  await page.request.post(`${API_BASE}/api/workspaces/${workspaceId}/dependencies`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { predecessorId: a, successorId: b },
  });

  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  const countNodes = async () => (await getPlan(page, token, workspaceId)).nodes.length;
  const before = await countNodes();

  // 节点:双击只应当开出**详情**编辑器,不是新建表单。
  const card = page.locator(`.react-flow__node[data-id="${a}"]`);
  const box = await card.boundingBox();
  if (!box) throw new Error('节点不在画布上');
  await page.mouse.dblclick(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.getByRole('dialog').getByRole('heading')).toContainText('编辑节点');
  await expect(page.getByRole('dialog').getByRole('heading'), '双击节点弹出了新建表单').not.toContainText('中新建节点');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);

  // 边:那条依赖的线。**点在真的落在笔画上的那一点**(见 `pointOnEdge`)——
  // 线上命中的是 `.react-flow__edge`,不是 `.react-flow__pane`,所以它不该建节点。
  const onLine = await pointOnEdge(page);
  await page.mouse.dblclick(onLine.x, onLine.y);
  await page.waitForTimeout(300);
  const headings = await page.getByRole('dialog').getByRole('heading').allInnerTexts();
  expect(headings.filter((text) => text.includes('新建节点')), '双击边建出了节点').toEqual([]);
  await page.keyboard.press('Escape');

  // 工具栏:那是一条**按钮**,不是空白。
  const toolbarButton = page.locator('.space-floating-tools > button').first();
  const tb = await toolbarButton.boundingBox();
  if (!tb) throw new Error('工具栏不在');
  await page.mouse.dblclick(tb.x + tb.width / 2, tb.y + tb.height / 2);
  await page.waitForTimeout(300);
  const afterToolbar = await page.getByRole('dialog').getByRole('heading').allInnerTexts();
  expect(afterToolbar.filter((text) => text.includes('新建节点')), '双击工具栏建出了节点').toEqual([]);

  // 而空白处照样能建 —— 少了这半句,一个"双击什么都不做"的实现也全绿。
  expect(await countNodes()).toBe(before);
  await createByDoubleClick(page, '空白处建的');
  expect(await countNodes()).toBe(before + 1);
});

// ---------------------------------------------------------------------------------
// 验收 3:取消不写库、重复双击不写两次、失败保留输入与位置
// ---------------------------------------------------------------------------------
test('取消不写库；连点两下只建一个；建失败时表单与位置都留着', async ({ page }) => {
  const { token } = await registerAccount(page, 'create-cancel');
  const workspaceId = await createWorkspace(page, token, '双击取消验收空间');

  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await waitForInitialFit(page);
  await panAway(page);

  const posts: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && /\/nodes$/.test(new URL(request.url()).pathname)) posts.push(request.url());
  });

  // --- 取消 ---------------------------------------------------------------
  const spot = await emptySpot(page);
  await page.mouse.dblclick(spot.x, spot.y);
  await expect(page.getByRole('dialog')).toBeVisible();
  await titleField(page).fill('我不想要了');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(posts, '取消还是发出了 POST').toEqual([]);
  expect((await getPlan(page, token, workspaceId)).nodes, '取消写库了').toHaveLength(1);

  // --- 连续两次双击 --------------------------------------------------------
  const spot2 = await emptySpot(page);
  await page.mouse.dblclick(spot2.x, spot2.y);
  await page.mouse.dblclick(spot2.x, spot2.y);
  await expect(page.getByRole('dialog'), '冒出了两个弹窗').toHaveCount(1);
  await titleField(page).fill('只建一个');
  await submitButton(page).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect.poll(() => posts.length, '两次双击建了两个节点').toBe(1);
  expect((await getPlan(page, token, workspaceId)).nodes.filter((n) => n.title === '只建一个')).toHaveLength(1);

  // --- 建失败:表单不关、输入还在、重试仍带同一个位置 -----------------------
  await page.route('**/api/workspaces/*/nodes', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'UNAVAILABLE', message: '暂时不可用' } }) });
  });
  const spot3 = await emptySpot(page);
  const expected3 = await expectedFlowPoint(page, spot3);
  await page.mouse.dblclick(spot3.x, spot3.y);
  await expect(page.getByRole('dialog')).toBeVisible();
  await titleField(page).fill('失败也要留住');
  await page.getByRole('dialog').getByLabel(/说明/).fill('这段说明不该丢');
  await submitButton(page).click();

  // 失败**不关弹窗**:关掉的话用户看到的是"我填了、点了、没了"。
  await expect(page.getByRole('dialog'), '失败之后表单被关掉了').toBeVisible();
  await expect(page.getByRole('dialog').locator('.form-error')).toBeVisible();
  await expect(titleField(page)).toHaveValue('失败也要留住');
  await expect(page.getByRole('dialog').getByLabel(/说明/)).toHaveValue('这段说明不该丢');
  // 位置也在草稿里留着(用户不用重新对准那一点)。
  expect((await getPlan(page, token, workspaceId)).nodes.filter((n) => n.title === '失败也要留住')).toHaveLength(0);

  // 恢复之后重试 —— **位置仍然是刚才双击的那一点**。
  await page.unroute('**/api/workspaces/*/nodes');
  await submitButton(page).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const created = await nodeIdByTitle(page, token, workspaceId, '失败也要留住');
  const placed = await nodeAt(page, created);
  expect(close(placed.x, expected3.x) && close(placed.y, expected3.y),
    `重试之后位置变了:${JSON.stringify(placed)} vs ${JSON.stringify(expected3)}`).toBe(true);
  // 三次 POST:成功那一次、被 503 挡掉那一次、重试那一次。数字写清楚,
  // 免得"重试其实没发出去、界面却显示成功了"这种事混过去。
  expect(posts.length, `POST 次数不对:${JSON.stringify(posts)}`).toBe(3);
});

// ---------------------------------------------------------------------------------
// 验收 4:菜单作用于**被右键的那一个**
// ---------------------------------------------------------------------------------
test('右键只读不写；先选中 A 再右键 B 时，归档问的是 B', async ({ page }) => {
  const { token } = await registerAccount(page, 'menu-target');
  const workspaceId = await createWorkspace(page, token, '菜单对象验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  const a = await createNode(page, token, workspaceId, { parentId: root.id, title: '甲节点', nodeType: 'task' });
  const b = await createNode(page, token, workspaceId, { parentId: root.id, title: '乙节点', nodeType: 'task' });

  const impactUrls: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/archive-impact')) impactUrls.push(request.url());
  });

  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  // --- 右键**不写库** ------------------------------------------------------
  const before = await getPlan(page, token, workspaceId);
  const bBox = await page.locator(`.react-flow__node[data-id="${b}"]`).boundingBox();
  if (!bBox) throw new Error('节点不在画布上');
  await page.mouse.click(bBox.x + bBox.width / 2, bBox.y + bBox.height / 2, { button: 'right' });
  await expect(page.getByRole('menu')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('menu')).toHaveCount(0);

  const after = await getPlan(page, token, workspaceId);
  expect(after.nodes.map((n) => n.id).sort(), '右键改了计划').toEqual(before.nodes.map((n) => n.id).sort());
  expect(after.revisionVersion, '右键产生了计划版本').toBe(before.revisionVersion);
  expect(impactUrls, '右键就去问了归档影响 —— 用户还没说要删').toEqual([]);

  // --- 先选中 A,再右键 B --------------------------------------------------
  await page.locator(`.react-flow__node[data-id="${a}"]`).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  // 选中状态**没被 Esc 清掉** —— 不清才有"A 选中着、B 被右键"这个局面。
  await expect(
    page.locator(`.react-flow__node[data-id="${a}"] .growth-node.is-selected`),
    '关掉详情之后 A 已经不是选中项了,这条测试要验的局面没造出来',
  ).toHaveCount(1);

  await openNodeMenu(page, b);
  await page.getByRole('menu').getByRole('menuitem', { name: '归档' }).click();
  const confirm = page.getByRole('dialog');
  await expect(confirm.getByRole('heading'), '归档问的是 A 而不是 B').toHaveText('归档「乙节点」？');
  // 请求是**异步**发的,标题却来自本地那一份计划 —— 所以等一拍再读 URL,
  // 否则这里会以"一个还没发出去"的方式红,而失败信息指向标题。
  await expect.poll(() => impactUrls.length, '没有去问归档影响').toBe(1);
  expect(impactUrls[0], `归档影响问的是别的节点:${impactUrls[0]}`).toContain(`/nodes/${b}/archive-impact`);

  // 取消之后两个都还在。
  await confirm.getByRole('button', { name: '取消' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const final = await getPlan(page, token, workspaceId);
  expect(final.nodes.map((n) => n.id).sort()).toEqual([root.id, a, b].sort());
});

// ---------------------------------------------------------------------------------
// 验收 5:问不到影响就不许放行
// ---------------------------------------------------------------------------------
test('取不到归档影响时只剩重试与取消，绕不过去', async ({ page }) => {
  const { token } = await registerAccount(page, 'menu-impact-fail');
  const workspaceId = await createWorkspace(page, token, '归档影响验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  const child = await createNode(page, token, workspaceId, { parentId: root.id, title: '要收的', nodeType: 'task' });
  await createNode(page, token, workspaceId, { parentId: child, title: '它的后代', nodeType: 'task' });

  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await waitForInitialFit(page);

  await page.route('**/archive-impact*', (route) =>
    route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'UNAVAILABLE', message: '暂时不可用' } }) }),
  );

  await openNodeMenu(page, child);
  await page.getByRole('menu').getByRole('menuitem', { name: '归档' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  /*
   * 判据有三条,缺一条就放过一种"闭着眼睛删"的实现:
   *
   * 1. **那份数字不在**。一个"拿不到就当 0"的实现会显示"后代 0 个",而实际要收走
   *    的是一整支 —— 用户看着 0 按下去,东西没了。
   * 2. **按钮整个不在**(不是"灰着")。灰着还能被键盘/脚本按到,而这里连
   *    `.archive-actions` 那一块都没渲染。
   * 3. **库里那一行还在**。前两条都是界面上的事,这条才是"没有绕过去"。
   */
  await expect(dialog.locator('.archive-impact'), '问不到影响却照样显示了一份数字').toHaveCount(0);
  await expect(dialog.locator('.archive-actions'), '问不到影响却还是给了按钮').toHaveCount(0);
  await expect(dialog.locator('.form-error')).toBeVisible();
  expect((await getPlan(page, token, workspaceId)).nodes.map((n) => n.id), '绕过去把节点收走了').toContain(child);

  // **出口是关掉重来,不是按一个"照样归档"。** 恢复之后重开一次,数字就回来了。
  await dialog.getByRole('button', { name: '关闭弹窗' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.unroute('**/archive-impact*');
  await openNodeMenu(page, child);
  await page.getByRole('menu').getByRole('menuitem', { name: '归档' }).click();
  await expect(page.getByRole('dialog').locator('.archive-impact')).toContainText('后代 1 个');
  await expect(page.getByRole('dialog').getByRole('button', { name: '归档' })).toBeEnabled();
});
