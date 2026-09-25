import { expect, test } from '@playwright/test';

test('账户拥有独立计划，个人资料可以编辑并持久化', async ({ page }) => {
  await page.goto('/login');
  await page.evaluate(() => localStorage.clear());
  await page.goto('/workbench');

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: '继续你的旅程' })).toBeVisible();
  await page.screenshot({ path: 'artifacts/login-account.png' });
  await page.getByRole('button', { name: '还没有账户？注册' }).click();
  await page.getByLabel('怎么称呼你').fill('王小途');
  await page.getByLabel('邮箱').fill('wang@example.com');
  await page.getByLabel('密码').fill('password123');
  await page.getByLabel('学校').fill('北京邮电大学');
  await page.getByLabel('专业').fill('人工智能');
  await page.getByLabel('目标方向').fill('保研');
  await page.getByRole('button', { name: '创建账户与计划' }).click();

  await expect(page).toHaveURL(/\/workbench$/);
  await expect(page.getByRole('link', { name: /王小途的个人档案/ })).toBeVisible();
  await page.locator('.react-flow__node[data-id="research"]').click();
  await page.getByRole('button', { name: '添加树叶', exact: true }).click();
  await page.getByLabel('树叶名称').fill('王小途的专属调研');
  await page.getByRole('dialog').getByRole('button', { name: '添加树叶', exact: true }).click();
  await expect(page.getByText('王小途的专属调研', { exact: true })).toBeVisible();

  await page.getByRole('navigation').getByRole('link', { name: '我的', exact: true }).click();
  await page.getByRole('button', { name: '编辑个人资料' }).click();
  await page.screenshot({ path: 'artifacts/profile-edit.png' });
  await page.getByLabel('姓名').fill('王小途同学');
  await page.getByLabel('专业排名').fill('18');
  await page.getByLabel('个人介绍').fill('想用人工智能解决真实问题。');
  await page.getByRole('button', { name: '保存个人资料' }).click();
  await expect(page.getByRole('heading', { name: '你好，王小途同学。' })).toBeVisible();

  await page.getByRole('button', { name: '退出当前账户' }).click();
  await page.getByRole('button', { name: '还没有账户？注册' }).click();
  await page.getByLabel('怎么称呼你').fill('李同学');
  await page.getByLabel('邮箱').fill('li@example.com');
  await page.getByLabel('密码').fill('password456');
  await page.getByLabel('学校').fill('知途大学');
  await page.getByLabel('专业').fill('计算机科学');
  await page.getByLabel('目标方向').fill('就业');
  await page.getByRole('button', { name: '创建账户与计划' }).click();
  await page.locator('.react-flow__node[data-id="research"]').click();
  await expect(page.getByText('王小途的专属调研', { exact: true })).toHaveCount(0);

  await page.goto('/me');
  await page.getByRole('button', { name: '退出当前账户' }).click();
  await page.getByLabel('邮箱').fill('wang@example.com');
  await page.getByLabel('密码').fill('password123');
  await page.getByRole('button', { name: '登录知途' }).click();
  await page.locator('.react-flow__node[data-id="research"]').click();
  await expect(page.getByText('王小途的专属调研', { exact: true })).toBeVisible();
  await page.goto('/me');
  await expect(page.getByRole('heading', { name: '你好，王小途同学。' })).toBeVisible();
  await expect(page.getByText('专业排名 18')).toBeVisible();
});
