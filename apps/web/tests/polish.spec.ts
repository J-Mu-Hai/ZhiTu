import { expect, type Page, test } from '@playwright/test';
import { artifactPath } from './support/artifacts';
import {
  assertBackendRunning,
  clickUntilVisible,
  createNode,
  createWorkspace,
  dayOffset,
  getPlan,
  openSpacePage,
  registerAccount,
  scheduleEverything,
  waitForRealPlan,
} from './support/session';

/**
 * 视觉精修的**人工验收现场**。
 *
 * ## 这个文件为什么存在
 *
 * 这一轮的验收标准是"静态截图即使完全不动,也必须足够精致" —— 而**"精致"不是我能
 * 自我认证的东西**。所以这里不验行为、不写业务断言(那些在别的 spec 里,一条没少),
 * 它只做一件事:**把画面冻住,拍下来,交给用户判断**。
 *
 * ## 为什么必须先冻住
 *
 * 整个文件跑在 `prefers-reduced-motion: reduce` 下(见文件末尾的 `test.use`)。
 * 那一档里 `motion.css` 把每一段时长压到 `1ms`,于是拍到的是一帧**完全静止**的画面。
 * 不这么做的话,拍到的是哪一帧取决于机器快慢 —— 同一个改动跑两遍会拍到两个样子,
 * 而"这两张哪张更好"就答不上来了。
 *
 * ## 为什么整页和特写都要拍
 *
 * 整页图看得出"整不整齐",看不出"这个节点到底哪儿不对"。用户说"这里不好看"的时候,
 * 需要的是"哪一张、哪一块、偏多少"这种能直接改的反馈,所以同一块拍两次:
 * 一次在整页里(看它和邻居的关系),一次单独拍它自己(看它本身的边距、字号、圆角)。
 *
 * 图落在本轮现场目录里(`artifactPath`),文件名固定 —— 人一眼认得出是哪一张,
 * 而**上一轮长什么样不会被这一轮无声盖掉**(理由见 `support/artifacts.ts` 的文件头)。
 */

/**
 * **全部冻住再拍。** 见文件头那段:动画不限速的话,拍到哪一帧取决于机器快慢,
 * 同一个改动跑两遍会拍到两个样子,而"哪张更好"就没有答案了。
 * 这一档里 `motion.css` 把每段时长压到 `1ms`,所以下面这些图是纯静态画质。
 */
test.use({ reducedMotion: 'reduce' });

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

/**
 * 等画面**落定**。
 *
 * 初始视角是「计划到达 + 布局问过之后 180 毫秒」才 fit 的(见 `PathView.tsx` 里那段
 * effect),所以刚 `waitForRealPlan` 完就拍,拍到的是一张还没摆好的画布。
 *
 * 这里等的是**渲染落定,不是业务状态** —— 它不制造任何一种状态,只是给已经发生的事
 * 一点时间。同一条理由和同一个数字见 `layout.spec.ts` 里"等过那次初始 fit"那一段。
 */
async function settle(page: Page): Promise<void> {
  await page.waitForTimeout(1000);
}

/**
 * 一份**像真的**计划:一个目标、三个阶段、两件有截止时间的任务。
 *
 * 为什么不用"建一个节点就拍":一个孤零零的根节点拍不出"层级"—— 而这一轮要看的
 * 恰恰是层级(根比阶段重、阶段比说明重)。数据太瘦的话,截图上看不出层级是
 * "没做"还是"没东西可做"。
 *
 * **注意画布上只有四个节点(根 + 三个阶段),那两件任务不在画布上 —— 这不是漏了。**
 * 路径视图画的**只有根和直接子节点**(`PathView.tsx:782`,后代只有根空间里的
 * `capability` 节点才展开),所以任务这一层在路径画布上本来就不可见。它们出现在
 * 时间线、今天、随笔那几张里 —— 那几张才是叶层的画面。
 */
async function scene(page: Page, prefix: string) {
  const { token } = await registerAccount(page, prefix);
  const workspaceId = await createWorkspace(page, token, '保研准备', '三个月内定下方向,并把导师联系上');
  const root = (await getPlan(page, token, workspaceId)).nodes[0];

  const stages = [
    { title: '确定研究方向', description: '把感兴趣的方向缩到两三个,每个都找一位在读的学长聊过,再决定先去哪一个。' },
    { title: '联系导师', description: '整理一份能说明自己做过什么的材料,先发邮件,再约时间当面聊。' },
    { title: '补齐课程与技能', description: '按目标方向补两门课,并通过一个小项目把整条工具链跑通。' },
  ];
  const stageIds: string[] = [];
  for (const stage of stages) {
    stageIds.push(await createNode(page, token, workspaceId, {
      parentId: root.id, nodeType: 'stage', title: stage.title, description: stage.description,
    }));
  }

  const leafIds: string[] = [];
  for (const leaf of [
    { title: '列出十位候选导师', deadline: dayOffset(14), estimateMinutes: 90, description: '按方向、课题组、招生名额三个维度各记一行,不要只抄主页。' },
    { title: '读完两篇代表论文', deadline: dayOffset(35), estimateMinutes: 180, description: '每篇写两百字小结,把看不懂的地方单独标出来。' },
  ]) {
    leafIds.push(await createNode(page, token, workspaceId, { parentId: stageIds[0], nodeType: 'task', ...leaf }));
  }

  return { token, workspaceId, rootId: root.id, stageId: stageIds[0], leafId: leafIds[0] };
}

