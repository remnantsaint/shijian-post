import { test, expect, devices } from '@playwright/test';
async function enter(page: import('@playwright/test').Page) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '今天，寄一点心意。' })).toBeVisible();
  await page.getByRole('button', { name: '登录邮局', exact: true }).click();
  await page.getByRole('button', { name: '预览邮局 · 不发送邮件' }).click();
  await expect(page.getByText('体验访客', { exact: true })).toBeAttached();
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

async function tinyImage(page: import('@playwright/test').Page) {
  const base64 = await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 300;
    canvas.height = 180;
    const context = canvas.getContext('2d')!;
    context.fillStyle = '#e5b57b';
    context.fillRect(0, 0, 300, 180);
    context.fillStyle = '#294e40';
    context.font = '26px sans-serif';
    context.fillText('SHIJIAN POST', 45, 98);
    return canvas.toDataURL('image/png').split(',')[1];
  });
  return { name: '邮局照片.png', mimeType: 'image/png', buffer: Buffer.from(base64, 'base64') };
}
async function middleCaret(page: import('@playwright/test').Page) {
  await page.getByLabel('想说的话').evaluate((el) => {
    el.focus();
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let node: Node | null = walker.nextNode();
    while (node && !node.textContent?.includes('后面的文字')) node = walker.nextNode();
    if (!node) throw new Error('Expected the following paragraph');
    const range = document.createRange();
    range.setStart(node, node.textContent!.indexOf('后面的文字'));
    range.collapse(true);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
  });
}
test('paste puts an image at the cursor and simulated completion removes the original', async ({
  page,
}) => {
  await enter(page);
  await page.getByLabel('寄给谁').fill('photo@example.com');
  await page.getByLabel('信的标题').fill('照片与文字 ' + Date.now());
  await page.getByLabel('想说的话').fill('前面的文字\n后面的文字');
  const file = await tinyImage(page);
  await middleCaret(page);
  await page.getByLabel('想说的话').evaluate((el, base64) => {
    const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
    const clipboardData = new DataTransfer();
    clipboardData.items.add(new File([bytes], '粘贴照片.png', { type: 'image/png' }));
    el.dispatchEvent(
      new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }),
    );
  }, file.buffer.toString('base64'));
  await expect(page.locator('.editor-image img')).toHaveCount(1);
  await expect(page.locator('.paper-image')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '选择图片', exact: true })).toBeEnabled();
  expect(
    await page
      .locator('.paper-body')
      .evaluate((el) =>
        Array.from(el.childNodes).map((n) =>
          (n as HTMLElement).tagName === 'IMG' ? 'IMG' : n.textContent,
        ),
      ),
  ).toEqual(['前面的文字\n', 'IMG', '\n后面的文字']);
  await page.screenshot({ path: 'test-results/desktop-images.png', fullPage: true });
  const response = page.waitForResponse(
    (r) => r.url().endsWith('/api/tasks') && r.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '寄出这封信', exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(201);
  const input = result.request().postDataJSON();
  expect(input.images).toHaveLength(1);
  expect(input.body).toBe('前面的文字\n[[image:' + input.images[0].id + ']]\n后面的文字');
  const saved = await result.json();
  expect(saved.task.status).toBe('simulated');
  expect(saved.task.imagesAvailable).toBe(false);
});
test('file picker preserves the cursor and a scheduled image stays private until cancellation', async ({
  page,
}) => {
  await enter(page);
  const subject = '待寄照片 ' + Date.now();
  await page.getByLabel('寄给谁').fill('later-photo@example.com');
  await page.getByLabel('信的标题').fill(subject);
  await page.getByLabel('想说的话').fill('前面的文字\n后面的文字');
  const file = await tinyImage(page);
  await middleCaret(page);
  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: '选择图片', exact: true }).click();
  await (await chooser).setFiles(file);
  await expect(page.locator('.editor-image img')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '选择图片', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: /约个时间/ }).click();
  await page
    .getByLabel('目标时间（北京时间）')
    .fill(new Date(Date.now() + 10 * 3600000).toISOString().slice(0, 16));
  const response = page.waitForResponse(
    (r) => r.url().endsWith('/api/tasks') && r.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '放进待寄信箱', exact: true }).click();
  const result = await response;
  expect(result.status()).toBe(201);
  const input = result.request().postDataJSON();
  expect(input.body.indexOf('[[image:')).toBeLessThan(input.body.indexOf('后面的文字'));
  const saved = await result.json();
  expect(saved.task.imagesAvailable).toBe(true);
  const url = `/api/tasks/${saved.task.id}/images/${input.images[0].id}`;
  expect((await page.request.get(url)).status()).toBe(200);
  await page.getByRole('button', { name: '去信件记录看看' }).click();
  await page.getByRole('textbox', { name: '搜索信件' }).fill(subject);
  await page.getByRole('button', { name: subject, exact: true }).click();
  await expect(page.getByRole('dialog').locator('.paper-image')).toBeVisible();
  expect(
    await page
      .getByRole('dialog')
      .locator('.paper-image')
      .evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0),
  ).toBe(true);
  page.once('dialog', (d) => d.accept());
  await page.getByRole('button', { name: '取消这封待寄的信' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await page.request.get(url)).status()).toBe(404);
});
test('mobile chooses images at the end, removes them and rejects a fourth image', async ({
  browser,
}) => {
  const context = await browser.newContext({
    ...devices['iPhone 13'],
    baseURL: 'http://127.0.0.1:5173',
  });
  const page = await context.newPage();
  await enter(page);
  await page.getByLabel('想说的话').fill('一张照片，一个日常。');
  const file = await tinyImage(page);
  await page
    .getByLabel('选择正文图片')
    .setInputFiles([file, { ...file, name: '第二张.png' }, { ...file, name: '第三张.png' }]);
  await expect(page.locator('.editor-image')).toHaveCount(3);
  await expect(page.locator('.paper-image')).toHaveCount(3);
  await expect(page.getByRole('button', { name: '选择图片', exact: true })).toBeDisabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.screenshot({ path: 'test-results/mobile-images.png', fullPage: true });
  await page.getByRole('button', { name: '删除图片 第二张.png' }).click();
  await expect(page.locator('.paper-image')).toHaveCount(2);
  await expect(page.getByRole('button', { name: '选择图片', exact: true })).toBeEnabled();
  await page.getByLabel('选择正文图片').setInputFiles([file, { ...file, name: '第四张.png' }]);
  await expect(page.getByRole('alert')).toHaveText('每封信最多插入 3 张图片');
  await expect(page.locator('.editor-image')).toHaveCount(2);
  await context.close();
});

