import { bodyParts, type ImageInfo } from '../shared/images';
import { escapeHtml } from './security';

export function renderLetter(
  subject: string,
  body: string,
  unsubscribe: string,
  images: ImageInfo[] = [],
) {
  const safeSubject = escapeHtml(subject);
  const parts = bodyParts(body);
  const safeBody = parts
    .map((part) => {
      if ('text' in part) return escapeHtml(part.text).replace(/\r\n|\r|\n/g, '<br />');
      const img = images.find((i) => i.id === part.imageId);
      return img
        ? `<img src="cid:photo-${img.id}" alt="${escapeHtml(img.name)}" width="${Math.min(img.width, 544)}" style="display:block;max-width:100%;height:auto;margin:16px 0;border:0;" />`
        : '';
    })
    .join('');
  const textBody = parts
    .map((part) =>
      'text' in part
        ? part.text
        : `[照片：${images.find((i) => i.id === part.imageId)?.name || '图片'}]`,
    )
    .join('');
  const safeUnsubscribe = escapeHtml(unsubscribe);
  return {
    text:
      textBody +
      '\n\n——\n柿间邮局 · 把今天的话，寄给某一天。\n这封信通过柿间邮局寄出。拒收后续来信：' +
      unsubscribe,
    html: `<!doctype html>
<html lang="zh-CN">
<head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width,initial-scale=1.0" /><title>${safeSubject}</title></head>
<body style="margin:0;padding:0;background-color:#f2f4ec;color:#344a3d;font-family:'Microsoft YaHei','PingFang SC',Arial,sans-serif;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#f2f4ec;"><tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:600px;background-color:#fffdf7;border:1px solid #e4e7d9;border-top:4px solid #879b72;">
<tr><td style="padding:30px 28px 24px;border-bottom:1px solid #ecebdd;">
<p style="margin:0 0 8px;font-size:21px;font-weight:bold;letter-spacing:2px;color:#24483e;">柿间邮局</p>
<p style="margin:0 0 14px;font-size:10px;letter-spacing:2px;color:#87917e;">SHIJIAN POST</p>
<p style="margin:0;font-size:12px;line-height:1.8;color:#87917e;">把今天的话，寄给某一天。</p>
</td></tr>
<tr><td style="padding:28px;">
<h1 style="margin:0 0 24px;font-size:22px;line-height:1.6;color:#24483e;overflow-wrap:anywhere;word-break:break-word;">${safeSubject}</h1>
<div style="font-size:15px;line-height:2;color:#344a3d;overflow-wrap:anywhere;word-break:break-word;">${safeBody}</div>
<p style="margin:32px 0 0;text-align:right;font-family:'KaiTi','STKaiti',serif;font-size:22px;letter-spacing:3px;color:#87917e;">见字如面</p>
</td></tr>
<tr><td style="padding:18px 28px 24px;border-top:1px solid #ecebdd;">
<p style="margin:0;font-size:11px;line-height:1.9;color:#87917e;">这封信通过柿间邮局寄出。</p>
<p style="margin:8px 0 0;font-size:11px;line-height:1.9;color:#87917e;">如果不希望收到后续来信，可以<a href="${safeUnsubscribe}" style="color:#637b51;text-decoration:underline;">在这里确认拒收</a>。</p>
</td></tr></table>
</td></tr></table>
</body></html>`,
  };
}