/** 在画布上按标题找那个节点(标题是唯一的,`scene()` 里取的都不是通用词)。 */
function nodeByTitle(page: Page, title: string) {
  return page.locator('.react-flow__node').filter({ has: page.locator('.node-title', { hasText: title }) });
}

/**
 * 把浮动组件**同框**拍下来。
 *
 * ## 为什么不能直接 `locator('.space-topbar').screenshot()`
 *
 * 因为 `.space-topbar` 是 `height:0` 的**定位容器** —— 子元素各自 `position:absolute`,
 * 容器自己不占面积。Playwright 拍元素前要等它有一个非空矩形,等不到就一直等,
 * 最后报 `locator.screenshot: Test ended.`。**那个报错读起来像页面崩了,其实不是**:
 * 元素在、可见、内容也在,只是它本来就没有面积可拍。
 *
 * ## 这里怎么拍
 *
 * 按每块**自己的矩形**算一个并集(再各边放 12px 呼吸),然后按这个矩形裁整页。
 * 之所以要并集而不是各拍各的:这一轮改的是"它们是不是一家人" —— 同一档圆角、
 * 同一条描边、同一个底、同一层阴影、**同一条水平线**。分开拍就看不出"同高"。
 *
 * ## 为什么"有几块"是**量**出来的,不是写死的
 *
 * 这四个选择器**不在同一时刻全部存在**,而这跟页面坏没坏无关:
 *
 * - `.reopen-chat` 是 `{!chatOpen && …}` 渲出来的 —— 对话面板开着的时候**它本就
 *   不存在**(`Workbench.tsx:26`),而不是"没渲染出来";
 * - `.space-floating-tools` 只属于路径视图 —— 切到时间线/任务/排期就没有。
 *
 * 所以这里逐个量、量到几张算几张,只要求"至少量到一块"。写成"四个都得在"的话,
 * 这条用例会在**完全正常**的页面上红,而红的原因和视觉品质毫无关系。
 *
 * 先问 `count()` 再量,是因为 **`boundingBox()` 会等**:元素不在的时候它不是返回
 * `null`,而是一路等到超时才报错 —— 那就又变回上面那种"红得莫名其妙"了。
 * `count()` 是**立刻**回答的,它只答"在不在",不负责"等它出现"。
 */
async function shootFloatCluster(page: Page, name: string): Promise<void> {
  const parts = ['.space-back', '.view-tabs', '.reopen-chat', '.space-floating-tools'];
  const boxes: { x: number; y: number; width: number; height: number }[] = [];
  for (const part of parts) {
    const locator = page.locator(part).first();
    if ((await locator.count()) === 0) continue;
    const box = await locator.boundingBox();
    if (box && box.width > 0 && box.height > 0) boxes.push(box);
  }
  expect(boxes.length, '一块浮动组件都没量到,别拿一张空图当验收').toBeGreaterThan(0);

  const pad = 12;
  const left = Math.max(0, Math.min(...boxes.map((b) => b.x)) - pad);
  const top = Math.max(0, Math.min(...boxes.map((b) => b.y)) - pad);
  const right = Math.max(...boxes.map((b) => b.x + b.width)) + pad;
  const bottom = Math.max(...boxes.map((b) => b.y + b.height)) + pad;

  await page.screenshot({
    path: artifactPath(name),
    clip: { x: left, y: top, width: right - left, height: bottom - top },
  });
}

