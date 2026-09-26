import { expect, test, type Locator, type Page } from '@playwright/test';

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
}

/**
 * 建一个空间,返回它的 id 与根节点 id。
 *
 * 建完之后**再进一次工作台** —— 这一步不是多余的:`/today`、`/journal`、
 * `/conversations` 在没有选中空间时会被重定向回 `/spaces`,而"选中了哪个空间"是
 * 打开工作台时记在 localStorage 里的(见 provider 里的 `activeKey`)。
 */
async function seedSpace(
  page: Page,
  token: string,
  input: { title: string; nodes?: SeedNode[] },
): Promise<{ workspaceId: string; rootId: string }> {
  const headers = { Authorization: `Bearer ${token}` };
  const created = await page.request.post(`${API_BASE}/api/workspaces`, {
    headers,
    data: { title: input.title, intent: '' },
  });
  const { workspace } = (await created.json()) as { workspace: { id: string } };
  const plan = await page.request.get(`${API_BASE}/api/workspaces/${workspace.id}/plan`, { headers });
  const { nodes } = (await plan.json()) as { nodes: { id: string }[] };
  const rootId = nodes[0].id;
  for (const node of input.nodes ?? []) {
    const response = await page.request.post(`${API_BASE}/api/workspaces/${workspace.id}/nodes`, {
      headers,
      data: { parentId: rootId, ...node },
    });
    if (!response.ok()) throw new Error(`建节点「${node.title}」失败:${response.status()} ${await response.text()}`);
  }
  await page.goto(`/workbench?workspace=${workspace.id}`);
  await expect(page.locator(`.react-flow__node[data-id="${rootId}"]`)).toBeVisible();
  return { workspaceId: workspace.id, rootId };
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

/** 元素往上找到第一个真正画了背景的祖先 —— 那就是它显示出来的"表面"。 */
async function paintedSurface(locator: Locator, label: string): Promise<string> {
  const found = await locator.evaluate((element) => {
    let node: HTMLElement | null = element as HTMLElement;
    while (node) {
      const background = getComputedStyle(node).backgroundColor;
      const numbers = background.match(/rgba?\(([^)]+)\)/);
      if (numbers) {
        const parts = numbers[1].split(',').map(Number);
        if (parts.length < 4 || parts[3] > 0.5) return background;
      }
      node = node.parentElement;
    }
    return null;
  });
  expect(found, `${label} 往上找不到任何画了背景的祖先`).not.toBeNull();
  return found!;
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

test('示例空间仍然是一棵有结构的演示计划', async ({ page }) => {
  await signIn(page);
  // 四个成长分类是**示例空间**的结构,真实空间没有这个概念(它只有根目标与子节点)。
  await page.goto('/workbench?workspace=primary');
  await expect(page.locator('.react-flow__node[data-id="goal"]')).toBeVisible();

  // 这条断言是补上的第二次:示例空间曾经**整棵变成空白** —— `kind === 'none'`
  // (还没选空间)和示例空间共用了同一个 localStorage 键,于是新用户只要先看过
  // 空间列表,那份空树就被当成示例数据读了回来,而界面上还写着
  // "点击四个成长分类进入专属路径"。只断言颜色的话,那个状态是"通过"的。
  await expect(page.locator('.react-flow__node[data-id="research"]')).toBeVisible();
  await expect(page.locator('.react-flow__node[data-id="academic"]')).toBeVisible();

  // 进入子路径是**双击**(`PathView.tsx` 的 `onNodeDoubleClick`)—— 单击只选中。
  // 之前这里写成单击,于是断言一直等一个不会出现的东西。
  await page.locator('.react-flow__node[data-id="research"]').dblclick();
  await expect(page.locator('.leaf-path')).toBeVisible();

  // 进到子路径之后,这几个表面必须还是同一套暖白。
  expectLightSurface(await paintedSurface(page.locator('.leaf-path'), '子路径'), '子路径的背景');
  const leaf = page.locator('.leaf-path .growth-node.task').first();
  if (await leaf.count() > 0) {
    expectLightSurface(
      await leaf.evaluate((element) => getComputedStyle(element).backgroundColor),
      '子路径的树叶',
    );
  }
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

    const result = await page.evaluate(() => {
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
      return { path: location.pathname, dark: [...new Set(dark)].slice(0, 6) };
    });

    expect(result.dark, `${path} 上有深色涂装`).toEqual([]);
  }
});

test('时间线画布与信息卡使用同一套表面', async ({ page }) => {
  const token = await signIn(page);
  // 时间线上的卡片来自**有日期**的节点。没有 deadline 的节点画不出信息卡 ——
  // 这正是这一版新加的、老断言没覆盖到的状态(老断言建的是无日期任务,然后假设有卡片)。
  await seedSpace(page, token, {
    title: '时间线主题空间',
    nodes: [{ title: '有截止日期的任务', nodeType: 'task', deadline: '2026-10-20' }],
  });

  // 视图由 `?view=` 决定(见 `Workbench.tsx`),不是组件内部状态 —— 刷新之后还在的。
  await page.goto('/workbench?view=timeline');

  const timeline = page.getByTestId('timeline-view');
  await expect(timeline).toBeVisible();
  // 时间线自己的背景是透明的,颜色来自上一层的画布 —— 所以往上找。
  expectLightSurface(await paintedSurface(timeline, '时间线画布'), '时间线画布');

  const card = page.locator('[data-timeline-card]').first();
  await expect(card).toBeVisible();
  expectLightSurface(
    await card.evaluate((element) => getComputedStyle(element).backgroundColor),
    '时间线信息卡',
  );
});
