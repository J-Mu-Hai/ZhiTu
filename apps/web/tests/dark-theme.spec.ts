import { expect, test, type Locator, type Page } from '@playwright/test';
import { clickUntilVisible, enterSpace } from './support/session';

/**
 * 视觉系统的一致性回归。
 *
 * ## 文件名是历史遗留
 *
 * 这个文件曾经断言"工作台使用统一的**深色**视觉系统",而实际的主题早就变成了暖白
 * (`dark-theme.css` 的最后一层把变量覆盖成 `--bg:#f8f6ef`,残留的深色规则全被它压住)。
 * 于是整套断言在描述一个不存在的设计 —— 它失败得完全正确,只是没人去改它。
 *
 * 现在断言的是**暖白**,职责没变:不管主题是哪一套,路径 / 时间线 / 其他核心页面
 * 必须用同一套。
 *
 * ## 判据是"亮",不是"等于某个色号"
 *
 * 换一个暖白色号不该让这些测试红,换成深色必须让它红。所以断言是
 * `Math.min(r,g,b) >= 200`(浅)和 `Math.max(r,g,b) <= 120`(深),不锁具体值。
 *
 * ## 两处实测出来的陷阱
 *
 * - **`.growth-node.goal` 的 `background-color` 是透明的。** 它的颜色在
 *   `background-image` 的渐变里(`linear-gradient(145deg,#e8f3fc,#d7ebf8)`),所以
 *   任何"取 backgroundColor 比亮度"的写法在这个元素上都会得到 `rgba(0,0,0,0)` ——
 *   老的 `expectDarkSurface` 因此在这里**假通过**(最大通道 0 < 70)。现在改成解析
 *   渐变的色标。
 * - **`.floating-conversation .message` 在空空间里不存在。** 老断言它在,因为那时
 *   看的是带预置对话的示例空间。真实的新空间里一条消息都没有。
 *
 * ## 测试数据从接口建,不从界面点
 *
 * 一个全新的账户 `GET /api/workspaces` 返回 `[]`,画布上什么都没有。这个文件要验的是
 * 颜色,不是"怎么建空间",所以空间和节点都用接口建好再进页面 —— 建失败时失败原因
 * 指向接口,而不是指向某个按钮。
 *
 * ## 最后一条测试是另一类断言
 *
 * `核心页面没有任何一处深色涂装` 不是在测某个组件,而是在**扫整页**:主题被残留的
 * 深色规则压回去时,表现往往只是"某个角落有个近黑的东西"(实测抓到的是 `/me` 上的
 * `.profile-action`,近黑 `#111a26` 配浅蓝字,出自 `auth.css`,连
 * `dark-theme.css` 的暖白层都没覆盖它)。逐个元素写断言抓不到这种漏网,扫一遍才能。
 */

