import { expect, test } from '@playwright/test';
import { openDemoSpace, registerAccount } from './support/session';

/*
 * 四个成长分类、以及"分类是它自己那条路径的中心" —— 验的都是示例空间里那棵树
 * 的**形状**(academic/research/experience/personal 四个节点,以及 research 底下的
 * explore 树叶)。
 *
 * 那棵树在新账户那里不存在:新空间只有根目标一个节点,分类是按需长出来的。
 * 所以这两条都要先进示例空间(`?workspace=primary`,见 `support/session.ts`)。
 *
 * ## 进子路径是**双击**
 *
 * 以前单击就进。现在单击是"选中并打开节点编辑框"(先是 `PathView` 里一段 240ms 的
 * 防抖,等真双击来了就取消),双击才 `enterSpace` —— 见 `PathView.tsx` 的
 * `onNodeClick` / `onNodeDoubleClick`,编辑框自己那行说明里也写着"双击节点可进入其子路径"。
 * 所以这里的动作从 `click()` 换成 `dblclick()`:断言的对象没变(进去之后
 * 面包屑要变成这个分类的名字),变的是"进去"这个动作的触发方式。
 */

test('four growth categories open their own leaf paths with a double click', async ({ page }) => {
  await registerAccount(page, 'leafpath');
  await openDemoSpace(page, '/workbench');

  for (const category of ['academic', 'research', 'experience', 'personal']) {
    await page.locator(`.react-flow__node[data-id="${category}"]`).dblclick({ delay: 60 });
    await expect(page.locator('.space-breadcrumb')).toContainText(
      ({
        academic: '学业成绩',
        research: '科研能力',
        experience: '综合经历',
        personal: '个人成长',
      } as const)[category as 'academic' | 'research' | 'experience' | 'personal'],
    );
    await expect(page.locator(`.react-flow__node[data-id="${category}"] .goal`)).toBeVisible();
    await page.getByRole('button', { name: '返回上级空间' }).click();
  }
});

test('a category is the center of its path and new leaves can carry details', async ({ page }) => {
  await registerAccount(page, 'leaf-add');
  await openDemoSpace(page, '/workbench');
  // 双击进 research 这条路径 —— 单击只会选中并打开编辑框(见上面那段注释)。
  await page.locator('.react-flow__node[data-id="research"]').dblclick({ delay: 60 });
  await expect(page.locator('.path-canvas.leaf-path')).toBeVisible();

  const center = page.locator('.react-flow__node[data-id="research"]');
  const existingLeaf = page.locator('.react-flow__node[data-id="explore"]');
  const centerBox = await center.boundingBox();
  const leafBox = await existingLeaf.boundingBox();
  expect(centerBox).not.toBeNull();
  expect(leafBox).not.toBeNull();
  expect(leafBox!.y).toBeGreaterThan(centerBox!.y + centerBox!.height);

  await page.getByRole('button', { name: '添加树叶', exact: true }).click();
  await expect(page.getByRole('heading', { name: '为「科研能力」添加树叶' })).toBeVisible();
  await page.getByLabel('树叶名称').fill('阅读导师最新论文');
  await page.getByLabel('树叶说明').fill('梳理研究问题、方法与可复现实验');
  await page.getByLabel('树叶类型').selectOption('capability');
  await page.getByRole('dialog').getByRole('button', { name: '添加树叶', exact: true }).click();

  const newLeaf = page.locator('.react-flow__node').filter({ hasText: '阅读导师最新论文' });
  await expect(newLeaf).toContainText('梳理研究问题、方法与可复现实验');
  await expect(page.getByRole('button', { name: '进入阅读导师最新论文空间' })).toBeVisible();
});
