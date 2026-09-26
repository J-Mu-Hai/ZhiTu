import {test,expect} from '@playwright/test';
import {openDemoSpace, registerAccount} from './support/session';

// 标题里原来写的是"两个输入框共享同一份对话"。那一对输入框里的小的那个
// (浮动卡片上的 `卡片内回答` / `发送卡片回答`)已经不在界面上了 —— 见下面那段注释。
// 现在验的是同一件事的现版本:一处发出的消息,在收起、换视图之后仍然在同一份列表里。
test('cards stay separate and move together, and sent messages survive view switches',async({page})=>{
  await registerAccount(page,'layout3');
  await openDemoSpace(page,'/workbench');
  await expect(page.locator('.react-flow__node[data-id="research"]')).toBeVisible();
  const cards=page.locator('.floating-conversation .message');
  const bounds=await cards.evaluateAll(es=>es.map(e=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom};}));
  for(let i=1;i<bounds.length;i++)expect(bounds[i].top-bounds[i-1].bottom).toBeGreaterThanOrEqual(20);
  const field=page.locator('.floating-conversation');const before=await field.boundingBox();
  await page.getByRole('button',{name:'整体移动对话'}).focus();
  await page.getByRole('button',{name:'整体移动对话'}).press('ArrowLeft');
  expect((await field.boundingBox())!.x).toBe(before!.x-10);
  // 这里原来填的是卡片上那个小的"回答"输入框。它连同 `inline-question-card`
  // 一起被删掉了:`ConversationPanel` 现在只有底部一个输入框,卡片群里再挂第二个
  // 只会让人分不清哪句话会进哪条对话。断言的对象不变 —— 消息要落进
  // `.floating-conversation .message` 这份列表。
  await page.getByLabel('给 AI 的消息').fill('先整理导师资料');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(cards.filter({hasText:'先整理导师资料'})).toBeVisible();
  await page.getByRole('button',{name:'收起对话'}).click();
  await expect(field).toBeHidden();
  await page.getByRole('tab',{name:'时间线',exact:true}).click();
  await expect(page.getByRole('group',{name:'时间尺度'})).toBeVisible();
  await page.getByRole('button',{name:'展开对话'}).click();
  await page.getByLabel('给 AI 的消息').fill('继续讨论');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await expect(cards.filter({hasText:'继续讨论'})).toBeVisible();
  // 掉上一条也还在。少了这句,"收起对话"和换视图各清一次消息也照样全绿。
  await expect(cards.filter({hasText:'先整理导师资料'})).toBeVisible();
});
