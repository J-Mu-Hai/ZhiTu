/**
 * 节点详情里的「AI 分析」那一块,以及它的重新分析入口。
 *
 * ## 这一块为什么值得单独验
 *
 * 它比对话里的一段回复更容易骗人,因为**它看起来像节点的属性**。它是折叠的、带
 * 徽标的、写着「依据」「风险」的分栏 —— 一段这样的文字摆在正文下面,读起来就像
 * "这个节点的事实"。所以这一组里有一半断言是**不许它说什么**:
 *
 * - 没有记录时不许显示成有判断(「还没有分析过」,不是一段编出来的分析)。
 * - 服务端说过期就必须说过期,服务端说最新才说最新 —— 徽标不许本地推算。
 * - 降级来的那一条必须写明"不是模型的判断",不许长得和真的一样。
 *
 * ## 那句按钮文案为什么在这里又断言了一遍
 *
 * 「它会替你说一句话」是按钮对用户的承诺,而兑现的地方是**对话**。所以这里验的是
 * 一整条链:点按钮 → 库里真的多了一条用户消息(内容就是那句话)→ 那句消息挂着这个
 * 节点。只在界面上看到一句话是不够的 —— 前端自己往对话列表里塞一条也能长得一样。
 * 服务端那句常量的措辞由 `backend/tests/test_analysis_refresh.py` 钉着,两边一起
 * 改才是对的:改了措辞却忘了改这里,这条测试会红,而那正是我们要的提醒。
 *
 * ## 哪些是**假**服务端,说清楚
 *
 * 隔离验收栈里没有模型 key(`AGENT_REASONER=rule`),而**降级不产生分析记录** ——
 * 这条边界本身是对的(一段不是模型的判断不能冒充判断),代价是那一栈里永远读不到
 * 一条分析。所以:
 *
 * - **读**(`GET /analyses`)在需要看"有记录时长什么样"的用例里被换成了固定载荷。
 *   换掉的只有这一读,它返回的字段名和 `AnalysisView` 逐字一致 —— 名字写错的话
 *   面板读不到值,那些"必须显示出来"的断言会红。
 * - **写**(`POST /nodes/{id}/analysis/refresh`)一律是真的。上面那条链验的就是它。
 *   用例二里徽标从"过期"翻成"最新",也是**因为这轮真的发出了**才翻的(见那里的
 *   `refreshPosts`)。
 */

import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  api,
  assertBackendRunning,
  createNode,
  createWorkspace,
  getPlan,
  openSpacePage,
  registerAccount,
  waitForRealPlan,
} from './support/session';

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
});

/**
 * 和 `analysis_service.REANALYZE_MESSAGE` 逐字相同。
 *
 * 抄一份在这里不是偷懒:这条测试要验的正是"按钮替你说了哪句话",从后端读回来再比
 * 就成了同义反复。
 */
const REANALYZE_MESSAGE = '根据最新内容重新分析一下这个节点。';

/** 一个最小场景:根目标 + 一个任务。分析是挂在节点上的,所以得有一个节点。 */
async function scene(page: Page, prefix: string) {
  const account = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, account.token, '分析验收空间', '验收节点分析区');
  const rootId = (await getPlan(page, account.token, workspaceId)).nodes[0].id;
  const taskId = await createNode(page, account.token, workspaceId, {
    parentId: rootId,
    title: '要被分析的节点',
    nodeType: 'task',
    description: '先跑一组对照实验。',
    estimateMinutes: 90,
  });
  return { account, workspaceId, rootId, taskId };
}

/** 画布上某个节点的卡片。 */
function card(page: Page, nodeId: string): Locator {
  return page.locator(`.react-flow__node[data-id="${nodeId}"]`);
}

/** 单击打开节点详情 —— 一次单击,不是双击(见 `tests/node-body.spec.ts` 的文件头)。 */
async function openEditor(page: Page, nodeId: string): Promise<Locator> {
  await card(page, nodeId).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('heading')).toContainText('编辑节点');
  return dialog;
}

