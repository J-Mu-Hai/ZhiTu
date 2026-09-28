import { expect, test } from '@playwright/test';
import {
  assertBackendRunning,
  clickUntilVisible,
  createNode,
  createWorkspace,
  getPlan,
  getToday,
  openSpacePage,
  registerAccount,
  scheduleEverything,
} from './support/session';

/**
 * 今天、随笔、我的 —— 三条和"计划"隔着一层的页面。
 *
 * ## 这个文件原来有两条测试,一条整份没有了
 *
 * 上一版的第一条验的是**空间嵌套**(进入子空间 → 在里面建节点和文件 → 返回上级)。
 * 那件事一个字没变,但它现在由 `level-navigation.spec.ts` 在真实空间里验,而且验得
 * 更狠:每一层都拿 `GET /plan` 对一遍,新节点必须挂在**当前所在的这一层**上。
 * 同一件事写两遍只会让人以为它们是两件事,所以这里那条删掉了。
 *
 * 上一版的第二条走 `?workspace=primary` 那份保研演示数据,验"今天 → 随笔 → 对话 →
 * 我的"之间是连着的。其中"今天"那一半验的是**示例空间专有的东西**:专注计时器、
 * "完成学习"、"为什么这样安排？" —— 那三样在真实空间里根本不存在(`RealToday` 里
 * 没有计时器)。所以它们没有被跳过,是跟着示例空间一起没有了。
 *
 * 真实空间的「今天」是另一件事,而且更值得验:**每一次勾选都会写到服务端**。
 * 下面第一条就验它 —— 先通过排期排出今天真的一场,再在界面上勾,最后回后端对。
 *
 * 跨页连接那一半由 `workbench.spec.ts` 接了过去(它走完今天/随笔/对话/我四页,
 * 再回工作台确认那份计划和那条消息都还在)。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

test('今天:排出来的安排勾一下，真的写进库', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));

  const { token } = await registerAccount(page, 'today');
  const workspaceId = await createWorkspace(page, token, '今天验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  // 带工时的节点才排得进去 —— 排期要算"这件事要做多久"。
  const task = await createNode(page, token, workspaceId, {
    parentId: root.id,
    title: '读两篇论文',
    nodeType: 'task',
    estimateMinutes: 60,
  });
  // 排期是**另一条写入路径**:它把"做多久"落成具体哪几天的场次,和截止时间是两件事
  // (见 `types/growth.ts` 里 `deadline` 那段)。这里先把它排出来,今天才有东西可勾。
  const { created } = await scheduleEverything(page, token, workspaceId);
  expect(created, '带工时的节点应该排得出场次').toBeGreaterThan(0);

  // 排期落进的是计划的 `sessions`,`GET /plan` 读得到 —— 界面画的就是它。
  const scheduled = (await getPlan(page, token, workspaceId)).nodes.find(item => item.id === task)!;
  expect(scheduled.title).toBe('读两篇论文');

  await openSpacePage(page, '/today', workspaceId);
  const item = page.locator('.today-item').filter({ hasText: '读两篇论文' });
  await expect(item.first()).toBeVisible();
  await expect(item.first()).toContainText('今天验收空间');

  // 勾第一件。**这条测试的重点在这一下之后**:真实空间的勾是一次写入,不是改内存。
  await page.getByRole('button', { name: '把「读两篇论文」标记为完成' }).first().click();

  // 判据不是"界面上变了",是**服务端那一行变了**。乐观更新能让屏幕上立刻出现"完成了",
  // 而写入失败时它同样会出现 —— 那时候这条断言会把它抓出来。
  await expect
    .poll(async () => (await getToday(page, token)).items.find(entry => entry.nodeId === task)?.result)
    .toBe('completed');
  // 「记一笔」要如实显示记录的是哪一种结果 —— 把"跳过"和"完成了"归成同一句话,
  // 等于把用户说的话抹掉。
  await expect(item.first()).toContainText('完成了');

  // 刷新之后还在:这一份是从库里读回来的,不是浏览器记着自己刚点过。
  await page.reload();
  await expect(page.locator('.today-item').filter({ hasText: '读两篇论文' }).first()).toContainText('完成了');
  await expect(page.getByRole('button', { name: '把「读两篇论文」标记为完成' }).first()).toHaveAttribute('aria-pressed', 'true');

  // 旁白说的是**服务端算出来的数**:今天几件、合计多少分钟、记了几件。
  // 把它写死在测试里等于在验自己,所以这几个数从 `/api/today` 取。
  const feed = await getToday(page, token);
  const aside = page.locator('.today-aside');
  await expect(aside).toContainText('来自知途的观察');
  await expect(aside).toContainText(`今天有 ${feed.itemCount} 件事，合计约 ${feed.plannedMinutes} 分钟。`);
  await expect(aside).toContainText(`已记下 ${feed.recordedCount} 件`);
  // 「没记的那些，我不会替你猜」只在**真的还有没记的**时候出现 —— 今天只有这一场,
  // 而它已经记过了,所以这句现在不该出现。服务端少算了一件的话,它会冒出来。
  expect(feed.recordedCount).toBe(feed.itemCount);
  await expect(aside).not.toContainText('没记的那些，我不会替你猜');

  expect(errors).toEqual([]);
});

test('随笔:发布的日期按本地算，关联的是这个空间真实的节点，而且不标"示例"', async ({ page }) => {
  // 日期按**东八区**算,不是 UTC。用 UTC 的话,东八区早上 8 点之前用户会看到昨天。
  const parts = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit' })
    .formatToParts(new Date());
  const month = parts.find(part => part.type === 'month')!.value;
  const day = parts.find(part => part.type === 'day')!.value;

  const { token } = await registerAccount(page, 'journal');
  const workspaceId = await createWorkspace(page, token, '随笔验收空间');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];
  const linked = await createNode(page, token, workspaceId, { parentId: root.id, title: '整理实验室资料', nodeType: 'task' });

  await openSpacePage(page, '/journal', workspaceId);
  await page.getByRole('button', { name: '写下一点此刻的想法' }).click();
  // 新空间的随笔是**空的** —— 没有那两篇别人写的。
  await expect(page.locator('.journal-list-item')).toHaveCount(0);

  /*
   * **顺序是有意的:先点开「标签」,再写正文。**
   *
   * `page.goto` 之后,服务端渲染出来的输入框**立刻就在 DOM 里**,而 React 还没接管它。
   * 那一刻 `fill` 写进去的字只在 DOM 里,组件里那个 state 还是空的;等水合完成,
   * React 按 state 重画一次,这段字就没了 —— 表现成"正文空着、发布按钮一直是灰的",
   * 看起来像产品坏了,其实是**这一枪打早了**。
   *
   * 实测撞到过一次(2026-09-27 01:03 那一轮,现场 `artifacts/runs/20260927-010324-551a8da/`),
   * 而且现场能证明原因:同一页里,之后才填的「标签」和「关联计划」两个控件**都留着值**,
   * 只有最先填的那个正文框是空的。
   *
   * 所以先点「标签」——这一下同时是"这一页已经醒过来"的证据:点不中效果就再点一次
   * (见 `clickUntilVisible`)。等这个下拉露出来,React 一定已经接管了这一页,再写正文
   * 就打得中。断言没有因此变松:发布之后下面那几条该对的还是要对。
   */
  await clickUntilVisible(
    page,
    page.getByRole('button', { name: '标签', exact: true }),
    page.getByLabel('关联计划', { exact: true }),
  );
  await page.getByLabel('此刻的想法').fill('迈出了第一步\n今天整理好了实验室资料。');
  await page.getByLabel('标签', { exact: true }).fill('科研');
  // 关联计划的下拉里是这个空间**真实的节点**,不是示例数据里写死的那几个。
  await page.getByLabel('关联计划', { exact: true }).selectOption(linked);
  await page.getByRole('button', { name: '发布', exact: true }).click();

  const entry = page.getByRole('article', { name: '随笔全文' });
  await expect(entry).toBeVisible();
  await expect(page.locator('.journal-list-item')).toHaveCount(1);
  await expect(entry.locator('time')).toContainText(`${month}-${day}`);
  await expect(page.locator('.journal-list-item .journal-tags')).toContainText('科研');
  // 自己写的东西永远不带"示例"这个标。示例空间删掉之后这个标连生产者都没有了
  // (`.example-badge` 全仓无人渲染),所以这一条现在的含义是"它不许回来"。
  await expect(entry.locator('.example-badge')).toHaveCount(0);
  await expect(page.locator('.journal-list-item .example-badge')).toHaveCount(0);

  // 关联的那件事是真的计划节点,点得到它所在的那一层。
  await expect(entry.locator('.journal-tags')).toContainText('整理实验室资料');
});

