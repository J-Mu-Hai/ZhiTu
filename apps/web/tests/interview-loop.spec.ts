/**
 * 访谈共建的闭环:用户说 → 模型提议 → 用户确认 → 画布上真的长出了东西。
 *
 * ## 这一条为什么需要脚本,而它**不是**产品能力
 *
 * 隔离验收栈里没有模型 key,兜底的那条规则**永远返回"没有动作"** —— 所以在这条
 * 模式出现之前,这一段链路上唯一能验的只有"请求返回了 200"。那是假绿:提案、预览、
 * 确认、写入这四步一步都没走过,而它们正是这一批新写的东西。
 *
 * 所以 `scripts/dev/accept-e2e.mjs --script=…` 会把后端切到 `AGENT_REASONER=script`
 * (见 `backend/agent/runtime/scripted.py`)。**它是测试脚手架**:念的是一份写死的
 * JSON,没有模型参与。DB 里那一行会明明白白记着 `model_source = 'scripted'`,
 * 界面上那个来源徽标也会写「脚本回放」—— 三处都不许把它说成模型的判断。
 *
 * ## 这一条钉的是**闭环**,不是模型的判断
 *
 * 脚本是输入,断言的是产品的输出:提案里的条目说人话、确认之后节点真的在库里、
 * 它是**信息用途**、它带着那条事实、它没有被算进"要去做的事",而笔记落在**另一个
 * 地方**。模型该不该这么判断,是 `scripts/accept_analysis.py` 在真实模型上守的事。
 *
 * ## 没有脚本的时候它必须是 `skipped`,不能是"跑了但什么都没验"
 *
 * 所以整组压在 `ZHITU_SCRIPTED_ACTIONS` 上,而那个环境变量由 `accept-e2e.mjs` 只在
 * `--script` 那一轮传给 Playwright。少了它这里**一条都不跑** —— 一句"测试没跑"
 * 看得见,而一条"跑过了、绿了、其实什么都没验"看不见。
 *
 * ## 那份脚本里写死的 `n1` / `n2` 是什么
 *
 * 记号(`handle`)是服务端按 `(depth, order_index, created_at)` 顺序现分的,所以
 * 一个**只有根节点**的空间里 `n1` 就是根。第一个动作把信息主题挂在 `n1` 下,于是
 * 下一轮里它成了 `n2`(根之后的第一个)。这也是这一组必须**自己建一个干净空间**、
 * 不在里面先放别的节点的原因 —— 多一个节点,`n2` 就不是它了。
 */

import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  api,
  assertBackendRunning,
  createWorkspace,
  getPlan,
  registerAccount,
  waitForRealPlan,
} from './support/session';

const SCRIPTED = process.env.ZHITU_SCRIPTED_ACTIONS ?? '';

test.beforeEach(async ({ request }) => {
  await assertBackendRunning(request);
  test.skip(!SCRIPTED, '这一条要 --script=apps/web/tests/fixtures/interview-script.json 才跑得起来');
});

/**
 * 发一句话给 AI,等这一轮**真的**结束。
 *
 * 判据是"回复的条数多了一条",不是"发送按钮又亮了" —— 那个按钮的 `disabled` 是
 * `!输入框有字 || 正在发送`,发完之后输入框是空的,所以它**永远不会**再亮起来,
 * 拿它当等待条件会一直等到超时。
 */
async function say(page: Page, text: string): Promise<void> {
  const replies = page.locator('.message.assistant');
  const before = await replies.count();
  await page.getByLabel('给 AI 的消息').fill(text);
  await page.getByLabel('发送消息').click();
  await expect(replies, '这一轮没有回复 —— 后端可能没在 script 模式里').toHaveCount(before + 1, { timeout: 20000 });
}

function card(page: Page, nodeId: string): Locator {
  return page.locator(`.react-flow__node[data-id="${nodeId}"]`);
}

