import type { Context } from 'hono';
import { getCookie, setCookie, deleteCookie } from 'hono/cookie';
import type { AppEnv, Env, UserRow } from './types';
import { userView } from './types';
export function token(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)), (n) =>
    n.toString(16).padStart(2, '0'),
  ).join('');
}
export async function digest(value: string): Promise<string> {
  const data = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(data), (n) => n.toString(16).padStart(2, '0')).join('');
}
export function isLocal(env: Env, url: string) {
  return (
    env.ENVIRONMENT === 'development' &&
    ['localhost', '127.0.0.1', '[::1]'].includes(new URL(url).hostname)
  );
}
export function appOrigin(env: Env, url: string) {
  return isLocal(env, url) ? new URL(url).origin : new URL(env.APP_URL).origin;
}
export function cookieOptions(c: Context<AppEnv>) {
  return {
    httpOnly: true,
    secure: new URL(c.req.url).protocol === 'https:',
    sameSite: 'Lax' as const,
    path: '/',
  };
}
export async function createSession(c: Context<AppEnv>, userId: string) {
  const raw = token();
  await c.env.DB.prepare('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)')
    .bind(await digest(raw), userId, new Date(Date.now() + 7 * 86400000).toISOString())
    .run();
  setCookie(c, 'manyou_session', raw, { ...cookieOptions(c), maxAge: 7 * 86400 });
}
export async function currentUser(c: Context<AppEnv>) {
  const raw = getCookie(c, 'manyou_session');
  if (!raw || !/^[a-f0-9]{64}$/.test(raw)) return null;
  const row = await c.env.DB.prepare(
    'SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?',
  )
    .bind(await digest(raw), new Date().toISOString())
    .first<UserRow>();
  return row ? userView(row) : null;
}
export async function logout(c: Context<AppEnv>) {
  const raw = getCookie(c, 'manyou_session');
  if (raw)
    await c.env.DB.prepare('DELETE FROM sessions WHERE token_hash=?')
      .bind(await digest(raw))
      .run();
  deleteCookie(c, 'manyou_session', cookieOptions(c));
}
export function escapeHtml(text: string) {
  return text.replace(
    /[&<>"']/g,
    (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!,
  );
}
