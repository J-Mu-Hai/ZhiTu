import { expect, test } from '@playwright/test';
import { assertBackendRunning, getPlan, openSpacePage, registerAccount, waitForRealPlan } from './support/session';

/**
 * 验收场景 1:**一个刚注册的账户,看到的必须是"没有",而不是"别人的一份"**。
 *
 * ## 为什么这条值得单独立一个文件
 *
 * 上一版有一个"示例空间"(保研那套演示数据),而它一度是**没选空间时的默认值**:
 * 一个刚注册、一个空间都没有的账户,第一次打开工作台,看到的是 24 个别人的保研节点、
 * 四条预置对话、两篇别人写的随笔,还有一段写着"来自知途的观察:你今天课程安排比较满"
 * 的文案 —— 而知途从来没有拿到过这个用户的课表。
 *
 * 演示空间已经整个删掉了。这个文件钉住的就是**它没有以任何形式回来**:不回来当默认值,
 * 不回来当"示例入口",不回来当空状态里的提示卡。
 *
 * 这条测试查的是"不存在",而"不存在"很容易被将来某次改动悄悄破坏 —— 所以每一条断言
 * 都指向一个具体的、曾经真的出现过的东西(保研、attention、示例、演示)。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

/**
 * 页面上任何地方都不该出现的字样。
 *
 * 它们是那份演示数据里真实出现过的词。不用"示例"这个词本身 —— 产品里还有
 * 别的地方可以正当地说"示例"(比如设置页的说明文字),拿它当判据会误伤。
 */
const DEMO_WORDS = ['保研', 'attention', 'Attention', '导师资料', '夏令营', 'Transformer', '示例空间'];

async function expectNoDemoContent(page: import('@playwright/test').Page, where: string): Promise<void> {
  const body = await page.locator('body').innerText();
  for (const word of DEMO_WORDS) {
    expect(body, `${where}不该出现演示内容里的「${word}」`).not.toContain(word);
  }
}

test('新建的空间里只有用户自己的根目标，没有任何演示内容', async ({ page }) => {
  const { token } = await registerAccount(page, 'bootstrap');

  // --- 空间列表:一个空间都没有,而且**没有"先看看示例空间"的入口** -------------
  await page.goto('/spaces');
  await expect(page.locator('.space-card')).toHaveCount(0);
  await expect(page.getByText('创建新的成长空间')).toBeVisible();
  // 那句"先看看示例空间（本地预置数据，不经过模型）"是这一版删掉的入口。
  // 它指向的 `?workspace=primary` 已经打不开任何东西,留着的话用户点下去
  // 只会看到"打不开这个成长空间",而他并没有做错任何事。
  await expect(page.getByText('先看看示例空间')).toHaveCount(0);
  await expectNoDemoContent(page, '空间列表');

  // --- 建一个空间。这是**真人走的那条路**:点卡片、填名称、提交 ------------------
  await page.getByText('创建新的成长空间').click();
  await page.getByLabel('空间名称').fill('我的第一个空间');
  await page.getByLabel('想在这里推进什么？').fill('把一件事做成');
  await page.getByRole('dialog').getByRole('button', { name: '创建并进入' }).click();

  await waitForRealPlan(page);

  // --- 后端那一份:恰好一个根目标,零个子节点 ----------------------------------
  const workspaceId = new URL(page.url()).searchParams.get('workspace');
  expect(workspaceId, '创建之后应该已经进到这个空间的地址上').toBeTruthy();
  const plan = await getPlan(page, token, workspaceId!);
  expect(plan.nodes, '新空间应该只有一个根目标').toHaveLength(1);
  expect(plan.nodes[0].nodeType).toBe('goal');
  expect(plan.nodes[0].title).toBe('我的第一个空间');
  expect(plan.totalNodes).toBe(1);

  // --- 界面那一份:只有这个根目标,没有别的东西 --------------------------------
  await expect(page.locator('.react-flow__node')).toHaveCount(1);
  // 画布上那一个节点就是根目标,标题是刚填的空间名。
  await expect(page.locator('.react-flow__node .node-title')).toHaveText(['我的第一个空间']);
  await expect(page.getByText('这里，还可以长出更多可能。')).toBeVisible();
  await expectNoDemoContent(page, '新空间的工作台');

  // --- 对话:一条空对话,不是四条预置的 ----------------------------------------
  await openSpacePage(page, '/conversations', workspaceId!);
  await expect(page.locator('.hub-item')).toHaveCount(1);
  await expect(page.locator('.hub-item')).toContainText('我的第一个空间');
  await expect(page.locator('.hub-messages .message')).toHaveCount(0);
  await expect(page.getByText('从一个问题开始。不用急着有答案。')).toBeVisible();
  // 「示例」这个标是给**系统预置内容**用的。示例空间删掉之后它连生产者都没有了
  // (全仓已经没有任何地方渲染 `.example-badge`),所以这一条现在是"它不许回来":
  // 一旦有人把预置内容重新塞回新用户的空间,这个标会跟着一起回来。
  // `DEMO_WORDS` 扫的是文字,这个扫的是那个标本身。
  await expect(page.locator('.example-badge')).toHaveCount(0);
  await expectNoDemoContent(page, '新空间的对话页');

  // --- 首页:旁白只说它真的知道的事 ----------------------------------------------
  //
  // 注意**没有**断言"来自知途的观察"这句话不存在 —— 它还在,而且留着是对的:
  // `RealToday` 的旁白现在说的是"今天有几项、来自哪个空间、报了多少分钟",全部由
  // 服务端算出来。要钉的是那段**写死的文案**没有回来:它说"你今天课程安排比较满"
  // "我把科研任务降低到了一个" —— 知途从来没有拿到过用户的课表,也没替谁做过决定。
  await openSpacePage(page, '/today', workspaceId!);
  const todayAside = page.locator('.today-aside');
  await expect(todayAside).toContainText('来自知途的观察');
  await expect(todayAside).not.toContainText('课程安排比较满');
  await expect(todayAside).not.toContainText('我把科研任务降低到了一个');
  await expect(todayAside).toContainText('今天还没有排上具体的事。');
  await expectNoDemoContent(page, '新空间的首页');

  // --- 随笔:空的,不是两篇别人的 --------------------------------------------------
  await openSpacePage(page, '/journal', workspaceId!);
  await expect(page.locator('.journal-list-item')).toHaveCount(0);
  await expect(page.getByText('这里还没有记录，写下第一篇随笔吧。')).toBeVisible();
  await expect(page.locator('.example-badge')).toHaveCount(0);
  await expectNoDemoContent(page, '新空间的随笔页');

  // 刷新一次再确认一遍:这一份"空"是后端说的,不是浏览器记错了。
  await page.goto(`/workbench?workspace=${workspaceId}`);
  await waitForRealPlan(page);
  await expect(page.locator('.react-flow__node')).toHaveCount(1);
  await expect(page.locator('.example-badge')).toHaveCount(0);
  await expectNoDemoContent(page, '刷新之后的工作台');
});
