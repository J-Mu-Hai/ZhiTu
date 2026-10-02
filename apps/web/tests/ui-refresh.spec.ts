import { expect, test, type Locator } from '@playwright/test';
import { createWorkspace, registerAccount, openSpacePage, createNode, getPlan, scheduleEverything, waitForRealPlan } from './support/session';
import { artifactPath } from './support/artifacts';

test('空间列表紧凑可滚动，桌面与手机均能创建空间', async ({ page }) => {
  const { token } = await registerAccount(page, 'ui-spaces');
  for (let i = 1; i <= 10; i++) await createWorkspace(page, token, `分析空间 ${i}`);
  await page.goto('/spaces');
  await expect(page.locator('.space-card')).toHaveCount(10);
  const card = await page.locator('.space-card').first().boundingBox();
  expect(card!.height).toBeLessThan(280);
  await page.screenshot({ path: artifactPath('ui-spaces-desktop.png') });
  await page.locator('.new-space-card').scrollIntoViewIfNeeded();
  await page.locator('.new-space-card').click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.spaces-grid')).toHaveCSS('grid-template-columns', /\d+px/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: artifactPath('ui-spaces-mobile.png') });
});

test('随笔收起保留输入，发布后左右阅读与筛选', async ({ page }) => {
  const { token } = await registerAccount(page, 'ui-journal');
  const id = await createWorkspace(page, token, '随笔布局');
  await openSpacePage(page, '/journal', id);
  await expect(page.getByLabel('此刻的想法')).toHaveCount(0);
  await page.locator('.journal-compose-trigger').click();
  await page.getByLabel('此刻的想法').fill('一段安静的思考\n这是可以完整阅读的正文。');
  await page.getByRole('button', { name: '收起随笔输入框' }).click();
  await page.locator('.journal-compose-trigger').click();
  await expect(page.getByLabel('此刻的想法')).toHaveValue('一段安静的思考\n这是可以完整阅读的正文。');
  await page.getByRole('button', { name: '发布', exact: true }).click();
  await expect(page.locator('.journal-reader')).toContainText('这是可以完整阅读的正文。');
  await page.screenshot({ path: artifactPath('ui-journal-desktop.png') });
  await page.getByRole('button', { name: '科研', exact: true }).click();
  await expect(page.locator('.journal-list-item')).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test('首页真实周排期与紧凑工作台保留四种视图', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const { token } = await registerAccount(page, 'ui-workbench');
  const id = await createWorkspace(page, token, '成长规划');
  const root = (await getPlan(page, token, id)).nodes[0];
  await createNode(page, token, id, { parentId: root.id, title: '整理课程与考核要求', nodeType: 'task', estimateMinutes: 60 });
  await scheduleEverything(page, token, id);
  await openSpacePage(page, '/today', id);
  await expect(page.locator('.week-selected-list')).toContainText('整理课程与考核要求');
  await page.getByRole('button', { name: '下一周' }).click();
  await expect(page.locator('.week-selected-list')).toContainText('这一天还没有排期');
  await page.getByRole('button', { name: '本周', exact: true }).click();
  await expect(page.locator('.week-selected-list')).toContainText('整理课程与考核要求');
  await page.screenshot({ path: artifactPath('ui-today-desktop.png') });
  await openSpacePage(page, '/workbench', id);
  for (const tab of ['路径', '时间线', '任务', '排期']) await expect(page.getByRole('tab', { name: tab, exact: true })).toBeVisible();
  // 左上角的「全部空间 / 当前空间名」整组控件已经移除。
  await expect(page.locator('.space-breadcrumb')).toHaveCount(0);
  await expect(page.locator('.workspace-switch')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '全部空间' })).toHaveCount(0);
  // 删掉左侧控件之后,四个视图标签仍然**视觉居中**(相对它们所在的画布容器)。
  const workspaceBox = await page.locator('.workspace').boundingBox();
  const tabsBox = await page.locator('.view-tabs').boundingBox();
  expect(workspaceBox && tabsBox, '画布或视图标签没有尺寸,居中断言无从谈起').toBeTruthy();
  const tabsCenter = tabsBox!.x + tabsBox!.width / 2;
  const workspaceCenter = workspaceBox!.x + workspaceBox!.width / 2;
  expect(
    Math.abs(tabsCenter - workspaceCenter),
    `视图标签中心 ${Math.round(tabsCenter)} 偏离画布中心 ${Math.round(workspaceCenter)}`,
  ).toBeLessThan(40);
  const dock = await page.locator('.floating-conversation').boundingBox();
  expect(dock!.width).toBeLessThan(page.viewportSize()!.width * .35);
  await expect(page.locator('.canvas-tools-popover')).not.toBeVisible();
  await page.locator('.canvas-tools-menu > summary').click();
  await expect(page.locator('.canvas-tools-popover')).toBeVisible();
  await expect(page.getByRole('button', { name: '建立关系', exact: true })).toBeVisible();
  await page.locator('.canvas-tools-menu > summary').click();
  await page.screenshot({ path: artifactPath('ui-workbench-desktop.png') });
  await page.getByRole('button', { name: '让对话内容消失', exact: true }).click();
  await expect(page.locator('.conversation-overlay')).toBeHidden();
  await page.getByRole('button', { name: '展开对话', exact: true }).click();
  await expect(page.locator('.conversation-overlay')).toBeVisible();
  expect(errors).toEqual([]);
});

