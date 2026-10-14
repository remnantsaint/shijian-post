import type { Invitation, PublicInvitation } from '../shared/schema';
import type { Env } from './types';
export interface InvitationRow {
  code_hash: string;
  code: string | null;
  created_at: string;
  expires_at: string;
  max_uses: number;
  used_count: number;
  is_public: number;
  revoked_at: string | null;
}
export const invitationView = (row: InvitationRow): Invitation => ({
  id: row.code_hash,
  code: row.code || '',
  createdAt: row.created_at,
  expiresAt: row.expires_at,
  maxUses: row.max_uses,
  usedCount: row.used_count,
  remaining: Math.max(0, row.max_uses - row.used_count),
  isPublic: Boolean(row.is_public),
  revokedAt: row.revoked_at,
  status: row.revoked_at
    ? 'revoked'
    : row.expires_at <= new Date().toISOString()
      ? 'expired'
      : row.used_count >= row.max_uses
        ? 'exhausted'
        : 'available',
});
export async function publicInvites(env: Env): Promise<PublicInvitation[]> {
  const rows = await env.DB.prepare(
    'SELECT code,max_uses-used_count AS remaining,expires_at AS expiresAt FROM invitations WHERE is_public=1 AND code IS NOT NULL AND revoked_at IS NULL AND expires_at>? AND used_count<max_uses ORDER BY created_at DESC LIMIT 5',
  )
    .bind(new Date().toISOString())
    .all<PublicInvitation>();
  return rows.results;
}