/** 关掉详情弹窗。它是模态的,不关掉下面点不动。 */
async function closeEditor(page: Page): Promise<void> {
  await page.getByRole('dialog').getByRole('button', { name: '关闭弹窗' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
}

const panelOf = (dialog: Locator) => dialog.locator('.analysis-panel');
const badgeOf = (dialog: Locator) => panelOf(dialog).locator('.analysis-badge');

/** 库里这个空间此刻的对话。断言"真的走了一轮"靠它,不靠屏幕上的字。 */
async function storedMessages(page: Page, token: string, workspaceId: string) {
  const view = await api<{
    messages: { role: string; content: string; contextNodeId: string | null }[];
  }>(page, token, `/api/workspaces/${workspaceId}/messages`);
  return view.messages;
}

// ---------------------------------------------------------------------------------
// 1. 没有模型时:如实说"还没有分析过",而按钮那条链是真的
// ---------------------------------------------------------------------------------
test('没有模型时这一块说「还没有分析过」，重新分析真的在对话里留下两条消息', async ({ page }) => {
  test.slow();
  const { account, workspaceId, taskId } = await scene(page, 'analysis-degraded');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);

  /** 这一块读了几次分析。请求照常发给后端,这里只数次数(理由见用例二)。 */
  let reads = 0;
  await page.route(`**/api/workspaces/${workspaceId}/analyses*`, async (route) => {
    reads += 1;
    await route.continue();
  });

  const dialog = await openEditor(page, taskId);
  const panel = panelOf(dialog);
  const toggle = panel.getByRole('button', { name: 'AI 分析' });

  // **收起状态就已经在说这句话了。** 徽标藏在折叠区里的话,用户得先展开才知道
  // 自己改了正文让什么作废了 —— 而"改正文 → 徽标变过期"正是这一块的验收路径。
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(badgeOf(dialog)).toHaveText('还没有分析过');

  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  // 这一句是**服务端**给的(`AnalysisListResponse.note`)。写死一段意思差不多的话
  // 也能绿,所以特意断言服务端那一版里特有的措辞。
  await expect(panel).toContainText('它对你写的内容做出的判断会记在这里');

  const readsBefore = reads;
  await panel.getByRole('button', { name: '根据最新内容重新分析' }).click();

  // ---- 第二层:库里真的多了两条消息,而且那句用户消息**挂着这个节点** ----------
  //
  // **一次等两条。** 用户消息是在模型调用**之前**落的,所以"看到用户消息"不等于
  // "这一轮结束了" —— 只等它就往下断言助手回复,会跑在它前面(实测红过一次)。
  await expect
    .poll(async () => {
      const messages = await storedMessages(page, account.token, workspaceId);
      return {
        asked: messages.filter((message) => message.role === 'user').map((message) => message.content),
        replied: messages.some((message) => message.role === 'assistant'),
      };
    }, { message: '点那个按钮之后,对话里没有留下那一问一答 —— 它没有真的走一轮对话' })
    .toEqual({ asked: [REANALYZE_MESSAGE], replied: true });

  const messages = await storedMessages(page, account.token, workspaceId);
  const asked = messages.find((message) => message.content === REANALYZE_MESSAGE)!;
  expect(asked.contextNodeId, '这一轮没有挂在这个节点上 —— 模型会以为在聊整个空间').toBe(taskId);

  // ---- 第三层:降级不产生分析记录,所以这一块**不许改口** ------------------------
  // 这一条是这一层最容易犯的错:既然用户点了"重新分析",界面顺手把徽标改成
  // 「基于当前内容」看起来完全合理 —— 而库里一条记录都没有,那句话没有任何依据。
  await expect(panel.getByRole('button', { name: '根据最新内容重新分析' })).toBeEnabled();
  await expect(badgeOf(dialog), '库里没有分析记录,徽标却说这份判断基于当前内容').toHaveText(
    '还没有分析过',
  );
  await expect(panel).not.toContainText('基于当前内容');
  // 徽标没变是因为**又读了一次**、服务端还是说"没有",不是因为它压根没问。
  expect(reads, '点完重新分析之后没有再读一次服务端').toBeGreaterThan(readsBefore);
});

// ---------------------------------------------------------------------------------
// 2. 服务端说过期就说过期、说最新才说最新 —— 徽标是本地的还是读回来的
// ---------------------------------------------------------------------------------
test('服务端说这份过期了就说过期，重新分析真的发出之后才改口说最新', async ({ page }) => {
  test.slow();
  const { workspaceId, taskId } = await scene(page, 'analysis-stale');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);

  /** 一条分析记录。字段名与 `AnalysisView` 一致 —— 写错的话面板读不到值。 */
  const record = (freshness: 'fresh' | 'stale') => ({
    id: '00000000-0000-4000-8000-000000000001',
    workspaceId,
    scopeRootId: null,
    focusNodeId: taskId,
    scopeRootTitle: null,
    focusNodeTitle: '要被分析的节点',
    promptVersion: 'v1',
    modelSource: 'direct_llm',
    createdAt: '2026-09-20T10:00:00Z',
    freshness,
    staleReasons: freshness === 'stale' ? ['「要被分析的节点」的正文改过了'] : [],
    coverageNote: null,
    known: ['先跑一组对照实验'],
    unknowns: ['一组要跑多久'],
    evidence: ['用户正文里写了对照实验'],
    assumptions: ['假设实验室周末开放'],
    diagnosis: ['这个任务还缺一个时长'],
    strategyOptions: ['先估一个时长再排'],
    risks: ['估不准会让这一周排不开'],
    confidenceNote: '正文很短,把握不大',
  });

  const payload = { analyses: [record('stale')], focusNodeId: taskId, scopeRootId: null, note: '' };
  /** 这一轮重新分析**真的发出去了几次**。徽标翻面必须由它解释。 */
  let refreshPosts = 0;
  /**
   * 这一块**读了几次分析**。
   *
   * 它是"徽标是本地的还是读回来的"唯一分得清的那个量:两种实现最后都能显示
   * 「基于当前内容」—— 一个是因为真去读到了,另一个是因为它自己把状态改了。
   * 差别只在**读没读**。所以这里数次数,而不是看最后那一行字。
   */
  let reads = 0;

  // 路径**写全空间 id**,不用 `*` 兜。用通配的话,面板要是把别的东西(比如当前那一层的
  // 根节点 id)当成空间 id 拼进 URL,这个拦截照样命中,于是"读到了一切正常" ——
  // 那条真缺陷就是这样躲过前面的假服务端的(用例一里它能露出来,是因为那边没有假服务端)。
  await page.route(`**/api/workspaces/${workspaceId}/analyses*`, async (route) => {
    reads += 1;
    await route.fulfill({ status: 200, json: payload });
  });
  await page.route('**/api/workspaces/*/nodes/*/analysis/refresh', async (route) => {
    // 真的转发给后端(:8000 上没有模型 key,这一轮会降级、不留下记录)。
    // 载荷换成"最新"是**服务端**在说这句话,不是前端自己改的徽标。
    const response = await route.fetch();
    refreshPosts += 1;
    payload.analyses = [record('fresh')];
    await route.fulfill({ response });
  });

  const dialog = await openEditor(page, taskId);
  const panel = panelOf(dialog);
  const toggle = panel.getByRole('button', { name: 'AI 分析' });

  // 收起着就说得出"这份过期了" —— 组件一挂载就读了一次,不是等展开才读。
  await expect(badgeOf(dialog)).toHaveText('内容已变，这份过期了');

  // 关掉再回来:徽标还在。它每次都是**读回来的**,没有"这一次会话里记着的状态"。
  await toggle.click();
  await expect(panel).toContainText('「要被分析的节点」的正文改过了');
  await closeEditor(page);
  const reopened = await openEditor(page, taskId);
  await expect(badgeOf(reopened)).toHaveText('内容已变，这份过期了');
  await expect(panelOf(reopened).getByRole('button', { name: 'AI 分析' })).toHaveAttribute(
    'aria-expanded',
    'false',
  );

  const reopenPanel = panelOf(reopened);
  await reopenPanel.getByRole('button', { name: 'AI 分析' }).click();
  // 一栏一栏地显示出来 —— 它们是分开的事实,不是一段话。
  await expect(reopenPanel).toContainText('还缺什么');
  await expect(reopenPanel).toContainText('一组要跑多久');
  await expect(reopenPanel).toContainText('假设');
  await expect(reopenPanel).toContainText('模型对这份判断的把握：正文很短,把握不大');
  // 这一块存在的那条边界,必须写在用户看得见的地方。
  await expect(reopenPanel).toContainText('不在你的正文里');

  // 点之前读过几次:挂载一次 + 上面展开之前那次重开没有重挂载,所以基准是此刻的值。
  const readsBefore = reads;
  await reopenPanel.getByRole('button', { name: '根据最新内容重新分析' }).click();
  await expect
    .poll(() => refreshPosts, { message: '点了重新分析,请求没有发出去' })
    .toBe(1);
  // 服务端改口之后才改口。**这一步是由上面那个 POST 真的发出去了解释的。**
  await expect(badgeOf(reopened)).toHaveText('基于当前内容');
  // 而且它是**读回来的**,不是本地推的 —— 只断言最后那行字的话,一个"点完就把徽标
  // 改成最新"的实现同样能绿,而它在降级那一轮会开始说没有依据的话。
  expect(reads, '徽标变了,但这一步没有再读一次服务端 —— 它是本地推出来的').toBeGreaterThan(
    readsBefore,
  );
});