/**
 * 手机工作台(390):画布优先,AI 是底部抽屉。
 *
 * 这一条守的是"**手机上第一眼看到的是画布**"这件事。原来这一档没有断点,桌面的三块浮动
 * 组件竖排两层压在画布顶上,AI 面板默认展开、盖住 y=288 往下全部 —— 根节点在 y=400,
 * 也就是说打开工作台看见的第一样东西是一个空状态面板。这里逐条把它钉住:
 *
 * 1. 顶部浮动组件占**一行**(顶边彼此相差不超过 8px,且整行落在 120px 以内);
 * 2. AI 面板默认**收起**;
 * 3. 根节点**正中心点得到** —— "完整落在视口里"不等于"点得到",第一版只量了边框有没有
 *    出屏,于是"被面板整个盖住"被记成了"完整可见";
 * 4. 工具条与缩放按钮**不重叠**;
 * 5. 点开之后是**底部抽屉**:铺满宽度、顶边留出画布,顶部那条浮动组件仍然点得到。
 *
 * 判据都用 `elementFromPoint` 与 `getBoundingClientRect`,不比对具体像素值 —— 这一档
 * 会继续调,钉死数字的话每一次调都得改测试,而它本来该守的是"点得到/没叠上"。
 */
test('手机工作台上画布优先，AI 面板是底部抽屉', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const { token } = await registerAccount(page, 'ui-workbench-mobile');
  const id = await createWorkspace(page, token, '手机工作台');
  const root = (await getPlan(page, token, id)).nodes[0];
  await createNode(page, token, id, { parentId: root.id, title: '整理课程与考核要求', nodeType: 'task', estimateMinutes: 60 });

  await page.setViewportSize({ width: 390, height: 844 });
  await openSpacePage(page, '/workbench', id);
  await expect(page.locator('.react-flow__node').first()).toBeVisible();
  await expect(page.locator('.conversation-overlay')).toBeHidden();
  await expect(page.getByRole('tab')).toHaveCount(4);
  await page.screenshot({ path: artifactPath('ui-workbench-mobile.png') });

  /**
   * 正中心那一点归谁。收一个 Locator 而不是选择器字符串 —— 画布上这时有两个节点
   * (根目标与刚建的那条任务),字符串选择器在这里是**歧义**的。
   */
  const centreHit = (target: Locator) => target.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return {
      self: top === element || element.contains(top),
      blockedBy: top ? `${top.tagName.toLowerCase()}.${String((top as HTMLElement).className)}` : 'null',
    };
  });

  // 1. 顶部浮动组件在同一行,而且整行不深。左上角的面包屑已移除,只留视图切换与
  //    “展开对话”;两行的话顶边会差几十像素 —— 容差取 8px。
  const tops = await page.evaluate(() => ['.view-tabs', '.reopen-chat']
    .map(selector => document.querySelector(selector))
    .filter((element): element is Element => element !== null)
    .map(element => Math.round(element.getBoundingClientRect().top)));
  expect(tops.length).toBe(2);
  expect(Math.max(...tops) - Math.min(...tops), `顶部浮动组件的顶边参差 ${tops.join('/')}`).toBeLessThanOrEqual(8);
  expect(Math.max(...tops), '顶部浮动组件的最下沿已经压到画布很深处').toBeLessThan(120);

  // 2. 根节点正中心点得到。ReactFlow 给每个节点挂的是 `role="group"` + 节点标题,
  //    所以这里能按名字点名要根目标那一个。
  const node = await centreHit(page.getByRole('group', { name: '手机工作台' }));
  expect(node.self, `根节点的正中心被 ${node.blockedBy} 盖住了`).toBe(true);

  // 3. 右下角的缩放控件与「聚焦所选」按钮已经移除 —— 它们不再出现,也不占位。
  await expect(page.locator('.react-flow__controls')).toHaveCount(0);
  await expect(page.locator('.focus-button')).toHaveCount(0);

  // 4. 抽屉:铺满宽度,顶上留出画布,顶部那条浮动组件仍然点得到。
  await page.getByRole('button', { name: '展开对话', exact: true }).click();
  await expect(page.locator('.conversation-overlay')).toBeVisible();
  const drawer = await page.locator('.conversation-overlay').boundingBox();
  if (!drawer) throw new Error('AI 抽屉没有尺寸,断言无从谈起');
  expect(drawer.width, '抽屉没有铺满宽度').toBeGreaterThanOrEqual(390 * .98);
  expect(drawer.y, '抽屉顶到屏幕顶上去了,画布一点没留').toBeGreaterThanOrEqual(100);
  const tabs = await centreHit(page.locator('.view-tabs'));
  expect(tabs.self, `抽屉拉开之后视图切换被 ${tabs.blockedBy} 盖住了`).toBe(true);
  await page.screenshot({ path: artifactPath('ui-workbench-mobile-drawer.png') });

  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(errors).toEqual([]);
});