test('访谈里的那句回答会变成一条信息主题，确认之后真的落库', async ({ page }) => {
  test.slow();
  const { token } = await registerAccount(page, 'interview');
  const workspaceId = await createWorkspace(page, token, '访谈验收空间', '三个月内把出国申请准备好');
  await page.goto(`/workbench?workspace=${workspaceId}`);
  // 阶段 12:进入空间会自动生成时间架构并切到时间线;这条用例验的是路径画布与
  // 业务提案,所以等自动切换完成后切回路径页。
  await expect(page).toHaveURL(/view=timeline/, { timeout: 25000 });
  await page.getByRole('tab', { name: '路径', exact: true }).click();
  await expect(page).toHaveURL(/view=path/);
  await waitForRealPlan(page);

  // ---------------------------------------------------------------- 第一轮:只有追问
  await say(page, '我想申请出国读研，但不知道从哪开始。');
  // **这一轮不该生出任何提案。** 单纯追问就提一个变更,用户会开始怀疑每一句寒暄
  // 都在动他的计划。脚本这一轮 `actions` 是空的,所以这句断言同时钉住了那条边界:
  // 没有动作的一轮**不产生提案框**。
  await expect(page.locator('.proposal')).toHaveCount(0);
  const afterFirst = await getPlan(page, token, workspaceId);
  expect(afterFirst.nodes, '还没确认,画布上就多出东西了').toHaveLength(1);

  // ---------------------------------------------------------------- 第二轮:提案
  await say(page, '我排名 38，每周能投入 10 小时。');
  const proposal = page.locator('.proposal').last();
  await expect(proposal).toBeVisible({ timeout: 20000 });
  // 预览要说人话,而且要说清"这是一条**信息主题**" —— 只说「新建阶段」的话,用户
  // 会以为它要做的事多了一件。
  await expect(proposal).toContainText('学业情况');
  await expect(proposal).toContainText('信息主题');

  // **还没确认,库里就不该有。** 预览是预览:这一段在"确认"之前是一个纯读的操作,
  // 而它会顺手把节点写进去的话,用户点「先不要」就成了一句空话。
  const beforeConfirm = await getPlan(page, token, workspaceId);
  expect(beforeConfirm.nodes, '只生成了提案,节点就已经在库里了').toHaveLength(1);

  await proposal.getByRole('button', { name: '确认，写入计划' }).click();

  // **等写库真的落地,而不是等按钮。**
  // 那个按钮按下之后文案会变成「处理中…」(见 `ConversationPanel` 里那个三元),
  // 所以"按名字找不到了"在**点下去的那一瞬间**就成立 —— 拿它当等待条件,下面那次
  // 读计划会和写入抢跑。实测差 15 毫秒:测试在 29.203 读到旧计划,而那一行节点是
  // 29.218 才写进去的(2026-09-27,trace 里两个时间点都在)。
  // 换成"计划里真的出现了它",测得的就是这件事本身:确认之后节点在库里。
  await expect
    .poll(async () => (await getPlan(page, token, workspaceId)).nodes.some(n => n.title === '学业情况'),
      { message: '确认之后画布上还是没有那条信息主题', timeout: 15000 })
    .toBe(true);

  const plan = await getPlan(page, token, workspaceId);
  const created = plan.nodes.find(node => node.title === '学业情况');
  expect(created, '确认之后画布上还是没有那条信息主题').toBeTruthy();
  expect(created!.purpose).toBe('information');
  expect(created!.description).toContain('38');
  // 信息用途**不进"要去做的事"**:根目标 + 它,而计数只数根目标(见 `plan_service`)。
  expect(plan.totalNodes, '信息主题被算进了"计划里有多少个节点"').toBe(1);

  await waitForRealPlan(page);
  await expect(card(page, created!.id)).toBeVisible();

  // AI 规划出来的节点**仍然**自动带一条父子结构线 —— 这与用户手工放下的节点相反
  // (`PathView.tsx` 里只有 `origin !== 'user'` 才 `connect`)。这条线画的是曲线:
  // 几乎同层时 `BranchEdge` 直接给一条三次贝塞尔(`C`),否则用圆角阶梯路径(`Q` 弧)
  // —— 两者都不是直线的 `L`,所以断言 "d 里有 C 或 Q"。
  await expect(page.locator('.react-flow__edge-branch')).toHaveCount(1);
  expect(
    await page.locator('.react-flow__edge-branch .react-flow__edge-path').first().getAttribute('d'),
  ).toMatch(/ [CQ] /);

  // ---------------------------------------------------------------- 第三轮:长正文
  await say(page, '把刚才那些细节记下来。');
  const noteProposal = page.locator('.proposal').last();
  await expect(noteProposal).toBeVisible({ timeout: 20000 });
  await noteProposal.getByRole('button', { name: '确认，写入计划' }).click();

  // 笔记**不在 `/plan` 里**(两万字的正文会让每一次读计划都背着它),所以要单独读。
  // 用 `expect.poll`:确认那一下的写入是异步的,直接读会读到"还没写上去",而那句
  // 失败会把原因指向"笔记没写",而不是"问得太早"。
  await expect
    .poll(async () => (await api<{ body: string }>(
      page, token, `/api/workspaces/${workspaceId}/nodes/${created!.id}/notes`,
    )).body, { message: '确认之后长笔记没有落到 node_notes 里', timeout: 10000 })
    .toContain('排名 38');

  // 它也不该顺手改掉说明 —— 两条路各写各的(见 `provider.tsx::saveNodeNote`)。
  const settled = await getPlan(page, token, workspaceId);
  expect(settled.nodes.find(node => node.id === created!.id)!.description).toBe(created!.description);

  // 打开节点详情,长正文那一栏要显示库里那一份(而不是一个空的编辑器)。
  // **`toHaveValue` 而不是 `toContainText`**:textarea 的值走的是 value 属性,
  // 它的 `textContent` 是空的(React 不往子节点里写)—— 断言 textContent 会红在
  // 一个和功能无关的地方。
  await card(page, created!.id).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('长正文（笔记）')).toHaveValue(/排名 38/, { timeout: 15000 });
});