const API_BASE = (process.env.API_BASE ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');
const TOKEN_KEY = 'zhitu.auth.token.v1';

/** 注册一个账户并把令牌放进 localStorage —— 和真人登录后的浏览器状态一致。 */
async function signIn(page: Page): Promise<string> {
  const email = `theme-${Date.now()}-${Math.floor(Math.random() * 1e6)}@zhitu.test`;
  const response = await page.request.post(`${API_BASE}/api/auth/register`, {
    data: { email, password: 'playwright-password', displayName: '主题验收', timezone: 'Asia/Shanghai' },
  });
  if (!response.ok()) throw new Error(`注册失败:${response.status()} ${await response.text()}`);
  const { token } = (await response.json()) as { token: string };
  await page.goto('/login');
  await page.evaluate(([key, value]) => localStorage.setItem(key, value), [TOKEN_KEY, token] as const);
  return token;
}

interface SeedNode {
  title: string;
  nodeType: string;
  deadline?: string;
  /** 挂在哪个节点下面,按**标题**找。不写就挂在根目标下面。 */
  parentTitle?: string;
}

/**
 * 建一个空间,返回它的 id、根节点 id,以及**每个节点的 id**(按标题)。
 *
 * 建完之后**再进一次工作台** —— 这一步不是多余的:`/today`、`/journal`、
 * `/conversations` 在没有选中空间时会被重定向回 `/spaces`,而"选中了哪个空间"是
 * 打开工作台时记在 localStorage 里的(见 provider 里的 `activeKey`)。
 */
async function seedSpace(
  page: Page,
  token: string,
  input: { title: string; nodes?: SeedNode[] },
): Promise<{ workspaceId: string; rootId: string; nodeIds: Record<string, string> }> {
  const headers = { Authorization: `Bearer ${token}` };
  const created = await page.request.post(`${API_BASE}/api/workspaces`, {
    headers,
    data: { title: input.title, intent: '' },
  });
  const { workspace } = (await created.json()) as { workspace: { id: string } };
  const plan = await page.request.get(`${API_BASE}/api/workspaces/${workspace.id}/plan`, { headers });
  const { nodes } = (await plan.json()) as { nodes: { id: string }[] };
  const rootId = nodes[0].id;
  const nodeIds: Record<string, string> = {};
  for (const { parentTitle, ...node } of input.nodes ?? []) {
    const parentId = parentTitle ? nodeIds[parentTitle] : rootId;
    if (!parentId) throw new Error(`找不到「${parentTitle}」—— 它得排在引用它的节点前面`);
    const response = await page.request.post(`${API_BASE}/api/workspaces/${workspace.id}/nodes`, {
      headers,
      data: { parentId, ...node },
    });
    if (!response.ok()) throw new Error(`建节点「${node.title}」失败:${response.status()} ${await response.text()}`);
    nodeIds[node.title] = ((await response.json()) as { node: { id: string } }).node.id;
  }
  await page.goto(`/workbench?workspace=${workspace.id}`);
  await expect(page.locator(`.react-flow__node[data-id="${rootId}"]`)).toBeVisible();
  return { workspaceId: workspace.id, rootId, nodeIds };
}

type Rgb = [number, number, number];

/** 解析一段颜色里的**所有**色标。渐变会返回多个,纯色返回一个。 */
function channels(value: string): Rgb[] {
  const found = [...value.matchAll(/rgba?\(([^)]+)\)/g)].map((match) => {
    const parts = match[1].split(',').map((piece) => Number.parseFloat(piece));
    // alpha 为 0 的色标是渐变里的透明端点,它不代表任何表面。
    return parts.length >= 4 && parts[3] === 0 ? null : ([parts[0], parts[1], parts[2]] as Rgb);
  });
  return found.filter((item): item is Rgb => item !== null);
}

/**
 * CSS 变量的值是 `#f8f6ef` 这种十六进制,而 `getComputedStyle` 给的是 `rgb(...)`。
 * 两者都要能进 `channels`,否则 `rgb(#f8f6ef)` 会解析失败 —— 那正是这个文件
 * 上一版的 bug:变量断言永远拿不到色标。
 */
function toRgb(value: string): string {
  const hex = value.trim().match(/^#([0-9a-f]{6})$/i);
  if (!hex) return value;
  const [r, g, b] = [0, 2, 4].map((offset) => Number.parseInt(hex[1].slice(offset, offset + 2), 16));
  return `rgb(${r}, ${g}, ${b})`;
}

/** 一个"浅色表面"的判据 —— 只看它是不是亮的,不锁具体色值。 */
function expectLightSurface(value: string, label: string): void {
  const marks = channels(toRgb(value));
  expect(marks.length, `${label} 解析不出任何颜色:${value}`).toBeGreaterThan(0);
  for (const mark of marks) {
    expect(Math.min(...mark) >= 200, `${label} 不是浅色表面:${value}`).toBe(true);
  }
}

/**
 * 元素往上找到第一个真正画了背景的祖先 —— 那就是它显示出来的"表面"。
 *
 * 找不到时要**分清是哪一种找不到**:页面真的没人画背景,还是这个元素**已经不在文档里**
 * (React 重新挂载把它换掉了)。后者量出来的是一串空字符串,一路走到头也找不到 —— 报出来
 * 的样子和前者一模一样,而两件事要修的地方完全不同。所以把 `isConnected` 一起报出来。
 */
async function paintedSurface(locator: Locator, label: string): Promise<string> {
  const result = await locator.evaluate((element) => {
    let node: HTMLElement | null = element as HTMLElement;
    const seen: string[] = [];
    while (node) {
      const background = getComputedStyle(node).backgroundColor;
      seen.push(`${node.tagName.toLowerCase()}=${background || '(空)'}`);
      const numbers = background.match(/rgba?\(([^)]+)\)/);
      if (numbers) {
        const parts = numbers[1].split(',').map(Number);
        if (parts.length < 4 || parts[3] > 0.5) return { background, connected: element.isConnected, seen };
      }
      node = node.parentElement;
    }
    return { background: null, connected: element.isConnected, seen };
  });
  expect(
    result.background,
    result.connected
      ? `${label} 往上找不到任何画了背景的祖先(查过:${result.seen.join(' → ')})`
      : `${label} 已经不在文档里了 —— 测量对象在断言之前被换掉了,看到的背景才是空的。` +
        `这多半是整页重新挂载造成的,往地址或加载时机上查,不要往颜色上查。`,
  ).not.toBeNull();
  return result.background!;
}

