import { expect, test } from '@playwright/test';

type Rgb = [number, number, number];

function rgbChannels(value: string): Rgb {
  const channels = value.match(/[\d.]+/g)?.slice(0, 3).map(Number);
  if (!channels || channels.length !== 3) {
    throw new Error(`无法解析颜色：${value}`);
  }
  return channels as Rgb;
}

function expectDarkSurface(value: string) {
  expect(Math.max(...rgbChannels(value))).toBeLessThan(70);
}

test('工作台使用统一的深色视觉系统', async ({ page }) => {
  await page.goto('/workbench');

  await expect(page.locator('.react-flow__node[data-id="goal"]')).toBeVisible();

  const palette = await page.evaluate(() => {
    const backgroundOf = (selector: string) =>
      getComputedStyle(document.querySelector<HTMLElement>(selector)!).backgroundColor;

    return {
      colorScheme: getComputedStyle(document.documentElement).colorScheme,
      bodyBackground: getComputedStyle(document.body).backgroundColor,
      bodyText: getComputedStyle(document.body).color,
      navigationBackground: backgroundOf('.top-navigation'),
      goalBackground: backgroundOf('.growth-node.goal'),
      conversationBackground: backgroundOf('.floating-conversation .message'),
      composerBackground: backgroundOf('.floating-conversation .composer'),
    };
  });

  expect(palette.colorScheme).toContain('dark');
  expectDarkSurface(palette.bodyBackground);
  expectDarkSurface(palette.navigationBackground);
  expectDarkSurface(palette.goalBackground);
  expectDarkSurface(palette.conversationBackground);
  expectDarkSurface(palette.composerBackground);
  expect(Math.min(...rgbChannels(palette.bodyText))).toBeGreaterThan(180);
});

test('分类子路径和其他核心页面延续深色层级', async ({ page }) => {
  await page.goto('/workbench');
  await page.locator('.react-flow__node[data-id="research"]').click();

  await expect(page.locator('.leaf-path')).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => ({
        window: window.scrollY,
        shell: document.querySelector<HTMLElement>('.app-shell')?.scrollTop ?? -1,
      })),
    )
    .toEqual({ window: 0, shell: 0 });
  const leafPalette = await page.evaluate(() => ({
    root: getComputedStyle(document.querySelector<HTMLElement>('.leaf-path .growth-node.goal')!).backgroundColor,
    leaf: getComputedStyle(document.querySelector<HTMLElement>('.leaf-path .growth-node.task')!).backgroundColor,
    addButton: getComputedStyle(document.querySelector<HTMLElement>('.leaf-path .space-floating-tools button')!)
      .backgroundColor,
  }));
  expectDarkSurface(leafPalette.root);
  expectDarkSurface(leafPalette.leaf);
  expectDarkSurface(leafPalette.addButton);

  const surfaces = [
    ['/today', '.editorial-page'],
    ['/journal', '.journal-composer'],
    ['/conversations', '.hub-list'],
    ['/me', '.profile-avatar'],
  ] as const;

  for (const [path, selector] of surfaces) {
    await page.goto(path);
    await expect(page.locator(selector)).toBeVisible();
    expectDarkSurface(
      await page.locator(selector).evaluate((element) => getComputedStyle(element).backgroundColor),
    );
  }
});

test('时间线画布与信息卡使用深色表面', async ({ page }) => {
  await page.goto('/workbench?view=timeline');
  const timeline = page.getByTestId('timeline-view');
  await expect(timeline).toBeVisible();
  expectDarkSurface(await timeline.evaluate((element) => getComputedStyle(element).backgroundColor));

  const timelineCard = timeline.locator('button').filter({ has: page.locator('strong') }).first();
  await expect(timelineCard).toBeVisible();
  expectDarkSurface(
    await timelineCard.evaluate((element) => getComputedStyle(element).backgroundColor),
  );
});