test('工作台与 AI 面板的静态画质（1440）', async ({ page }) => {
  const { token, workspaceId, leafId } = await scene(page, 'polish-desktop');

  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await settle(page);

  // 整张工作台。这张是主图:节点比例、画布中心、三块浮动组件、AI 面板的宽度,
  // 全都在这张里看它们**互相**的关系。
  await page.screenshot({ path: artifactPath('polish-workbench-1440.png') });

  // 节点特写。整页图里一个节点只有 300 来像素宽,字号与内边距的差别看不出来。
  await nodeByTitle(page, '联系导师').screenshot({ path: artifactPath('polish-node-closeup.png') });

  // 三块浮动组件。它们这一轮统一了圆角、描边、阴影、底色 —— 单看一块看不出
  // "是不是一家人",所以要**同框**:这张图里三块必须同高、同圆角、同一条边。
  await shootFloatCluster(page, 'polish-float-cluster.png');

  // AI 面板的空状态。新空间一条消息都没有(这一点由 `space-bootstrap.spec.ts` 钉着),
  // 所以这里拍到的是"面板里什么都没有"时的样子 —— 以前它是一大片空白加一句小字。
  await expect(page.locator('.conversation-empty')).toBeVisible();
  await page.locator('.floating-conversation').screenshot({ path: artifactPath('polish-ai-empty.png') });

  // 有消息的样子。发两条就够看出层级了:两条**我的**、和它回的那些。
  //
  // 这里按**角色**数,不按总条数:一次发送产生的不止一条(我的一条 + 它的一条回复),
  // 所以总数取决于它回几条 —— 那是产品行为,由 `conversation` 那几个 spec 去钉。
  // 这条用例只关心"两种角色都在画面上",否则拍出来的卡片层级是缺一半的。
  await page.getByLabel('给 AI 的消息').fill('我想在三个月内完成一个 Python 项目');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.floating-conversation .message.user')).toHaveCount(1);
  await page.getByLabel('给 AI 的消息').fill('每周大概能投入四个小时');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.floating-conversation .message.user')).toHaveCount(2);
  await expect(page.locator('.floating-conversation .message:not(.user)').first()).toBeVisible();
  await page.locator('.floating-conversation').screenshot({ path: artifactPath('polish-ai-messages.png') });

  // 时间线。它是这一页里唯一从上往下排的视图,也是"工具栏会不会被浮动组件盖住"
  // 那条几何用例盯着的画面 —— 图在这里,断言在 `timeline.spec.ts`。
  await page.getByRole('tab', { name: '时间线', exact: true }).click();
  await expect(page.getByTestId('timeline-view')).toBeVisible();
  await settle(page);
  await page.screenshot({ path: artifactPath('polish-timeline-1440.png') });

  // 「今天」。先真的排一次期,否则这一页是空的 —— 空的首页看不出卡片层级。
  await scheduleEverything(page, token, workspaceId);
  await page.goto('/today');
  await expect(page.locator('.today-columns')).toBeVisible();
  await settle(page);
  await page.screenshot({ path: artifactPath('polish-today-1440.png') });

  // 「随笔」。写一条出来,否则拍到的只是"没有记录"那句空话 —— 左右两栏那对
  // 失衡(左边一行字、右边一块大白卡)恰恰是这一轮要看的。
  await openSpacePage(page, '/journal', workspaceId);
  await page.getByRole('button', { name: '写下一点此刻的想法' }).click();
  // 先点「标签」再写正文,理由与写法都照 `experience.spec.ts:118-138` 那一段:
  // 这一页是 `page.goto` 进来的,服务端画出的输入框**先于** React 接管,
  // 那时候 `fill` 进去的字会在水合时被重画掉(表现成"写不进去")。点中「标签」
  // 这一下既是"页面已经醒过来"的证据,也是「关联计划」露出来的前提 ——
  // 它在这个表单里是**折叠**的,不点「标签」它根本不在 DOM 里。
  await clickUntilVisible(
    page,
    page.getByRole('button', { name: '标签', exact: true }),
    page.getByLabel('关联计划', { exact: true }),
  );
  await page.getByLabel('此刻的想法').fill('第一次和学长聊完,方向大致清楚了。\n下周先把候选导师那一行补满。');
  await page.getByLabel('标签', { exact: true }).fill('科研');
  // 按 **id** 选,不按显示文字 —— 下拉里的文字是节点的标题,而用它来选就等于
  // 在截图用例里断言"标题会原样出现在下拉里"。那是产品行为,该由
  // `experience.spec.ts` 去验,这里只想要那条关联存在。
  await page.getByLabel('关联计划', { exact: true }).selectOption(leafId);
  await page.getByRole('button', { name: '发布', exact: true }).click();
  await expect(page.getByRole('article', { name: '随笔全文' })).toBeVisible();
  await settle(page);
  await page.screenshot({ path: artifactPath('polish-journal-1440.png') });
});

test('手机工作台的静态画质（390）', async ({ page }) => {
  const { workspaceId } = await scene(page, 'polish-mobile');
  await page.setViewportSize({ width: 390, height: 844 });

  await openSpacePage(page, '/workbench', workspaceId);
  await waitForRealPlan(page);
  await settle(page);

  // 这一张是手机这一档的主图:画布还剩多少、浮动组件挤掉多少、AI 面板占了多少屏。
  await page.screenshot({ path: artifactPath('polish-workbench-390.png') });

  // 只拍浮动的部分:390 下它们是最挤的一块,而整页图里它们只有顶部一条。
  await shootFloatCluster(page, 'polish-float-cluster-390.png');
});