/**
 * 扫一遍当前页面上所有**真正画了背景**的元素,挑出其中看起来是深色的那些。
 *
 * 判据是"暗"(`max(r,g,b) < 160`),不是"不是暖白" —— 所以 `--blue: #5f94c8`
 * 这类彩色强调(最大通道 200)不会被误伤。
 *
 * 抽成函数是因为它现在有两个调用点:整页扫描,和"打开某个面板之后再扫一遍"。
 * 后者才是真正抓得住漏网的地方 —— 只 `goto` 不点开的组件,整页扫描永远看不见。
 */
async function darkPaint(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const dark: string[] = [];
    for (const node of document.querySelectorAll<HTMLElement>('body *')) {
      const background = getComputedStyle(node).backgroundColor;
      const numbers = background.match(/rgba?\(([^)]+)\)/);
      if (!numbers) continue;
      const [r, g, b, a = 1] = numbers[1].split(',').map(Number);
      // 半透明的一层压在别的表面上,它的本色不代表最终看到的颜色。
      if (a < 0.5) continue;
      if (Math.max(r, g, b) < 160) {
        dark.push(`${node.tagName.toLowerCase()}.${node.className.toString().trim().split(/\s+/)[0] ?? ''}=${background}`);
      }
    }
    return [...new Set(dark)].slice(0, 6);
  });
}

test('工作台使用统一的暖白视觉系统', async ({ page }) => {
  const token = await signIn(page);
  await seedSpace(page, token, {
    title: '主题验收空间',
    nodes: [{ title: '一条任务', nodeType: 'task' }],
  });

  const palette = await page.evaluate(() => {
    const painted = (selector: string) => {
      const element = document.querySelector<HTMLElement>(selector);
      if (!element) return null;
      const style = getComputedStyle(element);
      return { backgroundColor: style.backgroundColor, backgroundImage: style.backgroundImage };
    };
    const root = getComputedStyle(document.documentElement);
    return {
      colorScheme: root.colorScheme,
      background: root.getPropertyValue('--bg').trim(),
      surface: root.getPropertyValue('--surface').trim(),
      text: root.getPropertyValue('--text').trim(),
      bodyBackground: getComputedStyle(document.body).backgroundColor,
      bodyText: getComputedStyle(document.body).color,
      navigation: painted('.top-navigation'),
      canvas: painted('.workspace'),
      card: painted('.growth-node.task'),
      composer: painted('.composer'),
    };
  });

  // `color-scheme` 决定浏览器把滚动条和表单控件画成什么样。它与 CSS 变量脱节时,
  // 暖白的画布上会长出深色的滚动条 —— 所以这一条单独断言,不能只看颜色。
  expect(palette.colorScheme).toContain('light');

  // 主题变量本身。这两个值是整个主题的源头,它们错了下面每一条都会错,
  // 但反过来不成立:变量对了、某个组件硬编码了深色,只有逐处的断言抓得住。
  expectLightSurface(palette.background, '--bg');
  expectLightSurface(palette.surface, '--surface');
  expect(
    Math.max(...channels(toRgb(palette.text))[0]) <= 120,
    `--text 不够暗:${palette.text}`,
  ).toBe(true);

  expectLightSurface(palette.bodyBackground, '页面底色');
  expectLightSurface(palette.navigation!.backgroundColor, '顶部导航');
  expectLightSurface(palette.canvas!.backgroundColor, '画布工作面');
  expectLightSurface(palette.card!.backgroundColor, '任务节点卡片');
  expectLightSurface(palette.composer!.backgroundColor, '对话输入框');

  expect(
    Math.max(...channels(palette.bodyText)[0]) <= 120,
    `正文颜色不够暗:${palette.bodyText}`,
  ).toBe(true);
});

test('根目标节点的渐变是浅色的', async ({ page }) => {
  const token = await signIn(page);
  await seedSpace(page, token, { title: '渐变验收空间' });

  const goal = await page.locator('.growth-node.goal').first().evaluate((element) => {
    const style = getComputedStyle(element);
    return { backgroundColor: style.backgroundColor, backgroundImage: style.backgroundImage };
  });

  // 这一条专门钉住那个陷阱:根目标节点**不画背景色**。哪天有人把渐变改成
  // `background-color`,上面那条"卡片是浅色"的断言会突然开始覆盖它 —— 而这里会红,
  // 提醒改的人去看一眼是不是有意的。
  expect(goal.backgroundColor, '根目标节点开始画背景色了').toMatch(/rgba?\(0, 0, 0, 0\)/);
  expectLightSurface(goal.backgroundImage, '根目标节点渐变');
});

