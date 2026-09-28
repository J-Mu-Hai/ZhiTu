import { expect, test, type Locator } from '@playwright/test';
import { createWorkspace, registerAccount, openSpacePage, createNode, getPlan, scheduleEverything } from './support/session';
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

  // 1. 顶部三块在同一行,而且整行不深。两行的话最上面那一行的顶边会比下面那一行小
  //    几十像素 —— 容差取 8px,它在"居中造成的 1~2px 参差"和"换行造成的 40px"之间。
  const tops = await page.evaluate(() => ['.space-breadcrumb', '.view-tabs', '.reopen-chat']
    .map(selector => document.querySelector(selector))
    .filter((element): element is Element => element !== null)
    .map(element => Math.round(element.getBoundingClientRect().top)));
  expect(tops.length).toBe(3);
  expect(Math.max(...tops) - Math.min(...tops), `顶部浮动组件的顶边参差 ${tops.join('/')}`).toBeLessThanOrEqual(8);
  expect(Math.max(...tops), '顶部浮动组件的最下沿已经压到画布很深处').toBeLessThan(120);

  // 2. 根节点正中心点得到。ReactFlow 给每个节点挂的是 `role="group"` + 节点标题,
  //    所以这里能按名字点名要根目标那一个。
  const node = await centreHit(page.getByRole('group', { name: '手机工作台' }));
  expect(node.self, `根节点的正中心被 ${node.blockedBy} 盖住了`).toBe(true);

  // 3. 工具条与缩放按钮不叠。两块的矩形相交就是叠了 —— 它们都在右下角,差了 9px 就会
  //    压住缩放按钮的顶边(工具条有边框和阴影,压上去看得出来)。
  const overlap = await page.evaluate(() => {
    const tools = document.querySelector('.space-floating-tools')?.getBoundingClientRect();
    const controls = document.querySelector('.react-flow__controls')?.getBoundingClientRect();
    if (!tools || !controls) return null;
    return {
      tools: `${Math.round(tools.x)},${Math.round(tools.y)} → ${Math.round(tools.right)},${Math.round(tools.bottom)}`,
      controls: `${Math.round(controls.x)},${Math.round(controls.y)} → ${Math.round(controls.right)},${Math.round(controls.bottom)}`,
      x: Math.max(0, Math.min(tools.right, controls.right) - Math.max(tools.left, controls.left)),
      y: Math.max(0, Math.min(tools.bottom, controls.bottom) - Math.max(tools.top, controls.top)),
    };
  });
  expect(overlap, '这一档里画布右下角应该有工具条与缩放按钮').not.toBeNull();
  expect(overlap!.x * overlap!.y, `工具条(${overlap!.tools})与缩放按钮(${overlap!.controls})重叠`).toBe(0);

  // 4. 抽屉:铺满宽度,顶上留出画布,顶部那条浮动组件仍然点得到。
  await page.getByRole('button', { name: '展开对话', exact: true }).click();
  await expect(page.locator('.conversation-overlay')).toBeVisible();
  const drawer = await page.locator('.conversation-overlay').boundingBox();
  if (!drawer) throw new Error('AI 抽屉没有尺寸,断言无从谈起');
  expect(drawer.width, '抽屉没有铺满宽度').toBeGreaterThanOrEqual(390 * .98);
  expect(drawer.y, '抽屉顶到屏幕顶上去了,画布一点没留').toBeGreaterThanOrEqual(100);
  const breadcrumb = await centreHit(page.locator('.space-breadcrumb'));
  expect(breadcrumb.self, `抽屉拉开之后空间路径被 ${breadcrumb.blockedBy} 盖住了`).toBe(true);
  await page.screenshot({ path: artifactPath('ui-workbench-mobile-drawer.png') });

  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(errors).toEqual([]);
});