/**
 * 对话区收口:右侧“一起思考”是对话与输入区,不是系统诊断面板。
 *
 * 这一条钉三件事:
 * 1. `brief-missing` 与 `strategy-hint` 两条常驻诊断**不再渲染**(不是 CSS 隐藏);
 * 2. 没有战略/没有执行计划时,`replan` 入口完全不占空间;
 * 3. 没有问题也没有选中项时,不再有“选择画布中的节点”这条常驻空话。
 */
test('对话区不再挂常驻诊断，replan 只用在有执行计划时出现', async ({ page }) => {
  const { token } = await registerAccount(page, 'ui-conversation');
  const id = await createWorkspace(page, token, '对话精简空间');
  const root = (await getPlan(page, token, id)).nodes[0];
  await createNode(page, token, id, { parentId: root.id, title: '普通任务', nodeType: 'task', estimateMinutes: 60 });
  await openSpacePage(page, '/workbench', id);
  await waitForRealPlan(page);
  await page.waitForTimeout(400);

  // 两条常驻诊断提示彻底不渲染。
  await expect(page.locator('.brief-missing')).toHaveCount(0);
  await expect(page.locator('.strategy-hint')).toHaveCount(0);
  // 没有已确认战略、也没有执行计划:replan 入口整块不渲染,不占一行。
  await expect(page.getByRole('button', { name: '按最近的执行情况调整计划' })).toHaveCount(0);
  // 没有问题、也没选节点 -> “选择画布中的节点”那条常驻说明不渲染。
  await expect(page.locator('.context-hint')).toHaveCount(0);
  // 输入入口本身还在。
  await expect(page.getByLabel('给 AI 的消息')).toBeVisible();
});

