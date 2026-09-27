/**
 * 右键菜单这个**原语**本身(§9.1.1 验收 6)。
 *
 * ## 为什么要单独一个文件,而不是并进 `canvas-create.spec.ts`
 *
 * 那个文件验的是"菜单被接到了哪个节点上""归档前先问"—— 也就是**业务**。这个文件验的是
 * 菜单作为一个控件的四件事,和业务无关,换任何一个菜单来都该成立:
 *
 * 1. **边缘夹紧**:菜单不能跑出画布。跑出去的表现不是"少了一截"那么明显 —— 它会盖在
 *    工具栏或侧栏上,用户点"归档"点到的是别的东西。
 * 2. **`.focus()` 真的成功了**。这一条最值得单独钉:菜单以前是在
 *    `visibility: hidden` 那一帧聚焦的,而**隐藏元素不可聚焦** —— `.focus()` 是一次
 *    空操作,不报错、不抛异常。后果是方向键没反应、`Esc` 关不掉菜单,而三条打开路径里
 *    有两条看起来是好的(焦点恰好留在上一份菜单里)。所以判据必须是
 *    `document.activeElement`,不能是"按键之后菜单关了"。
 * 3. **`Esc` 之后焦点回到触发器**,不是掉到 `body`。
 * 4. **原生文本菜单没有被吃掉**。整仓只有画布与节点两处 `preventDefault`;一旦有人
 *    图省事挂一个全局 `contextmenu` 监听,详情弹窗里"选中一段字 → 右键 → 查一下"
 *    就没了 —— 而它在那个输入框里是最正常的操作之一。
 *
 * ## 断言都落在浏览器真的认的那件事上
 *
 * `toBeFocused()` 读的就是 `document.activeElement`;菜单的越界用的是它自己的
 * `boundingBox()` 与画布矩形比。**不读组件的 state、不读 `aria-expanded` 当替身** ——
 * 后者是给屏幕阅读器的,判"夹紧"和"聚焦"用它就成了自说自话。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  openSpacePage,
  registerAccount,
  waitForRealPlan,
  type TestAccount,
} from './support/session';

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

/** 一个空间、根下一个节点。菜单只有节点上才有,所以至少要有一个。 */
async function scene(page: Page, prefix: string) {
  const account = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, token(account), '菜单原语验收空间');
  const root = (await getPlan(page, token(account), workspaceId)).nodes[0];
  const node = await createNode(page, token(account), workspaceId, {
    parentId: root.id,
    title: '要开菜单的节点',
    nodeType: 'task',
  });
  return { account, workspaceId, rootId: root.id, nodeId: node };
}

const token = (account: TestAccount) => account.token;

const card = (page: Page, nodeId: string) => page.locator(`.react-flow__node[data-id="${nodeId}"]`);
const moreButton = (page: Page, nodeId: string) => card(page, nodeId).locator('.node-more');

/** 画布容器那一层。菜单就是按它的矩形夹紧的(`PathView` 把 `canvasRef` 传给了菜单)。 */
const canvas = (page: Page) => page.locator('.path-canvas');

async function viewportAt(page: Page): Promise<{ x: number; y: number; zoom: number }> {
  const style = (await page.locator('.react-flow__viewport').getAttribute('style')) ?? '';
  const found = /translate\((-?[\d.]+)px,\s*(-?[\d.]+)px\)\s*scale\(([\d.]+)\)/.exec(style);
  if (!found) throw new Error(`读不出视口(style="${style}")`);
  return { x: Number(found[1]), y: Number(found[2]), zoom: Number(found[3]) };
}

/**
 * 把画布拖开一个**精确**的量。
 *
 * 拖动=平移是 React Flow 的性质(一比一),所以这里可以把"要挪多远"直接当成参数用,
 * 而不是先拖一下再量、再补一次。落脚点必须真的是空白(`elementFromPoint` 判),
 * 否则会把某个节点拖走 —— 那样"按钮差 40 像素"这个前提就悄悄不成立了。
 */
