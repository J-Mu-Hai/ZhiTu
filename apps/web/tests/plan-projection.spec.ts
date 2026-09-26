import { expect, test } from '@playwright/test';
import { assertBackendRunning, createNode, createWorkspace, dayOffset, getPlan, registerAccount, renderedNodeIds, waitForRealPlan } from './support/session';

/**
 * 阶段 5 的验收:路径图上画出来的节点,必须和 `/plan` 里真实存在的节点一致。
 *
 * ## 为什么这条要单独立一个端到端测试
 *
 * 之前出过一次事故:后端把 AI 生成的 3 个阶段节点好好地存进了库,`GET /plan`
 * 也把 16 个节点全返回了,而画布上一个阶段都没有 —— 因为 `PathView.tsx` 里有一句
 * `node.type !== 'stage'` 的过滤。**没有任何一层会报错**:接口 200,数据在,测试全绿,
 * 界面上是一句"这里，还可以长出更多可能"。
 *
 * 单测抓不住这一类错误,因为它同时"正确"地存在于两端。只有把浏览器里真正渲染出来的
 * 东西和接口真正返回的东西放在一起比,才看得见。所以这个文件里所有断言都遵循同一个
 * 形状:**从 API 取真值,从 DOM 数结果,两者相等**。
 *
 * ## 它需要后端真的在跑
 *
 * 账户、会话、计划都已经是后端的事。后端不在时这个测试**失败**而不是跳过 ——
 * 跳过会让"界面和后端对不上"在没人注意的时候悄悄回来。
 *
 * 它**不需要模型 key**:所有节点都由这个测试自己通过接口创建,不经过 AI。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('路径图上画出来的节点，就是 /plan 里那一批', async ({ page }) => {
  const { token } = await registerAccount(page, 'projection');

  // 空间建好时**只有一个根目标**,零节点、零对话。这是阶段 2 定下的不变量,
  // 这里顺带再验一次:如果它又变成"新建空间先灌一份保研 Demo",下面的断言会立刻炸。
  const workspaceId = await createWorkspace(page, token, '投影验收空间', '确认界面和后端是同一份数据');

  const initial = await getPlan(page, token, workspaceId);
  expect(initial.nodes, '新建空间应该只有一个根目标').toHaveLength(1);
  const root = initial.nodes[0];
  expect(root.nodeType).toBe('goal');

  /**
   * 用**接口**直接建节点,而不是点界面上的按钮。
   *
   * 两个理由。一是这一条要验的是"后端有的,界面画不画得出来",节点必须先真的在
   * 后端;从界面建的话,建失败时测试会因为另一个原因(按钮没生效)而失败,分不清
   * 到底哪一环坏了。二是 `stage` 这个类型界面上根本建不了(新建对话框只给
   * 行动/能力/里程碑),而**被那句旧过滤滤掉的正是 stage** —— 不用接口建不出这个
   * 回归场景。
   */
  const stages: string[] = [];
  for (const title of ['阶段一 · 打基础', '阶段二 · 做项目', '阶段三 · 收尾']) {
    stages.push(await createNode(page, token, workspaceId, { parentId: root.id, title, nodeType: 'stage' }));
  }

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  const payload = await getPlan(page, token, workspaceId);
  expect(payload.totalNodes).toBe(4);
  // 顶层画布 = 根目标 + 根的直接子节点。不是 `payload.nodes.length`:子节点要**进入**
  // 自己的空间才画得出来,这是有意的层级设计,不是丢数据。所以比的是同一层的集合。
  const expected = [root.id, ...stages].sort();
  await expect
    .poll(() => renderedNodeIds(page), { message: '画布上的节点和 /plan 里的直接子节点对不上' })
    .toEqual(expected);

  // **这一条是这个文件存在的理由。** 三个阶段全都在画布上 —— 换回那句
  // `node.type !== 'stage'` 的过滤,这里立刻只剩根目标。
  for (const title of ['阶段一 · 打基础', '阶段二 · 做项目', '阶段三 · 收尾']) {
    await expect(page.getByText(title, { exact: true })).toBeVisible();
  }

  /*
   * 刷新一次再比一遍。
   *
   * 上一版的界面是从 localStorage 读计划的,所以"画对了"可能只是它记住了自己写过的
   * 东西。刷新之后仍然对,才说明这一份是从后端拿的。
   */
  await page.reload();
  await waitForRealPlan(page);
  await expect.poll(() => renderedNodeIds(page)).toEqual(expected);
});

test('进入阶段空间，它下面的任务画得出来', async ({ page }) => {
  const { token } = await registerAccount(page, 'projection');
  const workspaceId = await createWorkspace(page, token, '层级验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  const stageId = await createNode(page, token, workspaceId, { parentId: root.id, title: '唯一阶段', nodeType: 'stage' });
  const tasks: string[] = [];
  for (const title of ['任务 A', '任务 B']) {
    tasks.push(await createNode(page, token, workspaceId, { parentId: stageId, title, nodeType: 'task' }));
  }

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 根这一层:根 + 阶段,任务**不**在这一层 —— 它们属于阶段自己的空间。
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, stageId].sort());

  // 双击进入阶段的子空间。这是"层级"这个设计唯一的用法,也是它唯一能被验证的地方:
  // 如果进入之后什么都没有,那层级就是把节点藏起来了,而不是组织起来了。
  await page.locator(`.react-flow__node[data-id="${stageId}"]`).dblclick();
  await expect.poll(() => renderedNodeIds(page)).toEqual([stageId, ...tasks].sort());
  await expect(page.getByText('任务 A', { exact: true })).toBeVisible();
  await expect(page.getByText('任务 B', { exact: true })).toBeVisible();
});

