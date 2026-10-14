import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, post } from '../src/web/api';
afterEach(() => vi.unstubAllGlobals());
describe('请求重试边界', () => {
  it('may recover a read-only query after one network failure', async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(Response.json({ ok: true }));
    vi.stubGlobal('fetch', fetcher);
    expect(await api('/config')).toEqual({ ok: true });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it('may retry a GET after an internal service error', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response('', { status: 500 }))
      .mockResolvedValueOnce(Response.json({ ok: true }));
    vi.stubGlobal('fetch', fetcher);
    expect(await api('/config')).toEqual({ ok: true });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it('never automatically retries a mail submission on disconnect', async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError('offline'));
    vi.stubGlobal('fetch', fetcher);
    await expect(post('/tasks', { id: 'stable' })).rejects.toMatchObject({ status: 0 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it('never automatically retries a mail submission on HTTP 500', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(Response.json({ error: 'result unknown' }, { status: 500 }));
    vi.stubGlobal('fetch', fetcher);
    await expect(post('/tasks', { id: 'stable' })).rejects.toMatchObject({ status: 500 });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
