import { expect, test } from '@playwright/test';
import { assertBackendRunning, createNode, createWorkspace, enterSpace, getPlan, registerAccount, renderedNodeIds, waitForRealPlan } from './support/session';
import { canvasTool } from './support/menu';

/**
 * 层级导航:进入子空间、在里面建东西、返回上级 —— **每一层都是后端那一层**。
 *
 * ## 这个文件接的是 `leaf-path.spec.ts` 的班
 *
 * 那两条测试验的是"四个固定分类各自展开一条叶路径"(学业成绩 / 科研能力 /
 * 综合经历 / 个人成长)。那四个分类是**示例空间里写死的**,而示例空间整个删掉了 ——
 * 所以那两条测试连同它们验的界面一起没有了,不是被跳过。
 *
 * 但它们真正想钉的那件事还在,而且更重要:**层级是可用的**。如果进入子空间之后
 * 什么都没有、或者返回上级回不去,那"层级"就只是把节点藏起来了,而不是组织起来了。
 * 这个文件在**真实空间**里把这件事重验一遍 —— 空间、阶段、树叶全部由这个测试
 * 通过接口搭出来,看到的每一样东西都有后端那一行对应。
 *
 * ## 顺带钉住"文件挂在它所在的那一层"
 *
 * `SpaceFiles` 按 `ownerId === 当前空间` 过滤。返回上级之后还能看到子空间的文件,
 * 等于这一层的边界是假的 —— 所以下面既验"这一层有",也验"上一层没有"。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('进入子空间、在里面建节点、返回上级，每一层都对得上', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));

  const { token } = await registerAccount(page, 'levels');
  const workspaceId = await createWorkspace(page, token, '层级导航空间', '验证每层都是后端那一层');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  // 阶段 → 树叶,两层。全部用接口建:这一条验的是"界面画不画得出后端那一层",
  // 节点必须先真的在后端,否则建失败时会because另一个原因失败,分不清哪一环坏了。
  const stageId = await createNode(page, token, workspaceId, { parentId: root.id, title: '联系导师', nodeType: 'stage' });
  const leafId = await createNode(page, token, workspaceId, { parentId: stageId, title: '整理实验室资料', nodeType: 'task' });
  const grandId = await createNode(page, token, workspaceId, { parentId: leafId, title: '阅读导师论文', nodeType: 'task' });

  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  // --- 第一层:根 + 阶段。树叶**不在**这一层,它属于阶段自己的空间 --------------
  await expect.poll(() => renderedNodeIds(page)).toEqual([root.id, stageId].sort());

  // --- 进阶段:阶段是它自己那一层的中心,树叶画在它旁边 --------------------------
  await enterSpace(page, stageId);
  await expect(page.locator('.growth-node.goal .node-title')).toContainText('联系导师');
  await expect.poll(() => renderedNodeIds(page)).toEqual([stageId, leafId].sort());

  // --- 进树叶:再深一层 ----------------------------------------------------------
  await page.getByRole('button', { name: '进入整理实验室资料空间', exact: true }).click();
  await expect(page.locator('.growth-node.goal .node-title')).toContainText('整理实验室资料');
  await expect.poll(() => renderedNodeIds(page)).toEqual([leafId, grandId].sort());

  // --- 在这一层建一个新节点,走**真实的用户路径**(点按钮、填表单、提交) --------
  await (await canvasTool(page, '添加树叶')).click();
  await page.getByLabel('树叶名称').fill('给导师写一封邮件');
  await page.getByRole('dialog').getByRole('button', { name: '添加树叶', exact: true }).click();
  await expect(page.getByText('给导师写一封邮件', { exact: true })).toBeVisible();

  // 它必须真的进了后端,而且**挂在当前这一层**上 —— 挂错层是最容易悄悄发生的事:
  // 界面看着一切正常,只是这个节点跑到别的地方去了。
  const after = await getPlan(page, token, workspaceId);
  const created = after.nodes.find(node => node.title === '给导师写一封邮件');
  expect(created, '界面上建的节点必须真的进库').toBeTruthy();
  expect(created!.parentId, '新节点应该挂在当前所在的这一层下面').toBe(leafId);

  // --- 空间文件跟着层走 ----------------------------------------------------------
  await page.getByRole('button', { name: /空间文件/ }).click();
  await page.getByLabel('添加空间文件').setInputFiles({
    name: 'research-notes.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('导师方向：大模型与自然语言处理'),
  });
  await expect(page.getByRole('button', { name: '预览 research-notes.md' })).toBeVisible();
  await page.getByRole('button', { name: '关闭弹窗' }).click();

  // --- 任务视图读的是同一份计划 --------------------------------------------------
  await page.getByRole('tab', { name: '任务', exact: true }).click();
  await expect(page.locator('.task-detail')).toContainText(['给导师写一封邮件']);

  // --- 返回上级:回到阶段那一层,而文件**不**跟上来 ------------------------------
  await page.getByRole('tab', { name: '路径', exact: true }).click();
  await page.getByRole('button', { name: '返回上级空间' }).click();
  await expect(page.locator('.growth-node.goal .node-title')).toContainText('联系导师');
  await expect.poll(() => renderedNodeIds(page)).toEqual([stageId, leafId].sort());

  await page.getByRole('button', { name: /空间文件/ }).click();
  await expect(page.locator('.file-list'), '文件属于它上传时所在的那一层').toHaveCount(0);
  await page.getByRole('button', { name: '关闭弹窗' }).click();

  // --- 再进去一次,它还在 --------------------------------------------------------
  await page.getByRole('button', { name: '进入整理实验室资料空间', exact: true }).click();
  await page.getByRole('button', { name: /空间文件/ }).click();
  await expect(page.getByRole('button', { name: '预览 research-notes.md' })).toBeVisible();
  await page.getByRole('button', { name: '关闭弹窗' }).click();

  // 整条路上不该有任何未捕获的页面异常。白屏是 React 抛错的样子,
  // 而它对 Playwright 来说只是"某个元素没出现"。
  expect(errors).toEqual([]);
});