test('administrator saves a sender nickname and reloads it from the account', async ({ page }) => {
  await enter(page);
  await page.getByRole('button', { name: '邮局设置', exact: true }).click();
  await expect(page.getByRole('heading', { name: '管理邮局', exact: true })).toBeVisible();
  const nickname = '西柿界面' + Date.now().toString().slice(-4);
  await page.getByLabel('寄件昵称').fill(nickname);
  await expect(page.locator('.sender-preview')).toContainText(nickname + ' · 柿间邮局');
  await page.getByRole('button', { name: '保存昵称', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存昵称', exact: true })).toBeDisabled();
  await page.reload();
  await page.getByRole('button', { name: '邮局设置', exact: true }).click();
  await expect(page.getByLabel('寄件昵称')).toHaveValue(nickname);
  const config = await (await page.request.get('/api/config')).json();
  expect(config.user.senderName).toBe(nickname);
  expect(config.user.role).toBe('admin');
  await page.getByLabel('寄件昵称').fill('');
  await page.getByRole('button', { name: '保存昵称', exact: true }).click();
  await expect(page.locator('.sender-preview')).toContainText(config.from);
});
test('ordinary member preview saves a nickname, cannot manage the post office and sees only their own tasks', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto('/');
  await page.getByRole('button', { name: '登录邮局', exact: true }).click();
  await page.getByRole('button', { name: '普通寄信人预览 · 不发送邮件', exact: true }).click();
  await expect(page.getByRole('button', { name: '退出登录', exact: true })).toBeVisible();
  const origin = 'http://127.0.0.1:5173';
  const reset = await page.request.post('/api/profile', {
    data: { senderName: '' },
    headers: { Origin: origin },
  });
  expect(reset.status()).toBe(200);
  await page.reload();
  await page.getByRole('button', { name: '邮局设置', exact: true }).click();
  await expect(page.getByRole('heading', { name: '寄信人预览', exact: true })).toBeVisible();
  await expect(page.locator('.settings-card').getByText('寄信人', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '管理邮局', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '生成邀请', exact: true })).toHaveCount(0);
  expect(
    (
      await page.request.post('/api/admin/invites', { data: {}, headers: { Origin: origin } })
    ).status(),
  ).toBe(403);
  await page.getByLabel('寄件昵称').fill('小王');
  await page.getByRole('button', { name: '保存昵称', exact: true }).click();
  await expect(page.getByRole('button', { name: '保存昵称', exact: true })).toBeDisabled();
  const config = await (await page.request.get('/api/config')).json();
  expect(config.user.role).toBe('member');
  expect(config.user.senderName).toBe('小王');
  if (await page.getByRole('button', { name: '关闭提示', exact: true }).isVisible())
    await page.getByRole('button', { name: '关闭提示', exact: true }).click();
  await page.locator('.workspace').screenshot({ path: 'test-results/member-settings.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.screenshot({ path: 'test-results/member-settings-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.getByRole('button', { name: '写一封信', exact: true }).click();
  const title = '成员昵称验收 ' + Date.now();
  await page.getByLabel('寄给谁').fill('member-test@example.com');
  await page.getByLabel('信的标题').fill(title);
  await page.getByLabel('想说的话').fill('模拟验证昵称，没有发送真实邮件。');
  const response = page.waitForResponse(
    (r) => r.url().endsWith('/api/tasks') && r.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '寄出这封信', exact: true }).click();
  const saved = await (await response).json();
  expect(saved.task.status).toBe('simulated');
  expect(saved.task.senderFrom).toContain('小王 · 柿间邮局');
  await page.getByRole('button', { name: '去信件记录看看' }).click();
  await page.getByRole('textbox', { name: '搜索信件' }).fill(title);
  await page.getByRole('button', { name: title, exact: true }).click();
  await expect(page.getByRole('dialog').locator('.task-facts')).toContainText('小王 · 柿间邮局');
});
