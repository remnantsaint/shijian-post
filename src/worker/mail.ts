import { senderFrom } from '../shared/sender';
import { prepareImages, loadImages, cleanupImages } from './images';
import type { MailInput } from '../shared/schema';
import { digest, token } from './security';
import { renderLetter } from './mail-template';
import type { Env, TaskRow } from './types';

export function mailMode(env: Env): 'mock' | 'resend' | 'disabled' {
  if (env.MAIL_MODE === 'mock' && ['development', 'test'].includes(env.ENVIRONMENT)) return 'mock';
  if (
    env.MAIL_MODE === 'resend' &&
    env.RESEND_API_KEY &&
    env.MAIL_FROM &&
    !/[\r\n]/.test(env.MAIL_FROM)
  )
    return 'resend';
  return 'disabled';
}
export async function paused(env: Env) {
  return Boolean(
    (await env.DB.prepare('SELECT paused FROM settings WHERE id=1').first<{ paused: number }>())
      ?.paused,
  );
}
export async function createTask(env: Env, userId: string, input: MailInput, now = new Date()) {
  const images = await prepareImages(input.body, input.images);
  const metadata = images.map((img) => img.info);
  const hash = await digest(
    JSON.stringify({
      recipient: input.recipient,
      subject: input.subject,
      body: input.body,
      sendAt: input.sendAt || null,
      expiresAt: input.expiresAt || null,
      ...(metadata.length ? { images: metadata } : {}),
    }),
  );
  const old = await env.DB.prepare('SELECT * FROM tasks WHERE id=?')
    .bind(input.id)
    .first<TaskRow>();
  if (old) {
    if (old.user_id !== userId || old.payload_hash !== hash) throw new Error('ID_CONFLICT');
    if (old.deleted_at) throw new Error('TASK_DELETED');
    return { task: old, created: false };
  }
  const target = input.sendAt ? new Date(input.sendAt) : now;
  if (
    input.sendAt &&
    (target.getTime() < now.getTime() - 30000 || target.getTime() > now.getTime() + 731 * 86400000)
  )
    throw new Error('INVALID_TIME');
  if (input.expiresAt && new Date(input.expiresAt) < target) throw new Error('INVALID_TIME');
  if (
    await env.DB.prepare('SELECT recipient FROM suppressions WHERE recipient=?')
      .bind(input.recipient)
      .first()
  )
    throw new Error('SUPPRESSED');
  const identity = await env.DB.prepare('SELECT sender_name,reply_email FROM users WHERE id=?')
    .bind(userId)
    .first<{ sender_name: string; reply_email: string | null }>();
  const from = senderFrom(env.MAIL_FROM, identity?.sender_name || '');
  const insert = env.DB.prepare(
    "INSERT OR IGNORE INTO tasks(id,user_id,recipient,subject,body,payload_hash,status,target_at,expires_at,created_at,unsubscribe_token,images_metadata,sender_from,reply_to) VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)",
  ).bind(
    input.id,
    userId,
    input.recipient,
    input.subject,
    input.body,
    hash,
    target.toISOString(),
    input.expiresAt ? new Date(input.expiresAt).toISOString() : null,
    now.toISOString(),
    token(),
    JSON.stringify(metadata),
    from,
    identity?.reply_email || null,
  );
  // The task and all originals are saved together, including on concurrent replays.
  const [result] = await env.DB.batch([
    insert,
    ...images.map((img) =>
      env.DB.prepare(
        "INSERT OR IGNORE INTO task_images(task_id,image_id,content) SELECT id,?,? FROM tasks WHERE id=? AND user_id=? AND payload_hash=? AND status='queued' AND images_cleaned_at IS NULL",
      ).bind(img.info.id, img.bytes.buffer, input.id, userId, hash),
    ),
  ]);
  const task = await env.DB.prepare('SELECT * FROM tasks WHERE id=?')
    .bind(input.id)
    .first<TaskRow>();
  if (!task || task.user_id !== userId || task.payload_hash !== hash)
    throw new Error('ID_CONFLICT');
  return { task, created: result.meta.changes === 1 };
}

