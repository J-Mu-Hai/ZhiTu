import { expect, test, type Page } from '@playwright/test';
import {
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  registerAccount,
  renderedNodeIds,
  waitForRealPlan,
} from './support/session';

/**
 * 画布状态稳定性:画布子树被卸载、被重建之后,**没提交的输入与视口不许丢**。
 *
 * ## 先记下"哪条路真的会走到这里"
 *
 * 这套界面上画布子树被重建有四种触发,前两种是缺陷,后两种是设计:
 *
 * 1. **切一次视图。** `Workbench.tsx` 的四个视图是一个三元表达式,点一下页签
 *    (`router.replace('?view=…')`)整棵画布子树就卸载了 —— 每次切都发生。
 *    它带走的是 ReactFlow 内部那份平移缩放(`fittedScope` 是组件里的 ref,
 *    跟着一起没),以及将来会长在这棵子树上的正文编辑器。
 * 2. **离开工作台再回来**(浏览器前进后退、点顶栏的"成长空间"再进来)。整页组件
 *    重挂,画布内部的一切重来。
 * 3. 双击节点进入子空间 —— 换一层就该换一份状态,这个隔离要留着。
 * 4. 换空间 —— 同上,而且连 Provider 一起换。
 *
 * **顺带记一条实测出来、和直觉不同的事实:弹窗开着的时候,页签根本点不动。**
 * `<dialog showModal>` 把整页盖住,Playwright 会一直报"dialog intercepts pointer
 * events"。所以"弹窗里打了一半的字被切视图吞掉"**不是**今天能走到的路径 ——
 * 今天能走到的是上面第 2 条(弹窗不拦浏览器自己的前进后退)。两者都会在步骤 4
 * 拆掉单击语义、把正文编辑器从模态里搬出来之后变成同一件事,所以草稿机制按
 * "任何一次卸载都不许丢"来修,而不是按"今天哪条路能走到"来修。
 *
 * ## 为什么这必须是浏览器测试
 *
 * 丢的东西全是**组件内部的临时状态**:弹窗里没提交的字、ReactFlow 的平移缩放。
 * 它们不出现在任何接口响应里,也不在后端。所以断言分三层,缺一层就会把话说过头:
 *
 * - DOM 上看得见的(弹窗、输入框里的字);
 * - 画布的挂载记录(`__zhituCanvasLifecycle`)—— 用来证明**中间真的卸载过**,
 *   否则"回来了"可能只是因为画布压根没走,那是一组自己骗自己的绿;
 * - 视口的 `transform` —— 直接读 React Flow 画出来的那个值,**与产品自己存了什么无关**,
 *   所以"我存了"和"它真的用上了"是两件事,这里验的是后者。
 *
 * ## 最后一条是另一个方向:这些东西**也不许活得比账户长**
 *
 * 草稿存的是模块级的 Map,它不跟着组件走 —— 上面四条说的都是"卸载不该丢",
 * 那第五条要问的是反面:登出、换个人登进来之后,它**必须**已经清了。
 * 机制与为什么这么清写在 `features/auth/provider.tsx` 里那段 effect 上。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

/**
 * 画布挂载过几次、每次是哪个层级。
 *
 * 这个数组由 `PathView` 里那个 effect 写在 `window` 上(同时它也是"组件生命周期记录"):
 * 重挂载会把组件里的一切清掉,**包括用来记数的 ref**,所以它必须记在组件外面。
 * 它不参与任何产品逻辑,只是给这里一个可读的证据。
 */
async function canvasMounts(page: Page): Promise<string[]> {
  return page.evaluate(
    () => (window as unknown as { __zhituCanvasLifecycle?: string[] }).__zhituCanvasLifecycle ?? [],
  );
}

/**
 * 这个数组**封了顶**(`PathView` 里的 `CANVAS_LIFECYCLE_LIMIT`,50 条;理由见那段说明)。
 *
 * 封顶对"涨了没有"这类断言没影响,但**会让"没涨"变成恒真**:数到上限之后再怎么重挂载
 * 长度都不变。所以下面那条"不许重挂载"的用例要先确认没数到上限 —— 数到上限时,
 * 它就该以"这条断言失去意义"红掉,而不是安静地绿。
 * (这个数在 `PathView.tsx` 里,改那边要顺手改这里。)
 */
