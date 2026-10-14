import { z } from 'zod';
import { MAX_IMAGES, MAX_IMAGE_BYTES, type ImageInfo } from './images';
export const mailSchema = z
  .object({
    id: z.uuid(),
    recipient: z
      .email()
      .max(254)
      .transform((v) => v.trim().toLowerCase()),
    subject: z
      .string()
      .trim()
      .min(1, '请写一个标题')
      .max(200)
      .refine((v) => !/[\r\n\x00-\x1f\x7f]/.test(v), '标题只能有一行'),
    body: z
      .string()
      .min(1)
      .max(20000)
      .refine((v) => v.trim().length > 0 && !v.includes('\0'), '正文不能为空或包含空字符'),
    images: z
      .array(
        z
          .object({
            id: z.uuid(),
            name: z
              .string()
              .min(1)
              .max(100)
              .refine((v) => !/[\x00-\x1f\x7f/\\]/.test(v)),
            contentType: z.enum(['image/jpeg', 'image/png']),
            content: z
              .string()
              .min(4)
              .max(Math.ceil(MAX_IMAGE_BYTES / 3) * 4)
              .regex(/^[A-Za-z0-9+/]*={0,2}$/)
              .refine((v) => v.length % 4 === 0),
          })
          .strict(),
      )
      .max(MAX_IMAGES)
      .optional(),
    sendAt: z.iso.datetime({ offset: true }).optional(),
    expiresAt: z.iso.datetime({ offset: true }).optional(),
  })
  .strict();
export const templateSchema = z
  .object({
    name: z.string().trim().min(1).max(40),
    subject: mailSchema.shape.subject,
    body: mailSchema.shape.body,
  })
  .strict();
export const profileSchema = z
  .object({
    senderName: z
      .string()
      .trim()
      .max(30)
      .refine((v) => !/[\x00-\x1f\x7f<>"\\]/.test(v), '昵称不能包含换行或邮件地址符号'),
  })
  .strict();
export type MailInput = z.infer<typeof mailSchema>;
export type Status =
  | 'queued'
  | 'sending'
  | 'accepted'
  | 'simulated'
  | 'failed'
  | 'uncertain'
  | 'cancelled'
  | 'expired';
export interface User {
  senderName: string;
  id: string;
  login: string;
  name: string;
  role: 'admin' | 'member';
  active: boolean;
}
export interface MailTask {
  senderFrom: string | null;
  images: ImageInfo[];
  imagesAvailable: boolean;
  id: string;
  recipient: string;
  subject: string;
  body: string;
  status: Status;
  targetAt: string;
  createdAt: string;
  finishedAt: string | null;
  reason: string | null;
  providerId: string | null;
}
export interface Template {
  id: string;
  name: string;
  subject: string;
  body: string;
}
export interface SiteConfig {
  senderFrom: string;
  user: User | null;
  demoAvailable: boolean;
  mailMode: 'mock' | 'resend' | 'disabled';
  githubConfigured: boolean;
  from: string;
  paused: boolean;
}
export const statusLabels: Record<Status, string> = {
  queued: '等待寄出',
  sending: '正在提交',
  accepted: '服务已接受',
  simulated: '预览完成',
  failed: '发送失败',
  uncertain: '结果待核实',
  cancelled: '已取消',
  expired: '已过期',
};
