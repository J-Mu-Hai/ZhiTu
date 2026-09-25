import { test, expect } from '@playwright/test';
import { anchoredZoom, dateToX, dayNumber, getVisibleItems, layoutItems, timelineItems, timelineTicks, xToDate } from '../src/features/growth/timeline';
import { initialGrowth } from '../src/mock/growth-state';

test('calendar mapping, cross-year ticks, semantic levels and dense layout', () => {
  const start = dayNumber('2026-12-01'), anchor = 263, density = 4;
  const next = anchoredZoom(start, density, 20, anchor);
  expect(dateToX(xToDate(anchor, start, density), next, 20)).toBeCloseTo(anchor);
  const ticks = timelineTicks(start, dayNumber('2027-02-01'), 'month').filter(t => t.major);
  expect(ticks.map(t => [t.label, t.year])).toEqual([['12月',2026],['1月',2027],['2月',2027]]);
  const items = timelineItems(initialGrowth, 'goal');
  expect(getVisibleItems(items, 'year').map(i => i.node.id)).toEqual(['goal','direction-set']);
  expect(getVisibleItems(items, 'day').every(i => i.node.timelineLevel === 'action')).toBeTruthy();
  const monthly = getVisibleItems(items, 'month');
  const layout = layoutItems(monthly, dayNumber('2026-08-22'), 4, 760, 'project', 164, 2);
  expect(layout.placed.some(i => i.item.node.id === 'project')).toBeTruthy();
  expect(layout.hidden.length).toBeGreaterThan(0);
  for (const a of layout.placed) for (const b of layout.placed) {
    if (a !== b && a.lane === b.lane) expect(Math.abs(a.left - b.left)).toBeGreaterThanOrEqual(176);
  }
});

test('semantic zoom, pointer anchor, pan, overview, cards and chat proposal', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('/workbench?view=timeline');
  const timeline = page.getByTestId('timeline-view'), canvas = page.getByTestId('timeline-canvas');
  await expect(timeline).toHaveAttribute('data-zoom','month');
  await expect(canvas).toHaveAttribute('data-ready','true');
  await expect(page.getByTestId('today-marker')).toBeVisible();
  await page.screenshot({ path: 'artifacts/timeline-month.png' });
  const cards = await page.locator('[data-timeline-card]').evaluateAll(elements => elements.map(e => { const r=e.getBoundingClientRect(); return { x:r.x,y:r.y,right:r.right,bottom:r.bottom }; }));
  for (let i=0;i<cards.length;i++) for (let j=i+1;j<cards.length;j++) {
    const a=cards[i],b=cards[j]; expect(a.right <= b.x || b.right <= a.x || a.bottom <= b.y || b.bottom <= a.y).toBeTruthy();
  }
  await page.getByRole('group',{name:'时间尺度'}).getByRole('button',{name:'年',exact:true}).click();
  await expect(timeline).toHaveAttribute('data-zoom','year');
  await expect(page.locator('[data-timeline-item="goal"]')).toHaveCount(1);
  await expect(page.locator('[data-timeline-item="coursework-today"]')).toHaveCount(0);
  await page.getByRole('group',{name:'时间尺度'}).getByRole('button',{name:'天',exact:true}).click();
  await page.getByRole('button',{name:'今天',exact:true}).click();
  await expect(timeline).toHaveAttribute('data-zoom','day');
  await expect(page.locator('[data-timeline-item="project"]')).toHaveCount(0);
  await expect(page.locator('[data-timeline-item="coursework-today"]')).toHaveCount(1);
  await page.screenshot({ path:'artifacts/timeline-day.png' });
  await page.getByRole('group',{name:'时间尺度'}).getByRole('button',{name:'月',exact:true}).click();
  const box=await canvas.boundingBox(); if(!box) throw new Error('No canvas');
  const clientX=Math.round(box.x+box.width*.65), pointerX=clientX-box.x;
  const before={start:Number(await canvas.getAttribute('data-start')),density:Number(await canvas.getAttribute('data-density'))};
  await canvas.dispatchEvent('wheel',{clientX,clientY:box.y+100,deltaY:-80,ctrlKey:true});
  await expect.poll(async()=>Number(await canvas.getAttribute('data-density'))).toBeGreaterThan(before.density);
  const after={start:Number(await canvas.getAttribute('data-start')),density:Number(await canvas.getAttribute('data-density'))};
  expect(after.start+pointerX/after.density).toBeCloseTo(before.start+pointerX/before.density,5);
  await page.mouse.move(box.x+box.width*.8,box.y+box.height-22); await page.mouse.down(); await page.mouse.move(box.x+box.width*.8-100,box.y+box.height-22,{steps:5}); await page.mouse.up();
  await expect.poll(async()=>Number(await canvas.getAttribute('data-start'))).toBeGreaterThan(after.start);
  const panStart=Number(await canvas.getAttribute('data-start'));
  const overview=page.getByRole('slider',{name:'概览视窗位置'}); await overview.focus(); await overview.press('ArrowLeft');
  await expect.poll(async()=>Number(await canvas.getAttribute('data-start'))).toBeLessThan(panStart);
  await page.getByRole('group',{name:'时间尺度'}).getByRole('button',{name:'月',exact:true}).click();
  await page.getByRole('button',{name:'今天',exact:true}).click();
  if (!await page.locator('[data-timeline-item="project"]').count()) { await page.getByRole('button',{name:/另有.*展开/}).click(); await page.locator('[data-cluster-panel]').getByRole('button',{name:/科研项目/}).click(); }
  await page.locator('[data-timeline-item="project"] [data-timeline-card]').click();
  await expect(page.getByRole('textbox',{name:'给 AI 的消息'})).toHaveAttribute('placeholder',/科研项目/);
  await page.getByRole('textbox',{name:'给 AI 的消息'}).fill('我觉得安排太早了，推迟到2月');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await page.getByRole('button',{name:'查看影响'}).click();
  await expect(page.getByTestId('plan-ghost')).toHaveCount(1);
  await expect(page.getByTestId('date-inspector')).toContainText('2026-10-18');
  await expect(page.locator('[data-timeline-item="project"]')).toHaveAttribute('data-start-date','2027-02-01');
  await page.screenshot({path:'artifacts/timeline-preview.png'});
  await page.getByRole('button',{name:'接受调整'}).click();
  await expect(page.getByTestId('date-inspector')).toContainText('2027-02-01');
  await expect(page.getByTestId('plan-ghost')).toHaveCount(0);
  expect(errors).toEqual([]);
});
