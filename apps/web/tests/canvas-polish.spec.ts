import { expect, test } from '@playwright/test';
import { api, createWorkspace, registerAccount } from './support/session';

test('品牌标识一致，长标题与正文完整排版，子空间沿用圆滑连线', async ({ page }) => {
  test.slow();
  await page.goto('/login');
  await expect(page.locator('.auth-brand img')).toHaveAttribute('src', '/icon.svg');
  const icon = await page.request.get('/icon.svg');
  expect(icon.ok()).toBeTruthy();
  expect(await icon.text()).toContain('linearGradient');
  const apple = await page.request.get('/apple-icon');
  expect(apple.ok()).toBeTruthy();
  expect(apple.headers()['content-type']).toContain('image/png');
  const { token } = await registerAccount(page, 'canvas-polish');
  const workspace = await createWorkspace(page, token, '画布视觉验收');
  const plan = await api<{ nodes: { id: string }[] }>(page, token, `/api/workspaces/${workspace}/plan`);
  const root = plan.nodes[0].id;
  const longTitle = '阶段一：建立完整的学习基础与复盘流程，同时整理课程成绩和研究方向';
  const description = '这是一段应完整显示的节点说明。先梳理课程知识，再记录遇到的问题，最后形成下一周能够执行的行动。\n' + 'LongUnbrokenDescription'.repeat(6);
  let first = '';
  for (const title of [longTitle, '阶段二：英语能力', '阶段三：科研探索']) {
    const added = await api<{ node: { id: string } }>(page, token, `/api/workspaces/${workspace}/nodes`, {
      method: 'POST', data: { parentId: root, nodeType: 'stage', title, description },
    });
    if (!first) first = added.node.id;
  }
  await api(page, token, `/api/workspaces/${workspace}/nodes`, {
    method: 'POST', data: { parentId: first, nodeType: 'task', title: longTitle, description },
  });
  await page.goto(`/workbench?workspace=${workspace}`);
  await expect(page.locator('.react-flow__node')).toHaveCount(4);
  const stage = page.locator('.react-flow__node').filter({ has: page.locator('.node-title', { hasText: longTitle }) });
  await expect(stage).toHaveCount(1);
  await expect(stage.locator('.node-description')).toHaveText(description);
  await expect.poll(async () => stage.evaluate((element) => {
    const title = element.querySelector('.node-title') as HTMLElement;
    const marker = element.querySelector('.node-marker') as HTMLElement;
    const text = element.querySelector('.node-description') as HTMLElement;
    const t = title.getBoundingClientRect();
    const m = marker.getBoundingClientRect();
    return m.right <= t.left && Math.abs(m.top - t.top) < 2
      && title.scrollWidth <= title.clientWidth + 1
      && text.scrollHeight <= text.clientHeight + 1
      && text.scrollWidth <= text.clientWidth + 1;
  })).toBe(true);
  await expect(page.locator('.react-flow__edge-branch')).toHaveCount(3);
  expect(await page.locator('.react-flow__edge-path').first().getAttribute('d')).toContain('Q');
  await page.screenshot({ path: 'artifacts/canvas-polish.png' });
  await stage.getByRole('button', { name: `进入${longTitle}空间` }).click();
  await expect(page.locator('.react-flow__node')).toHaveCount(2);
  await expect(page.locator('.growth-node.task .node-task-box')).toBeVisible();
  expect(await page.locator('.growth-node.goal .node-title').evaluate((element) => ({
    align: getComputedStyle(element).textAlign,
    maxWidth: getComputedStyle(element).maxWidth,
  }))).toEqual({ align: 'left', maxWidth: 'none' });
  await expect(page.locator('.react-flow__edge-branch')).toHaveCount(1);
  await page.screenshot({ path: 'artifacts/canvas-polish-subspace.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/login');
  await page.evaluate(() => localStorage.clear());
  await page.goto('/login');
  await expect(page.locator('.auth-brand img')).toBeVisible();
  await page.screenshot({ path: 'artifacts/brand-mobile.png' });
});