const LIFECYCLE_LIMIT = 50;

/** React Flow 此刻的平移与缩放。**从 DOM 里读**,与产品自己存了什么无关。 */
async function canvasTransform(page: Page): Promise<string> {
  return (await page.locator('.react-flow__viewport').getAttribute('style')) ?? '';
}

/**
 * 把画布拖开一段,让视口**不等于**初始的那一次 fit。
 *
 * 没有这一步,"切回来之后视口没变"就是一句废话 —— 重新 fit 出来的视口和没动过的
 * 视口长得一模一样,断言会绿得毫无意义。所以这里顺带把前提也验了:拖完必须真的变了,
 * 没变说明这条测试的前提没成立(而不是产品有问题)。
 */
async function panAway(page: Page): Promise<string> {
  const before = await canvasTransform(page);
  const box = await page.locator('.react-flow__pane').boundingBox();
  if (!box) throw new Error('画布没渲染出来,拖不动');
  // 从下缘中间起手:那里没有节点,也不会撞上左下角的缩略图和右下角的控件。
  await page.mouse.move(box.x + box.width / 2, box.y + box.height - 30);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 - 140, box.y + box.height - 110, { steps: 12 });
  await page.mouse.up();
  await expect
    .poll(() => canvasTransform(page), { message: '没能把视口拖开 —— 这条测试的前提没成立,别把它读成产品有问题' })
    .not.toBe(before);
  return canvasTransform(page);
}

/**
 * 在**当前这一个**画布 DOM 元素上留一个印记,用来证明它后来被销毁了。
 *
 * 为什么不数挂载次数:"挂载了几次"是个**环境相关**的量,拿它当断言会两头出错。
 * 开发服务器开着 React StrictMode,一次挂载的 effect 会跑两遍(数字直接翻倍);
 * 空间信息解析出来前后还会各挂一次。而这条测试真正要证的是"中间那棵子树**没了**"
 * —— 那是"视口还在"这句话的前提。印记只可能长在原来那个元素上:元素被复用则印记还在,
 * 元素被重建则读不到它。
 */
async function markCanvas(page: Page): Promise<void> {
  await page.locator('.react-flow').evaluate((element) => {
    (element as unknown as Record<string, unknown>).__zhituProbeMark = true;
  });
}

async function canvasWasRebuilt(page: Page): Promise<boolean> {
  return page
    .locator('.react-flow')
    .evaluate((element) => (element as unknown as Record<string, unknown>).__zhituProbeMark === undefined);
}

/** 弹窗里那个"节点名称"输入框。没开弹窗时读不到,所以调用方先确认弹窗在。 */
const titleField = (page: Page) => page.getByRole('dialog').getByLabel('节点名称');

test('切一次视图，画布确实被重建了，但视口回得来', async ({ page }) => {
  const { token } = await registerAccount(page, 'canvas-view');
  const workspaceId = await createWorkspace(page, token, '切视图验收空间', '换个视图看看画布');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  const mountsBefore = await canvasMounts(page);
  const panned = await panAway(page);
  await markCanvas(page);

  await page.getByRole('tab', { name: '时间线', exact: true }).click();
  await expect(page.locator('.react-flow'), '切走之后画布应该已经不在了').toHaveCount(0);
  await page.getByRole('tab', { name: '路径', exact: true }).click();
  await expect(page.locator('.react-flow')).toBeVisible();

  // 视口必须是用户拖到的那一处,不是被重新 fit 出来的。
  await expect.poll(() => canvasTransform(page), { message: '切视图回来之后视口被重置了' }).toBe(panned);
  // 初始定位那个 180ms 的窗口过完再看一眼:它不该在定位之后又变一次。
  await page.waitForTimeout(400);
  expect(await canvasTransform(page), '视口在定位之后又动了一次').toBe(panned);

  // **然后才问"前提成立吗"。** 顺序是有意的:上面那句"视口还在"单看是**可能自己
  // 骗自己**的 —— 画布如果压根没卸载,它当然还在原地。所以这里补两条:挂载记录涨了,
  // 而且那个 DOM 元素已经不是原来那一个了。放在后面是为了让修复前的运行停在上面那句
  // (用户真正看到的症状),而不是停在这一句(仪器没装上)。
  expect((await canvasMounts(page)).length, '画布没有重挂载,这条测试的前提没成立').toBeGreaterThan(
    mountsBefore.length,
  );
  expect(await canvasWasRebuilt(page), '画布还是原来那棵 DOM 子树,这条测试的前提没成立').toBe(true);
});

