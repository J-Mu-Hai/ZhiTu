/**
 * 「用途」这条轴在界面上的后果(第二批 §2.5)。
 *
 * ## 这条轴是什么
 *
 * `purpose` 与 `nodeType` **正交**:一个节点可以是「主题/方向」(信息)也可以是
 * 「行动」(规划),两者的 `nodeType` 可以一样。它决定三件**用户看得见**的事:
 *
 * 1. 不排进日历(排期预览里没有它、`totalNodes` 不数它);
 * 2. 画布上不画任务勾选框 —— 一个记下来的事实没有"做完"这回事;
 * 3. 「任务」那一页里不出现它。
 *
 * ## 为什么"翻面"这一步走接口
 *
 * 界面上**没有**把已有节点从行动改成主题的入口(新建时的三个语义选项是唯一的入口)。
 * 那是有意的:改用途要连带决定它的工时与截止怎么办,那是一个用户该自己看清的决定
 * —— 但它今天只能从接口做。所以这条测试从接口把一面翻过去,验的是**翻过去之后界面
 * 的样子**:真到了用户能翻的那一天(第三步的详情编辑器),这一组断言不用改。
 *
 * 反过来说,今天"用户在界面上翻不了面"是一个**真实的限度**,不是漏测 —— 交付报告里
 * 如实写了这一条。
 */

import { expect, test, type Page } from '@playwright/test';
import {
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  registerAccount,
  waitForRealPlan,
} from './support/session';

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
});

async function scene(page: Page, prefix: string) {
  const account = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, account.token, '用途验收空间', '验信息用途节点');
  const rootId = (await getPlan(page, account.token, workspaceId)).nodes[0].id;
  return { account, workspaceId, rootId };
}

function card(page: Page, nodeId: string) {
  return page.locator(`.react-flow__node[data-id="${nodeId}"]`);
}

// ---------------------------------------------------------------------------------
// 1. 建「主题/方向」建出来的是一对 `(information, capability)`
// ---------------------------------------------------------------------------------
test('建一个「主题 / 方向」建出来的节点是信息用途，而且没有任务勾选框', async ({ page }) => {
  const { account, workspaceId } = await scene(page, 'purpose-create');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  const trigger = page.getByRole('button', { name: '新建节点' });
  await expect(trigger).toBeEnabled();
  await trigger.click();

  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('节点名称').fill('学业情况');
  await dialog.getByLabel(/节点类型|树叶类型/).selectOption('topic');
  // 选了主题之后,**工时那一栏就不该再问** —— 一个不进日历的东西被问"要做多久",
  // 用户会开始怀疑自己刚才选的是什么。
  await expect(dialog.getByLabel('预计要做多久（分钟）')).toHaveCount(0);
  await dialog.getByRole('button', { name: '新建节点' }).click();
  await expect(dialog).toBeHidden();

  const plan = await getPlan(page, account.token, workspaceId);
  const created = plan.nodes.find(node => node.title === '学业情况');
  expect(created, '主题没有建出来').toBeTruthy();
  // **两个字段一起断言。** 只断言 `purpose` 的话,一个把 `nodeType` 也改成
  // capability 之外的实现照样能绿 —— 而界面上那个语义选项承诺的是**一对**值。
  expect(created!.purpose).toBe('information');
  expect(created!.nodeType).toBe('capability');
  // 规划那一类默认是 planning:这条轴是加出来的,不是把原有的默认值也换掉了。
  // `nodes[0]` 就是根目标(后端按 `(depth, order_index, created_at)` 排)。
  expect(plan.nodes[0].purpose).toBe('planning');

  await waitForRealPlan(page);
  await expect(card(page, created!.id).locator('.node-task-box'), '信息主题上画了任务勾选框').toHaveCount(0);
});

// ---------------------------------------------------------------------------------
// 2. 一个任务翻成信息用途之后:勾选框没了、任务页里没了、`totalNodes` 也不数它
// ---------------------------------------------------------------------------------
test('一个任务翻成信息用途之后，它就不排期、不计数、也不在任务页里', async ({ page }) => {
  const { account, workspaceId, rootId } = await scene(page, 'purpose-flip');
  const taskId = await createNode(page, account.token, workspaceId, {
    parentId: rootId, title: '会被翻面的任务', nodeType: 'task', estimateMinutes: 60,
  });

  // `waitForRealPlan` 与卡片断言都只能在**路径**那一页做 —— 「任务」页里根本没有
  // 画布,`.react-flow__node` 一个都没有,在那个页面上等它等到的是超时。
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await expect(card(page, taskId).locator('.node-task-box')).toHaveCount(1);

  const tasks = page.locator('.task-view');
  await page.goto(`/workbench?workspace=${workspaceId}&view=tasks`);
  // 翻之前它在「任务」页里 —— 否则下面那两句"没了"就什么也没验。
  await expect(tasks.locator('.task-row').filter({ hasText: '会被翻面的任务' })).toHaveCount(1);
  await expect(tasks.locator('.muted')).toContainText('1 项任务');

  const before = await getPlan(page, account.token, workspaceId);
  // 根节点 + 这一个任务。信息用途的节点**不进这个数**(见 `plan_service` 里那段
  // docstring:"计划里有多少个节点"说的是要去做的事)。
  expect(before.totalNodes).toBe(2);

  // ---- 翻面。工时**必须一起清掉**:信息节点带工时会被服务端直接拒(400),而那条
  //      规则正是"用户改用途时该看清的事"(§4.1:信息主题不需要工时、截止或勾选)。
  await api(page, account.token, `/api/workspaces/${workspaceId}/nodes/${taskId}`, {
    method: 'PATCH',
    data: { purpose: 'information', estimateMinutes: null, deadline: null },
  });

  // ---- 画布上:勾选框没了
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await expect(card(page, taskId).locator('.node-task-box'), '翻成信息用途之后还画着任务勾选框').toHaveCount(0);

  // ---- 「任务」页里:不出现,而且那行计数也跟着回到 0
  await page.goto(`/workbench?workspace=${workspaceId}&view=tasks`);
  await expect(tasks.locator('.task-row').filter({ hasText: '会被翻面的任务' })).toHaveCount(0);
  await expect(tasks.locator('.muted')).toContainText('0 项任务');

  // ---- 计数:2 → 1
  const after = await getPlan(page, account.token, workspaceId);
  expect(after.totalNodes, '信息用途的节点被算进了"计划里有多少个节点"').toBe(1);
  // 它**还在画布上**:不进计数不等于被删掉。少这一条,一个"直接从计划里摘掉"
  // 的实现也能让上面三句全绿。
  expect(after.nodes.some(node => node.id === taskId)).toBe(true);
});