async function panBy(page: Page, dx: number, dy: number): Promise<void> {
  const box = await page.locator('.react-flow__pane').boundingBox();
  if (!box) throw new Error('画布没渲染出来');
  let from: { x: number; y: number } | null = null;
  for (const [fx, fy] of [[0.06, 0.94], [0.5, 0.94], [0.94, 0.94], [0.06, 0.06]]) {
    const x = box.x + box.width * fx;
    const y = box.y + box.height * fy;
    const isPane = await page.evaluate(
      ([px, py]) => document.elementFromPoint(px, py)?.classList.contains('react-flow__pane') ?? false,
      [x, y] as const,
    );
    if (isPane) { from = { x, y }; break; }
  }
  if (!from) throw new Error('找不到可以下手的空白 —— 平移这件事的前提不成立');

  const before = await viewportAt(page);
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(from.x + dx, from.y + dy, { steps: 12 });
  await page.mouse.up();
  const after = await viewportAt(page);
  expect(
    Math.abs(after.x - (before.x + dx)) < 2 && Math.abs(after.y - (before.y + dy)) < 2,
    `平移没按预期的量走:${JSON.stringify(before)} -> ${JSON.stringify(after)},期望位移 (${dx}, ${dy})`,
  ).toBe(true);
}

// ---------------------------------------------------------------------------------
// 1. 边缘:菜单不出画布
// ---------------------------------------------------------------------------------

/**
 * 画布上**属于产品自己的浮层**。按钮落到它们底下就点不到了 —— 而失败会报成
 * "元素被别的元素挡住",和被验的夹紧逻辑毫无关系。
 *
 * React Flow 的缩放控件固定在右下角(`vertical bottom right`),工具栏也在画布上,
 * 所以"右下角"这个最自然的选择恰恰是唯一不能用的那个。
 */
const CANVAS_CHROME = '.react-flow__controls, .react-flow__minimap, .space-floating-tools, [data-testid="rf__controls"]';

/** 这个点上没有本产品自己的浮层吗(用浏览器真正的命中测试问)。 */
async function unoccupied(page: Page, point: { x: number; y: number }): Promise<boolean> {
  return page.evaluate(
    ([x, y, chrome]) => !document.elementFromPoint(x, y)?.closest(chrome),
    [point.x, point.y, CANVAS_CHROME] as const,
  );
}

test('节点贴着画布边缘时，菜单翻到另一侧，整块仍在画布内', async ({ page }) => {
  const { workspaceId, nodeId } = await scene(page, 'menu-clamp');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await page.waitForTimeout(700);

  const area = await canvas(page).boundingBox();
  if (!area) throw new Error('画布不在');

  /*
   * 把按钮**右下角**挪到离画布边缘 40 像素的地方。
   *
   * 40 这个数是有意选的:菜单最小宽度 168,比 40 宽得多,所以它一定越界,一定走
   * "翻到另一侧"那条路。离得再远一点(比如 200)菜单就正好放得下,这条测试会以
   * "菜单本来就没越界"的方式通过 —— 什么都没验到。
   *
   * 两条边都试(横翻与竖翻是两段代码),哪条边的落点没有浮层就验哪条。一条都试不了
   * 就报"前提不成立",不静默跳过。
   */
  const gap = 40;
  const verified: string[] = [];
  for (const [side, label] of [['right', '右边'], ['bottom', '下边']] as const) {
    const button = await moreButton(page, nodeId).boundingBox();
    if (!button) throw new Error('按钮不在画布上');
    const dx = side === 'right' ? area.x + area.width - gap - (button.x + button.width) : 0;
    const dy = side === 'bottom' ? area.y + area.height - gap - (button.y + button.height) : 0;
    if (!(await unoccupied(page, { x: button.x + dx + button.width / 2, y: button.y + dy + button.height / 2 }))) continue;

    await panBy(page, dx, dy);
    const moved = await moreButton(page, nodeId).boundingBox();
    if (!moved) throw new Error('平移之后按钮不见了');
    const distance = side === 'right'
      ? area.x + area.width - gap - (moved.x + moved.width)
      : area.y + area.height - gap - (moved.y + moved.height);
    expect(Math.abs(distance) < 6, `没把按钮挪到贴着${label}的位置:${JSON.stringify(moved)} 画布 ${JSON.stringify(area)}`).toBe(true);

    await card(page, nodeId).hover();
    await moreButton(page, nodeId).click();
    const menu = page.getByRole('menu');
    await expect(menu).toBeVisible();

    const box = await menu.boundingBox();
    if (!box) throw new Error('菜单没有盒子 —— 它没画出来');
    // 四条边都要在画布里。少验一条就放过一种"从这一边漏出去"的实现。
    expect(box.x, `${label}:菜单左边越界:${JSON.stringify(box)} 画布 ${JSON.stringify(area)}`).toBeGreaterThanOrEqual(area.x - 1);
    expect(box.y, `${label}:菜单上边越界:${JSON.stringify(box)} 画布 ${JSON.stringify(area)}`).toBeGreaterThanOrEqual(area.y - 1);
    expect(box.x + box.width, `${label}:菜单右边越界:${JSON.stringify(box)} 画布 ${JSON.stringify(area)}`).toBeLessThanOrEqual(area.x + area.width + 1);
    expect(box.y + box.height, `${label}:菜单下边越界:${JSON.stringify(box)} 画布 ${JSON.stringify(area)}`).toBeLessThanOrEqual(area.y + area.height + 1);
    verified.push(label);

    await page.keyboard.press('Escape');
    await expect(page.getByRole('menu')).toHaveCount(0);
  }

  expect(verified, '画布上没有一条边既贴得上又没有浮层挡着 —— 这条断言的前提不成立').not.toEqual([]);
});