export async function dispatchTask(
  env: Env,
  id: string,
  options: { now?: Date; fetcher?: typeof fetch } = {},
) {
  const now = (options.now || new Date()).toISOString();
  if (await paused(env)) return;
  await env.DB.prepare(
    "UPDATE tasks SET status='expired',finished_at=?,reason='超过最晚发送时间' WHERE id=? AND status='queued' AND expires_at IS NOT NULL AND expires_at<?",
  )
    .bind(now, id, now)
    .run();
  const task = await env.DB.prepare(
    "UPDATE tasks SET status='sending',claimed_at=? WHERE id=? AND status='queued' AND deleted_at IS NULL AND target_at<=? RETURNING *",
  )
    .bind(now, id, now)
    .first<TaskRow>();
  if (!task) {
    await cleanupImages(env, id);
    return;
  }
  let status: 'accepted' | 'simulated' | 'failed' | 'uncertain' = 'failed';
  let reason: string | null = null;
  let providerId: string | null = null;
  // All definite preflight failures happen before handing any content to Resend.
  if (
    await env.DB.prepare('SELECT recipient FROM suppressions WHERE recipient=?')
      .bind(task.recipient)
      .first()
  )
    reason = '收件人已拒收后续来信';
  else if (await paused(env)) {
    await env.DB.prepare(
      "UPDATE tasks SET status='queued',claimed_at=NULL WHERE id=? AND status='sending'",
    )
      .bind(id)
      .run();
    return;
  } else if (mailMode(env) === 'disabled') reason = '邮件服务尚未配置或已关闭';
  else if (mailMode(env) === 'mock') {
    try {
      await loadImages(env, task);
    } catch {
      await env.DB.prepare(
        "UPDATE tasks SET status='failed',finished_at=?,reason='图片原图缺失或损坏，未提交邮件' WHERE id=? AND status='sending'",
      )
        .bind(now, id)
        .run();
      return;
    }
    status = 'simulated';
    reason = '本地模拟，未向真实邮箱投递';
  } else {
    const unsubscribe = new URL('/unsubscribe/' + task.unsubscribe_token, env.APP_URL).href;
    let images: Awaited<ReturnType<typeof loadImages>>;
    try {
      images = await loadImages(env, task);
    } catch {
      await env.DB.prepare(
        "UPDATE tasks SET status='failed',finished_at=?,reason='图片原图缺失或损坏，未提交邮件' WHERE id=? AND status='sending'",
      )
        .bind(now, id)
        .run();
      return;
    }
    const letter = renderLetter(task.subject, task.body, unsubscribe, images);
    try {
      const response = await (options.fetcher || fetch)('https://api.resend.com/emails', {
        method: 'POST',
        headers: {
          Authorization: 'Bearer ' + env.RESEND_API_KEY,
          'Content-Type': 'application/json',
          'Idempotency-Key': 'manyou/' + task.id,
        },
        signal: AbortSignal.timeout(15000),
        body: JSON.stringify({
          from: task.sender_from || env.MAIL_FROM,
          ...(task.reply_to ? { reply_to: task.reply_to } : {}),
          to: [task.recipient],
          subject: task.subject,
          text: letter.text,
          html: letter.html,
          ...(images.length
            ? {
                attachments: images.map((img) => ({
                  filename: img.name,
                  content: img.content,
                  content_type: img.contentType,
                  content_id: 'photo-' + img.id,
                })),
              }
            : {}),
        }),
      });
      if (response.ok) {
        const receipt = (await response.json()) as { id?: unknown };
        if (typeof receipt.id !== 'string' || !receipt.id || receipt.id.length > 200)
          throw new Error('RECEIPT_MISSING');
        status = 'accepted';
        providerId = receipt.id;
      } else if (response.status >= 400 && response.status < 500 && response.status !== 408) {
        status = 'failed';
        reason = '邮件服务拒绝本次请求（HTTP ' + response.status + '），未自动重发';
      } else {
        status = 'uncertain';
        reason = '邮件服务结果未获确认，请核对收件箱与服务记录；不会自动重发';
      }
    } catch {
      status = 'uncertain';
      reason = '提交期间连接中断或超时，结果待核实；不会自动重发';
    }
  }
  // A persistence failure leaves the claim in place, never queues another submission.
  await env.DB.prepare(
    "UPDATE tasks SET status=?,finished_at=?,provider_id=?,reason=? WHERE id=? AND status='sending'",
  )
    .bind(status, new Date().toISOString(), providerId, reason, id)
    .run();
  await cleanupImages(env, id);
}
export async function runDueTasks(env: Env, options: { now?: Date; fetcher?: typeof fetch } = {}) {
  const now = options.now || new Date();
  await env.DB.prepare(
    "UPDATE tasks SET status='uncertain',finished_at=?,reason='执行中断，发送结果待核实；不会自动重发' WHERE status='sending' AND claimed_at<?",
  )
    .bind(now.toISOString(), new Date(now.getTime() - 15 * 60000).toISOString())
    .run();
  await env.DB.batch([
    env.DB.prepare('DELETE FROM sessions WHERE expires_at<?').bind(now.toISOString()),
    env.DB.prepare('DELETE FROM oauth_states WHERE expires_at<?').bind(now.toISOString()),
  ]);
  await cleanupImages(env);
  if (await paused(env)) return;
  const due = await env.DB.prepare(
    "SELECT id FROM tasks WHERE status='queued' AND deleted_at IS NULL AND target_at<=? ORDER BY target_at LIMIT 10",
  )
    .bind(now.toISOString())
    .all<{ id: string }>();
  for (const task of due.results) {
    try {
      await dispatchTask(env, task.id, options);
    } catch {
      console.error('Task execution or result persistence failed; claim retained.');
    }
  }
}