/*
 * ## 这一条是**红的**,原因写在这里,不藏在别处
 *
 * `publishJournal` 只往 React state 里塞一条,后端**没有随笔表,也没有随笔接口**
 * (`backend/db/models/` 里没有它,`api/routes/` 里也没有)。所以刷新之后这条记录
 * 就没了 —— 用户看得到自己刚写的东西,回到这一页却什么都不剩。
 *
 * 这不是这次删示例空间删出来的:**以前它存在 localStorage 里**,是真实空间这一侧
 * 一直没有的存储。示例空间整个删掉之后,这一页就成了"发布看起来成功了、其实只在
 * 这一次会话里"。
 *
 * 用 `fixme` 而不是删掉:删掉等于这件事不存在,而它是一件会丢用户东西的事。
 * 修它要加一张表(和步骤 2 的迁移一起做),那是要单独决定的事 —— 所以先把它
 * 摆在这里,让下一个人一眼看见。
 */
test.fixme('随笔刷新之后还在（后端还没有随笔表，publishJournal 只写内存）', async ({ page }) => {
  const { token } = await registerAccount(page, 'journal-reload');
  const workspaceId = await createWorkspace(page, token, '随笔持久化空间');

  await openSpacePage(page, '/journal', workspaceId);
  await page.getByLabel('此刻的想法').fill('这条记录应该活过一次刷新');
  await page.getByRole('button', { name: '发布', exact: true }).click();
  await expect(page.locator('.journal-list-item')).toHaveCount(1);

  await page.reload();
  await expect(page.locator('.journal-list-item')).toHaveCount(1);
  await expect(page.locator('.journal-list-item').first()).toContainText('这条记录应该活过一次刷新');
});

