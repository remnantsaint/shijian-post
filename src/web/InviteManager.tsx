import { useCallback, useEffect, useState } from 'react';
import { Copy, RefreshCw } from 'lucide-react';
import type { Invitation } from '../shared/schema';
import { api, post } from './api';
import { formatTime, Modal } from './components';
const labels: Record<Invitation['status'], string> = {
  available: '可使用',
  exhausted: '名额已用完',
  expired: '已过期',
  revoked: '已停用',
};
export default function InviteManager() {
  const [items, setItems] = useState<Invitation[]>([]);
  const [maxUses, setMaxUses] = useState(10);
  const [isPublic, setIsPublic] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [created, setCreated] = useState<Invitation | null>(null);
  const [copied, setCopied] = useState('');
  const refresh = useCallback(async () => {
    try {
      setItems((await api<{ invitations: Invitation[] }>('/admin/invites')).invitations);
      setError('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (!document.hidden) void refresh();
    }, 30000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  const copy = async (item: Invitation) => {
    try {
      await navigator.clipboard.writeText(item.code);
      setCopied(item.id);
    } catch {
      setError('复制未完成，请选中邀请码手动复制');
    }
  };
  const update = async (item: Invitation, action: 'public' | 'revoke') => {
    if (busy) return;
    if (action === 'revoke' && !confirm('停用这个邀请码？已开通的用户仍可使用邮局。')) return;
    setBusy(true);
    try {
      await post(
        '/admin/invites/' + item.id + '/' + action,
        action === 'public' ? { isPublic: !item.isPublic } : {},
      );
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="invite-manager">
      <form
        className="invite-create"
        onSubmit={async (e) => {
          e.preventDefault();
          if (busy) return;
          setBusy(true);
          try {
            const result = await post<{ invitation: Invitation }>('/admin/invites', {
              maxUses,
              isPublic,
            });
            setCreated(result.invitation);
            await refresh();
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <div>
          <h3>邀请朋友来寄信</h3>
          <p>每个账号首次开通占用 1 个名额，之后登录不扣次数。邀请码有效期七天。</p>
        </div>
        <label className="invite-capacity">
          可开通人数
          <input
            type="number"
            min={1}
            max={1000}
            required
            value={maxUses}
            disabled={busy}
            onChange={(e) => setMaxUses(Number(e.target.value))}
          />
        </label>
        <label className="invite-public-option">
          <input
            type="checkbox"
            checked={isPublic}
            disabled={busy}
            onChange={(e) => setIsPublic(e.target.checked)}
          />
          在登录页公开邀请码
        </label>
        <button className="button secondary" type="submit" disabled={busy}>
          {busy ? '处理中…' : '生成邀请'}
        </button>
      </form>
      <div className="invite-list-heading">
        <h3>我的邀请码</h3>
        <button type="button" className="text-button" onClick={() => void refresh()}>
          <RefreshCw size={14} />
          刷新名额
        </button>
      </div>
      {error && (
        <p role="alert" className="image-error">
          {error}
        </p>
      )}
      {items.length ? (
        <div className="invite-list">
          {items.map((item) => (
            <article className="invite-item" key={item.id}>
              <div className="invite-item-head">
                <span>
                  {labels[item.status]} · {item.isPublic ? '公开' : '私有'}
                </span>
                <strong>
                  剩余 {item.remaining} / {item.maxUses} 名
                </strong>
              </div>
              <p>
                已开通 {item.usedCount} 人 · {formatTime(item.expiresAt)} 到期
              </p>
              {item.code ? (
                <code className="invite-token">{item.code}</code>
              ) : (
                <p>旧邀请码未保存原文，原来的使用规则仍有效。</p>
              )}
              <div className="invite-item-actions">
                {item.code && (
                  <button type="button" className="text-button" onClick={() => void copy(item)}>
                    <Copy size={14} />
                    {copied === item.id ? '已复制' : '复制邀请码'}
                  </button>
                )}
                {item.code && item.status === 'available' && (
                  <button
                    type="button"
                    className="text-button"
                    disabled={busy}
                    onClick={() => void update(item, 'public')}
                  >
                    {item.isPublic ? '取消公开' : '公开邀请码'}
                  </button>
                )}
                {!item.revokedAt && (
                  <button
                    type="button"
                    className="text-button danger-text"
                    disabled={busy}
                    onClick={() => void update(item, 'revoke')}
                  >
                    停用
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      ) : (
        <p className="fine-print">还没有邀请码，生成一个邀请朋友来玩吧。</p>
      )}
      {created && (
        <Modal title="邀请朋友来寄信" onClose={() => setCreated(null)}>
          <p className="modal-copy">
            可供 {created.maxUses} 个新账号开通，有效期七天。
            {created.isPublic ? '这个邀请码已显示在登录页。' : '你可以复制后分享给朋友。'}
          </p>
          <textarea
            className="invite-code"
            value={created.code}
            readOnly
            aria-label="生成的邀请码"
          />
          <button type="button" className="button primary full" onClick={() => void copy(created)}>
            <Copy size={16} />
            {copied === created.id ? '已复制' : '复制邀请码'}
          </button>
        </Modal>
      )}
    </div>
  );
}
