import {
  useCallback,
  useEffect,
  useRef,
  useMemo,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react';
import { z } from 'zod';
import { mailSchema } from '../shared/schema';
import { emptyDraft, type Draft } from './Composer';
export const draftKey = (owner: string) => 'shijian-post:draft:' + owner;
const schema = z
  .object({
    version: z.literal(1),
    owner: z.string().min(1).max(100),
    savedAt: z.iso.datetime(),
    pendingId: z.uuid().nullable(),
    draft: z
      .object({
        recipient: z.string().max(254),
        subject: z.string().max(200),
        body: z.string().max(20000),
        images: mailSchema.shape.images.default([]),
        timing: z.enum(['now', 'schedule']),
        sendAt: z.string().max(32),
      })
      .strict(),
  })
  .strict()
  .refine(
    (r) =>
      !r.pendingId ||
      r.draft.timing !== 'schedule' ||
      !Number.isNaN(Date.parse(r.draft.sendAt + ':00+08:00')),
  );
export type DraftRecord = z.infer<typeof schema>;
type StorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
export const hasDraft = (draft: Draft) =>
  Boolean(draft.recipient || draft.subject || draft.body || draft.images.length || draft.sendAt);
export function readDraft(storage: StorageLike, owner: string): DraftRecord | null {
  const raw = storage.getItem(draftKey(owner));
  if (!raw) return null;
  try {
    const parsed = schema.safeParse(JSON.parse(raw));
    return parsed.success && parsed.data.owner === owner ? parsed.data : null;
  } catch {
    return null;
  }
}
export function writeDraft(storage: StorageLike, record: DraftRecord) {
  if (!schema.safeParse(record).success) throw new Error('草稿内容超过支持的范围');
  if (!hasDraft(record.draft) && !record.pendingId) storage.removeItem(draftKey(record.owner));
  else storage.setItem(draftKey(record.owner), JSON.stringify(record));
}
export const mailPayload = (draft: Draft, id: string) => ({
  id,
  recipient: draft.recipient,
  subject: draft.subject,
  body: draft.body,
  images: draft.images,
  ...(draft.timing === 'schedule'
    ? { sendAt: new Date(draft.sendAt + ':00+08:00').toISOString() }
    : {}),
});
interface State {
  owner: string | null;
  draft: Draft;
  pendingId: string | null;
  restoredPending: string | null;
  revision: number;
  restoreVersion: number;
  dirty: boolean;
  savedAt: string | null;
  error: string;
}
const initial: State = {
  owner: null,
  draft: { ...emptyDraft },
  pendingId: null,
  restoredPending: null,
  revision: 0,
  restoreVersion: 0,
  dirty: false,
  savedAt: null,
  error: '',
};
export function useSavedDraft(owner: string | null) {
  const [state, setState] = useState<State>(initial);
  const current = useRef(state);
  const commit = useCallback((value: State) => {
    current.current = value;
    setState(value);
  }, []);
  const persist = useCallback((value: State): State => {
    if (!value.owner) return value;
    const savedAt = new Date().toISOString();
    try {
      writeDraft(window.localStorage, {
        version: 1,
        owner: value.owner,
        draft: value.draft,
        pendingId: value.pendingId,
        savedAt,
      });
      return { ...value, dirty: false, savedAt, error: '' };
    } catch {
      return { ...value, dirty: true, error: '草稿未能自动保存，请检查浏览器存储或先复制正文。' };
    }
  }, []);
  useEffect(() => {
    if (!owner || current.current.owner === owner) return;
    const previous = persist(current.current);
    let record: DraftRecord | null = null,
      error = '';
    try {
      record = readDraft(window.localStorage, owner);
      if (!record && owner !== 'guest' && previous.owner === 'guest' && hasDraft(previous.draft)) {
        record = {
          version: 1,
          owner,
          draft: previous.draft,
          pendingId: null,
          savedAt: new Date().toISOString(),
        };
        writeDraft(window.localStorage, record);
        window.localStorage.removeItem(draftKey('guest'));
      }
    } catch {
      error = '浏览器暂时无法读取或保存草稿，请检查存储设置。';
    }
    commit({
      owner,
      draft: record?.draft || { ...emptyDraft },
      pendingId: record?.pendingId || null,
      restoredPending: record?.pendingId || null,
      revision: previous.revision + 1,
      restoreVersion: previous.restoreVersion + 1,
      dirty: false,
      savedAt: record?.savedAt || null,
      error,
    });
  }, [owner, persist, commit]);
  useEffect(() => {
    if (!state.dirty || state.owner !== owner) return;
    const revision = state.revision;
    const timer = window.setTimeout(() => {
      if (current.current.revision === revision) commit(persist(current.current));
    }, 500);
    return () => window.clearTimeout(timer);
  }, [state.revision, state.dirty, state.owner, owner, persist, commit]);
  useEffect(() => {
    const flush = () => {
      if (current.current.dirty) commit(persist(current.current));
    };
    const hidden = () => {
      if (document.visibilityState === 'hidden') flush();
    };
    window.addEventListener('pagehide', flush);
    window.addEventListener('beforeunload', flush);
    document.addEventListener('visibilitychange', hidden);
    return () => {
      window.removeEventListener('pagehide', flush);
      window.removeEventListener('beforeunload', flush);
      document.removeEventListener('visibilitychange', hidden);
      if (current.current.dirty) persist(current.current);
    };
  }, [persist, commit]);
  const setDraft: Dispatch<SetStateAction<Draft>> = useCallback(
    (value) => {
      const old = current.current;
      if (!owner || old.owner !== owner) return;
      const draft = typeof value === 'function' ? value(old.draft) : value;
      if (draft !== old.draft)
        commit({ ...old, draft, revision: old.revision + 1, dirty: true, error: '' });
    },
    [owner, commit],
  );
  const setPending = useCallback(
    (id: string | null) => {
      const old = current.current;
      if (!owner || old.owner !== owner) return;
      // Persist the original request ID before the POST; reloads must never create a fresh submission.
      commit(persist({ ...old, pendingId: id, revision: old.revision + 1, dirty: true }));
    },
    [owner, commit, persist],
  );
  const clear = useCallback(() => {
    const old = current.current;
    if (!owner || old.owner !== owner) return;
    commit(
      persist({
        ...old,
        draft: { ...emptyDraft },
        pendingId: null,
        revision: old.revision + 1,
        dirty: true,
      }),
    );
  }, [owner, commit, persist]);
  const useGuest = useCallback(() => {
    const old = current.current;
    if (!owner || owner === 'guest' || old.owner !== owner) return;
    try {
      const guest = readDraft(window.localStorage, 'guest');
      if (!guest) return;
      const saved = persist({
        ...old,
        draft: guest.draft,
        pendingId: null,
        restoredPending: null,
        revision: old.revision + 1,
        restoreVersion: old.restoreVersion + 1,
        dirty: true,
      });
      commit(saved);
      if (!saved.error) window.localStorage.removeItem(draftKey('guest'));
    } catch {
      commit({ ...old, error: '登录前的草稿暂时无法读取，请检查浏览器存储。' });
    }
  }, [owner, persist, commit]);
  const guestAvailable = useMemo(() => {
    try {
      return Boolean(owner && owner !== 'guest' && readDraft(window.localStorage, 'guest'));
    } catch {
      return false;
    }
  }, [owner, state.restoreVersion]);
  const ready = state.owner === owner && owner !== null;
  return {
    guestAvailable,
    useGuest,
    draft: ready ? state.draft : { ...emptyDraft },
    setDraft,
    setPending,
    clear,
    ready,
    restoreVersion: state.restoreVersion,
    restoredPending: state.restoredPending,
    error: state.error,
    status: !ready
      ? '正在读取草稿…'
      : state.error
        ? '草稿未保存'
        : state.dirty
          ? '正在保存草稿…'
          : state.savedAt && hasDraft(state.draft)
            ? '草稿已自动保存'
            : '草稿自动保存在此浏览器',
  };
}