test('我的:几页子页都打得开，开关是能给用户拨的', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await registerAccount(page, 'profile-pages');

  // 我的页不需要空间,用裸地址就行。
  await page.goto('/me');
  await expect(page.locator('.profile-page')).toBeVisible();

  const slugs = ['archive', 'profile', 'reports', 'assets', 'behavior', 'memory', 'settings'];
  for (const slug of slugs) {
    await page.locator(`.top-profile-nav a[href="/me/${slug}"]`).click();
    await expect(page.locator('.detail-page h1')).toBeVisible();
  }

  await page.getByRole('button', { name: '陪伴', exact: true }).click();
  await page.getByRole('switch', { name: '主动聊天' }).click();
  await expect(page.getByRole('switch', { name: '主动聊天' })).toHaveAttribute('aria-checked', 'false');

  await page.locator('.top-profile-nav a[href="/me/reports"]').click();
  // 先等报告列表真的画出来再点进去。少了这一句,点"周报"有可能落在上一页
  // (个人中心每一页的导航都是同一套链接,换页是客户端跳转)—— 那是竞态,不是产品问题。
  await expect(page.locator('.report-list')).toBeVisible();
  await page.getByRole('link', { name: /周报/ }).click();
  await expect(page.locator('.report-body')).toBeVisible();

  // 换一页再回来,开关还是关着的 —— 它记住了。
  await page.locator('.top-profile-nav a[href="/me/settings"]').click();
  await expect(page.getByRole('button', { name: '陪伴', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('switch', { name: '主动聊天' })).toHaveAttribute('aria-checked', 'false');
  expect(errors).toEqual([]);
});
