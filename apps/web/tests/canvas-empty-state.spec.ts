/**
 * 画布空态的边界:空态只在**画布上真的什么都没有**时出现。
 *
 * 这一条**不需要 --script**:隔离栈默认走 `rule` 兜底,它不产出问题地图、也不提问,
 * 所以一个刚建好、只有一个根目标的空间正是"完全空白"的状态。此时空态入口必须还在
 * —— 否则新建空间会既没有计划、也没有引导。
 *
 * 反面(有 reasoning map / question 时空态必须消失)由 `goal-reasoning.spec.ts` 覆盖,
 * 那条要 --script 才能拉起地图。
 */

import { expect, test } from '@playwright/test';
import { assertBackendRunning, createWorkspace, registerAccount, waitForRealPlan } from './support/session';

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
});

test('完全空白的新空间仍显示空态入口', async ({ page }) => {
  const { token } = await registerAccount(page, 'canvas-empty');
  const workspaceId = await createWorkspace(page, token, '空白画布空间', '我想先想清楚要做点什么');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);

  const note = page.locator('.empty-space-note');
  await expect(note, '没有任何内容的画布应该显示空态入口').toBeVisible({ timeout: 20000 });
  await expect(note).toContainText('这里，还可以长出更多可能');
  await expect(note.getByRole('button', { name: '添加第一个子节点' })).toBeVisible();

  // 空态只是引导:点它应该打开新建节点对话框,而不是别的动作。
  await note.getByRole('button', { name: '添加第一个子节点' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: '关闭弹窗' }).click();
});