test('画布上是一棵真的树，不是一处颜色正确的空白', async ({ page }) => {
  const token = await signIn(page);
  const { rootId, nodeIds } = await seedSpace(page, token, {
    title: '结构验收空间',
    nodes: [
      { title: '阶段一 · 打基础', nodeType: 'stage' },
      { title: '阶段二 · 做项目', nodeType: 'stage' },
      { title: '读两篇论文', nodeType: 'task', parentTitle: '阶段一 · 打基础' },
    ],
  });

  // 上一版这一条看的是示例空间那棵保研树。它当年**整棵变成空白**过:`kind === 'none'`
  // (还没选空间)和示例空间共用同一个 localStorage 键,于是新用户只要先看过空间列表,
  // 那份空树就被当成示例数据读了回来。而只断言颜色的话,那个状态是**通过**的 ——
  // 空白的地方没有一处是深色。
  //
  // 真实空间的失效模式一模一样,而且更常见:接口挂了、投影多滤了一层、层级算错,
  // 画布都会安静地空着。所以这里数的是**节点个数**:根 + 两个阶段。任务不在这一层。
  await expect(page.locator('.react-flow__node')).toHaveCount(3);
  await expect(page.locator(`.react-flow__node[data-id="${rootId}"]`)).toBeVisible();

  // 进入子空间走节点右上角那个箭头(步骤 4 之前是双击;单击现在是"打开正文与详情",
  // 那条路会把弹窗盖在画布上 —— 所以这里不能再用单击,也不再有双击)。
  await enterSpace(page, nodeIds['阶段一 · 打基础']);
  await expect(page.locator('.leaf-path')).toBeVisible();

  // 这一层是"阶段一 + 它下面的任务"。另一个阶段**不在**这一层 —— 而它并没有消失,
  // 返回上级就有。层级画出来的必须是后端那一层,不是"把整棵树铺平"。
  await expect(page.locator('.react-flow__node')).toHaveCount(2);
  await expect(page.locator(`.react-flow__node[data-id="${nodeIds['读两篇论文']}"]`)).toBeVisible();
  await expect(page.locator(`.react-flow__node[data-id="${nodeIds['阶段二 · 做项目']}"]`)).toHaveCount(0);

  // 进到子路径之后,这几个表面必须还是同一套暖白。
  expectLightSurface(await paintedSurface(page.locator('.leaf-path'), '子路径'), '子路径的背景');
  const leaf = page.locator('.leaf-path .growth-node.task').first();
  await expect(leaf).toBeVisible();
  expectLightSurface(
    await leaf.evaluate((element) => getComputedStyle(element).backgroundColor),
    '子路径的树叶',
  );
});

/**
 * 这一条扫的是**整页**,而不是某几个选择器。
 *
 * 深度校准过:`Math.max(r,g,b) < 160` 在修好之前精确命中 `/me` 上那两个近黑按钮
 * (`#111a26`),修好之后全站为零;而 `--blue: #5f94c8` 这类彩色强调(最大通道 200)
 * 不会被误判 —— 判据是"暗",不是"不是暖白"。
 *
 * ## 为什么要 `slow()`
 *
 * 这一条要**整页加载六个页面**。实测(4 个 worker、dev server 现场编译):它自己
 * 就要 15~27 秒,而默认预算是 30 秒 —— 也就是说它不是"会不会失败"的问题,是
 * **离失败只差几秒**。整套跑的时候偶尔红的就是它,失败信息是 `page.goto` 超时,
 * 和颜色毫无关系。
 *
 * `slow()` 只是把这个测试的**墙钟预算**放宽到三倍,它不碰断言的 20 秒上限 ——
 * 真正在乎的那条判据(整页没有一个元素是深色)**没有被放宽**。
 */
test('核心页面没有任何一处深色涂装', async ({ page }) => {
  // `slow()` 只能这样用(`test.slow('名字', fn)` 这种写法**不是**注册测试,
  // 它会让这条测试被静默丢掉 —— 整套从 30 条变成 28 条而没有任何报错)。
  test.slow();
  const token = await signIn(page);
  await seedSpace(page, token, { title: '涂装验收空间' });

  const pages = ['/workbench', '/today', '/journal', '/conversations', '/me', '/spaces'] as const;
  for (const path of pages) {
    await page.goto(path);
    await expect(page.locator('main')).toBeVisible();
    await page.waitForTimeout(600);

    expect(await darkPaint(page), `${path} 上有深色涂装`).toEqual([]);
  }
});