// ---------------------------------------------------------------------------------
// 3. 降级来的那一条不许长得像模型说的
// ---------------------------------------------------------------------------------
test('降级产生的分析写明「不是模型的判断」', async ({ page }) => {
  const { workspaceId, taskId } = await scene(page, 'analysis-degraded-label');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);

  // 这一条**说得出出处**,而出口必须一眼看得出跟模型无关 —— 一份不是模型给的判断
  // 长得和真的一模一样,是这一层最容易骗到人的地方。
  await page.route(`**/api/workspaces/${workspaceId}/analyses*`, async (route) => {
    await route.fulfill({
      status: 200,
      json: {
        analyses: [{
          id: '00000000-0000-4000-8000-000000000002',
          workspaceId,
          scopeRootId: null,
          focusNodeId: taskId,
          scopeRootTitle: null,
          focusNodeTitle: '要被分析的节点',
          promptVersion: 'v1',
          modelSource: 'rule_fallback',
          createdAt: '2026-09-20T10:00:00Z',
          freshness: 'fresh',
          staleReasons: [],
          coverageNote: null,
          known: ['先跑一组对照实验'],
          unknowns: [],
          evidence: [],
          assumptions: [],
          diagnosis: [],
          strategyOptions: [],
          risks: [],
          confidenceNote: null,
        }],
        focusNodeId: taskId,
        scopeRootId: null,
        note: '',
      },
    });
  });

  const dialog = await openEditor(page, taskId);
  const panel = panelOf(dialog);
  await panel.getByRole('button', { name: 'AI 分析' }).click();
  await expect(panel.locator('.analysis-meta')).toContainText('这一条不是模型的判断');
  await expect(badgeOf(dialog)).toHaveText('基于当前内容');
});

