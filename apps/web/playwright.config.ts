import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { defineConfig } from '@playwright/test';
import { pinRunDir } from './tests/support/artifacts';

/**
 * **每一次运行有自己的现场目录,上一次的不许被覆盖。**
 *
 * 这里以前写死 `outputDir: './artifacts/test-results'`。后果不是"有点乱":同一台机器上
 * 连跑几轮,后一轮的截图和 trace 会**无声地**盖掉前一轮的 —— 而"上一轮到底失败在哪"
 * 恰恰是失败之后第一个要问的问题。这件事已经真实发生过一次:`docs/08-DEPLOYMENT.md`
 * 第四节记的那五轮里,第 4 轮的现场就是被后续按用例隔离复现的几次运行盖掉的,取不回来。
 * 那次没有补造截图,只在文档里如实留了说明 —— 现在修的是机制,不是那一份记录。
 *
 * 目录怎么定、为什么要在 worker 起来之前就定下来,写在 `tests/support/artifacts.ts`。
 * **测试自己存的那几张截图也从那里取路径** —— 它们原来是写死的 `artifacts/xxx.png`,
 * 于是 trace 分轮、截图不分轮,一半的现场照样被盖。两处必须同一个来源。
 */
const runDir = pinRunDir();

/**
 * 测试用的端口,**不能随便挑一个**。
 *
 * 浏览器里的每个请求都要过 CORS,而后端的 `CORS_ORIGINS` 只列了 5173 那几个。
 * 挑一个没列进去的端口(比如以前的 3104),表现是**登录页一直不跳转** ——
 * `GET /api/users/me` 被浏览器挡掉,前端把令牌清掉,于是每个测试都停在登录页。
 * 这个失败长得像"测试写错了选择器",其实是配置问题,所以写在这里。
 *
 * ## 两种运行方式,别把它们混起来
 *
 * 这个文件被两件事共用,它们的**可信度不一样**:
 *
 * 1. **本地开发检查** —— `npm run test:e2e`。默认 5173,`reuseExistingServer` 为真,
 *    所以它**会复用你正跑着的 `next dev`**。快、能边改边验,但结果受开发服务器
 *    按需编译的影响:失败信息经常是 `page.goto` 超时,和被测的东西无关。**不构成验收。**
 * 2. **正式验收** —— `node scripts/dev/accept-e2e.mjs`。它带 `CI=1`(把
 *    `reuseExistingServer` 变成 false)、一个显式的 `TEST_PORT`、一个**独立的测试后端**
 *    (自己的数据库、没有模型 key、`CORS_ORIGINS` 只放行这个测试端口),并且跑在
 *    production 构建上。**结论只认这一种。**
 *
 * 换句话说:下面这台 `next start` 只在 `CI=1` 时才会被真正启动;不设 `CI` 时它只是
 * 一份"5173 上大概有个服务器"的声明。
 */
const port = process.env.TEST_PORT || '5173';

/**
 * 浏览器里不预置任何登录状态。
 *
 * 这里以前塞过 `zhitu.auth.accounts.v1` / `zhitu.auth.session.v1` —— 那是上一版
 * **把账户存在 localStorage 里**时才读的两个键。账户改成后端之后没人再看它们,
 * 于是这段配置变成了一句谎话:它看起来像"测试已经登录好了",实际上什么都没做。
 * 需要登录的测试自己去注册一个账户(见 `tests/plan-projection.spec.ts`)。
 */
