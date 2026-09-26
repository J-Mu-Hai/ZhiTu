import { test, expect } from '@playwright/test';
import { TOKEN_KEY, registerAccount } from './support/session';

/**
 * 刷新页面时"恢复登录状态"遇到后端出问题,会怎样。
 *
 * 这一条来自一个真实的缺陷,不是假想的边界:恢复会话的 `catch` 原来不分错误类型,
 * 一律 `clearToken()`。于是**一次**被中断的 `GET /api/users/me`(后端重启、网络抖动、
 * 请求被中断都算)就把浏览器里那张令牌删了 —— 用户看到"莫名其妙退出了登录",
 * 而且刷新也回不来,只能重新输密码。部署时重启服务,正好在刷新页面的每个用户都会
 * 撞上这一下。
 *
 * 探针复现过一次:让第一次 `/api/users/me` 连接失败,页面停在 `/login`,令牌为 null。
 * 现在这里把两种情况分别钉住 —— 服务端**拒绝**令牌(401)该回登录页;
 * 只是**够不着**后端,令牌必须留着。
 *
 * ## 为什么没有"第一次失败、第二次成功"那条
 *
 * 开发模式下一次加载会发**一到两次** `GET /api/users/me`(`reactStrictMode` 会重挂
 * 一次 effect;实测两个都出现过)。也就是说"第一次被打断之后还会不会有第二次"
 * 本身是不确定的 —— 有第二次时界面直接进工作台(失败的那一趟被取代,不留痕迹),
 * 只有一次时显示重试屏。两种都对,而**哪一种会发生不由测试决定**。
 * 所以这里不去断言那种碰运气的结果,只断言在两种情况下都成立的两件事:
 * 令牌还在、没被踢到登录页。
 */

test('后端一直够不着时,留住令牌并给一个重试,而不是把用户登出', async ({ page }) => {
  const account = await registerAccount(page, 'restore-blip');

  let failing = true;
  await page.route('**/api/users/me', (route) =>
    failing ? route.abort('connectionfailed') : route.continue(),
  );

  await page.goto('/workbench?workspace=primary&view=timeline');

  // 停在"连不上,重试",而不是登录页 —— 用户并没有被登出。
  await expect(page.getByText('暂时连不上后端,你的登录状态还在。')).toBeVisible();
  await expect(page.getByLabel('密码')).toHaveCount(0);
  expect(await page.evaluate((key) => localStorage.getItem(key), TOKEN_KEY)).toBe(account.token);

  // 后端回来了:点重试就该进去,不需要重新输密码。
  failing = false;
  await page.getByRole('button', { name: '重试' }).click();
  await expect(page.getByRole('button', { name: '收起对话' })).toBeVisible();
  expect(new URL(page.url()).pathname).toBe('/workbench');
});

test('服务端明确拒绝这张令牌时,才回到登录页', async ({ page }) => {
  await registerAccount(page, 'restore-401');

  await page.route('**/api/users/me', (route) =>
    route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'UNAUTHENTICATED', message: '令牌无效或已过期。' } }),
    }),
  );

  await page.goto('/workbench?workspace=primary&view=timeline');

  await expect(page).toHaveURL(/\/login$/);
  // 令牌被拒绝了,留着它只会让用户反复看到"已登录但什么都没有"。
  expect(await page.evaluate((key) => localStorage.getItem(key), TOKEN_KEY)).toBeNull();
});
