import { expect, test } from '@playwright/test';
import { assertBackendRunning, clickUntilVisible, createWorkspace, registerAccount, TOKEN_KEY } from './support/session';

/**
 * 账户与档案。
 *
 * ## 这一版为什么和上一版长得不一样
 *
 * 上一版验的是"账户存在浏览器里":注册两个账户,再往 `research` 节点底下各加一片树叶,
 * 然后断言"换个账户进去看不到那片树叶"。那套断言里的两层假设都已经不成立了:
 *
 * 1. **登录之后直接就是工作台,而且工作台里已经有一份保研计划。**
 *    现在不是了 —— 账户、空间、计划都在后端,刚注册的账户**一个空间都没有**,
 *    界面会把他送到"成长空间"列表,让他自己建一个。所以"注册完落在 /workbench"
 *    和"画布上有 research 节点"都不能再断言。
 * 2. **"独立计划"是同一份演示数据的不同副本。** 现在它是后端的行:两个账户各有各的
 *    空间,隔离由 `POST /api/workspaces` 的鉴权保证,不是靠 localStorage 分键。
 *
 * ## 为什么拆成两条
 *
 * 拆之前是**一条**走完全部路径的测试:两次"填八个字段"的界面注册、建空间、建节点、
 * 刷新、改档案、登出、登回来……实测 28.9s ~ 30.3s,而 Playwright 的单条测试上限是
 * 30s —— 它没在验产品,它在跟自己的预算赛跑,跑十次输四次。
 *
 * 所以按**两个不同的主张**拆开,断言一条没少:
 *   - 第一条 = 没登录进不去;注册之后自己建空间、建节点,刷新后还在(界面路径)。
 *   - 第二条 = 档案能改能存;而且**一看就知道哪些东西只属于我这个账户**。
 * 第二条里账户和空间用接口建(那条路径第一条已经走过一遍了),这样它才有余量
 * 把"登出、再登回来、换个账户"这些真正要验的东西做完。
 *
 * 另外:整条流水线是**并行**跑的(Playwright 默认按 CPU 数开工作进程,这台机器上不
 * 限就是 8 个;`playwright.config.ts` 里现在限成 4),而它们共用同一台 `next dev` ——
 * 单独跑一条只要 4 秒,跑满时同一条能慢上几倍。所以这里每一次整页加载都要算钱,能省则省
 * (见下面 `switchAccount` 那段)。
 */

test.beforeAll(async ({ request }) => {
  await assertBackendRunning(request);
});

/** 注册只需要手机号与密码。 */
async function fillRegisterForm(
  page: import('@playwright/test').Page,
  input: { name: string; email: string; password: string; school: string; major: string; target: string },
) {
  await expect(page.locator('.auth-card input')).toHaveCount(2);
  await page.getByLabel('手机号码').fill(input.email);
  await page.getByLabel('密码').fill(input.password);
}

/**
 * 把浏览器里的身份换成另一个账户,并直接走到要看的那一页。
 *
 * 换令牌和"去看某页"合成一步:分开写就是先重载一次再跳转一次,同样的加载跑两遍。
 * 这一页的每一次整页加载都要现场渲染整个应用,而这条测试在跑满工作进程时本来就紧
 * (见文件开头那段),所以能省的加载都省掉。
 */
async function switchAccount(
  page: import('@playwright/test').Page,
  token: string,
  path: string,
): Promise<void> {
  await page.evaluate(([key, value]) => localStorage.setItem(key, value), [TOKEN_KEY, token] as const);
  await page.goto(path);
}

