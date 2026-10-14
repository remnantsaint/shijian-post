import type { ImageInfo } from '../shared/images';
import type { Status, User } from '../shared/schema';
export interface Env {
  DB: D1Database;
  APP_URL: string;
  ENVIRONMENT: string;
  MAIL_MODE: string;
  MAIL_FROM: string;
  ADMIN_GITHUB_LOGIN: string;
  GITHUB_CLIENT_ID?: string;
  GITHUB_CLIENT_SECRET?: string;
  RESEND_API_KEY?: string;
  RESEND_WEBHOOK_SECRET?: string;
}
export type AppEnv = { Bindings: Env; Variables: { user: User | null } };
export interface TaskRow {
  sender_from: string | null;
  id: string;
  user_id: string;
  recipient: string;
  subject: string;
  body: string;
  payload_hash: string;
  status: Status;
  target_at: string;
  expires_at: string | null;
  created_at: string;
  claimed_at: string | null;
  finished_at: string | null;
  provider_id: string | null;
  reason: string | null;
  unsubscribe_token: string;
  images_metadata: string;
  images_cleaned_at: string | null;
}
export interface UserRow {
  sender_name: string;
  id: string;
  github_id: string;
  github_login: string;
  name: string;
  email: string | null;
  avatar_url: string | null;
  role: 'admin' | 'member';
  active: number;
  created_at: string;
}
export const userView = (row: UserRow): User => ({
  senderName: row.sender_name || '',
  id: row.id,
  login: row.github_login,
  name: row.name,
  role: row.role,
  active: Boolean(row.active),
});
export const taskView = (row: TaskRow) => ({
  senderFrom: row.sender_from || null,
  images: (JSON.parse(row.images_metadata || '[]') as ImageInfo[]).map(
    ({ id, name, contentType, size, width, height }) => ({
      id,
      name,
      contentType,
      size,
      width,
      height,
    }),
  ),
  imagesAvailable: Boolean(
    row.images_metadata && row.images_metadata !== '[]' && !row.images_cleaned_at,
  ),
  id: row.id,
  recipient: row.recipient,
  subject: row.subject,
  body: row.body,
  status: row.status,
  targetAt: row.target_at,
  createdAt: row.created_at,
  finishedAt: row.finished_at,
  reason: row.reason,
  providerId: row.provider_id,
});
