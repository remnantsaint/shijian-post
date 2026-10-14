import { publicInvites, invitationView, type InvitationRow } from './invitations';
import { senderFrom } from '../shared/sender';
import { cleanupImages } from './images';
import { Hono } from 'hono';
import { bodyLimit } from 'hono/body-limit';
import { secureHeaders } from 'hono/secure-headers';
import { z } from 'zod';
import { mailSchema, templateSchema, profileSchema, invitationSchema } from '../shared/schema';
import { auth } from './auth';
import type { AppEnv, Env, TaskRow, UserRow } from './types';
import { taskView, userView } from './types';
import { appOrigin, currentUser, digest, isLocal, token } from './security';
import { createTask, dispatchTask, mailMode, paused, runDueTasks } from './mail';

export const app = new Hono<AppEnv>();
app.use('/api/*', secureHeaders({ referrerPolicy: 'no-referrer', xFrameOptions: 'DENY' }));
app.use('/api/*', async (c, next) =>
  bodyLimit({
    maxSize: c.req.method === 'POST' && c.req.path === '/api/tasks' ? 1_500_000 : 96 * 1024,
    onError: (ctx) => ctx.json({ error: '内容太长，请减少文字或压缩图片' }, 413),
  })(c, next),
);
app.use('/api/*', async (c, next) => {
  c.header('Cache-Control', 'no-store');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(c.req.method)) {
    if (c.req.header('Origin') !== appOrigin(c.env, c.req.url))
      return c.json({ error: '请求来源不正确，请从网站页面操作' }, 403);
    if (!c.req.header('Content-Type')?.startsWith('application/json'))
      return c.json({ error: '请使用 JSON 请求' }, 415);
  }
  c.set('user', await currentUser(c));
  await next();
});
app.get('/api/config', async (c) =>
  c.json({
    user: c.get('user'),
    demoAvailable: isLocal(c.env, c.req.url) && mailMode(c.env) === 'mock',
    mailMode: mailMode(c.env),
    githubConfigured: Boolean(
      c.env.GITHUB_CLIENT_ID && c.env.GITHUB_CLIENT_SECRET && c.env.ADMIN_GITHUB_LOGIN,
    ),
    from: c.env.MAIL_FROM,
    senderFrom: senderFrom(c.env.MAIL_FROM, c.get('user')?.senderName || ''),
    paused: await paused(c.env),
    publicInvites: await publicInvites(c.env),
  }),
);
app.route('/api/auth', auth);

const tasks = new Hono<AppEnv>();
const templates = new Hono<AppEnv>();
const admin = new Hono<AppEnv>();
const profile = new Hono<AppEnv>();
for (const router of [tasks, templates, admin, profile])
  router.use('*', async (c, next) => {
    const user = c.get('user');
    if (!user) return c.json({ error: '请先登录后再操作' }, 401);
    if (!user.active) return c.json({ error: '请先使用邀请码开通' }, 403);
    await next();
  });
admin.use('*', async (c, next) => {
  if (c.get('user')?.role !== 'admin') return c.json({ error: '仅管理员可操作' }, 403);
  await next();
});
profile.post('/', async (c) => {
  const parsed = profileSchema.safeParse(await c.req.json());
  if (!parsed.success)
    return c.json({ error: parsed.error.issues[0]?.message || '寄件设置不正确' }, 400);
  const updated = await c.env.DB.prepare(
    'UPDATE users SET sender_name=COALESCE(?,sender_name),reply_email=CASE WHEN ? THEN ? ELSE reply_email END WHERE id=? RETURNING *',
  )
    .bind(
      parsed.data.senderName ?? null,
      parsed.data.replyEmail !== undefined ? 1 : 0,
      parsed.data.replyEmail || null,
      c.get('user')!.id,
    )
    .first<UserRow>();
  if (!updated) return c.json({ error: '账号已失效，请重新登录' }, 401);
  return c.json({
    user: userView(updated),
    senderFrom: senderFrom(c.env.MAIL_FROM, updated.sender_name),
  });
});
app.route('/api/profile', profile);

