import {
  ArrowUpRight,
  BookmarkPlus,
  CalendarDays,
  ChevronDown,
  Clock3,
  Feather,
  Send,
  Sparkles,
} from 'lucide-react';
import type { SiteConfig } from '../shared/schema';
import { Paper } from './components';
export interface Draft {
  recipient: string;
  subject: string;
  body: string;
  timing: 'now' | 'schedule';
  sendAt: string;
}
export const emptyDraft: Draft = {
  recipient: '',
  subject: '',
  body: '',
  timing: 'now',
  sendAt: '',
};
export default function Composer({
  draft,
  onChange,
  onSubmit,
  busy,
  config,
  onTemplates,
  onSave,
  uncertain,
  onHistory,
}: {
  draft: Draft;
  onChange: (draft: Draft) => void;
  onSubmit: () => void;
  busy: boolean;
  config: SiteConfig;
  onTemplates: () => void;
  onSave: () => void;
  uncertain: boolean;
  onHistory: () => void;
}) {
  const set = (key: keyof Draft, value: string) => onChange({ ...draft, [key]: value });
  return (
    <>
      <div className="page-title">
        <div>
          <p className="eyebrow">A LITTLE LETTER, A LITTLE CLOSER</p>
          <h1>今天，寄一点心意。</h1>
          <p>把问候、想念和小小的日常，装进一封信里。</p>
        </div>
        <span className="day-note">
          <span>✦</span> 见字如面，慢慢来。
        </span>
      </div>
      <div className="compose-grid">
        <section className="composer panel">
          <div className="panel-heading">
            <h2>
              <Feather size={18} /> 写一封信
            </h2>
            <button className="text-button" onClick={onTemplates}>
              从模板开始 <ArrowUpRight size={15} />
            </button>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              onSubmit();
            }}
          >
            <div className="field">
              <label htmlFor="recipient">
                寄给谁 <span>TO</span>
              </label>
              <input
                id="recipient"
                type="email"
                placeholder="收件人的邮箱地址"
                value={draft.recipient}
                onChange={(e) => set('recipient', e.target.value)}
                required
                maxLength={254}
                autoComplete="email"
              />
              <p className="field-note">支持 QQ、163、Gmail 等常用邮箱。</p>
            </div>
            <div className="field">
              <label htmlFor="subject">
                信的标题 <span>SUBJECT</span>
              </label>
              <input
                id="subject"
                placeholder="给这份心意起个名字"
                value={draft.subject}
                onChange={(e) => set('subject', e.target.value)}
                required
                maxLength={200}
              />
            </div>
            <div className="field body-field">
              <div className="label-row">
                <label htmlFor="body">
                  想说的话 <span>MESSAGE</span>
                </label>
                <button className="subtle-button" type="button" onClick={onSave}>
                  <BookmarkPlus size={14} /> 存为模板
                </button>
              </div>
              <textarea
                id="body"
                placeholder={'嗨，\n\n今天有件小事，想和你分享……'}
                value={draft.body}
                onChange={(e) => set('body', e.target.value)}
                required
                maxLength={20000}
              />
              <div className="editor-footer">
                <span>文字会保留你的换行与段落</span>
                <span>{draft.body.length.toLocaleString()} / 20,000</span>
              </div>
            </div>
            <div className="timing-label">什么时候寄出？</div>
            <div className="timing-options">
              <button
                type="button"
                className={draft.timing === 'now' ? 'timing selected' : 'timing'}
                aria-pressed={draft.timing === 'now'}
                onClick={() => set('timing', 'now')}
              >
                <Send size={17} />
                <div>
                  <strong>现在寄出</strong>
                  <small>让心意即刻出发</small>
                </div>
                <i />
              </button>
              <button
                type="button"
                className={draft.timing === 'schedule' ? 'timing selected' : 'timing'}
                aria-pressed={draft.timing === 'schedule'}
                onClick={() => set('timing', 'schedule')}
              >
                <CalendarDays size={18} />
                <div>
                  <strong>约个时间</strong>
                  <small>把惊喜留给那一天</small>
                </div>
                <i />
              </button>
            </div>
            {draft.timing === 'schedule' && (
              <div className="schedule-input">
                <label htmlFor="send-at">
                  <Clock3 size={15} /> 目标时间（北京时间）
                </label>
                <input
                  id="send-at"
                  type="datetime-local"
                  required
                  value={draft.sendAt}
                  onChange={(e) => set('sendAt', e.target.value)}
                />
                <p>到点后由邮局尽快寄出，可能有少量延迟。</p>
              </div>
            )}
            <div className="compose-actions">
              <span>
                <span className="tiny-dot" />{' '}
                {config.mailMode === 'mock' ? '预览模式 · 不发送邮件' : '一封信，一份认真'}
              </span>
              {uncertain ? (
                <button type="button" className="button primary" onClick={onHistory}>
                  核对发送记录 <ArrowUpRight size={16} />
                </button>
              ) : (
                <button type="submit" className="button primary" disabled={busy || config.paused}>
                  {busy ? <span className="spinner" /> : <Send size={17} />}{' '}
                  {busy
                    ? '正在交给邮差…'
                    : config.paused
                      ? '邮局暂时休息'
                      : draft.timing === 'schedule'
                        ? '放进待寄信箱'
                        : '寄出这封信'}
                </button>
              )}
            </div>
          </form>
        </section>
        <aside className="preview-column">
          <div className="preview-title">
            <span>
              信件预览 <ChevronDown size={13} />
            </span>
            <span className="live">
              <i /> 实时
            </span>
          </div>
          <Paper
            subject={draft.subject}
            body={draft.body}
            recipient={draft.recipient}
            from={config.from}
          />
          <div className="preview-foot">
            <Sparkles size={16} />
            <p>
              有些话不用很长，
              <br />
              被认真写下，就已经很珍贵。
            </p>
          </div>
        </aside>
      </div>
    </>
  );
}