/** 邮箱带时间戳 —— 测试会往开发库 `data/zhitu_dev.db` 里真的写行。 */
const stamp = () => `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;

test('注册之后自己建空间、建节点，刷新后还在', async ({ page }) => {
  const s = stamp();
  const wang = { name: '知途用户', email: `139${Date.now().toString().slice(-8)}`, password: 'password123' };
  const spaceTitle = `王小途的科研计划 ${s}`;
  const nodeTitle = '王小途的专属调研';

  await page.goto('/login');
  await page.evaluate(() => localStorage.clear());
  await page.goto('/workbench');

  // 没有令牌就没有空间可看 —— 工作台会把人送回登录页,而不是给他一份编出来的计划。
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: '继续你的旅程' })).toBeVisible();
  await page.screenshot({ path: 'artifacts/login-account.png' });

  await page.getByRole('button', { name: '还没有账户？注册' }).click();
  await fillRegisterForm(page, { ...wang, school: '北京邮电大学', major: '人工智能', target: '保研' });
  await page.getByRole('button', { name: '创建账户', exact: true }).click();

  // **落点是空间列表,不是工作台。** 工作台需要一个具体空间,而这个账户刚注册、
  // 一个都还没有。以前这里会落到示例空间,于是新用户看到的是一份别人的保研计划。
  await expect(page).toHaveURL(/\/spaces$/);
  await expect(page.getByRole('link', { name: `${wang.name}的个人档案` })).toBeVisible();

  await page.getByRole('button', { name: '创建成长空间' }).first().click();
  await page.getByLabel('空间名称').fill(spaceTitle);
  await page.getByLabel('想在这里推进什么？').fill('确认计划真的存在后端');
  await page.getByRole('button', { name: '创建并进入' }).click();

  // 建完直接进这个新空间 —— 新空间是**空的**,只有根目标。
  await expect(page).toHaveURL(/\/workbench\?workspace=/);
  await expect(page.getByRole('button', { name: '新建节点' })).toBeVisible();
  await page.getByRole('button', { name: '新建节点' }).click();
  await page.getByLabel('节点名称').fill(nodeTitle);
  await page.getByRole('dialog').getByRole('button', { name: '新建节点' }).click();
  await expect(page.getByText(nodeTitle, { exact: true })).toBeVisible();

  // 刷新一次:它得是从后端读回来的,不是内存里还热着的那个状态。
  await page.reload();
  await expect(page.getByText(nodeTitle, { exact: true })).toBeVisible();
});

test('个人资料可以编辑并持久化，而且只属于自己这个账户', async ({ page }) => {
  const s = stamp();
  const spaceTitle = `王小途的科研计划 ${s}`;
  const nodeTitle = '王小途的专属调研';

  // 账户和空间走接口建 —— "填八个字段注册"那条界面路径第一条已经完整走过一遍,
  // 这里再走一遍只是把预算烧在重复的地方。要验的是账户之间互相看不见。
  const wangAccount = await registerAccount(page, 'profile-wang');
  await createWorkspace(page, wangAccount.token, spaceTitle, '确认计划真的存在后端');
  await page.goto('/spaces');

  await expect(page.getByRole('heading', { name: spaceTitle })).toBeVisible();
  await page.getByRole('button', { name: '进入工作台' }).click();
  await page.getByRole('button', { name: '新建节点' }).click();
  await page.getByLabel('节点名称').fill(nodeTitle);
  await page.getByRole('dialog').getByRole('button', { name: '新建节点' }).click();
  await expect(page.getByText(nodeTitle, { exact: true })).toBeVisible();

  await page.goto('/me');
  // 整页加载之后紧跟一次点击 —— 用"验效果"的那种点法,见 `clickUntilVisible`。
  await clickUntilVisible(
    page,
    page.getByRole('button', { name: '编辑个人资料' }),
    page.getByLabel('姓名'),
  );
  await page.screenshot({ path: 'artifacts/profile-edit.png' });
  // 没填过的目标年份必须是**空白**,不能是一个用户没输入过的 `0`(见下面的注释)。
  await expect(page.getByLabel('目标年份')).toHaveValue('');
  await page.getByLabel('姓名').fill('王小途同学');
  await page.getByLabel('专业排名').fill('18');
  await page.getByLabel('个人介绍').fill('想用人工智能解决真实问题。');
  /*
   * 学校、专业、目标方向、目标年份是 `required`(目标年份还有 `min=2026`)——
   * 只要有一个不合法,浏览器就**不会触发 submit**:点"保存"看起来毫无反应,
   * 页面停在表单上。
   *
   * 这是**产品决定**,不是要绕过的障碍:档案要求写清"现在的你"和"要去哪"。
   * 界面上走一遍注册(`AuthScreen`)这四个字段就填好了,而这个账户是走接口建的
   * (见上),所以它们是空的 —— 这里补齐,是在模拟一个真实用户填完表单。
   *
   * 顺带钉住一个曾经的显示错误:目标年份是 `0` 时(后端 NULL 的归一化结果),
   * 这个输入框现在显示**空白**,而不是一个用户从没打过的 `0`。
   */
  await page.getByLabel('学校').fill('北京邮电大学');
  // `exact` 是必须的:这一页还有"专业排名",不精确匹配会同时命中两个输入框。
  await page.getByLabel('专业', { exact: true }).fill('人工智能');
  await page.getByLabel('目标方向').fill('保研');
  await page.getByLabel('目标年份').fill('2027');
  await page.getByRole('button', { name: '保存个人资料' }).click();
  await expect(page.getByRole('heading', { name: '你好，王小途同学。' })).toBeVisible();
  await expect(page.getByText('专业排名 18')).toBeVisible();

  /*
   * 这里原来还有一次 `page.reload()` 再验一遍这两条 —— 去了。
   * 因为下面"换个账户、再换回来"那一趟是**整页加载**(相当于刷新,而且比刷新更狠:
   * 换了身份之后重新从后端读),它已经把这些字段又验了一遍。同一条主张验两遍,
   * 在单条测试只有 30 秒预算、并发跑满时每多一次整页加载就多几秒的情况下不划算。
   */

  /*
   * **换一个账户,看不到上一个账户的空间。**
   *
   * 这一条以前是"进去点 research 节点、找不到那片树叶"。现在账户一多,第一件该
   * 确认的事是"他那份计划根本不在我的列表里" —— 断言的对象从"某片树叶"换成
   * "那个空间",因为空间才是这一版里隔离的单位。
   */
  const liAccount = await registerAccount(page, 'profile-li', { navigate: false });
  await page.goto('/spaces');
  await expect(page.getByRole('heading', { name: spaceTitle })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '选择一个成长空间' })).toBeVisible();

  // 再登回去(来回切一次身份):空间还在,节点还在,档案里改过的两处也还在 —— 都在后端。
  await switchAccount(page, wangAccount.token, '/spaces');
  await expect(page.getByRole('heading', { name: spaceTitle })).toBeVisible();
  await page.getByRole('button', { name: '进入工作台' }).click();
  await expect(page.getByText(nodeTitle, { exact: true })).toBeVisible();
  await page.goto('/me');
  await expect(page.getByRole('heading', { name: '你好，王小途同学。' })).toBeVisible();
  await expect(page.getByText('专业排名 18')).toBeVisible();

  // 切回另一个账户:它的列表里始终没有别人那个空间。
  await switchAccount(page, liAccount.token, '/spaces');
  await expect(page.getByRole('heading', { name: spaceTitle })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '选择一个成长空间' })).toBeVisible();
});