// ---------------------------------------------------------------------------------
// 2. Esc:关掉,而且把焦点还给触发器
// ---------------------------------------------------------------------------------
test('Esc 关掉菜单，焦点回到那个「更多」按钮', async ({ page }) => {
  const { workspaceId, nodeId } = await scene(page, 'menu-escape');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await page.waitForTimeout(700);

  await card(page, nodeId).hover();
  await moreButton(page, nodeId).click();
  await expect(page.getByRole('menu')).toBeVisible();

  // 焦点**真的**进了菜单。这一条就是 `visibility: hidden` 那一帧聚焦要红的地方:
  // 那种实现下菜单照样"打开"了(它可见、也有 role),只是键盘对它没有任何作用。
  await expect(page.getByRole('menu').getByRole('menuitem').first()).toBeFocused();
  // 触发器同时要告诉辅助技术"我开出来的东西现在开着"。
  await expect(moreButton(page, nodeId)).toHaveAttribute('aria-expanded', 'true');

  await page.keyboard.press('Escape');
  await expect(page.getByRole('menu')).toHaveCount(0);
  await expect(moreButton(page, nodeId), '关掉菜单之后焦点掉到了别处').toBeFocused();
  await expect(moreButton(page, nodeId), '菜单关了但按钮还说自己是展开的').not.toHaveAttribute('aria-expanded', 'true');
});

// ---------------------------------------------------------------------------------
// 3. 键盘:Shift+F10 开菜单,方向键在菜单项之间走
// ---------------------------------------------------------------------------------
test('Shift+F10 打开菜单，方向键与 Home/End 在菜单项之间移动焦点', async ({ page }) => {
  const { workspaceId, nodeId } = await scene(page, 'menu-keyboard');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await page.waitForTimeout(700);

  const items = page.getByRole('menu').getByRole('menuitem');

  await moreButton(page, nodeId).focus();
  await page.keyboard.press('Shift+F10');
  await expect(page.getByRole('menu')).toBeVisible();
  const count = await items.count();
  expect(count, '菜单里一项都没有').toBeGreaterThan(1);
  await expect(items.first()).toBeFocused();

  /*
   * 方向键必须动**焦点**,不是只动一个高亮的样式。
   *
   * 菜单挂在 `document.body` 上,但 React 的事件是沿组件树冒泡的 —— 不
   * `stopPropagation` 的话,这两下方向键会冒到 ReactFlow 那一层去**平移画布**
   * (它在节点被选中时接管方向键)。所以下面每移一次,都顺手确认画布没被按着走。
   */
  const viewportBefore = await viewportAt(page);
  await page.keyboard.press('ArrowDown');
  await expect(items.nth(1)).toBeFocused();
  await page.keyboard.press('ArrowUp');
  await expect(items.first()).toBeFocused();
  // 绕回去:在第一项上按上,应当是最后一项。
  await page.keyboard.press('ArrowUp');
  await expect(items.nth(count - 1)).toBeFocused();
  await page.keyboard.press('End');
  await expect(items.nth(count - 1)).toBeFocused();
  await page.keyboard.press('Home');
  await expect(items.first()).toBeFocused();
  expect(await viewportAt(page), '在菜单里按方向键把画布平移了').toEqual(viewportBefore);

  await page.keyboard.press('Escape');
  await expect(page.getByRole('menu')).toHaveCount(0);
});