tasks.get('/', async (c) => {
  const result = await c.env.DB.prepare(
    'SELECT * FROM tasks WHERE user_id=? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 100',
  )
    .bind(c.get('user')!.id)
    .all<TaskRow>();
  return c.json({ tasks: result.results.map(taskView) });
});
tasks.get('/:id', async (c) => {
  const task = await c.env.DB.prepare('SELECT * FROM tasks WHERE id=? AND user_id=?')
    .bind(c.req.param('id'), c.get('user')!.id)
    .first<TaskRow>();
  if (task?.deleted_at) return c.json({ error: '这封信的记录已删除，不会再次执行' }, 410);
  return task ? c.json({ task: taskView(task) }) : c.json({ error: '未找到这封信' }, 404);
});
tasks.get('/:id/images/:imageId', async (c) => {
  const row = await c.env.DB.prepare(
    'SELECT i.content,t.images_metadata FROM task_images i JOIN tasks t ON t.id=i.task_id WHERE t.id=? AND t.user_id=? AND i.image_id=?',
  )
    .bind(c.req.param('id'), c.get('user')!.id, c.req.param('imageId'))
    .first<{ content: number[] | ArrayBuffer; images_metadata: string }>();
  if (!row) return c.json({ error: '图片已清理或不存在' }, 404);
  const info = (JSON.parse(row.images_metadata) as { id: string; contentType: string }[]).find(
    (i) => i.id === c.req.param('imageId'),
  );
  if (!info) return c.json({ error: '图片不存在' }, 404);
  c.header('Content-Type', info.contentType);
  c.header('X-Content-Type-Options', 'nosniff');
  return c.body(new Uint8Array(row.content).buffer);
});
tasks.post('/', async (c) => {
  const input = mailSchema.safeParse(await c.req.json());
  if (!input.success)
    return c.json(
      {
        error: '请检查收件地址、标题、正文、图片和发送时间',
        fields: input.error.flatten().fieldErrors,
      },
      400,
    );
  try {
    // Replay inspection precedes configuration checks, but never executes the old task again.
    const old = await c.env.DB.prepare('SELECT * FROM tasks WHERE id=?')
      .bind(input.data.id)
      .first<TaskRow>();
    if (old) {
      const { task } = await createTask(c.env, c.get('user')!.id, input.data);
      return c.json({ task: taskView(task), created: false });
    }
    if (await paused(c.env)) return c.json({ error: '邮局暂时休息，管理员恢复后即可寄信' }, 503);
    if (mailMode(c.env) === 'disabled')
      return c.json({ error: '邮件服务尚未配置，暂时无法寄信' }, 503);
    const { task, created } = await createTask(c.env, c.get('user')!.id, input.data);
    if (created && new Date(task.target_at).getTime() <= Date.now())
      await dispatchTask(c.env, task.id);
    const latest = await c.env.DB.prepare('SELECT * FROM tasks WHERE id=?')
      .bind(task.id)
      .first<TaskRow>();
    return c.json({ task: taskView(latest!), created }, created ? 201 : 200);
  } catch (error) {
    const code = error instanceof Error ? error.message : '';
    if (code === 'TASK_DELETED') return c.json({ error: '这封信的记录已删除，不会再次提交' }, 410);
    if (code === 'ID_CONFLICT')
      return c.json({ error: '这个请求编号已使用，请先检查原任务记录' }, 409);
    if (code === 'INVALID_IMAGES')
      return c.json(
        { error: '图片需要为有效的 PNG/JPEG，每封最多 3 张、合计不超过 1 MB，且应放在正文中' },
        400,
      );
    if (code === 'INVALID_TIME')
      return c.json({ error: '发送时间需在未来两年内，截止时间不能早于发送时间' }, 400);
    if (code === 'SUPPRESSED') return c.json({ error: '该收件人已拒收本网站的来信' }, 400);
    throw error;
  }
});
tasks.post('/:id/cancel', async (c) => {
  const task = await c.env.DB.prepare(
    "UPDATE tasks SET status='cancelled',finished_at=?,reason='寄件人取消' WHERE id=? AND user_id=? AND status='queued' RETURNING *",
  )
    .bind(new Date().toISOString(), c.req.param('id'), c.get('user')!.id)
    .first<TaskRow>();
  if (task) {
    await cleanupImages(c.env, task.id);
    const latest = await c.env.DB.prepare('SELECT * FROM tasks WHERE id=?')
      .bind(task.id)
      .first<TaskRow>();
    return c.json({ task: taskView(latest!) });
  }
  const existing = await c.env.DB.prepare('SELECT status FROM tasks WHERE id=? AND user_id=?')
    .bind(c.req.param('id'), c.get('user')!.id)
    .first<{ status: string }>();
  return existing
    ? c.json({ error: '这封信已经开始处理或已经结束，无法撤回' }, 409)
    : c.json({ error: '未找到这封信' }, 404);
});
tasks.delete('/:id', async (c) => {
  const id = c.req.param('id'),
    userId = c.get('user')!.id;
  const now = new Date().toISOString();
  const [updated] = await c.env.DB.batch([
    c.env.DB.prepare(
      "UPDATE tasks SET deleted_at=?,subject='',body='',images_metadata='[]',images_cleaned_at=?,sender_from=NULL,reply_to=NULL WHERE id=? AND user_id=? AND deleted_at IS NULL AND status IN ('accepted','simulated','failed','cancelled','expired')",
    ).bind(now, now, id, userId),
    c.env.DB.prepare(
      'DELETE FROM task_images WHERE task_id=? AND EXISTS(SELECT 1 FROM tasks WHERE id=? AND user_id=? AND deleted_at IS NOT NULL)',
    ).bind(id, id, userId),
  ]);
  if (updated.meta.changes) return c.json({ ok: true });
  const task = await c.env.DB.prepare('SELECT deleted_at FROM tasks WHERE id=? AND user_id=?')
    .bind(id, userId)
    .first<{ deleted_at: string | null }>();
  if (task?.deleted_at) return c.json({ ok: true });
  return task
    ? c.json({ error: '待寄信请先取消；正在处理或结果待核实的信暂时不能删除' }, 409)
    : c.json({ error: '未找到这封信' }, 404);
});

