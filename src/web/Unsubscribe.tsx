import { useEffect, useState } from 'react';
import { Check, MailX } from 'lucide-react';
import { api, post } from './api';
import { Mark } from './components';
export default function Unsubscribe() {
  const id = window.location.pathname.split('/').pop() || '';
  const [data, setData] = useState<{ recipient: string; blocked: boolean } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api<{ recipient: string; blocked: boolean }>('/unsubscribe/' + encodeURIComponent(id))
      .then(setData)
      .catch((e) => setError(e.message));
  }, [id]);
  const confirm = async () => {
    setBusy(true);
    try {
      await post('/unsubscribe/' + encodeURIComponent(id));
      setData((data) => (data ? { ...data, blocked: true } : data));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="standalone">
      <div className="standalone-brand">
        <Mark />
        <strong>柿间邮局</strong>
      </div>
      <section className="panel unsubscribe-card">
        <div className="result-icon">{data?.blocked ? <Check /> : <MailX />}</div>
        <h1>{data?.blocked ? '以后，不再打扰。' : '停止接收柿间邮局来信'}</h1>
        <p>
          {data?.blocked
            ? '已停止向 ' + data.recipient + ' 寄送后续来信，待寄信件也已取消。'
            : data
              ? '确认后，' + data.recipient + ' 将不再接收这个网站的后续来信。'
              : '正在检查这封信的链接…'}
        </p>
        <p className="fine-print">已经提交给邮件服务的信件可能仍会到达。</p>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        {data && !data.blocked && (
          <button className="button primary full" disabled={busy} onClick={confirm}>
            {busy ? '正在确认…' : '确认拒收后续来信'}
          </button>
        )}
      </section>
      <p className="standalone-footer">尊重每一份心意，也尊重每一次选择。</p>
    </main>
  );
}
