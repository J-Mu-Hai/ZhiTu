import { existsSync } from 'node:fs';
import { defineConfig } from '@playwright/test';

/**
 * 测试用的端口,**不能随便挑一个**。
 *
 * 浏览器里的每个请求都要过 CORS,而后端的 `CORS_ORIGINS`(在 `.env` 里)只列了
 * 5173。挑一个没列进去的端口(比如以前的 3104),表现是**登录页一直不跳转** ——
 * `GET /api/users/me` 被浏览器挡掉,前端把令牌清掉,于是每个测试都停在登录页。
 * 这个失败长得像"测试写错了选择器",其实是配置问题,所以写在这里。
 *
 * 这也意味着 playwright 起的那台服务器必须和本地开发用同一台:5173。
 * 本地已经在跑 `next dev` 时直接复用它(`reuseExistingServer`),CI 里没有就跑
 * `next start`。
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
   * **并发上限,不能跟着核数走。**
   *
   * 不写这一行,Playwright 默认按 CPU 数开工作进程 —— 这台 16 逻辑核的机器上是 8 个。
   * 而这 8 个浏览器**共用同一台 `next dev`**(见下面 `webServer` 那段:本地永远复用
   * 5173),它要给每个页面现场编译、现场渲染;人机两边抢同一份 CPU 和内存,单条测试的
   * 墙钟时间会涨到单独跑的好几倍。实测(同一天、同一台机器、同一份代码):
   *
   *   8 个进程:23~27/28 通过,失败的那几条**都是卡在 30 秒上限**,时间戳精确地落在
   *            30.1/30.2/30.3 秒 —— 它们没验出任何产品问题,只是被挤到超时;
   *   4 个进程:28/28 通过,而且**整轮耗时一样**(都是 1.1 分钟) —— 因为瓶颈本来就是
   *            那台 dev server,不是 CPU。
   *
   * 所以这里限成 4:少一点并发,不多花时间,换来的是"失败=真的有问题"。
   * (断言等待上限 20 秒那条注释是一个道理的另一半。)
   *
   * ## 30 秒的测试预算是护栏,不是判断标准 —— 但"要走六个页面"的那两条另算
   *
   * 后来补测到的:限成 4 之后仍然偶尔红,红的永远是同两条 —— `dark-theme.spec.ts`
   * 里扫六个页面的那条、和 `experience.spec.ts` 里跨四个页面的那条。它们各自要
   * **整页加载 4~6 次**,实测一条 15~27 秒、一条 19 秒,离 30 秒的预算只差几秒;
   * 失败信息是 `page.goto` 超时,和它们要验的东西毫无关系。
   *
   * 这两条自己在测试体里写了 `test.slow()`(预算 ×3)。放宽的是那一条测试的**墙钟**,
   * 断言上限仍然是 20 秒 —— 真正在乎的判据没有被放宽。别把全局超时调大:
   * 那才是把护栏拆掉。
   *
   * (顺带一个坑:`test.slow()` 只能在测试体里调用。写成 `test.slow('名字', fn)` 不会
   * 报错,而是把那条测试**静默丢掉** —— 整套从 30 条变成 28 条。)
   */
  workers: process.env.CI ? undefined : 4,
  outputDir: './artifacts/test-results',
  // 断言超时从默认 5 秒放宽到 20 秒:复用 `next dev` 时,页面首次访问要现场编译,
  // 工作台那一个包(ReactFlow + 画布)冷编译会超过 5 秒。放宽的是**等待**上限,
  // 不是判断标准 —— 20 秒还没出现就是真的没出现。
  expect: { timeout: 20000 },
  use: { baseURL: `http://127.0.0.1:${port}`, viewport: { width: 1440, height: 960 },
    launchOptions: localBrowser ? { executablePath: localBrowser } : undefined, screenshot: 'only-on-failure' },
  webServer: { command: `node node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port ${port}`, url: `http://127.0.0.1:${port}`, reuseExistingServer: !process.env.CI, timeout: 60000 },
});