test('在界面上新建的节点，后端真的存下来了', async ({ page }) => {
  const { token } = await registerAccount(page, 'projection');
  const workspaceId = await createWorkspace(page, token, '写回验收空间');
  const before = await getPlan(page, token, workspaceId);
  const root = before.nodes[0];

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // 这一条走的是**真实的用户路径**:点按钮、填表单、提交。它同时也在验
  // `POST /nodes` 那条直写路径(用户自己加节点是明确操作,不需要 AI 提案)。
  await page.getByRole('button', { name: '新建节点' }).click();
  await page.getByLabel('节点名称').fill('手写的一个节点');
  await page.getByRole('dialog').getByRole('button', { name: '新建节点' }).click();
  // **等界面认了这件事再去问后端。** `click()` 在事件派发完就返回了,而提交是异步的
  // (提交 → POST → 重新拉计划)。不等的话下面这次查询会跑在 POST 前面 —— 得到的
  // 是"后端没有这个节点",而实际上它只是还没写上。这类断言失败会把人引向错误的方向。
  await expect(page.getByText('手写的一个节点', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);

  const after = await getPlan(page, token, workspaceId);
  expect(after.nodes, '界面上新建的节点必须真的进库').toHaveLength(2);
  expect(after.totalNodes).toBe(2);
  // 直写也要走版本记账 —— 计划变了就必须留下一个版本,否则"计划在提案生成后被改过"
  // 这件事在服务端就查不出来了。
  expect(after.revisionVersion).toBe(before.revisionVersion + 1);

  const added = after.nodes.find((node) => node.id !== root.id)!;
  expect(added.title).toBe('手写的一个节点');
  expect(added.parentId).toBe(root.id);

  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, added.id].sort());

  // 再刷新一次:它得是从后端读回来的,不是内存里那个还热着的状态。
  await page.reload();
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, added.id].sort());
});

test('没有日期的节点不会从时间线上悄悄消失', async ({ page }) => {
  const { token } = await registerAccount(page, 'projection');
  const workspaceId = await createWorkspace(page, token, '时间线验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  const deadline = dayOffset(30);
  const datedId = await createNode(page, token, workspaceId, { parentId: root.id, title: '有截止日的任务', nodeType: 'task', deadline });
  const undatedId = await createNode(page, token, workspaceId, { parentId: root.id, title: '还没定日期的任务', nodeType: 'task' });
  // 真值从**计划**里取,不是取我发出去的那个字符串 —— 后端把日期规范化过
  // (`date` 类型),拿自己发的值去比等于在验自己。
  const plan = await getPlan(page, token, workspaceId);
  const storedDeadline = plan.nodes.find(node => node.id === datedId)!.deadline!;
  expect(storedDeadline).toBe(deadline);
  expect(plan.nodes.find(node => node.id === undatedId)!.deadline).toBeNull();

  await page.goto(`/workbench?workspace=${workspaceId}&view=timeline`);

  /*
   * 有截止日的节点画在时间线上,而且**说的是"截止"**。
   *
   * 真实节点的 `startDate`/`endDate` 是从 `deadline` 映出来的(见 planProjection),
   * 所以它落在时间线上是一个点 —— 而这个点和"这件事安排在这一天"长得一模一样。
   * 标签是 `10.25` 的话,用户读到的是"这天要做这件事",而那天其实什么都没排。
   */
  //
  // 断言落在 `[data-timeline-card]` 那个按钮上,不是外面那层 `[data-timeline-item]`:
  // 外层 div 里装的全是绝对定位的子元素,它自己高度是 0 —— Playwright 判它"不可见",
  // 而它在屏幕上明明是画着的。
  const card = page.locator(`[data-timeline-item="${datedId}"] [data-timeline-card]`);
  await expect(card).toBeVisible();
  await expect(card.locator('time')).toHaveText(
    `截止 ${Number(storedDeadline.slice(5, 7))}.${Number(storedDeadline.slice(8, 10))}`,
  );
  await expect(card.locator('small')).toHaveText('截止日 · 还没排出具体安排');

  // 没有日期的那个:**不在**时间线上(它画不出来,这不是 bug),但也不能凭空消失。
  // 这条断言和下面那条是一对 —— 只有前者,等于承认了"没日期 = 不存在"。
  await expect(page.locator(`[data-timeline-item="${undatedId}"]`)).toHaveCount(0);

  const unscheduled = page.getByTestId('unscheduled-items');
  await expect(unscheduled).toBeVisible();
  await expect(unscheduled).toContainText('还有 1 项没有日期');
  await expect(unscheduled).toContainText('还没定日期的任务');
  // 根目标没有日期,但它**能**从有日期的子节点推出一个范围,所以它不在这份名单里。
  // 少了这条,把"没有 deadline"直接等同于"未排期"的实现也能让上面几条全绿。
  await expect(unscheduled).not.toContainText('时间线验收空间');
});