/**
 * 「我的」的资料编辑表单 —— 上面那条整页扫描**看不见它**。
 *
 * 整页扫描只 `goto` 六个页面,而这块表单是点击"编辑个人资料"之后才挂上来的。
 * 于是它成了这一层最久的漏网:面板 `#0f1824`、输入框 `#0a121c`,全在 `auth.css` 里
 * 写死,而 `auth.css` 排在 `dark-theme.css` **之后**(`layout.tsx:11` vs `:12`),
 * 同特异度下后写的赢 —— 所以 `dark-theme.css` 里那条不带 `!important` 的
 * `border-color` 一直是空转的。
 *
 * 断言分两层:整块表单不能有深色,以及输入框得是**亮的输入框**而不是亮底上的深色凹陷。
 */
test('资料编辑表单在暖白主题下不是一块黑色卡片', async ({ page }) => {
  await signIn(page);
  await page.goto('/me');
  await clickUntilVisible(
    page,
    page.getByRole('button', { name: '编辑个人资料' }),
    page.getByLabel('姓名'),
  );
  await page.waitForTimeout(400);

  await expect(page.locator('.profile-edit-form')).toBeVisible();
  expect(await darkPaint(page), '/me 的资料编辑表单里有深色涂装').toEqual([]);

  // 输入框自己也得亮 —— 上面那条只看"不是深色",而 `#f8f6ef` 上的 `#0a121c`
  // 已经在整页扫描里被抓到过,这里再钉一次"填字的地方是白的"。
  for (const label of ['姓名', '专业排名', '个人介绍'] as const) {
    const field = page.getByLabel(label);
    expectLightSurface(
      await field.evaluate((element) => getComputedStyle(element).backgroundColor),
      `资料表单的「${label}」输入框`,
    );
  }
});

test('时间线画布与信息卡使用同一套表面', async ({ page }) => {
  const token = await signIn(page);
  // 时间线上的卡片来自**有日期**的节点。没有 deadline 的节点画不出信息卡 ——
  // 这正是这一版新加的、老断言没覆盖到的状态(老断言建的是无日期任务,然后假设有卡片)。
  const { workspaceId } = await seedSpace(page, token, {
    title: '时间线主题空间',
    nodes: [{ title: '有截止日期的任务', nodeType: 'task', deadline: '2026-10-20' }],
  });

  /*
   * 视图由 `?view=` 决定(见 `Workbench.tsx`),不是组件内部状态 —— 刷新之后还在的。
   * 地址上带 `?workspace=`:这一页在没有空间参数时会先按"还没有空间"渲染一次,
   * 靠本地键找回空间之后**整个应用外壳重新挂载**。
   */
  await page.goto(`/workbench?workspace=${workspaceId}&view=timeline`);

  const timeline = page.getByTestId('timeline-view');
  const card = page.locator('[data-timeline-card]').first();

  /*
   * **先等卡片,再量颜色。** 顺序是这两条的要点,不是随手排的。
   *
   * 计划到达之前,时间线这一页**已经画出来了** —— 只是空的。上一版量颜色在等卡片之前,
   * 于是量到的可能是"计划到达"那一次重挂载**换掉的那个元素**:它已经脱离文档,
   * `getComputedStyle` 对它返回一串空字符串,往上走到头都没有背景,报出来的是
   * "找不到任何画了背景的祖先" —— 看着像配色错了,其实是**测量对象被换掉了**。
   * 实测:老顺序连跑三次红两次,和基线里它一直红着对得上。
   *
   * 卡片只可能来自真实的节点,所以"卡片画出来了"就是"这一页已经画完最后一次了"。
   * 等它,量到的就是最终那一份 DOM;这不是放宽等待,是把**测量的时机**摆对。
   */
  await expect(card).toBeVisible();
  await expect(timeline).toBeVisible();

  // 往上找第一个真正画了背景的祖先。时间线这一层现在自己就画了表面,所以第一个找到的
  // 就是它;写成"往上找"是为了哪天它改回透明时这条断言仍然成立 —— 那时颜色来自画布,
  // 而画布是不是浅色这件事仍然要有人看着。
  expectLightSurface(await paintedSurface(timeline, '时间线画布'), '时间线画布');
  expectLightSurface(
    await card.evaluate((element) => getComputedStyle(element).backgroundColor),
    '时间线信息卡',
  );
});
