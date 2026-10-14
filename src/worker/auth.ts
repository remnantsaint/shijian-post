import { z } from 'zod';
import { Hono } from 'hono';
import { getCookie, setCookie, deleteCookie } from 'hono/cookie';
import type { AppEnv, UserRow } from './types';
import {
  appOrigin,
  cookieOptions,
  createSession,
  digest,
  isLocal,
  logout,
  token,
} from './security';

export const auth = new Hono<AppEnv>();
auth.get('/github', async (c) => {
  if (!c.env.GITHUB_CLIENT_ID || !c.env.GITHUB_CLIENT_SECRET || !c.env.ADMIN_GITHUB_LOGIN)
    return c.redirect('/?authError=setup');
  const state = token();
  await c.env.DB.prepare('INSERT INTO oauth_states(state_hash,expires_at) VALUES(?,?)')
    .bind(await digest(state), new Date(Date.now() + 600000).toISOString())
    .run();
  setCookie(c, 'manyou_oauth', state, { ...cookieOptions(c), maxAge: 600 });
  const url = new URL('https://github.com/login/oauth/authorize');
  url.searchParams.set('client_id', c.env.GITHUB_CLIENT_ID);
  url.searchParams.set('redirect_uri', appOrigin(c.env, c.req.url) + '/api/auth/github/callback');
  url.searchParams.set('scope', 'read:user');
  url.searchParams.set('state', state);
  return c.redirect(url.href);
});
auth.get('/github/callback', async (c) => {
  const state = c.req.query('state'),
    code = c.req.query('code');
  if (
    !state ||
    !/^[a-f0-9]{64}$/.test(state) ||
    state !== getCookie(c, 'manyou_oauth') ||
    !code ||
    code.length > 512
  )
    return c.redirect('/?authError=state');
  deleteCookie(c, 'manyou_oauth', cookieOptions(c));
  const consumed = await c.env.DB.prepare(
    'DELETE FROM oauth_states WHERE state_hash=? AND expires_at>? RETURNING state_hash',
  )
    .bind(await digest(state), new Date().toISOString())
    .first();
  if (!consumed || !c.env.GITHUB_CLIENT_ID || !c.env.GITHUB_CLIENT_SECRET)
    return c.redirect('/?authError=state');
  try {
    const response = await fetch('https://github.com/login/oauth/access_token', {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(10000),
      body: JSON.stringify({
        client_id: c.env.GITHUB_CLIENT_ID,
        client_secret: c.env.GITHUB_CLIENT_SECRET,
        code,
        redirect_uri: appOrigin(c.env, c.req.url) + '/api/auth/github/callback',
      }),
    });
    const credentials = (await response.json()) as { access_token?: string };
    if (!response.ok || !credentials.access_token) throw new Error('OAUTH');
    const profileResponse = await fetch('https://api.github.com/user', {
      headers: {
        Authorization: 'Bearer ' + credentials.access_token,
        'User-Agent': 'manyou-mail',
        Accept: 'application/vnd.github+json',
      },
      signal: AbortSignal.timeout(10000),
    });
    const profile = (await profileResponse.json()) as {
      id: number;
      login: string;
      name?: string;
      email?: string;
      avatar_url?: string;
    };
    if (
      !profileResponse.ok ||
      !Number.isSafeInteger(profile.id) ||
      !/^[a-z0-9-]{1,39}$/i.test(profile.login)
    )
      throw new Error('PROFILE');
    let user = await c.env.DB.prepare('SELECT * FROM users WHERE github_id=?')
      .bind(String(profile.id))
      .first<UserRow>();
    if (!user) {
      const hasAdmin = await c.env.DB.prepare(
        "SELECT id FROM users WHERE role='admin' LIMIT 1",
      ).first();
      const admin =
        !hasAdmin && profile.login.toLowerCase() === c.env.ADMIN_GITHUB_LOGIN.toLowerCase();
      const id = crypto.randomUUID();
      await c.env.DB.prepare(
        'INSERT OR IGNORE INTO users(id,github_id,github_login,name,email,avatar_url,role,active,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
      )
        .bind(
          id,
          String(profile.id),
          profile.login,
          (profile.name || profile.login).slice(0, 80),
          profile.email || null,
          profile.avatar_url || null,
          admin ? 'admin' : 'member',
          admin ? 1 : 0,
          new Date().toISOString(),
        )
        .run();
      user = await c.env.DB.prepare('SELECT * FROM users WHERE github_id=?')
        .bind(String(profile.id))
        .first<UserRow>();
    } else {
      await c.env.DB.prepare('UPDATE users SET github_login=?,name=? WHERE id=?')
        .bind(profile.login, (profile.name || profile.login).slice(0, 80), user.id)
        .run();
    }
    if (!user) throw new Error('USER');
    await createSession(c, user.id);
    return c.redirect('/');
  } catch {
    return c.redirect('/?authError=provider');
  }
});
auth.post('/logout', async (c) => {
  await logout(c);
  return c.json({ ok: true });
});
auth.post('/demo', async (c) => {
  if (!isLocal(c.env, c.req.url) || c.env.MAIL_MODE !== 'mock')
    return c.json({ error: '未找到此入口' }, 404);
  const parsed = z
    .object({ role: z.enum(['admin', 'member']).optional() })
    .strict()
    .safeParse(await c.req.json());
  if (!parsed.success) return c.json({ error: '预览身份格式不正确' }, 400);
  const role = parsed.data.role || 'admin';
  const id = role === 'admin' ? 'local-demo' : 'local-member';
  await c.env.DB.prepare(
    'INSERT OR IGNORE INTO users(id,github_id,github_login,name,role,active,created_at) VALUES(?,?,?,?,?,1,?)',
  )
    .bind(id, id, id, role === 'admin' ? '体验访客' : '寄信人预览', role, new Date().toISOString())
    .run();
  await createSession(c, id);
  return c.json({ ok: true });
});
auth.post('/invite', async (c) => {
  const user = c.get('user');
  if (!user) return c.json({ error: '请先登录' }, 401);
  const input = await c.req.json<{ code?: unknown }>();
  if (typeof input.code !== 'string' || !/^[a-f0-9]{64}$/.test(input.code))
    return c.json({ error: '邀请码格式不正确' }, 400);
  if (user.active) return c.json({ ok: true });
  const codeHash = await digest(input.code);
  const now = new Date().toISOString();
  // One SQL statement plus its trigger atomically debits a slot and activates the account.
  await c.env.DB.prepare(
    'INSERT OR IGNORE INTO invitation_redemptions(code_hash,user_id,created_at) SELECT code_hash,?,? FROM invitations WHERE code_hash=? AND expires_at>? AND revoked_at IS NULL AND used_count<max_uses AND EXISTS(SELECT 1 FROM users WHERE id=? AND active=0) AND NOT EXISTS(SELECT 1 FROM invitation_redemptions WHERE user_id=?)',
  )
    .bind(user.id, now, codeHash, now, user.id, user.id)
    .run();
  const active = await c.env.DB.prepare('SELECT active FROM users WHERE id=?')
    .bind(user.id)
    .first<{ active: number }>();
  if (!active?.active) return c.json({ error: '邀请码已过期、已停用或开通名额已用完' }, 400);
  return c.json({ ok: true });
});