/**
 * 输入框:一行时不能像大文本编辑器,多行时增长,到顶就在内部滚动。
 *
 * 三个数都从真实渲染的包围盒读,不读 CSS 声明。上限给到 126px 是因为滚动条
 * 与亚像素取整会有几像素余量;真正要挡的是“输入框跟着内容无限变高把面板挤走”。
 */
test('输入框初高约一行，随内容增高，到上限后内部滚动', async ({ page }) => {
  const { token } = await registerAccount(page, 'ui-composer-height');
  const id = await createWorkspace(page, token, '输入框高度空间');
  await openSpacePage(page, '/workbench', id);
  await waitForRealPlan(page);

  const textarea = page.getByLabel('给 AI 的消息');
  await expect(textarea).toBeVisible();
  const initial = await textarea.boundingBox();
  expect(initial, '输入框不在').not.toBeNull();
  expect(initial!.height, `初始输入框太高了:${initial!.height}`).toBeLessThanOrEqual(56);
  expect(initial!.height, `初始输入框太矮了:${initial!.height}`).toBeGreaterThanOrEqual(40);

  await textarea.fill(Array.from({ length: 12 }, (_, index) => `第 ${index + 1} 行`).join('\n'));
  await expect
    .poll(async () => (await textarea.boundingBox())!.height, { message: '多行输入之后输入框没有长高' })
    .toBeGreaterThan(initial!.height);
  const grown = await textarea.boundingBox();
  expect(grown!.height, `输入框超过了最大高度:${grown!.height}`).toBeLessThanOrEqual(126);
  expect(
    await textarea.evaluate(element => element.scrollHeight > element.clientHeight + 1),
    '内容超过上限后应该在输入框内部滚动',
  ).toBe(true);

  // 删回一行:高度缩回去,不是“长过一次就回不去了”。
  await textarea.fill('');
  await expect
    .poll(async () => Math.round((await textarea.boundingBox())!.height), { message: '清空之后输入框没有缩回来' })
    .toBeLessThanOrEqual(56);
});

/**
 * proposal 校验失败:必须保留这个事实,但默认只占一行,原因可展开、提示可关闭。
 *
 * 真实校验失败需要一个能产出动作的 reasoner;隔离栈默认是 `rule`(永远没有动作),
 * 所以这里只把响应里的 `proposalErrors` 换成一份真实的用户可读原因 —— 其余字段照后端
 * 原样送进界面。这一条验的是它到了界面之后长什么样,与后端为什么拒它无关。
 */
test('提案校验失败保留为紧凑提示，可展开原因、可关闭', async ({ page }) => {
  const { token } = await registerAccount(page, 'ui-proposal-error');
  const id = await createWorkspace(page, token, '提案校验空间');
  await openSpacePage(page, '/workbench', id);
  await waitForRealPlan(page);

  await page.route('**/api/workspaces/*/messages', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({
      status: response.status(),
      contentType: 'application/json',
      body: JSON.stringify({
        ...body,
        proposalErrors: [{ code: 'VALIDATION', message: '这条调整会把一个任务排到它自己的前置之前。' }],
      }),
    });
  });

  await page.getByLabel('给 AI 的消息').fill('帮我调整一下计划。');
  await page.getByLabel('发送消息').click();

  const notice = page.locator('.proposal-error');
  await expect(notice).toBeVisible({ timeout: 20000 });
  await expect(notice).toContainText('本次计划建议未应用');
  // 默认不展开原因 —— 这是“最多一行”的具体含义。
  await expect(notice.locator('.turn-error-reasons')).toHaveCount(0);
  // 展开了才看得到原始原因。
  await notice.getByRole('button', { name: '查看原因' }).click();
  await expect(notice.locator('.turn-error-reasons')).toContainText('排到它自己的前置之前');
  // 关闭只关这条前端提示。
  await notice.getByRole('button', { name: '关闭这条提示' }).click();
  await expect(page.locator('.proposal-error')).toHaveCount(0);
});