// ---------------------------------------------------------------------------------
// 4. "它回答时你说的情况已经变了" —— 说出来,而且**不给按钮**
// ---------------------------------------------------------------------------------
test('输入在模型回答期间变过时说清下一步在哪，而不是再摆一个按钮', async ({ page }) => {
  test.slow();
  const { workspaceId } = await scene(page, 'analysis-input-changed');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);

  /*
   * 只改**那一个布尔**:其余字段照后端真实返回的原样送进界面。
   *
   * 这一手是必要的 —— `inputChanged` 为真的前提是"模型思考期间用户改了东西",而在
   * 一个没有模型、瞬间返回的验收栈里这个窗口根本不存在。真造它就得让某一轮慢下来,
   * 那验的成了计时而不是这段话。
   *
   * 这个布尔**真的会被置真**这件事,由 `backend/tests/test_analysis_staleness.py`
   * 钉着(那里造得出输入变过的窗口)。这里管的是它到了界面之后长什么样。
   */
  await page.route('**/api/workspaces/*/messages', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({
      status: response.status(),
      contentType: 'application/json',
      body: JSON.stringify({ ...body, inputChanged: true }),
    });
  });

  await page.getByLabel('给 AI 的消息').fill('我每周能投入 10 小时。');
  await page.getByLabel('发送消息').click();

  const hint = page.locator('.turn-error[role="status"]');
  await expect(hint).toBeVisible();
  await expect(hint).toContainText('你说的情况已经变了');
  // 指向下一步在**哪**:只说"没有可应用的变更"的话,用户会以为模型什么都没想出来,
  // 而真正该做的是去那个节点让 AI 重看一遍。
  await expect(hint).toContainText('AI 分析');

  // **不给按钮。** 入口在那些内容的旁边(节点详情的分析块里);在对话末尾再放一个,
  // 用户看不出它要重新分析的是哪个节点 —— 而"界面只加必要的分析入口,不扩充一排
  // 新按钮"是这一批的硬边界。
  await expect(hint.getByRole('button')).toHaveCount(0);
});

