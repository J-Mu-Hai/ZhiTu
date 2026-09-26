import { expect, test } from '@playwright/test';
import {
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  openSpacePage,
  registerAccount,
  waitForRealPlan,
} from './support/session';

/**
 * 对话页。
 *
 * ## 这个文件接的是 `conversation-management.spec.ts` 的班,但接的不是同一件事
 *
 * 那一条验的是"自己建的第一条对话会顶掉那四条示例":左侧一列对话、新建对话、编辑
 * 名称和标签、按标签搜索。那一整套现在**没有对应的东西**了 —— 后端每个空间恰好一条
 * `kind='primary'` 会话(`uq_conversations_primary` 就是这条规则的落点),左侧那一列
 * 对话全部来自示例空间的演示数据。所以"新建对话"这个入口连同那个弹窗一起删掉了,
 * 这条测试不是被跳过,是它验的界面没有了。
 *
 * 留下来的那件事更重要,而且在真实空间里才验得动:**对话页和工作台是同一份对话**。
 * 在工作台里对着某个节点说的话,翻到对话页要看得出来它是关于那个节点的 —— 而
 * "关于哪个节点"这件事的唯一真实来源,是那条消息自己在库里的 `contextNodeId`。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('对话页:只有这个空间那一条，而且和工作台是同一份', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));

  const { token } = await registerAccount(page, 'hub');
  const workspaceId = await createWorkspace(page, token, '对话验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  const stage = await createNode(page, token, workspaceId, { parentId: root.id, title: '联系导师', nodeType: 'stage' });
  await createNode(page, token, workspaceId, { parentId: stage, title: '整理实验室资料', nodeType: 'task' });

  // --- 新空间:一条对话,零条消息 -------------------------------------------------
  await openSpacePage(page, '/conversations', workspaceId);
  await expect(page.locator('.hub-item')).toHaveCount(1);
  // 那一列里**没有"新建对话"** —— 后端只支持一条,给了按钮也没有地方存。
  await expect(page.getByRole('button', { name: '新建对话' })).toHaveCount(0);
  await expect(page.locator('.hub-item')).toContainText('对话验收空间');
  await expect(page.locator('.hub-messages .message')).toHaveCount(0);
  await expect(page.getByText('从一个问题开始。不用急着有答案。')).toBeVisible();

  // --- 在对话页发一句:真相在后端 -------------------------------------------------
  await page.getByLabel('继续历史对话').fill('这个空间先从哪儿开始？');
  await page.getByRole('button', { name: '发送历史对话' }).click();
  const mine = page.locator('.hub-messages .message').filter({ hasText: '这个空间先从哪儿开始？' });
  await expect(mine).toBeVisible();
  // 刷新之后还在,才说明这条不在浏览器的内存里。少了这一句,"发出去立刻显示"这句话
  // 对一份纯前端的假对话同样成立。
  await page.reload();
  await expect(page.locator('.hub-messages .message').filter({ hasText: '这个空间先从哪儿开始？' })).toBeVisible();

  // --- 工作台里对着一个节点说的话,对话页看得出是关于那个节点 ---------------------
  //
  // 双击进阶段:进子空间会把它选中,而"当前选中了谁"就是工作台发消息时带上的上下文
  // (`contextNodeId`)。这是"画布上下文"那件事在库里的落点。
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await page.locator(`.react-flow__node[data-id="${stage}"]`).dblclick();
  await expect(page.getByRole('textbox', { name: '给 AI 的消息' }))
    .toHaveAttribute('placeholder', '关于「联系导师」，告诉 AI 你的想法……');
  await page.getByRole('textbox', { name: '给 AI 的消息' }).fill('这个阶段我想先联系导师');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.floating-conversation .message').filter({ hasText: '这个阶段我想先联系导师' })).toBeVisible();

  await openSpacePage(page, '/conversations', workspaceId);
  // 两条都在**同一份**列表里 —— 对话页不是另一份记录。
  await expect(page.locator('.hub-messages .message').filter({ hasText: '这个空间先从哪儿开始？' })).toBeVisible();
  await expect(page.locator('.hub-messages .message').filter({ hasText: '这个阶段我想先联系导师' })).toBeVisible();
  // 「关联内容」里的节点来自这条对话里真实出现过的 `contextNodeId`,不是写死的数组。
  const linked = page.locator('.hub-linked');
  await expect(linked).toContainText('联系导师');

  // 点它回到那一层 —— 这是关联内容唯一的用处,点不动它就只是一行字。
  await linked.getByRole('button', { name: /联系导师/ }).click();
  await expect(page).toHaveURL(/\/workbench/);
  await expect(page.locator('.space-breadcrumb')).toContainText('联系导师');

  expect(errors).toEqual([]);
});
