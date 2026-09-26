import { test, expect } from '@playwright/test';
import { demoDate, demoUrl, openDemoSpace, registerAccount } from './support/session';

/*
 * 这一条是"一个状态、多个视图"的总验收:同一份计划在路径、时间线、任务三个视图里
 * 是同一份,拖动时间线产生的调整会被路径视图认下来,而且几个页面之间共享。
 *
 * 它验的是那份保研演示数据,所以走的是**示例空间**;里面所有数字(10-18、科研项目、
 * 四个任务)都是那棵树上的节点。
 */

test('shared growth state, canvas context, timeline drag and accepted proposal', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await registerAccount(page, 'workbench');
  /*
   * 落点变了:根路径现在把人送到"成长空间"列表,不是工作台。
   * 工作台需要一个具体的空间,而刚注册的账户一个都没有 —— 见 `src/app/page.tsx`
   * 的 `redirect('/spaces')`,以及 WorkspaceRouter 的注释。
   * (得先登录再开这个地址:没登录时 AppShell 会先把人送到 `/login`。)
   */
  await page.goto('/');
  await expect(page).toHaveURL(/\/spaces$/);

  await openDemoSpace(page, '/workbench');
  // 双击进 research 这条路径。单击是"选中 + 打开节点编辑框",那个 modal 会一直挡在
  // 画布上面,后面点视图页签就会被它截住(见 `PathView.tsx` 的两个回调)。
  await page.locator('.react-flow__node[data-id="research"]').dblclick({ delay: 60 });
  await expect(page.getByRole('textbox', { name: '给 AI 的消息' })).toHaveAttribute('placeholder', '关于「科研能力」，告诉 AI 你的想法……');
  await page.screenshot({ path: 'artifacts/path.png' });
  await page.getByRole('tab', { name: '时间线' }).click();
  await expect(page.getByTestId('timeline-canvas')).toHaveAttribute('data-ready', 'true');
  if (!await page.locator('[data-timeline-item="project"]').count()) {
    await page.getByRole('button', { name: /另有.*展开/ }).click();
    await page.locator('[data-cluster-panel]').getByRole('button', { name: /科研项目/ }).click();
  }
  const bar = page.locator('[data-timeline-item="project"] [data-timeline-card]');
  const box = await bar.boundingBox();
  if (!box) throw new Error('Missing project card');
  const density = Number(await page.getByTestId('timeline-canvas').getAttribute('data-density'));
  /*
   * 把同一个任务拖 45 天。起点不是字面量:示例数据的日期是相对种子锚点排的,
   * 运行时会整体平移到"今天" —— 见 `demoDate` 上面那段。45 天是**绝对**位移,
   * 所以拖完之后落点是"种子日期 2026-12-02 平移之后"的那一天。
   */
  await page.mouse.move(box.x + 12, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + 12 + 45 * density, box.y + box.height / 2, { steps: 12 });
  await page.mouse.up();
  await expect(page.getByRole('button', { name: '接受调整' })).toBeVisible();
  await page.getByRole('button', { name: '查看影响' }).click();
  await expect(page.getByTestId('plan-ghost')).toHaveCount(1);
  await expect(page.getByTestId('date-inspector')).toContainText(demoDate('2026-12-02'));
  /*
   * 下面两天仍然是**字面量**,别顺手也套上 `demoDate`。
   * 这不是种子数据里的日期,是演示用的一段写死的建议:"把科研项目挪到寒假"
   * (见 `provider.tsx` 里那条提案的 `startDate: '2027-01-18'`)。它不随"今天"平移,
   * 所以它该原样是 1 月 18 日 —— 平移了反而是把产品的行为说错了。
   */
  await expect(page.locator('[data-timeline-item="project"]')).toHaveAttribute('data-start-date', '2027-01-18');
  await page.getByRole('button', { name: '接受调整' }).click();
  await expect(page.getByTestId('date-inspector')).toContainText('2027-01-18');
  await expect(page.getByTestId('plan-ghost')).toHaveCount(0);
  await page.screenshot({ path: 'artifacts/timeline.png' });
  await page.getByRole('tab', { name: '任务', exact: true }).click();
  await page.getByRole('button', { name: '完成科研项目', exact: true }).click();
  await page.getByRole('button', { name: '今天', exact: true }).click();
  await expect(page.locator('.task-detail')).toHaveCount(4);
  await page.getByRole('button', { name: '联系导师', exact: false }).last().click();
  await expect(page.getByRole('textbox', { name: '给 AI 的消息' })).toHaveAttribute('placeholder', '关于「联系导师」，告诉 AI 你的想法……');
  await page.getByRole('textbox', { name: '给 AI 的消息' }).fill('我想先整理导师资料');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.locator('.message').last()).toContainText('联系导师');
  await page.screenshot({ path: 'artifacts/tasks.png' });
  await page.getByRole('tab', { name: '路径', exact: true }).click();
  await expect(page.locator('.react-flow__node[data-id="project"] .growth-node')).toHaveClass(/is-complete/);
  /*
   * 跨页走一圈,再回到工作台 —— 上面在时间线上做的调整和刚才勾的完成状态都得还在。
   *
   * 用 `demoUrl` 拼地址而不是点导航链接:那六个链接指向的是裸地址,不带
   * `?workspace=`,在示例空间里点它们会丢掉空间上下文(见 `support/session.ts`)。
   * 这条要验的是"换页面不丢状态",不是导航链接的拼写。
   */
  for (const name of ['today', 'journal', 'conversations', 'me']) {
    await page.goto(demoUrl(`/${name}`));
    await expect(page.locator('.editorial-page, .conversation-hub')).toBeVisible();
  }
  await openDemoSpace(page, '/workbench');
  await expect(page.locator('.react-flow__node[data-id="project"] .growth-node')).toHaveClass(/is-complete/);
  expect(errors).toEqual([]);
});
