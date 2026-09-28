import { expect, test } from '@playwright/test';
import { api, assertBackendRunning, createWorkspace, registerAccount, waitForRealPlan } from './support/session';
import { canvasTool } from './support/menu';

/**
 * 闭环的后半段:**排出日程 → 今天做 → 把结果记下来,而且真的落库了。**
 *
 * ## 为什么这一段必须单独验一次
 *
 * 在它接通之前,「今天」页面对真实空间做的是**假保存**:勾一下完成走的是
 * `apply({type:'UPDATE_STATUS'})`,一个只改内存的 reducer。界面会显示"完成了",
 * 库里什么都没有,刷新就回来。而"根据执行情况持续调整"这条闭环的起点,正是
 * "用户报告了实际发生了什么" —— 起点是假的,后面每一步都无从谈起。
 *
 * 更早的一层是:节点的**预计工时**在界面上根本没有输入框。没有工时,排期算法
 * 排不出任何一场(`scheduler` 对没有工时的叶子节点给一条缺口),于是「排期」和
 * 「今天」永远是空的 —— 而链路上没有任何一处会报错。所以这个测试从"在界面上建一个
 * 带工时的任务"开始,而不是从接口塞一个节点进去:那样正好会绕开这个 bug。
 *
 * ## 断言问的是"库里的真值",不是"屏幕上的字"
 *
 * 每一段都从 `GET /api/...` 取真值再和界面比。只看界面的话,一个只改内存的
 * 实现同样能全绿 —— 那正是上面那个 bug 当时的样子。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

interface TodayPayload {
  itemCount: number;
  recordedCount: number;
}
interface PlanPayload {
  nodes: { id: string; title: string; estimateMinutes: number | null }[];
}

test('在浏览器里建的任务能排进日程，做完之后记录真的落库', async ({ page }) => {
  const { token } = await registerAccount(page, 'live-loop');
  const workspaceId = await createWorkspace(page, token, 'Python 学习', '三个月内做出一个能跑的东西');
  const planPath = `/api/workspaces/${workspaceId}/plan`;

  // ---------------------------------------------------------------- 建一个带工时的任务
  await page.goto(`/workbench?workspace=${workspaceId}`);

  // 计划到达之前画布上是一棵只有一个哨兵根节点的占位树,那时"新建节点"是禁用的
  // (`POST /nodes` 要一个真 UUID)。先等真数据到,否则失败原因会指向选择器。
  // 它在工具栏那个低频菜单里(见 `support/menu.ts`)。禁用态是产品行为,
  // 不因为多了一层菜单而改变 —— 这里断言的东西一个字都没改。
  //
  // **"先等真数据到"这一步以前只是这句话,代码里没有。** 于是 `canvasTool` 拿回来的
  // 是一个 `disabled title="正在读取计划…"` 的按钮,而后面的 `toBeEnabled()` 只能硬等;
  // 等的那段时间里计划到了、画布重渲染,那个 `<details>` 连同它里面的按钮一起被换掉 ——
  // 元素 detach,这一条就撞上 30 秒上限。它红的时候长得像"按钮被删了",其实是没等前置条件。
  await waitForRealPlan(page);
  const createButton = await canvasTool(page, '新建节点');
  await expect(createButton).toBeEnabled();
  await createButton.click();

  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('节点名称').fill('装好环境并跑通 Hello World');
  // **这一个填了才排得出来。** 它是这个测试存在的一半理由。
  await dialog.getByLabel('预计要做多久（分钟）').fill('90');
  await dialog.getByRole('button', { name: '新建节点' }).click();
  await expect(dialog).toBeHidden();

  const created = await api<PlanPayload>(page, token, planPath);
  const task = created.nodes.find(node => node.title === '装好环境并跑通 Hello World');
  expect(task, '界面上建的任务没有出现在 /plan 里').toBeTruthy();
  // 工时要按**分钟**原样送到后端,不是被当成小时、也不是被丢掉。
  expect(task!.estimateMinutes, '预计工时没有写进计划 —— 这个任务将永远排不进任何一天').toBe(90);

  // ---------------------------------------------------------------- 排期
  await page.goto(`/workbench?workspace=${workspaceId}&view=schedule`);
  await page.getByTestId('schedule-preview').click();
  await expect(page.getByTestId('schedule-preview-result')).toBeVisible();
  // 排不进去的部分不会被静默吞掉,所以这里能直接断言"没有缺口"——
  // 有缺口说明这个任务根本没排上,那后面的「今天」也就无从谈起。
  await expect(page.getByTestId('schedule-gaps')).toHaveCount(0);
  await page.getByTestId('schedule-apply').click();
  await expect(page.getByTestId('schedule-apply')).toHaveCount(0);

  // ---------------------------------------------------------------- 今天
  await page.goto('/today');
  const row = page.locator('.today-item').filter({ hasText: '装好环境并跑通 Hello World' }).first();
  await expect(row).toBeVisible();

  // 排期从今天开始排,所以第一场就应该落在今天。落不上说明排期和「今天」对同一天
  // 的理解不一致 —— 那会让用户"排了,但今天没事做"。
  const before = await api<TodayPayload>(page, token, '/api/today');
  expect(before.itemCount, '排完之后今天应该有安排').toBeGreaterThan(0);
  expect(before.recordedCount, '还没做,不该有任何记录').toBe(0);
  // **排了但还没做,不能说成"完成了",也不能说成"没完成"。** 后者是在替用户断言
  // 一件我们并不知道的事,而它会一路影响后面的偏差判定。
  await expect(row.locator('.today-item-result')).toHaveCount(0);

  await row.getByRole('button', { name: /标记为完成/ }).click();
  await expect(row.locator('.today-item-result')).toContainText('完成了');

  // ---------------------------------------------------------------- 真的落库了吗
  const after = await api<TodayPayload>(page, token, '/api/today');
  expect(after.recordedCount, '界面上说完成了,但库里没有这条记录 —— 这就是那个假保存').toBeGreaterThan(0);

  /*
   * 刷新之后再问一遍。
   *
   * 这一条是**整个测试里最重要的断言**:只改内存的实现同样能让上面那句
   * `.today-item-result` 出现,但它过不了刷新 —— 因为记录不在任何地方的库里。
   */
  await page.reload();
  const reloaded = page.locator('.today-item').filter({ hasText: '装好环境并跑通 Hello World' }).first();
  await expect(reloaded.locator('.today-item-result')).toContainText('完成了');
});

