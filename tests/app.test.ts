import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { build } from 'esbuild';
import { readFile } from 'node:fs/promises';
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
  });
  mf = new Miniflare(
    convertV4MiniflareOptions({
      modules: true,
      script: compiled.outputFiles[0].text,
      compatibilityDate: '2026-10-13',
      compatibilityFlags: ['nodejs_compat'],
      d1Databases: ['DB'],
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
  const sql = await readFile('migrations/0001_initial.sql', 'utf8');
  for (const statement of sql
    .split(';')
    .map((s) => s.trim())
    .filter(Boolean))
    await db.prepare(statement).run();
}, 60000);
beforeEach(async () => {
  for (const table of [
    'tasks',
    'templates',
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
    const invite = (await (await request('/admin/invites', 'POST', {})).json()) as { code: string };
    const other = await otherUser(false);
    expect((await request('/tasks', 'POST', payload(), other)).status).toBe(403);
    expect((await request('/auth/invite', 'POST', { code: invite.code }, other)).status).toBe(200);
    expect((await request('/tasks', 'POST', payload(), other)).status).toBe(201);
    expect((await request('/auth/invite', 'POST', { code: invite.code })).status).toBe(400);
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
