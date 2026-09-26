import { expect, test } from '@playwright/test';
import { openDemoSpace, registerAccount } from './support/session';

/*
 * 这两条验的是那份保研演示数据里的**空间嵌套**和**跨页共享**。
 *
 * 演示数据还在(`src/mock/`),但它现在只属于示例空间 —— 一个空间都没有的新账户
 * 打开工作台会被送回空间页,不会再看到别人那份计划。所以每条都得先登录、再带上
 * `?workspace=primary` 显式进示例空间(见 `support/session.ts` 里 `demoUrl` 那段)。
 */

test('recursive spaces keep nodes and files attached to their parent', async ({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await registerAccount(page,'nested');
  await openDemoSpace(page,'/workbench');
  await expect(page.getByRole('tab', { name: '时间线', exact: true })).toBeVisible();
  await page.locator('.react-flow__node[data-id="research"]').dblclick({delay:100});
  await expect(page.locator('.space-breadcrumb')).toContainText('科研能力');
  await expect(page.locator('.react-flow__node[data-id="research"] .goal')).toBeVisible();
  await page.getByRole('button',{name:'进入联系导师空间',exact:true}).click();
  await page.getByRole('button',{name:'添加树叶',exact:true}).click();
  await page.getByLabel('树叶名称').fill('整理实验室资料');
  await page.getByRole('dialog').getByRole('button',{name:'添加树叶',exact:true}).click();
  await expect(page.getByRole('button',{name:'进入整理实验室资料空间'})).toBeVisible();
  await page.getByRole('button',{name:'进入整理实验室资料空间'}).click();
  await page.getByRole('button',{name:'添加树叶',exact:true}).click();
  await page.getByLabel('树叶名称').fill('阅读导师论文');
  await page.getByRole('dialog').getByRole('button',{name:'添加树叶',exact:true}).click();
  await page.getByRole('button',{name:/空间文件/}).click();
  await page.getByLabel('添加空间文件').setInputFiles({name:'research-notes.md',mimeType:'text/markdown',buffer:Buffer.from('导师方向：大模型与自然语言处理')});
  await expect(page.getByRole('button',{name:'预览 research-notes.md'})).toBeVisible();
  await page.getByRole('button',{name:'预览 research-notes.md'}).click();
  await expect(page.locator('.file-preview')).toContainText('大模型与自然语言处理');
  await page.getByRole('button',{name:'关闭弹窗'}).last().click();
  await page.getByRole('button',{name:'关闭弹窗'}).click();
  await page.screenshot({path:'artifacts/nested-space.png'});
  await page.getByRole('tab',{name:'任务',exact:true}).click();
  await expect(page.locator('.task-detail')).toContainText(['整理实验室资料','阅读导师论文']);
  await page.getByRole('tab',{name:'路径',exact:true}).click();
  await page.getByRole('button',{name:'返回上级空间'}).click();
  await expect(page.locator('.space-breadcrumb')).toContainText('联系导师');
  await page.getByRole('button',{name:/空间文件/}).click();
  await expect(page.locator('.file-list')).toHaveCount(0);
  await page.getByRole('button',{name:'关闭弹窗'}).click();
  await page.getByRole('button',{name:'进入整理实验室资料空间'}).click();
  await page.getByRole('button',{name:/空间文件/}).click();
  await expect(page.getByRole('button',{name:'预览 research-notes.md'})).toBeVisible();
  expect(errors).toEqual([]);
});

/*
 * `slow()`:这条要在四个页面之间各走一趟(今天 → 随笔 → 对话 → 我的 → 报告),
 * 每一次都是整页加载。实测 19 秒左右,离默认的 30 秒预算不远 —— 和 dark-theme
 * 那条同一个道理:放宽的是**墙钟**,不是任何一条断言的上限。
 */
test('today, journal, conversation history and profile remain connected',async({page})=>{
  // `slow()` 只能这样用 —— `test.slow('名字', fn)` 会让这条测试被静默丢掉。
  test.slow();
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await registerAccount(page,'connected');
  await openDemoSpace(page,'/today');
  await page.getByRole('button',{name:'开始专注'}).click();
  await expect(page.locator('.focus-timer')).toBeVisible({timeout:5000});
  await page.getByRole('button',{name:'完成学习'}).click();
  await expect(page.locator('.completed-label')).toBeVisible();
  await page.getByRole('button',{name:'为什么这样安排？'}).click();
  await expect(page.locator('.insight-explanation')).toBeVisible();
  await page.screenshot({path:'artifacts/today.png'});
  /*
   * **跨页用显式地址,不点导航链接。**
   *
   * 六个主导航链接指向 `/journal`、`/conversations` 这样的裸地址,不带 `?workspace=`。
   * 在示例空间里点它们会丢掉空间上下文,被 WorkspaceRouter 送回空间页;走回来也一样,
   * 因为示例空间不像真实空间那样记在 `zhitu.active.workspace.<用户>` 里。
   * 这条测试要验的是"几个页面共享同一份状态",所以直接用示例空间里的地址,
   * 而不是绕一圈去验导航链接的 href。
   */
  await openDemoSpace(page,'/journal');
  await page.getByLabel('此刻的想法').fill('迈出了联系导师的第一步\n今天整理好了实验室资料。');
  await page.getByRole('button',{name:'关联计划',exact:true}).click();
  await page.getByLabel('标签', {exact:true}).fill('科研');
  await page.getByLabel('关联计划', {exact:true}).selectOption('contact');
  await page.getByRole('button',{name:'发布',exact:true}).click();
  await expect(page.locator('.journal-entry').first()).toContainText('迈出了联系导师的第一步');
  await page.screenshot({path:'artifacts/journal.png'});
  await openDemoSpace(page,'/conversations');
  await page.getByRole('button',{name:/Transformer 学习/}).click();
  await expect(page.locator('.hub-messages')).toContainText('Query');
  await page.getByLabel('继续历史对话').fill('我已经理解了注意力分数');
  await page.getByRole('button',{name:'发送历史对话'}).click();
  await expect(page.locator('.hub-messages')).toContainText('我已经理解了注意力分数');
  await page.screenshot({path:'artifacts/conversations.png'});
  // 我的页不需要空间,用裸地址就行。
  await page.goto('/me');
  await expect(page.locator('.profile-page')).toBeVisible();
  await page.screenshot({path:'artifacts/profile.png'});
  const slugs=['archive','profile','reports','assets','behavior','memory','settings'];
  for(const slug of slugs){await page.locator(`.top-profile-nav a[href="/me/${slug}"]`).click();await expect(page.locator('.detail-page h1')).toBeVisible();}
  await page.getByRole('button',{name:'陪伴',exact:true}).click();
  await page.getByRole('switch',{name:'主动聊天'}).click();
  await expect(page.getByRole('switch',{name:'主动聊天'})).toHaveAttribute('aria-checked','false');
  await page.locator('.top-profile-nav a[href="/me/reports"]').click();
  // 先等报告列表真的画出来再点进去。少了这一句,点"周报"有可能落在上一页
  // (个人中心每一页的导航都是同一套链接,换页是客户端跳转)——
  // 那是竞态,不是产品问题。
  await expect(page.locator('.report-list')).toBeVisible();
  await page.getByRole('link',{name:/周报/}).click();
  await expect(page.locator('.report-body')).toBeVisible();
  await page.locator('.top-profile-nav a[href="/me/settings"]').click();
  await expect(page.getByRole('button',{name:'陪伴',exact:true})).toHaveAttribute('aria-pressed','true');
  await expect(page.getByRole('switch',{name:'主动聊天'})).toHaveAttribute('aria-checked','false');
  expect(errors).toEqual([]);
});