// ---------------------------------------------------------------------------------
// 4. 原生文本菜单:画布上吃掉,输入框里必须留着
// ---------------------------------------------------------------------------------
test('画布上的右键被接管，详情编辑框里的原生菜单原封不动', async ({ page }) => {
  const { workspaceId, nodeId } = await scene(page, 'menu-native');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await page.waitForTimeout(700);

  /*
   * 用 `dispatchEvent` 派发一个真的 `contextmenu`,读它的 `defaultPrevented`。
   *
   * 不走"真的按右键再看系统菜单有没有弹出来":那件事在无头浏览器里读不到,而绕道剪贴板
   * (选中 → 右键复制 → 读剪贴板)在 CI 上是出了名的脆。`defaultPrevented` 问的正是
   * 被验的那件事:**有没有人把这次右键取消掉**。取消掉了,原生菜单就不会出现。
   */
  const preventedOn = (locator: ReturnType<typeof page.locator>) => locator.evaluate((el) => {
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    el.dispatchEvent(event);
    return event.defaultPrevented;
  });

  // 正对照:画布上的右键**应当**被接管 —— 少了这半句,一个"谁都不接管"的实现也全绿,
  // 而那种实现意味着右键菜单这个功能整个不存在。
  expect(await preventedOn(page.locator('.react-flow__pane')), '画布上的右键没有被接管').toBe(true);
  await expect(page.getByRole('menu')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('menu')).toHaveCount(0);

  // 打开详情,在正文那一段字里派发同一次右键。
  await card(page, nodeId).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('heading')).toContainText('编辑节点');
  const body = dialog.getByLabel('详细说明');
  await body.fill('这段正文里要能用系统菜单查一个词。');
  expect(await preventedOn(body), '详情编辑框里的原生右键菜单被吃掉了').toBe(false);
  await expect(page.getByRole('menu'), '在编辑框里右键开出了画布菜单').toHaveCount(0);
});

// ---------------------------------------------------------------------------------
// 5. 触屏:菜单入口不靠悬停也存在
// ---------------------------------------------------------------------------------
test('触屏上没有悬停，入口恒显而且能开菜单', async ({ browser }) => {
  /*
   * `isMobile` + `hasTouch` 才会让 Chromium 报出 `hover: none`/`pointer: coarse` ——
   * 只设 `hasTouch` 不够。而**这正是产品那个 `@media (hover: none)` 读的东西**,
   * 所以下面第一条断言不是装饰:模拟条件不成立的话,这条测试会以"入口一直看得见"
   * 的方式通过,而它其实一次都没有在触屏的口径下跑过。
   */
  const context = await browser.newContext({
    hasTouch: true,
    isMobile: true,
    viewport: { width: 1024, height: 768 },
  });
  const page = await context.newPage();
  try {
    expect(
      await page.evaluate(() => matchMedia('(hover: none)').matches),
      '这个上下文没有报出 hover: none —— 触屏这条断言的前提不成立',
    ).toBe(true);

    // `scene` 里的 `registerAccount` 会先打开 `/login` 再把令牌写进 localStorage ——
    // 新上下文是空的,而那一步正是"这台浏览器登录了"的全部。
    const { workspaceId, nodeId } = await scene(page, 'menu-touch');
    await openSpacePage(page, '/workbench', workspaceId);
    await waitForRealPlan(page);
    await page.waitForTimeout(700);

    // 指针**从没进过这张卡片** —— 没有悬停,入口照样得在。
    await expect(moreButton(page, nodeId), '触屏上入口是隐的 —— 等于没有入口').toHaveCSS('opacity', '1');

    // 触屏没有右键,所以这个按钮是唯一的入口:它得真的能开出菜单来,不只是看得见。
    await moreButton(page, nodeId).click();
    await expect(page.getByRole('menu')).toBeVisible();
    await expect(page.getByRole('menu').getByRole('menuitem', { name: '归档' })).toBeVisible();
  } finally {
    await context.close();
  }
});