test('离开工作台再回到同一层，没提交的输入还在（明确关掉才算丢弃）', async ({ page }) => {
  const { token } = await registerAccount(page, 'canvas-draft');
  const workspaceId = await createWorkspace(page, token, '草稿验收空间', '离开再回来,输入还在不在');
  const draft = '还没想好要不要建';

  // 用界面上的软导航进工作台:`page.goto` 是整页刷新,那验的是"刷新页面",两回事。
  await page.goto('/spaces');
  await page.locator('.space-card', { hasText: '草稿验收空间' }).getByRole('button', { name: '进入工作台' }).click();
  await waitForRealPlan(page);

  await page.getByRole('button', { name: '新建节点' }).click();
  await titleField(page).fill(draft);

  // **弹窗是模态的,页签点不动**(见文件头),所以这里走浏览器自己的后退前进 ——
  // 它不受模态限制,而"离开这个页面再回来"本来也是最常见的一种离开。
  await page.goBack();
  await expect(page).toHaveURL(/\/spaces$/);
  await page.goForward();
  await expect(page).toHaveURL(new RegExp(workspaceId));
  await waitForRealPlan(page);

  await expect(page.getByRole('dialog'), '离开再回来,弹窗被关掉了').toBeVisible();
  await expect(titleField(page), '离开再回来,没提交的输入没了').toHaveValue(draft);

  // 另一半同样要验:**用户明确关掉就是丢弃**,不能下一次又自己冒出来。
  await page.getByRole('dialog').getByRole('button', { name: '关闭弹窗' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.goBack();
  await page.goForward();
  await waitForRealPlan(page);
  await expect(page.getByRole('dialog'), '明确关掉之后它又自己回来了').toHaveCount(0);
});

test('换空间时草稿不串：新空间看不到旧空间的输入，切回去它还在', async ({ page }) => {
  const { token } = await registerAccount(page, 'canvas-space');
  const first = await createWorkspace(page, token, '草稿验收空间一', '第一个空间');
  await createWorkspace(page, token, '草稿验收空间二', '第二个空间');
  const draft = '这是空间一里没提交的东西';

  await page.goto('/spaces');
  await page.locator('.space-card', { hasText: '草稿验收空间一' }).getByRole('button', { name: '进入工作台' }).click();
  await waitForRealPlan(page);
  await page.getByRole('button', { name: '新建节点' }).click();
  await titleField(page).fill(draft);

  // 同上:弹窗开着时模态拦住页面上的一切按钮,所以先退出去(草稿留着),
  // 从空间列表进另一个空间 —— 这也是用户真实的走法。
  await page.goBack();
  await expect(page).toHaveURL(/\/spaces$/);
  await page.locator('.space-card', { hasText: '草稿验收空间二' }).getByRole('button', { name: '进入工作台' }).click();
  await waitForRealPlan(page);

  // 第二个空间里**不许**出现第一个空间正在编辑的东西。
  await expect(page.getByRole('dialog'), '换空间之后弹窗跟着串过来了').toHaveCount(0);
  await page.getByRole('button', { name: '新建节点' }).click();
  await expect(titleField(page), '第二个空间里的新建弹窗带着第一个空间的字').toHaveValue('');
  // 这个弹窗本来就是空手打开的,关掉它不丢任何东西 —— 但它挡着"全部空间"那个按钮
  // (模态,见文件头),所以要先收起来。
  await page.getByRole('dialog').getByRole('button', { name: '关闭弹窗' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: '全部空间' }).click();
  await expect(page).toHaveURL(/\/spaces$/);

  // 切回第一个空间:那一份草稿还在 —— 用户没说过要丢,而"点错了空间"是很常见的事。
  await page.locator('.space-card', { hasText: '草稿验收空间一' }).getByRole('button', { name: '进入工作台' }).click();
  await expect(page).toHaveURL(new RegExp(first));
  await waitForRealPlan(page);
  await expect(titleField(page), '切回原来的空间,没提交的输入应该还在').toHaveValue(draft);
});

test('一次真实的计划写入之后，画布不重建、视口不被打回初始位置', async ({ page }) => {
  const { token } = await registerAccount(page, 'canvas-refresh');
  const workspaceId = await createWorkspace(page, token, '重取计划验收空间', '写一次计划,看视口');
  const plan = await getPlan(page, token, workspaceId);
  const root = plan.nodes[0];
  const doomed = '这一条会被收起来';
  await createNode(page, token, workspaceId, { parentId: root.id, title: doomed });

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  const panned = await panAway(page);
  const mountsBefore = await canvasMounts(page);

  // 真的写一次计划:收走一个节点会让后端推进版本号,界面随后**重取整份 /plan**。
  //
  // 两下,不是一下 —— 垃圾桶现在只**问**,真正写库的是确认框里那个按钮(步骤 3C:
  // 按下去之前先让你看到会带走什么)。这条用例验的是"写完之后画布不重建",所以
  // 那两下都得走完,否则它测的是一次空点击。
  await page
    .locator('.react-flow__node')
    .filter({ hasText: doomed })
    .getByRole('button', { name: `归档${doomed}及其子节点` })
    .click();
  await page.getByRole('dialog').getByRole('button', { name: '归档' }).click();
  await expect(page.locator('.react-flow__node')).toHaveCount(1);

  // 库里那一条真的没了 —— 不然上面那个"只剩根节点"可能只是画布少画了一个。
  const after = await getPlan(page, token, workspaceId);
  expect(after.nodes.map((node) => node.title)).not.toContain(doomed);

  // 先确认没数到上限:数到上限的话,"长度没变"这句话恒真,下面那条断言就没有意义了。
  expect(mountsBefore.length, '挂载记录已经封顶,下面那条"没有重挂载"会变成恒真的断言').toBeLessThan(
    LIFECYCLE_LIMIT,
  );
  expect((await canvasMounts(page)).length, '计划重取把画布整棵子树重建了').toBe(mountsBefore.length);
  expect(await canvasTransform(page), '计划重取把视口打回了初始位置').toBe(panned);
});

test('换个人登进来，上一个人没提交的输入不跟过来', async ({ page }) => {
  // 两个账户:甲是现在这个浏览器上登录的,乙只在后端存在(`signIn: false`)。
  // 两个人都要有一个空间 —— 理由见下面那段"为什么非得站在还没到的画布上"。
  const jia = await registerAccount(page, 'draft-iso-a');
  // 空间 id 不存下来:这个用例是按卡片上的名字点的,存着它反而会让人以为哪一步在用它。
  await createWorkspace(page, jia.token, '草稿隔离甲空间', '甲的空间');
  const yi = await registerAccount(page, 'draft-iso-b', { signIn: false });
  await createWorkspace(page, yi.token, '草稿隔离乙空间', '乙的空间');
  const marker = '甲写了一半的东西';

  /**
   * 把"读空间详情"这一个请求**挂住不答**。
   *
   * 于是 provider 停在 `kind: 'none'` 上:画布是那棵只有根目标的占位树,而
   * **"新建节点"是可点的** —— `canCreate` 只跟 `planLoading` 有关,占位空间没有
   * 计划要读,所以它是 true。这正是用户点"进入工作台"之后那几百毫秒的样子
   * (空间详情还在路上),这里只是把这扇窗拉长到能打字。**不是造一个走不到的状态。**
   *
   * 为什么非要停在这儿:草稿的键是 `空间:层级`,真实空间那一半是各自的 UUID,
   * 两个账户串不到一起;唯一**所有账户共用**的是"还没进空间"那一份 ——
   * `'none'`(见 `provider.tsx` 的 `NO_SPACE`)配层级哨兵 `'goal'`,也就是
   * `none:goal`。不站到这个键上,这条测试就没有能失败的地方,那样的绿是自己骗自己。
   */
  const detailRequest = /\/api\/workspaces\/[0-9a-fA-F-]{36}$/;
  const holdSpaceLookup = () => page.route(detailRequest, () => new Promise<void>(() => {}));

  // 先把会话立起来(这一步是整页加载,那一刻草稿还是空的 —— 换账户之前
  // 唯一允许的刷新就是这一次)。
  await page.goto('/spaces');
  await expect(page.locator('.space-card', { hasText: '草稿隔离甲空间' })).toBeVisible();

  await holdSpaceLookup();
  await page.locator('.space-card', { hasText: '草稿隔离甲空间' }).getByRole('button', { name: '进入工作台' }).click();
  // 等画布真的画出来。用节点数而不是工具栏那个按钮:下面乙的那一处会用同一个
  // 按钮名,而到时候多出来的第二个正是要抓的东西 —— 闸门不能建在待测的东西上。
  await expect(page.locator('.react-flow__node'), '画布没渲染出来').toHaveCount(1);
  // 前提:这里真的是一棵占位树(根还是哨兵值 `'goal'`),不是"甲那个空间"的画布。
  // 少了这一条,断言挂在哪个键上就没人知道了 —— 而这条测试的全部意义就在那个键上。
  expect(await renderedNodeIds(page), '画布不是占位树,这条测试的前提没成立').toEqual(['goal']);

  await page.getByRole('button', { name: '新建节点' }).click();
  await titleField(page).fill(marker);
  await expect(titleField(page)).toHaveValue(marker);

  // 从这里到"乙看到画布"**全程软导航,一次整页刷新都没有**:草稿活在模块里,
  // 整页刷新会把那个 Map 一起清掉 —— 那样这条测试验的就成了"刷新清空了它",
  // 跟换不换账户没有关系。
  //
  // 弹窗是模态的,页面上的按钮点不动(见文件头),所以先用浏览器自己的后退离开
  // 工作台 —— 它不受模态限制,而且草稿按设计不跟着走。
  // (这一步也必须走界面上的软导航:上一次写成 `page.goto` 时,浏览器用
  // 往返缓存把**当时还没登录**的那个 /login 文档原样端了回来,于是测试卡在
  // 登录页 —— 失败长得像"登出坏了",其实是历史里挑错了入口。)
  await page.goBack();
  await expect(page).toHaveURL(/\/spaces$/);
  await page.locator('.top-profile').click();
  await expect(page).toHaveURL(/\/me$/);
  await page.getByRole('button', { name: '退出当前账户' }).click();

  await expect(page.getByLabel('手机号 / 邮箱')).toBeVisible();
  await page.getByLabel('手机号 / 邮箱').fill(yi.email);
  await page.getByLabel('密码').fill(yi.password);
  await page.getByRole('button', { name: '登录知途' }).click();
  await expect(page).toHaveURL(/\/spaces$/);

  // 乙也站到同一个 `none:goal` 上:上面那条挂住规则**一直有效**,所以她的空间
  // 详情同样到不了。(代价是空间页上那几个统计数字会停在"—"——它读的也是这个接口。
  // 卡片本身来自列表接口,不受影响。)

  await page.locator('.space-card', { hasText: '草稿隔离乙空间' }).getByRole('button', { name: '进入工作台' }).click();
  await expect(page.locator('.react-flow__node'), '画布没渲染出来').toHaveCount(1);
  expect(await renderedNodeIds(page), '画布不是占位树,这条测试的前提没成立').toEqual(['goal']);

  // 甲的东西一样都不许出现。**先看弹窗**:草稿里记着 `dialog: 'node'`,没清掉的话
  // 它会自己开着站在乙面前 —— 那是用户最先看到的东西,也是这条测试变红时该报的那一句。
  await expect(page.getByRole('dialog'), '换账户之后,甲那边开着的新建弹窗跟着过来了').toHaveCount(0);
  await page.getByRole('button', { name: '新建节点' }).click();
  await expect(titleField(page), '乙的新建弹窗里带着甲打的字').toHaveValue('');
});
