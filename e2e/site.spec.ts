import { test, expect, devices } from '@playwright/test';
async function enter(page: import('@playwright/test').Page) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '今天，寄一点心意。' })).toBeVisible();
  await page.getByRole('button', { name: '登录邮局', exact: true }).click();
  await page.getByRole('button', { name: '预览邮局 · 不发送邮件' }).click();
  await expect(page.getByText('体验访客', { exact: true })).toBeVisible();
}
test('desktop writes, previews, saves a template and simulates delivery', async ({
  page,
  request,
}) => {
  const config = await (await request.get('/api/config')).json();
  expect(config.mailMode).toBe('mock');
  await page.setViewportSize({ width: 1440, height: 1050 });
  await enter(page);
  const subject = '一封来自界面验收的小信 ' + Date.now();
  await page.getByLabel('寄给谁').fill('friend@example.com');
  await page.getByLabel('信的标题').fill(subject);
  await page
    .getByLabel('想说的话')
    .fill('嗨，\n\n今天的邮局已经开门了。\n愿你遇见一点没有预料到的温柔。\n\n见字如面。');
  await expect(page.locator('.letter-paper h3')).toHaveText(subject);
  await page.getByRole('button', { name: '存为模板' }).click();
  await page.getByLabel('模板名称').fill('界面验收模板');
  await page.getByRole('button', { name: '保存模板', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.screenshot({ path: 'test-results/desktop-compose.png', fullPage: true });
  await page.getByRole('button', { name: '寄出这封信', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('预览完成', { exact: true })).toBeVisible();
  await expect(page.getByText('预览已完成，这封信未实际发送。')).toBeVisible();
  await page.getByRole('button', { name: '去信件记录看看' }).click();
  await page.getByRole('textbox', { name: '搜索信件' }).fill(subject);
  await expect(page.getByRole('button', { name: subject, exact: true })).toBeVisible();
  await page.getByRole('button', { name: subject, exact: true }).click();
  await expect(page.getByRole('dialog').locator('.paper-to')).toContainText('friend@example.com');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.screenshot({ path: 'test-results/desktop-history.png', fullPage: true });
});
test('scheduled letter can be cancelled from its detail view', async ({ page }) => {
  await enter(page);
  const subject = '等待出发的信 ' + Date.now();
  await page.getByLabel('寄给谁').fill('later@example.com');
  await page.getByLabel('信的标题').fill(subject);
  await page.getByLabel('想说的话').fill('留给未来的一个小小问候。');
  await page.getByRole('button', { name: /约个时间/ }).click();
  await page
    .getByLabel('目标时间（北京时间）')
    .fill(new Date(Date.now() + 10 * 3600000).toISOString().slice(0, 16));
  await page.getByRole('button', { name: '放进待寄信箱', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('等待寄出', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '去信件记录看看' }).click();
  await page.getByRole('textbox', { name: '搜索信件' }).fill(subject);
  await page.getByRole('button', { name: subject, exact: true }).click();
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: '取消这封待寄的信' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('table').getByText('已取消', { exact: true })).toBeVisible();
});
test('phone layout fits and navigation works', async ({ browser }) => {
  const context = await browser.newContext({ ...devices['iPhone 13'] });
  const page = await context.newPage();
  await page.goto('http://127.0.0.1:5173/');
  await expect(page.getByRole('heading', { name: '今天，寄一点心意。' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.screenshot({ path: 'test-results/mobile-compose.png', fullPage: true });
  await page.getByRole('button', { name: '展开菜单' }).click();
  await page.getByRole('button', { name: '模板信纸', exact: true }).click();
  await expect(page.getByRole('heading', { name: '灵感，先从一句话开始。' })).toBeVisible();
  await page.getByRole('button', { name: '用这封信开始' }).first().click();
  await expect(page.getByLabel('信的标题')).toHaveValue('今天，也想和你说声嗨');
  await context.close();
});