/**
 * 画布上的“删除”必须可发现,而且仍然走既有的可恢复归档。
 *
 * 这一条同时钉住“选中之后更多操作常驻”“菜单说的是用户语言”“确认归档后能在归档列表
 * 里找回来”。它不重验后端归档语义(那个由 `node-delete.spec.ts` 钉)。
 */
test('单击普通节点后选中与更多操作可见，菜单里是「删除（可恢复）」', async ({ page }) => {
  const { token } = await registerAccount(page, 'ui-node-actions');
  const id = await createWorkspace(page, token, '节点操作空间');
  const root = (await getPlan(page, token, id)).nodes[0];
  const nodeId = await createNode(page, token, id, { parentId: root.id, title: '可删除节点', nodeType: 'task' });

  await openSpacePage(page, '/workbench', id);
  await waitForRealPlan(page);
  await page.waitForTimeout(500);

  const node = page.locator(`.react-flow__node[data-id="${nodeId}"]`);
  const card = node.locator('.growth-node');
  const more = node.locator('.node-more');

  // 桌面、未选中、指针不在上面:更多操作是隐的。
  await page.mouse.move(2, 2);
  await expect(more).toHaveCSS('opacity', '0');

  // 单击选中(会打开详情);关掉详情后选中仍在 —— 选中样式与更多操作都可见,
  // 不需要用户猜位置去悬停。
  await card.click();
  await page.getByRole('dialog').getByRole('button', { name: '关闭弹窗' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(card).toHaveClass(/is-selected/);
  await expect(more, '选中之后“更多操作”还藏着 —— 删除就不可发现').toHaveCSS('opacity', '1');

  // 菜单项是用户语言,并说清“删到哪去”。
  await more.click();
  const menu = page.getByRole('menu');
  await expect(menu).toBeVisible();
  const remove = menu.getByRole('menuitem', { name: '删除（可恢复）' });
  await expect(remove).toBeVisible();
  await expect(remove).toContainText('将移至归档，不会立即永久删除');

  // 真的走一遍:确认影响 -> 归档 -> 节点消失,而且在归档列表里找得回来。
  await remove.click();
  const confirm = page.getByRole('dialog');
  await expect(confirm.getByRole('heading')).toHaveText('归档「可删除节点」？');
  await confirm.getByRole('button', { name: '归档' }).click();
  await expect(node, '确认归档之后节点还在画布上').toHaveCount(0);

  await page.locator('.canvas-tools-menu > summary').click();
  await page.locator('.space-floating-tools button', { hasText: '归档' }).click();
  const list = page.getByRole('dialog');
  await expect(
    list.locator('.archive-list li').filter({ hasText: '可删除节点' }),
    '“删除”之后在归档列表里找不到它 —— 它不是可恢复的',
  ).toHaveCount(1);
});

/** 根目标仍然不可删除,而且禁用项要说出原因。 */
test('根目标的删除项禁用并说明原因', async ({ page }) => {
  const { token } = await registerAccount(page, 'ui-root-delete');
  const id = await createWorkspace(page, token, '根目标删除空间');
  const root = (await getPlan(page, token, id)).nodes[0];
  await openSpacePage(page, '/workbench', id);
  await waitForRealPlan(page);
  await page.waitForTimeout(500);

  const rootNode = page.locator(`.react-flow__node[data-id="${root.id}"]`);
  await rootNode.hover();
  await rootNode.locator('.node-more').click();
  const remove = page.getByRole('menu').getByRole('menuitem', { name: '删除（可恢复）' });
  await expect(remove, '根目标的删除入口要在,只是不可选').toHaveAttribute('aria-disabled', 'true');
  await expect(remove.locator('.context-menu-why')).toHaveText('根目标不能归档');
});