// ---------------------------------------------------------------------------------
// 5. 正文保存成功之后,这一块会重新去问一次服务端
// ---------------------------------------------------------------------------------
/**
 * 验收路径是「改正文 → 徽标变过期」,而**过期是服务端读的时候现算的** ——
 * 界面上没有"把徽标设成过期"这回事,它唯一的贡献是:正文一存成功,就重新去问一次。
 *
 * 所以这一条验的就是那一下**问**。只断言"改完正文徽标还是原来那个"是不够的 ——
 * 那种实现同样能绿,而它在真有一条分析记录的时候会**一直显示旧的新鲜度**:
 * 用户改完了正文,"基于当前内容"那几个字还挂在那里,而那正是这块面板最容易骗人的样子。
 *
 * (另一半 —— 库里那条记录真的会因此变成 `stale` —— 由
 * `backend/tests/test_analysis_staleness.py` 与 `test_analysis_refresh.py` 钉着。
 * 隔离栈里没有模型 key、没有分析记录,这一半在浏览器里造不出来。)
 */
test('正文保存成功之后这一块会重新问一次服务端', async ({ page }) => {
  test.slow();
  const { workspaceId, taskId } = await scene(page, 'analysis-body-reread');
  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);

  let reads = 0;
  await page.route(`**/api/workspaces/${workspaceId}/analyses*`, async (route) => {
    reads += 1;
    await route.continue();
  });

  const dialog = await openEditor(page, taskId);
  await panelOf(dialog).getByRole('button', { name: 'AI 分析' }).click();
  await expect(panelOf(dialog).locator('.analysis-badge')).toHaveText('还没有分析过');
  const readsBefore = reads;

  await dialog.getByLabel('详细说明').fill('只能周末做:周中实验室排不开。');
  // 等"已保存"真的出现 —— 只在服务端确认之后它才写上去(见 `PathView.tsx::flushBody`)。
  await expect(page.locator('.body-save-note')).toHaveClass(/is-saved/, { timeout: 15000 });

  await expect
    .poll(() => reads, { message: '正文存进去了,这一块却没有再问一次服务端 —— 徽标会一直显示旧的新鲜度' })
    .toBeGreaterThan(readsBefore);
});