templates.get('/', async (c) => {
  const rows = await c.env.DB.prepare(
    'SELECT id,name,subject,body FROM templates WHERE user_id=? ORDER BY created_at DESC',
  )
    .bind(c.get('user')!.id)
    .all();
  return c.json({ templates: rows.results });
});
templates.post('/', async (c) => {
  const parsed = templateSchema.safeParse(await c.req.json());
  if (!parsed.success) return c.json({ error: '模板需要名称、标题和正文' }, 400);
  const item = { id: crypto.randomUUID(), ...parsed.data };
  await c.env.DB.prepare(
    'INSERT INTO templates(id,user_id,name,subject,body,created_at) VALUES(?,?,?,?,?,?)',
  )
    .bind(item.id, c.get('user')!.id, item.name, item.subject, item.body, new Date().toISOString())
    .run();
  return c.json({ template: item }, 201);
});
templates.delete('/:id', async (c) => {
  const result = await c.env.DB.prepare('DELETE FROM templates WHERE id=? AND user_id=?')
    .bind(c.req.param('id'), c.get('user')!.id)
    .run();
  return result.meta.changes ? c.json({ ok: true }) : c.json({ error: '未找到这个模板' }, 404);
});
admin.get('/invites', async (c) => {
  const rows = await c.env.DB.prepare(
    'SELECT * FROM invitations ORDER BY created_at DESC LIMIT 100',
  ).all<InvitationRow>();
  return c.json({ invitations: rows.results.map(invitationView) });
});
admin.post('/invites', async (c) => {
  const parsed = invitationSchema.safeParse(await c.req.json());
  if (!parsed.success) return c.json({ error: '开通人数需要为 1 至 1000 的整数' }, 400);
  const code = token(),
    id = await digest(code);
  const createdAt = new Date().toISOString(),
    expiresAt = new Date(Date.now() + 7 * 86400000).toISOString();
  await c.env.DB.prepare(
    'INSERT INTO invitations(code_hash,code,created_by,created_at,expires_at,max_uses,is_public) VALUES(?,?,?,?,?,?,?)',
  )
    .bind(
      id,
      code,
      c.get('user')!.id,
      createdAt,
      expiresAt,
      parsed.data.maxUses,
      parsed.data.isPublic ? 1 : 0,
    )
    .run();
  const row = await c.env.DB.prepare('SELECT * FROM invitations WHERE code_hash=?')
    .bind(id)
    .first<InvitationRow>();
  return c.json({ invitation: invitationView(row!), code, expiresAt }, 201);
});
admin.post('/invites/:id/public', async (c) => {
  const parsed = z
    .object({ isPublic: z.boolean() })
    .strict()
    .safeParse(await c.req.json());
  if (!parsed.success) return c.json({ error: '公开设置格式不正确' }, 400);
  const row = await c.env.DB.prepare(
    'UPDATE invitations SET is_public=? WHERE code_hash=? AND code IS NOT NULL RETURNING *',
  )
    .bind(parsed.data.isPublic ? 1 : 0, c.req.param('id'))
    .first<InvitationRow>();
  return row
    ? c.json({ invitation: invitationView(row) })
    : c.json({ error: '未找到邀请码，或旧邀请码没有保存原文' }, 404);
});
admin.post('/invites/:id/revoke', async (c) => {
  const row = await c.env.DB.prepare(
    'UPDATE invitations SET revoked_at=COALESCE(revoked_at,?),is_public=0 WHERE code_hash=? RETURNING *',
  )
    .bind(new Date().toISOString(), c.req.param('id'))
    .first<InvitationRow>();
  return row ? c.json({ invitation: invitationView(row) }) : c.json({ error: '未找到邀请码' }, 404);
});
admin.post('/settings', async (c) => {
  const data = z
    .object({ paused: z.boolean() })
    .strict()
    .safeParse(await c.req.json());
  if (!data.success) return c.json({ error: '设置格式不正确' }, 400);
  await c.env.DB.prepare('UPDATE settings SET paused=? WHERE id=1')
    .bind(data.data.paused ? 1 : 0)
    .run();
  return c.json({ ok: true, paused: data.data.paused });
});
app.route('/api/tasks', tasks);
app.route('/api/templates', templates);
app.route('/api/admin', admin);
app.get('/api/unsubscribe/:token', async (c) => {
  if (!/^[a-f0-9]{64}$/.test(c.req.param('token'))) return c.json({ error: '链接无效' }, 404);
  const task = await c.env.DB.prepare('SELECT recipient FROM tasks WHERE unsubscribe_token=?')
    .bind(c.req.param('token'))
    .first<{ recipient: string }>();
  if (!task) return c.json({ error: '链接无效' }, 404);
  const [name, domain] = task.recipient.split('@');
  const blocked = Boolean(
    await c.env.DB.prepare('SELECT recipient FROM suppressions WHERE recipient=?')
      .bind(task.recipient)
      .first(),
  );
  return c.json({ recipient: name.slice(0, 1) + '***@' + domain, blocked });
});
app.post('/api/unsubscribe/:token', async (c) => {
  const task = await c.env.DB.prepare('SELECT recipient FROM tasks WHERE unsubscribe_token=?')
    .bind(c.req.param('token'))
    .first<{ recipient: string }>();
  if (!task) return c.json({ error: '链接无效' }, 404);
  await c.env.DB.batch([
    c.env.DB.prepare(
      "INSERT OR IGNORE INTO suppressions(recipient,reason,created_at) VALUES(?,'recipient_opt_out',?)",
    ).bind(task.recipient, new Date().toISOString()),
    c.env.DB.prepare(
      "UPDATE tasks SET status='cancelled',finished_at=?,reason='收件人拒收' WHERE recipient=? AND status='queued'",
    ).bind(new Date().toISOString(), task.recipient),
  ]);
  await cleanupImages(c.env);
  return c.json({ ok: true });
});
app.notFound((c) => c.json({ error: '未找到此接口' }, 404));
app.onError((error, c) => {
  if (error instanceof SyntaxError) return c.json({ error: '请求内容格式不正确' }, 400);
  console.error('Request failed; sensitive details omitted.');
  return c.json({ error: '请求未能完成；若刚刚提交过邮件，请先查看记录，避免重复寄出' }, 500);
});
export default {
  fetch: app.fetch,
  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(runDueTasks(env));
  },
} satisfies ExportedHandler<Env>;