/**
 * 提醒。
 *
 * 这里验的是**「关掉」到底关在哪里**。提醒本身不是一行数据,是每次请求按事实重算的
 * (库里只记"用户关掉了哪条"),所以关掉之后**刷新一次它不应该回来** —— 如果它回来了,
 * 说明那次"关掉"只改了浏览器里的一个数组,而用户会以为自己没点中。
 *
 * 同时验另一半:提醒**只由具体事件触发**。这个空间是新建且空的,所以该出现的只有
 * "空间是空的"这一条,而不是一句泛泛的"该学习了"。
 */
test('站内提醒会出现、能关掉，而且关掉之后刷新不会回来', async ({ page }) => {
  const { token } = await registerAccount(page, 'live-reminder');
  const workspaceId = await createWorkspace(page, token, '提醒验收空间', '');

  // 先真的进一次这个空间。`/today` 的导航地址上不带 `?workspace=`,它靠"上次打开
  // 的那个空间"来决定显示谁的数据 —— 一次都没进过的话,这一页会被送回空间页,
  // 而"提醒条没出现"就会变成一个和提醒毫无关系的失败。
  //
  // **而且要等它真的加载完。** 那个"上次打开的空间"是空间详情取回来之后才写进
  // 本地的,取回来之前就走,下一页读到的还是空 —— 于是又被送回空间页。
  //
  // "等它真的加载完"用的是 `waitForRealPlan`,不是下面那个 `toBeEnabled()`:
  // 后者要经过工具栏菜单,而菜单正是被"加载完"这一次重渲染换掉的东西 ——
  // 拿一个会被加载过程销毁的元素去等加载,是在跟自己做对(见上面那条注释)。
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await expect(await canvasTool(page, '新建节点')).toBeEnabled();

  await page.goto('/today');
  const strip = page.locator('.reminder-strip');
  await expect(strip).toContainText('这个空间还是空的');

  await strip.getByRole('button', { name: '知道了' }).click();
  // 这一条之后没有了 —— 组件在"没有提醒"时什么都不渲染,而不是留一个空的提醒条。
  await expect(strip).toHaveCount(0);

  const settled = await api<{ reminders: unknown[] }>(page, token, '/api/reminders');
  expect(settled.reminders, '后端还在推这条提醒,界面却把它藏起来了').toHaveLength(0);

  // **关掉的是服务端的状态,不是本地的一个数组。**
  await page.reload();
  await expect(strip).toHaveCount(0);
});
