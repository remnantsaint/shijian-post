import { describe, expect, it } from 'vitest';
import {
  readDraft,
  writeDraft,
  draftKey,
  mailPayload,
  type DraftRecord,
} from '../src/web/draft-store';
import { emptyDraft } from '../src/web/Composer';
function storage() {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value);
    },
    removeItem: (key: string) => {
      data.delete(key);
    },
  };
}
const record = (owner: string): DraftRecord => ({
  version: 1,
  owner,
  savedAt: new Date().toISOString(),
  pendingId: null,
  draft: {
    ...emptyDraft,
    recipient: 'friend@example.com',
    subject: '草稿',
    body: '保留我的文字。',
  },
});
describe('浏览器草稿与请求编号', () => {
  it('isolates users and restores text, photo bytes and an in-flight request ID', () => {
    const db = storage();
    const draft = record('user-a');
    draft.pendingId = crypto.randomUUID();
    draft.draft.images = [
      { id: crypto.randomUUID(), name: '照片.png', contentType: 'image/png', content: 'aGVsbG8=' },
    ];
    writeDraft(db, draft);
    writeDraft(db, { ...record('user-b'), draft: { ...emptyDraft, subject: '另一个账号' } });
    expect(readDraft(db, 'user-a')).toEqual(draft);
    expect(readDraft(db, 'guest')).toBeNull();
    expect(readDraft(db, 'user-b')?.draft.subject).toBe('另一个账号');
    expect(mailPayload(draft.draft, draft.pendingId).id).toBe(draft.pendingId);
  });
  it('clears only the selected account draft and rejects malformed or mismatched storage', () => {
    const db = storage();
    writeDraft(db, record('user-a'));
    writeDraft(db, record('user-b'));
    writeDraft(db, { ...record('user-a'), draft: { ...emptyDraft } });
    expect(readDraft(db, 'user-a')).toBeNull();
    expect(readDraft(db, 'user-b')).not.toBeNull();
    for (const raw of [
      'not-json',
      JSON.stringify({ ...record('user-a'), owner: 'user-b' }),
      JSON.stringify({ ...record('user-a'), draft: { ...emptyDraft, body: 'x'.repeat(20001) } }),
    ]) {
      db.setItem(draftKey('user-a'), raw);
      expect(readDraft(db, 'user-a')).toBeNull();
    }
  });
  it('does not silently report saved when browser storage is full or unavailable', () => {
    const db = {
      ...storage(),
      setItem: () => {
        throw new DOMException('Quota exceeded', 'QuotaExceededError');
      },
    };
    expect(() => writeDraft(db, record('user-a'))).toThrow('Quota exceeded');
  });
});