const localBrowser = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
].find((path): path is string => Boolean(path && existsSync(path)));
export default defineConfig({
  testDir: './tests', fullyParallel: false,
  /*
   * **默认串行。** 这是基线口径,不是性能旋钮。
   *
   * ## 一段需要如实记录的历史
   *
   * 这里曾经写的是 `process.env.CI ? undefined : 4`,配套的理由是"8 个进程会互相抢
   * dev server,4 个刚好"。那段观察本身没错(8 个进程时失败的时间戳精确落在
   * 30.1/30.2/30.3 秒,确实是挤到超时),**但结论下早了**:当时看到的是
   * `4 个进程时 28/28 通过`,于是把"4"当成了安全值。
   *
   * 后来的实测把这个说法推翻了 —— 同一份代码、同一台机器,连跑三轮:
   *
   *   4 个进程:分别 7 / 7 / 8 条失败,而且**失败的集合每次都不一样**。
   *
   * 失败集合不稳定,说明它取决于共享状态、时序和资源竞争,而不是某一个确定的缺陷;
   * 所以"4 能过"不是配置的性质,是那几次运行的运气。**旧结论作废。**
   *
   * ## 现在的口径
   *
   * `workers=1`。先要的是一个**可重复**的结果:同一提交、同一配置连跑三轮应当给出
   * 同一份失败清单,有了那个才谈得上区分"环境问题"和"功能问题"。稳定之后再显式
   * 传 `--workers=4` 去查竞态 —— 那时两次运行在记录里长得不一样,不会互相冒充。
   *
   * 不要靠全局放宽超时或自动重试来让它变绿:那只是把失败藏起来,代价是"通过"
   * 这件事从此不再说明任何问题。
   *
   * ## 30 秒的测试预算是护栏,不是判断标准 —— 但"要走六个页面"的那两条另算
   *
   * `dark-theme.spec.ts` 里扫六个页面的那条、和 `experience.spec.ts` 里跨四个页面的
   * 那条,各自要**整页加载 4~6 次**,实测一条 15~27 秒、一条 19 秒,离 30 秒的预算
   * 只差几秒;失败信息是 `page.goto` 超时,和它们要验的东西毫无关系。
   *
   * 这两条自己在测试体里写了 `test.slow()`(预算 ×3)。放宽的是那一条测试的**墙钟**,
   * 断言上限仍然是 20 秒 —— 真正在乎的判据没有被放宽。
   *
   * (顺带一个坑:`test.slow()` 只能在测试体里调用。写成 `test.slow('名字', fn)` 不会
   * 报错,而是把那条测试**静默丢掉** —— 整套从 30 条变成 28 条。)
   */
  workers: Number(process.env.PLAYWRIGHT_WORKERS ?? 1),
  // 一次运行一个目录(见上面 `resolveRunDir`)。截图、trace、以及每条测试自己的
  // 输出都落在 `test-results/` 里,不会被下一轮盖掉。
  outputDir: join(runDir, 'test-results'),
  /*
   * 报告分两份,职责不同:
   *
   * - `list` 是给人**当场看**的,一直开着。
   * - `json` 是给**记录**用的,只在 `PLAYWRIGHT_JSON_OUTPUT` 有值时开 —— 验收脚本
   *     用它写这一次的摘要。不设时(本地 `npm run test:e2e`)它不出现,免得往控制台
   *     上吐一大段 JSON,把真正要看的失败信息淹掉。
   */
  reporter: process.env.PLAYWRIGHT_JSON_OUTPUT
    ? [['list'], ['json', { outputFile: process.env.PLAYWRIGHT_JSON_OUTPUT }]]
    : [['list']],
  // 断言超时从默认 5 秒放宽到 20 秒:复用 `next dev` 时,页面首次访问要现场编译,
  // 工作台那一个包(ReactFlow + 画布)冷编译会超过 5 秒。放宽的是**等待**上限,
  // 不是判断标准 —— 20 秒还没出现就是真的没出现。
  expect: { timeout: 20000 },
  use: { baseURL: `http://127.0.0.1:${port}`, viewport: { width: 1440, height: 960 },
    launchOptions: localBrowser ? { executablePath: localBrowser } : undefined,
    // 失败要留得下现场:截图 + trace。截图只说明"最后一眼长什么样",而超时类失败
    // 真正要回答的是"它卡在哪一步" —— 那只有 trace 里逐个 action 的时间线答得了。
    //
    // `retain-on-failure` 会给每条测试都录、只保留失败的那些(所以有开销);
    // 不用 `on-first-retry` 是因为它依赖重试,而重试恰好是这套基线的禁忌
    // —— 重试会让"第一次就失败"这件事消失,失败率就再也读不出来了。
    screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: { command: `node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port ${port}`, url: `http://127.0.0.1:${port}`, reuseExistingServer: !process.env.CI, timeout: 60000 },
});
