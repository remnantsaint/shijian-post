import { prepareImages } from '../src/worker/images';
import { imageMarker, type ImageInput } from '../src/shared/images';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { build } from 'esbuild';
import { readFile, readdir } from 'node:fs/promises';
import { createTask, dispatchTask, runDueTasks } from '../src/worker/mail';
import { digest } from '../src/worker/security';
import { mailSchema } from '../src/shared/schema';
import type { Env, TaskRow } from '../src/worker/types';
let mf: Miniflare;
let db: D1Database;
let cookie = '';
const origin = 'http://localhost';
const future = () => new Date(Date.now() + 3600000).toISOString();
const payload = (extra: Record<string, unknown> = {}) => ({
  id: crypto.randomUUID(),
  recipient: 'friend@example.com',
  subject: '一封测试信',
  body: '你好，见字如面。',
  sendAt: future(),
  ...extra,
});
async function request(
  path: string,
  method = 'GET',
  body?: unknown,
  session = cookie,
  extra: Record<string, string> = {},
) {
  return mf.dispatchFetch(origin + '/api' + path, {
    method,
    redirect: 'manual',
    headers: {
      Origin: origin,
      'Content-Type': 'application/json',
      ...(session ? { Cookie: session } : {}),
      ...extra,
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
}
async function login() {
  const response = await request('/auth/demo', 'POST', {}, '');
  expect(response.status).toBe(200);
  cookie = response.headers.get('Set-Cookie')!.split(';')[0];
  return response;
}
async function otherUser(active = true) {
  await db
    .prepare(
      "INSERT INTO users(id,github_id,github_login,name,role,active,created_at) VALUES('other','other','other','朋友','member',?,?)",
    )
    .bind(active ? 1 : 0, new Date().toISOString())
    .run();
  const raw = 'b'.repeat(64);
  await db
    .prepare('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)')
    .bind(await digest(raw), 'other', future())
    .run();
  return 'manyou_session=' + raw;
}
async function create(extra: Record<string, unknown> = {}) {
  const response = await request('/tasks', 'POST', payload(extra));
  expect(response.status).toBe(201);
  return (await response.json()) as { task: TaskRow & { targetAt: string } };
}
function engineEnv(extra: Partial<Env> = {}): Env {
  return {
    DB: db,
    ENVIRONMENT: 'test',
    MAIL_MODE: 'resend',
    RESEND_API_KEY: 'test-only-key',
    MAIL_FROM: '柿间邮局 <letter@send.example.com>',
    APP_URL: origin,
    ADMIN_GITHUB_LOGIN: 'postmaster',
    ...extra,
  };
}
async function due(id: string) {
  await db
    .prepare('UPDATE tasks SET target_at=? WHERE id=?')
    .bind(new Date(Date.now() - 1000).toISOString(), id)
    .run();
}
async function row(id: string) {
  return (await db.prepare('SELECT * FROM tasks WHERE id=?').bind(id).first<TaskRow>())!;
}
beforeAll(async () => {
  const compiled = await build({
    entryPoints: ['src/worker/index.ts'],
    bundle: true,
    write: false,
    format: 'esm',
    platform: 'browser',
    target: 'es2022',
    external: ['node:buffer'],
  });
  mf = new Miniflare(
    convertV4MiniflareOptions({
      modules: true,
      script: compiled.outputFiles[0].text,
      compatibilityDate: '2026-10-13',
      compatibilityFlags: ['nodejs_compat'],
      d1Databases: ['DB', 'LegacyDB'],
      bindings: {
        ENVIRONMENT: 'development',
        MAIL_MODE: 'mock',
        APP_URL: origin,
        MAIL_FROM: '柿间邮局 <letter@send.example.com>',
        ADMIN_GITHUB_LOGIN: 'postmaster',
        GITHUB_CLIENT_ID: 'test-client',
        GITHUB_CLIENT_SECRET: 'test-secret',
      },
      outboundService: async (request) => {
        const url = new URL(request.url);
        if (url.hostname === 'github.com' && url.pathname === '/login/oauth/access_token')
          return Response.json({ access_token: 'test-provider-token' });
        if (url.hostname === 'api.github.com' && url.pathname === '/user')
          return Response.json({ id: 42, login: 'postmaster', name: '邮局管理员' });
        throw new Error('Tests must not access the network');
      },
    }),
  );
  db = (await mf.getD1Database('DB')) as unknown as D1Database;
  for (const name of (await readdir('migrations')).filter((n) => n.endsWith('.sql')).sort()) {
    const sql = await readFile('migrations/' + name, 'utf8');
    const triggers: string[] = [];
    const plain = sql.replace(/CREATE TRIGGER[\s\S]*?END;/gi, (trigger) => {
      triggers.push(trigger);
      return '';
    });
    for (const statement of [
      ...plain
        .split(';')
        .map((s) => s.trim())
        .filter(Boolean),
      ...triggers,
    ])
      await db.prepare(statement).run();
  }
}, 60000);
beforeEach(async () => {
  for (const table of [
    'task_images',
    'tasks',
    'templates',
    'invitation_redemptions',
    'invitations',
    'sessions',
    'oauth_states',
    'suppressions',
    'users',
  ])
    await db.prepare('DELETE FROM ' + table).run();
  await db.prepare('UPDATE settings SET paused=0').run();
  cookie = '';
  await login();
});
afterAll(async () => {
  await mf?.dispose();
});

describe('账户、权限与请求边界', () => {
  it('stores only session hashes and issues an HttpOnly cookie', async () => {
    const response = await login();
    expect(response.headers.get('Set-Cookie')).toContain('HttpOnly');
    const session = await db
      .prepare('SELECT token_hash FROM sessions LIMIT 1')
      .first<{ token_hash: string }>();
    expect(cookie).not.toContain(session!.token_hash);
  });
  it('requires a login for sending and listing', async () => {
    expect((await request('/tasks', 'POST', payload(), '')).status).toBe(401);
    expect((await request('/tasks', 'GET', undefined, '')).status).toBe(401);
  });
  it('rejects cross-origin writes before touching tasks', async () => {
    expect(
      (await request('/tasks', 'POST', payload(), cookie, { Origin: 'https://evil.example' }))
        .status,
    ).toBe(403);
    expect(await db.prepare('SELECT id FROM tasks').first()).toBeNull();
  });
  it('rejects expired sessions', async () => {
    await db.prepare("UPDATE sessions SET expires_at='2020-01-01T00:00:00.000Z'").run();
    expect((await request('/tasks')).status).toBe(401);
  });
  it('never exposes demo login on a public hostname', async () => {
    const response = await mf.dispatchFetch('https://email.example.com/api/auth/demo', {
      method: 'POST',
      headers: { Origin: origin, 'Content-Type': 'application/json' },
      body: '{}',
    });
    expect(response.status).toBe(404);
  });
  it('keeps each user private and prevents cancelling others tasks', async () => {
    const { task } = await create();
    const other = await otherUser();
    expect((await request('/tasks/' + task.id, 'GET', undefined, other)).status).toBe(404);
    expect((await request('/tasks/' + task.id + '/cancel', 'POST', {}, other)).status).toBe(404);
    const listing = (await (await request('/tasks', 'GET', undefined, other)).json()) as {
      tasks: unknown[];
    };
    expect(listing.tasks).toEqual([]);
  });
  it('requires invitation activation and allows exactly one claimant', async () => {
    const invite = (await (await request('/admin/invites', 'POST', { maxUses: 1 })).json()) as {
      code: string;
    };
    const other = await otherUser(false);
    expect((await request('/tasks', 'POST', payload(), other)).status).toBe(403);
    expect((await request('/auth/invite', 'POST', { code: invite.code }, other)).status).toBe(200);
    expect((await request('/tasks', 'POST', payload(), other)).status).toBe(201);
    expect((await request('/auth/invite', 'POST', { code: invite.code })).status).toBe(200);
    expect(await db.prepare('SELECT used_count FROM invitations').first()).toEqual({
      used_count: 1,
    });
    expect((await request('/admin/invites', 'POST', {}, other)).status).toBe(403);
  });
  it('consumes OAuth state and creates the configured administrator', async () => {
    await db.prepare('DELETE FROM sessions').run();
    await db.prepare('DELETE FROM users').run();
    const start = await request('/auth/github', 'GET', undefined, '');
    const state = new URL(start.headers.get('Location')!).searchParams.get('state');
    const stateCookie = start.headers.get('Set-Cookie')!.split(';')[0];
    const callback = await request(
      '/auth/github/callback?state=' + state + '&code=test',
      'GET',
      undefined,
      stateCookie,
    );
    expect(callback.headers.get('Location')).toBe('/');
    expect(callback.headers.get('Set-Cookie')).toContain('manyou_session=');
    expect(await db.prepare('SELECT role,active FROM users').first()).toMatchObject({
      role: 'admin',
      active: 1,
    });
    const replay = await request(
      '/auth/github/callback?state=' + state + '&code=test',
      'GET',
      undefined,
      stateCookie,
    );
    expect(replay.headers.get('Location')).toBe('/?authError=state');
  });
});
describe('任务生命周期', () => {
  it('validates content, unknown fields and zoned timestamps', async () => {
    for (const extra of [
      { recipient: 'bad-address' },
      { subject: 'bad\r\nBcc: x@example.com' },
      { body: '   ' },
      { sendAt: '2026-10-20T12:00' },
      { from: 'attacker@example.com' },
    ])
      expect((await request('/tasks', 'POST', payload(extra))).status).toBe(400);
  });
  it('simulates immediate mail without actual provider calls', async () => {
    const { task } = await create({ sendAt: undefined });
    expect(task.status).toBe('simulated');
    expect(task.reason).toContain('未向真实邮箱投递');
  });
  it('returns the same task on duplicate submission and rejects altered payloads', async () => {
    const input = payload({ sendAt: undefined });
    expect((await request('/tasks', 'POST', input)).status).toBe(201);
    const replay = (await (await request('/tasks', 'POST', input)).json()) as {
      created: boolean;
      task: TaskRow;
    };
    expect(replay.created).toBe(false);
    expect(replay.task.status).toBe('simulated');
    expect((await request('/tasks', 'POST', { ...input, body: 'changed' })).status).toBe(409);
  });
  it('preserves cancellation tombstones and never sends a cancelled task', async () => {
    const { task } = await create();
    expect((await request('/tasks/' + task.id + '/cancel', 'POST', {})).status).toBe(200);
    await due(task.id);
    let calls = 0;
    await dispatchTask(engineEnv(), task.id, {
      fetcher: async () => {
        calls++;
        return Response.json({ id: 'provider' });
      },
    });
    expect(calls).toBe(0);
    expect((await row(task.id)).status).toBe('cancelled');
  });
  it('allows only one concurrent execution claim', async () => {
    const { task } = await create();
    await due(task.id);
    let calls = 0;
    const fetcher: typeof fetch = async () => {
      calls++;
      await new Promise((resolve) => setTimeout(resolve, 20));
      return Response.json({ id: 'one-provider-id' });
    };
    await Promise.all([
      dispatchTask(engineEnv(), task.id, { fetcher }),
      dispatchTask(engineEnv(), task.id, { fetcher }),
    ]);
    expect(calls).toBe(1);
    expect((await row(task.id)).status).toBe('accepted');
  });
  it('treats timeout as uncertain and never retries during polling', async () => {
    const { task } = await create();
    await due(task.id);
    let calls = 0;
    const fetcher: typeof fetch = async () => {
      calls++;
      throw new TypeError('secret must not escape');
    };
    await dispatchTask(engineEnv(), task.id, { fetcher });
    await runDueTasks(engineEnv(), { fetcher });
    expect(calls).toBe(1);
    expect((await row(task.id)).status).toBe('uncertain');
    expect((await row(task.id)).reason).not.toContain('secret');
  });
  it('retains a claim when final persistence fails after provider acceptance', async () => {
    const { task } = await create();
    await due(task.id);
    let calls = 0;
    const brokenDB = {
      prepare: (sql: string) =>
        sql.startsWith('UPDATE tasks SET status=?,finished_at=')
          ? {
              bind: () => ({
                run: async () => {
                  throw new Error('write not acknowledged');
                },
              }),
            }
          : db.prepare(sql),
    } as unknown as D1Database;
    const fetcher: typeof fetch = async () => {
      calls++;
      return Response.json({ id: 'accepted-before-write-failure' });
    };
    await expect(dispatchTask(engineEnv({ DB: brokenDB }), task.id, { fetcher })).rejects.toThrow();
    expect((await row(task.id)).status).toBe('sending');
    await runDueTasks(engineEnv(), { now: new Date(Date.now() + 16 * 60000), fetcher });
    expect((await row(task.id)).status).toBe('uncertain');
    expect(calls).toBe(1);
  });
  it('keeps 5xx and a missing receipt uncertain, not automatically retryable', async () => {
    for (const response of [new Response('', { status: 500 }), Response.json({ ok: true })]) {
      const { task } = await create();
      await due(task.id);
      await dispatchTask(engineEnv(), task.id, { fetcher: async () => response });
      expect((await row(task.id)).status).toBe('uncertain');
    }
  });
  it('records definite provider rejection without exposing its body', async () => {
    const { task } = await create();
    await due(task.id);
    await dispatchTask(engineEnv(), task.id, {
      fetcher: async () => new Response('private provider details', { status: 422 }),
    });
    const result = await row(task.id);
    expect(result.status).toBe('failed');
    expect(result.reason).toContain('422');
    expect(result.reason).not.toContain('private');
  });
  it('recovers stale claims as uncertain without another send', async () => {
    const { task } = await create();
    await db
      .prepare("UPDATE tasks SET status='sending',claimed_at=? WHERE id=?")
      .bind(new Date(Date.now() - 3600000).toISOString(), task.id)
      .run();
    let calls = 0;
    await runDueTasks(engineEnv(), {
      fetcher: async () => {
        calls++;
        return Response.json({ id: 'never' });
      },
    });
    expect(calls).toBe(0);
    expect((await row(task.id)).status).toBe('uncertain');
  });
  it('honors pause for new tasks and existing queued tasks', async () => {
    const { task } = await create();
    await request('/admin/settings', 'POST', { paused: true });
    expect((await request('/tasks', 'POST', payload())).status).toBe(503);
    await due(task.id);
    let calls = 0;
    await runDueTasks(engineEnv(), {
      fetcher: async () => {
        calls++;
        return Response.json({ id: 'never' });
      },
    });
    expect(calls).toBe(0);
    expect((await row(task.id)).status).toBe('queued');
  });
  it('does not submit an expired task', async () => {
    const now = new Date();
    const input = mailSchema.parse(
      payload({
        sendAt: new Date(now.getTime() - 20000).toISOString(),
        expiresAt: new Date(now.getTime() - 10000).toISOString(),
      }),
    );
    const { task } = await createTask(engineEnv(), 'local-demo', input, now);
    let calls = 0;
    await dispatchTask(engineEnv(), task.id, {
      fetcher: async () => {
        calls++;
        return Response.json({ id: 'never' });
      },
    });
    expect(calls).toBe(0);
    expect((await row(task.id)).status).toBe('expired');
  });
  it('escapes user HTML and uses a stable provider idempotency key', async () => {
    const { task } = await create({
      subject: '<script>x</script>',
      body: '<img src=x onerror=alert(1)>',
    });
    await due(task.id);
    let sent: Record<string, unknown> = {};
    let headers: HeadersInit | undefined;
    await dispatchTask(engineEnv(), task.id, {
      fetcher: async (_url, options) => {
        sent = JSON.parse(options!.body as string);
        headers = options!.headers;
        return Response.json({ id: 'escaped-provider' });
      },
    });
    expect(sent.html).toContain('&lt;img');
    expect(sent.html).not.toContain('<script>');
    expect(sent.html).toContain('柿间邮局');
    expect(sent.html).not.toContain('慢邮');
    expect(sent.text).toContain('柿间邮局');
    expect(sent.html).toContain('/unsubscribe/');
    expect(new Headers(headers).get('Idempotency-Key')).toBe('manyou/' + task.id);
    expect(sent.from).toBe('柿间邮局 <letter@send.example.com>');
  });
  it('refuses unconfigured production delivery and production mock mode', async () => {
    const { task } = await create();
    await due(task.id);
    let calls = 0;
    await dispatchTask(
      engineEnv({ ENVIRONMENT: 'production', MAIL_MODE: 'mock', RESEND_API_KEY: undefined }),
      task.id,
      {
        fetcher: async () => {
          calls++;
          return Response.json({ id: 'never' });
        },
      },
    );
    expect(calls).toBe(0);
    expect((await row(task.id)).status).toBe('failed');
  });
});
describe('模板与收件人拒收', () => {
  it('saves templates privately and only permits owner deletion', async () => {
    const saved = (await (
      await request('/templates', 'POST', { name: '问候', subject: '你好', body: '见字如面' })
    ).json()) as { template: { id: string } };
    const other = await otherUser();
    const list = (await (await request('/templates', 'GET', undefined, other)).json()) as {
      templates: unknown[];
    };
    expect(list.templates).toEqual([]);
    expect(
      (await request('/templates/' + saved.template.id, 'DELETE', undefined, other)).status,
    ).toBe(404);
    expect((await request('/templates/' + saved.template.id, 'DELETE')).status).toBe(200);
  });
  it('a GET cannot unsubscribe; confirmation blocks future mail and cancels queued tasks', async () => {
    const { task } = await create();
    const raw = await row(task.id);
    const path = '/unsubscribe/' + raw.unsubscribe_token;
    expect((await request(path, 'GET', undefined, '')).status).toBe(200);
    expect(await db.prepare('SELECT * FROM suppressions').first()).toBeNull();
    expect((await request(path, 'POST', {}, '')).status).toBe(200);
    expect((await row(task.id)).status).toBe('cancelled');
    expect((await request('/tasks', 'POST', payload())).status).toBe(400);
  });
  it('rechecks opt-out immediately before external submission', async () => {
    const { task } = await create();
    await due(task.id);
    await db
      .prepare("INSERT INTO suppressions VALUES(?,'recipient_opt_out',?)")
      .bind(task.recipient, new Date().toISOString())
      .run();
    let calls = 0;
    await dispatchTask(engineEnv(), task.id, {
      fetcher: async () => {
        calls++;
        return Response.json({ id: 'never' });
      },
    });
    expect(calls).toBe(0);
    expect((await row(task.id)).status).toBe('failed');
  });
});

const png =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';
const photo = (): ImageInput => ({
  id: crypto.randomUUID(),
  name: '照片.png',
  contentType: 'image/png',
  content: png,
});
const withImage = (img = photo(), extra: Record<string, unknown> = {}) =>
  payload({ body: '前面的文字\n' + imageMarker(img.id) + '\n后面的文字', images: [img], ...extra });
const imageCount = async (id: string) =>
  (await db
    .prepare('SELECT COUNT(*) AS count FROM task_images WHERE task_id=?')
    .bind(id)
    .first<{ count: number }>())!.count;

describe('正文图片和存储生命周期', () => {
  it('stores originals privately and never returns base64 or hashes in task JSON', async () => {
    const img = photo();
    const input = withImage(img);
    const response = await request('/tasks', 'POST', input);
    expect(response.status).toBe(201);
    const json = (await response.json()) as {
      task: { images: unknown[]; imagesAvailable: boolean };
    };
    expect(json.task.images).toHaveLength(1);
    expect(json.task.imagesAvailable).toBe(true);
    expect(JSON.stringify(json)).not.toContain(png);
    expect(JSON.stringify(json)).not.toContain('sha256');
    const path = `/tasks/${input.id}/images/${img.id}`;
    const image = await request(path);
    expect(image.status).toBe(200);
    expect(image.headers.get('Cache-Control')).toBe('no-store');
    expect(image.headers.get('Content-Type')).toBe('image/png');
    expect(Buffer.from(await image.arrayBuffer()).toString('base64')).toBe(png);
    expect((await request(path, 'GET', undefined, '')).status).toBe(401);
    expect((await request(path, 'GET', undefined, await otherUser())).status).toBe(404);
  });
  it('rejects invalid format, duplicate or missing references and too many images', async () => {
    const img = photo();
    for (const input of [
      withImage({ ...img, contentType: 'image/jpeg' }),
      withImage({ ...img, content: btoa('not a raster') }),
      withImage({ ...img, content: 'broken' }),
      withImage(img, { body: '没有图片位置' }),
      withImage(img, { body: imageMarker(img.id).repeat(2) }),
      payload({ body: imageMarker(img.id) }),
      withImage(img, { images: [img, img, img, img] }),
      withImage({ ...img, name: '../x.png' }),
    ])
      expect((await request('/tasks', 'POST', input)).status).toBe(400);
    expect(await db.prepare('SELECT id FROM tasks').first()).toBeNull();
  });
  it('rejects oversized combined bytes and malicious dimensions', async () => {
    const img = photo();
    const bytes = Buffer.from(png, 'base64');
    const padding = Buffer.alloc(600012);
    padding.writeUInt32BE(600000, 0);
    padding.write('tEXt', 4);
    const oversized = Buffer.concat([
      bytes.subarray(0, -12),
      padding,
      bytes.subarray(-12),
    ]).toString('base64');
    const other = photo();
    expect(
      (await prepareImages(imageMarker(img.id), [{ ...img, content: oversized }]))[0].bytes.length,
    ).toBeGreaterThan(600000);
    await expect(
      createTask(
        engineEnv(),
        'local-demo',
        mailSchema.parse(
          withImage(img, {
            body: imageMarker(img.id) + imageMarker(other.id),
            images: [
              { ...img, content: oversized },
              { ...other, content: oversized },
            ],
          }),
        ),
      ),
    ).rejects.toThrow('INVALID_IMAGES');
    bytes.writeUInt32BE(5000, 16);
    expect(
      (await request('/tasks', 'POST', withImage({ ...img, content: bytes.toString('base64') })))
        .status,
    ).toBe(400);
  });
  it('rolls back the task if an original cannot be stored', async () => {
    await db
      .prepare(
        "CREATE TRIGGER reject_original BEFORE INSERT ON task_images BEGIN SELECT RAISE(ABORT, 'test storage failure'); END",
      )
      .run();
    const input = withImage();
    try {
      expect((await request('/tasks', 'POST', input)).status).toBe(500);
    } finally {
      await db.prepare('DROP TRIGGER reject_original').run();
    }
    expect(await db.prepare('SELECT id FROM tasks WHERE id=?').bind(input.id).first()).toBeNull();
    expect(await imageCount(input.id)).toBe(0);
  });
  it('submits CID bytes in body order and cleans only after durable acceptance', async () => {
    const img = photo();
    const input = withImage(img, { subject: '<script>alert(1)</script>' });
    await request('/tasks', 'POST', input);
    await due(input.id);
    let calls = 0;
    const fetcher: typeof fetch = async (_url, init) => {
      calls++;
      expect(await imageCount(input.id)).toBe(1);
      expect((await row(input.id)).status).toBe('sending');
      const mail = JSON.parse(String(init?.body));
      expect(mail.attachments).toEqual([
        {
          filename: img.name,
          content: png,
          content_type: 'image/png',
          content_id: 'photo-' + img.id,
        },
      ]);
      expect(mail.html).toContain('src="cid:photo-' + img.id + '"');
      expect(mail.html.indexOf('前面的文字')).toBeLessThan(mail.html.indexOf('src="cid:'));
      expect(mail.html.indexOf('后面的文字')).toBeGreaterThan(mail.html.indexOf('src="cid:'));
      expect(mail.html).not.toContain('<script>');
      expect(mail.text).toContain('[照片：照片.png]');
      expect(mail.text).not.toContain('[[image:');
      return Response.json({ id: 'image-provider-id' });
    };
    await Promise.all([
      dispatchTask(engineEnv(), input.id, { fetcher }),
      dispatchTask(engineEnv(), input.id, { fetcher }),
    ]);
    expect(calls).toBe(1);
    expect((await row(input.id)).status).toBe('accepted');
    expect(await imageCount(input.id)).toBe(0);
    expect(JSON.parse((await row(input.id)).images_metadata)).toHaveLength(1);
    expect((await request('/tasks', 'POST', input)).status).toBe(200);
    expect(await imageCount(input.id)).toBe(0);
    expect(
      (await request('/tasks', 'POST', { ...input, images: [{ ...img, name: '改名.png' }] }))
        .status,
    ).toBe(409);
  });
  it('cleans simulated images and cancellation without rehydrating an old task', async () => {
    const immediate = withImage(photo(), { sendAt: undefined });
    const response = await request('/tasks', 'POST', immediate);
    expect(response.status).toBe(201);
    expect(await imageCount(immediate.id)).toBe(0);
    const scheduled = withImage();
    await request('/tasks', 'POST', scheduled);
    const cancel = await request('/tasks/' + scheduled.id + '/cancel', 'POST', {});
    expect(cancel.status).toBe(200);
    expect(await imageCount(scheduled.id)).toBe(0);
    expect(
      ((await cancel.json()) as { task: { imagesAvailable: boolean } }).task.imagesAvailable,
    ).toBe(false);
    await request('/tasks', 'POST', scheduled);
    expect(await imageCount(scheduled.id)).toBe(0);
  });
  it('keeps bytes for uncertainty and definite failure and never auto retries', async () => {
    for (const status of [500, 422]) {
      const input = withImage();
      await request('/tasks', 'POST', input);
      await due(input.id);
      let calls = 0;
      const fetcher: typeof fetch = async () => {
        calls++;
        return new Response('', { status });
      };
      await dispatchTask(engineEnv(), input.id, { fetcher });
      await runDueTasks(engineEnv(), { fetcher });
      expect(calls).toBe(1);
      expect(await imageCount(input.id)).toBe(1);
      expect((await row(input.id)).images_cleaned_at).toBeNull();
    }
  });
  it('keeps originals when the provider accepted but the result write failed', async () => {
    const input = withImage();
    await request('/tasks', 'POST', input);
    await due(input.id);
    const brokenDB = {
      prepare: (sql: string) =>
        sql.startsWith('UPDATE tasks SET status=?,finished_at=')
          ? {
              bind: () => ({
                run: async () => {
                  throw new Error('test write failure');
                },
              }),
            }
          : db.prepare(sql),
    } as unknown as D1Database;
    await expect(
      dispatchTask(engineEnv({ DB: brokenDB }), input.id, {
        fetcher: async () => Response.json({ id: 'accepted' }),
      }),
    ).rejects.toThrow();
    await runDueTasks(engineEnv(), {
      now: new Date(Date.now() + 16 * 60000),
      fetcher: async () => {
        throw new Error('must not submit again');
      },
    });
    expect((await row(input.id)).status).toBe('uncertain');
    expect(await imageCount(input.id)).toBe(1);
  });
  it('retries cleanup during polling, including while paused, without resubmitting', async () => {
    const input = withImage();
    await request('/tasks', 'POST', input);
    await due(input.id);
    const brokenDB = {
      prepare: (sql: string) => db.prepare(sql),
      batch: async () => {
        throw new Error('cleanup write failure');
      },
    } as unknown as D1Database;
    await expect(
      dispatchTask(engineEnv({ DB: brokenDB }), input.id, {
        fetcher: async () => Response.json({ id: 'accepted' }),
      }),
    ).rejects.toThrow();
    expect((await row(input.id)).status).toBe('accepted');
    expect(await imageCount(input.id)).toBe(1);
    await request('/admin/settings', 'POST', { paused: true });
    await runDueTasks(engineEnv(), {
      fetcher: async () => {
        throw new Error('must not submit');
      },
    });
    expect(await imageCount(input.id)).toBe(0);
    expect((await row(input.id)).images_cleaned_at).not.toBeNull();
  });
  it('cleans expired and recipient-cancelled originals', async () => {
    const input = withImage();
    await request('/tasks', 'POST', input);
    await db
      .prepare('UPDATE tasks SET expires_at=?,target_at=? WHERE id=?')
      .bind(
        new Date(Date.now() - 1000).toISOString(),
        new Date(Date.now() - 2000).toISOString(),
        input.id,
      )
      .run();
    await dispatchTask(engineEnv(), input.id, {
      fetcher: async () => {
        throw new Error('must not submit');
      },
    });
    expect((await row(input.id)).status).toBe('expired');
    expect(await imageCount(input.id)).toBe(0);
    const blocked = withImage();
    await request('/tasks', 'POST', blocked);
    await request('/unsubscribe/' + (await row(blocked.id)).unsubscribe_token, 'POST', {}, '');
    expect((await row(blocked.id)).status).toBe('cancelled');
    expect(await imageCount(blocked.id)).toBe(0);
  });
  it('does not send when an original is missing or corrupted', async () => {
    const input = withImage();
    await request('/tasks', 'POST', input);
    await due(input.id);
    await db
      .prepare('UPDATE task_images SET content=? WHERE task_id=?')
      .bind(new Uint8Array([1, 2, 3]).buffer, input.id)
      .run();
    let calls = 0;
    await dispatchTask(engineEnv(), input.id, {
      fetcher: async () => {
        calls++;
        return Response.json({ id: 'never' });
      },
    });
    expect(calls).toBe(0);
    expect((await row(input.id)).status).toBe('failed');
    expect(await imageCount(input.id)).toBe(1);
  });
});

describe('寄件昵称与成员设置', () => {
  it('saves only the current user profile and keeps the fixed address', async () => {
    const other = await otherUser();
    const saved = await request('/profile', 'POST', { senderName: '  小王  ' }, other);
    expect(saved.status).toBe(200);
    expect(await saved.json()).toMatchObject({
      user: { login: 'other', senderName: '小王', role: 'member' },
      senderFrom: '"小王 · 柿间邮局" <letter@send.example.com>',
    });
    const config = await (await request('/config', 'GET', undefined, other)).json();
    expect(config).toMatchObject({
      user: { senderName: '小王' },
      from: '柿间邮局 <letter@send.example.com>',
      senderFrom: '"小王 · 柿间邮局" <letter@send.example.com>',
    });
    expect((await request('/admin/settings', 'POST', { paused: true }, other)).status).toBe(403);
    expect((await request('/admin/invites', 'POST', {}, other)).status).toBe(403);
    expect(await db.prepare("SELECT sender_name FROM users WHERE id='local-demo'").first()).toEqual(
      { sender_name: '' },
    );
    expect((await request('/profile', 'POST', { senderName: '' }, other)).status).toBe(200);
    expect(await (await request('/config', 'GET', undefined, other)).json()).toMatchObject({
      senderFrom: '柿间邮局 <letter@send.example.com>',
    });
  });
  it('rejects account/address overrides, anonymous or inactive users and header injection', async () => {
    expect((await request('/profile', 'POST', { senderName: 'x' }, '')).status).toBe(401);
    expect(
      (await request('/profile', 'POST', { senderName: 'x' }, await otherUser(false))).status,
    ).toBe(403);
    for (const input of [
      { senderName: '昵称', userId: 'other' },
      { senderName: '昵称', role: 'admin' },
      { senderName: '昵称', from: 'x@evil.example' },
      { senderName: '名字\r\nBcc: x@example.com' },
      { senderName: '<x@example.com>' },
      { senderName: '"someone"' },
      { senderName: 'x'.repeat(31) },
      { senderName: 1 },
    ])
      expect((await request('/profile', 'POST', input)).status).toBe(400);
    expect(await db.prepare("SELECT sender_name FROM users WHERE id='local-demo'").first()).toEqual(
      { sender_name: '' },
    );
  });
  it('snapshots identity when creating a task and preserves it across changes and replays', async () => {
    await request('/profile', 'POST', { senderName: '西柿' });
    const input = payload();
    await request('/tasks', 'POST', input);
    await request('/profile', 'POST', { senderName: '新昵称' });
    expect((await request('/tasks', 'POST', input)).status).toBe(200);
    expect((await row(input.id)).sender_from).toBe('"西柿 · 柿间邮局" <letter@send.example.com>');
    await due(input.id);
    await dispatchTask(engineEnv(), input.id, {
      fetcher: async (_url, init) => {
        expect(JSON.parse(String(init?.body)).from).toBe(
          '"西柿 · 柿间邮局" <letter@send.example.com>',
        );
        return Response.json({ id: 'nickname-receipt' });
      },
    });
    const newer = await create();
    expect((await row(newer.task.id)).sender_from).toBe(
      '"新昵称 · 柿间邮局" <letter@send.example.com>',
    );
  });
  it('uses default identity for legacy tasks and RFC quoting for punctuation in names', async () => {
    const old = await create();
    await db.prepare('UPDATE tasks SET sender_from=NULL WHERE id=?').bind(old.task.id).run();
    await request('/profile', 'POST', { senderName: '小王, 小李' });
    await due(old.task.id);
    await dispatchTask(engineEnv(), old.task.id, {
      fetcher: async (_url, init) => {
        expect(JSON.parse(String(init?.body)).from).toBe('柿间邮局 <letter@send.example.com>');
        return Response.json({ id: 'legacy-receipt' });
      },
    });
    const newer = await create();
    expect((await row(newer.task.id)).sender_from).toBe(
      '"小王, 小李 · 柿间邮局" <letter@send.example.com>',
    );
  });
  it('previews an activated member in mock development with no admin privileges', async () => {
    const login = await request('/auth/demo', 'POST', { role: 'member' }, '');
    expect(login.status).toBe(200);
    const member = login.headers.get('Set-Cookie')!.split(';')[0];
    expect(await (await request('/config', 'GET', undefined, member)).json()).toMatchObject({
      user: { id: 'local-member', role: 'member', active: true },
    });
    expect((await request('/tasks', 'POST', payload(), member)).status).toBe(201);
    expect((await request('/admin/invites', 'POST', {}, member)).status).toBe(403);
    const publicLogin = await mf.dispatchFetch('https://email.example.com/api/auth/demo', {
      method: 'POST',
      headers: { Origin: origin, 'Content-Type': 'application/json' },
      body: JSON.stringify({ role: 'admin' }),
    });
    expect(publicLogin.status).toBe(404);
  });
});

async function inactiveMember(id: string) {
  await db
    .prepare(
      "INSERT INTO users(id,github_id,github_login,name,role,active,created_at) VALUES(?,?,?,?,'member',0,?)",
    )
    .bind(id, id, id, '新朋友', new Date().toISOString())
    .run();
  const raw = await digest('test-session-' + id);
  await db
    .prepare('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)')
    .bind(await digest(raw), id, future())
    .run();
  return 'manyou_session=' + raw;
}
describe('多人邀请码、公开名额与并发开通', () => {
  it('only publicly selected usable codes appear to visitors; admins can inspect private balances', async () => {
    const visible = await request('/admin/invites', 'POST', { maxUses: 10, isPublic: true });
    expect(visible.status).toBe(201);
    const pub = (await visible.json()) as { invitation: { id: string }; code: string };
    const privateInvite = (await (
      await request('/admin/invites', 'POST', { maxUses: 2 })
    ).json()) as { code: string };
    const config = (await (await request('/config', 'GET', undefined, '')).json()) as {
      publicInvites: { code: string; remaining: number }[];
    };
    expect(config.publicInvites).toEqual([
      { code: pub.code, remaining: 10, expiresAt: expect.any(String) },
    ]);
    expect(JSON.stringify(config)).not.toContain(privateInvite.code);
    expect((await request('/admin/invites', 'GET', undefined, '')).status).toBe(401);
    expect((await request('/admin/invites', 'GET', undefined, await otherUser())).status).toBe(403);
    const list = (await (await request('/admin/invites')).json()) as {
      invitations: { maxUses: number; usedCount: number; remaining: number }[];
    };
    expect(list.invitations).toHaveLength(2);
    expect(list.invitations.find((i) => i.maxUses === 10)).toMatchObject({
      usedCount: 0,
      remaining: 10,
    });
    await request('/admin/invites/' + pub.invitation.id + '/public', 'POST', { isPublic: false });
    expect(await (await request('/config', 'GET', undefined, '')).json()).toMatchObject({
      publicInvites: [],
    });
    for (const data of [
      { maxUses: 0 },
      { maxUses: 1.5 },
      { maxUses: 1001 },
      { maxUses: 10, role: 'admin' },
    ])
      expect((await request('/admin/invites', 'POST', data)).status).toBe(400);
  });
  it('grants exactly the last available slot under simultaneous redemptions', async () => {
    const created = (await (
      await request('/admin/invites', 'POST', { maxUses: 1, isPublic: true })
    ).json()) as { code: string };
    const cookies = await Promise.all(
      Array.from({ length: 8 }, (_, i) => inactiveMember('claim-' + i)),
    );
    const results = await Promise.all(
      cookies.map((session) => request('/auth/invite', 'POST', { code: created.code }, session)),
    );
    expect(results.filter((r) => r.status === 200)).toHaveLength(1);
    expect(results.filter((r) => r.status === 400)).toHaveLength(7);
    expect(await db.prepare('SELECT used_count,max_uses FROM invitations').first()).toEqual({
      used_count: 1,
      max_uses: 1,
    });
    expect(
      await db.prepare("SELECT SUM(active) AS total FROM users WHERE id LIKE 'claim-%'").first(),
    ).toEqual({ total: 1 });
    const winner = cookies[results.findIndex((r) => r.status === 200)];
    await request('/auth/invite', 'POST', { code: created.code }, winner);
    expect(await db.prepare('SELECT used_count FROM invitations').first()).toEqual({
      used_count: 1,
    });
    expect(await (await request('/config', 'GET', undefined, '')).json()).toMatchObject({
      publicInvites: [],
    });
  });
  it('a user does not consume several codes and revoked or expired invitations cannot activate anyone', async () => {
    const first = (await (
      await request('/admin/invites', 'POST', { maxUses: 10, isPublic: true })
    ).json()) as { code: string; invitation: { id: string } };
    const second = (await (await request('/admin/invites', 'POST', { maxUses: 10 })).json()) as {
      code: string;
      invitation: { id: string };
    };
    const member = await inactiveMember('repeat');
    await Promise.all([
      request('/auth/invite', 'POST', { code: first.code }, member),
      request('/auth/invite', 'POST', { code: second.code }, member),
    ]);
    expect(await db.prepare('SELECT SUM(used_count) AS used FROM invitations').first()).toEqual({
      used: 1,
    });
    await request('/admin/invites/' + first.invitation.id + '/revoke', 'POST', {});
    expect(
      (await request('/auth/invite', 'POST', { code: first.code }, await inactiveMember('revoked')))
        .status,
    ).toBe(400);
    await db
      .prepare("UPDATE invitations SET expires_at='2000-01-01T00:00:00.000Z' WHERE code_hash=?")
      .bind(second.invitation.id)
      .run();
    expect(
      (
        await request(
          '/auth/invite',
          'POST',
          { code: second.code },
          await inactiveMember('expired'),
        )
      ).status,
    ).toBe(400);
    expect(await (await request('/config', 'GET', undefined, '')).json()).toMatchObject({
      publicInvites: [],
    });
    expect((await request('/tasks', 'POST', payload(), member)).status).toBe(201);
  });
  it('rolls back the debit and redemption if activation cannot be written', async () => {
    const invitation = (await (
      await request('/admin/invites', 'POST', { maxUses: 10 })
    ).json()) as { code: string };
    const member = await inactiveMember('rollback');
    await db
      .prepare(
        "CREATE TRIGGER reject_activation BEFORE UPDATE OF active ON users WHEN NEW.id='rollback' BEGIN SELECT RAISE(ABORT, 'test write failure'); END",
      )
      .run();
    try {
      expect(
        (await request('/auth/invite', 'POST', { code: invitation.code }, member)).status,
      ).toBe(500);
    } finally {
      await db.prepare('DROP TRIGGER reject_activation').run();
    }
    expect(await db.prepare('SELECT used_count FROM invitations').first()).toEqual({
      used_count: 0,
    });
    expect(await db.prepare('SELECT * FROM invitation_redemptions').first()).toBeNull();
    expect(await db.prepare("SELECT active FROM users WHERE id='rollback'").first()).toEqual({
      active: 0,
    });
    expect((await request('/auth/invite', 'POST', { code: invitation.code }, member)).status).toBe(
      200,
    );
  });
  it('migrates previously used single-use invitations without reopening them', async () => {
    const legacy = (await mf.getD1Database('LegacyDB')) as unknown as D1Database;
    for (const name of ['0001_initial.sql', '0002_inline_images.sql', '0003_sender_nickname.sql']) {
      for (const statement of (await readFile('migrations/' + name, 'utf8'))
        .split(';')
        .map((s) => s.trim())
        .filter(Boolean))
        await legacy.prepare(statement).run();
    }
    await legacy
      .prepare(
        "INSERT INTO users(id,github_id,github_login,name,role,active,created_at) VALUES('admin','a','a','a','admin',1,'2026-01-01'),('member','b','b','b','member',1,'2026-01-01')",
      )
      .run();
    await legacy
      .prepare(
        "INSERT INTO invitations VALUES('used-code','admin','2026-01-01','2099-01-01','member'),('unused-code','admin','2026-01-01','2099-01-01',NULL)",
      )
      .run();
    const sql = await readFile('migrations/0004_invites_history.sql', 'utf8');
    const triggers: string[] = [];
    const plain = sql.replace(/CREATE TRIGGER[\s\S]*?END;/gi, (trigger) => {
      triggers.push(trigger);
      return '';
    });
    for (const statement of [
      ...plain
        .split(';')
        .map((s) => s.trim())
        .filter(Boolean),
      ...triggers,
    ])
      await legacy.prepare(statement).run();
    expect(
      await legacy
        .prepare(
          "SELECT max_uses,used_count,is_public,code FROM invitations WHERE code_hash='used-code'",
        )
        .first(),
    ).toEqual({ max_uses: 1, used_count: 1, is_public: 0, code: null });
    expect(
      await legacy
        .prepare("SELECT max_uses,used_count FROM invitations WHERE code_hash='unused-code'")
        .first(),
    ).toEqual({ max_uses: 1, used_count: 0 });
    expect(await legacy.prepare('SELECT user_id FROM invitation_redemptions').first()).toEqual({
      user_id: 'member',
    });
  });
});
describe('删除指定历史记录与去重', () => {
  it('removes content and images but retains the task ID and opt-out route', async () => {
    const input = withImage();
    await request('/tasks', 'POST', input);
    await due(input.id);
    await dispatchTask(engineEnv(), input.id, {
      fetcher: async () => new Response('', { status: 422 }),
    });
    const original = await row(input.id);
    expect(await imageCount(input.id)).toBe(1);
    const other = await otherUser();
    expect((await request('/tasks/' + input.id, 'DELETE', {}, other)).status).toBe(404);
    expect((await request('/tasks/' + input.id, 'DELETE', {})).status).toBe(200);
    const removed = await row(input.id);
    expect(removed.subject).toBe('');
    expect(removed.body).toBe('');
    expect(removed.deleted_at).not.toBeNull();
    expect(removed.payload_hash).toBe(original.payload_hash);
    expect(await imageCount(input.id)).toBe(0);
    expect((await request('/tasks/' + input.id)).status).toBe(410);
    expect((await request('/tasks/' + input.id, 'GET', undefined, other)).status).toBe(404);
    expect(await (await request('/tasks')).json()).toEqual({ tasks: [] });
    expect((await request('/tasks', 'POST', input)).status).toBe(410);
    expect(await imageCount(input.id)).toBe(0);
    expect((await request('/tasks/' + input.id, 'DELETE', {})).status).toBe(200);
    expect(
      (await request('/unsubscribe/' + original.unsubscribe_token, 'POST', {}, '')).status,
    ).toBe(200);
    expect(await db.prepare('SELECT recipient FROM suppressions').first()).toEqual({
      recipient: original.recipient,
    });
  });
  it('protects queued, sending and uncertain tasks and still never retries hidden tasks', async () => {
    for (const status of ['queued', 'sending', 'uncertain']) {
      const input = withImage();
      await request('/tasks', 'POST', input);
      await db.prepare('UPDATE tasks SET status=? WHERE id=?').bind(status, input.id).run();
      expect((await request('/tasks/' + input.id, 'DELETE', {})).status).toBe(409);
      expect(await imageCount(input.id)).toBe(1);
      expect((await row(input.id)).deleted_at).toBeNull();
    }
    const input = payload({ sendAt: undefined });
    await request('/tasks', 'POST', input);
    await request('/tasks/' + input.id, 'DELETE', {});
    let calls = 0;
    await dispatchTask(engineEnv(), input.id, {
      fetcher: async () => {
        calls++;
        return Response.json({ id: 'never' });
      },
    });
    expect(calls).toBe(0);
    expect((await request('/tasks/' + input.id, 'DELETE', {}, '')).status).toBe(401);
  });
});
