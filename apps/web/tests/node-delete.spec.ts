import { expect, test } from '@playwright/test';
import { openDemoSpace, registerAccount } from './support/session';
import { growthReducer } from '../src/features/growth/reducer';
import { initialGrowth } from '../src/mock/growth-state';
import type { GrowthState } from '../src/types/growth';

test('deleting a node removes all descendants and connected edges', () => {
  const state: GrowthState = structuredClone(initialGrowth);
  state.nodes['delete-parent'] = {
    id: 'delete-parent', title: '待删除父节点', type: 'capability', parentId: 'research', category: 'research',
    status: 'pending', priority: 'medium',
  };
  state.nodes['delete-child'] = {
    id: 'delete-child', title: '待删除子节点', type: 'task', parentId: 'delete-parent', category: 'research',
    status: 'pending', priority: 'medium',
  };
  state.nodes['delete-grandchild'] = {
    id: 'delete-grandchild', title: '待删除孙节点', type: 'task', parentId: 'delete-child', category: 'research',
    status: 'pending', priority: 'medium',
  };
  state.edges.push(
    { id: 'delete-edge', source: 'delete-child', target: 'project', type: 'dependency' },
    { id: 'keep-edge', source: 'contact', target: 'project', type: 'dependency' },
  );

  const next = growthReducer(state, { type: 'DELETE_NODE', nodeId: 'delete-parent' });

  expect(next.nodes['delete-parent']).toBeUndefined();
  expect(next.nodes['delete-child']).toBeUndefined();
  expect(next.nodes['delete-grandchild']).toBeUndefined();
  expect(next.nodes.project).toBeDefined();
  expect(next.edges.map(edge => edge.id)).not.toContain('delete-edge');
  expect(next.edges.map(edge => edge.id)).toContain('keep-edge');
});

/*
 * 这一条要一片"能挂到 research 底下的树叶"做靶子,而 research 本身只长在示例空间
 * 那棵树上 —— 新账户的空间是空的,连分类都还没有。所以先登录再进示例空间。
 *
 * 进 research 这条路径要**双击**:单击是选中 + 打开节点编辑框,双击才 `enterSpace`
 * (见 `PathView.tsx` 的 `onNodeClick` / `onNodeDoubleClick`)。只有真的进了子空间,
 * `添加树叶` 才会出现 —— 根这一层加的是分类,按钮上写的是别的字。
 */
test('node trash appears on hover and deletes the node from its path', async ({ page }) => {
  await registerAccount(page, 'delete');
  await openDemoSpace(page, '/workbench');
  await page.locator('.react-flow__node[data-id="research"]').dblclick({ delay: 60 });
  await expect(page.getByRole('button', { name: '添加树叶', exact: true })).toBeVisible();

  await page.getByRole('button', { name: '添加树叶', exact: true }).click();
  await page.getByLabel('树叶名称').fill('待删除方向');
  await page.getByLabel('树叶类型').selectOption('capability');
  await page.getByRole('dialog').getByRole('button', { name: '添加树叶', exact: true }).click();
  await page.getByRole('button', { name: '进入待删除方向空间' }).click();

  await page.getByRole('button', { name: '添加树叶', exact: true }).click();
  await page.getByLabel('树叶名称').fill('待删除子任务');
  await page.getByRole('dialog').getByRole('button', { name: '添加树叶', exact: true }).click();
  await page.getByRole('button', { name: '返回上级空间' }).click();

  const parentNode = page.locator('.react-flow__node').filter({ hasText: '待删除方向' });
  const deleteButton = parentNode.getByRole('button', { name: '删除待删除方向及其子节点' });
  await expect(deleteButton).toHaveCSS('opacity', '0');
  await parentNode.hover();
  await expect(deleteButton).toHaveCSS('opacity', '1');
  await deleteButton.click();

  await expect(parentNode).toHaveCount(0);
  await page.reload();
  await expect(page.locator('.react-flow__node').filter({ hasText: '待删除方向' })).toHaveCount(0);
});
