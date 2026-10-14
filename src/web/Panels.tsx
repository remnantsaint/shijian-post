import InviteManager from './InviteManager';
import { senderFrom, senderDisplay } from '../shared/sender';
import {
  ArrowUpRight,
  BookOpen,
  Check,
  CircleHelp,
  Clock3,
  FileText,
  Inbox,
  LockKeyhole,
  Mail,
  Pause,
  Play,
  Plus,
  Search,
  ShieldCheck,
  Trash2,
  UserPlus,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import type { MailTask, SiteConfig, Template } from '../shared/schema';
import { statusLabels, canDeleteRecord } from '../shared/schema';
import { EmptyState, shortDate, StatusBadge } from './components';
export const starters: Template[] = [
  {
    id: 'starter-hello',
    name: '一封日常问候',
    subject: '今天，也想和你说声嗨',
    body: '嗨，\n\n今天过得怎么样？\n\n日子里总有些不起眼的小事，让人忍不住想起某个人。于是写下这封信，想把一点好心情分享给你。\n\n愿你今天也遇见一件让自己微笑的事。\n\n见字如面。',
  },
  {
    id: 'starter-future',
    name: '写给未来的自己',
    subject: '给那一天的我',
    body: '亲爱的自己：\n\n不知道你读到这封信时，正过着怎样的生活。\n\n此刻的我，想认真记下一件事：\n\n……\n\n希望你仍然记得那些让自己眼睛发亮的小事，也记得照顾好自己。\n\n来自今天的我',
  },
  {
    id: 'starter-thanks',
    name: '谢谢你在这里',
    subject: '有句话，想认真告诉你',
    body: '嗨，\n\n有些谢谢，平时总觉得不必说，写下来才发现，原来一直放在心里。\n\n谢谢你……\n\n希望这封小小的信，能替我把这份心意送到。\n\n祝你一切都好。',
  },
];
export function History({
  tasks,
  onDetail,
  onWrite,
  onDelete,
}: {
  tasks: MailTask[];
  onDelete: (task: MailTask) => void;
  onDetail: (task: MailTask) => void;
  onWrite: () => void;
}) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const rows = tasks.filter(
    (t) =>
      (filter === 'all' || t.status === filter) &&
      [t.subject, t.recipient].some((s) => s.toLowerCase().includes(query.toLowerCase())),
  );
  return (
    <>
      <div className="page-title">
        <div>
          <p className="eyebrow">EVERY LETTER HAS A STORY</p>
          <h1>寄出的，和等待中的。</h1>
          <p>在这里，看看每一份心意走到了哪里。</p>
        </div>
        <button className="button primary" onClick={onWrite}>
          <Plus size={17} /> 写一封新信
        </button>
      </div>
      <div className="stats-row">
        <div>
          <span>最近写下的信</span>
          <strong>
            {tasks.length}
            <small> 封</small>
          </strong>
          <Mail size={20} />
        </div>
        <div>
          <span>正在等待出发</span>
          <strong>
            {tasks.filter((t) => t.status === 'queued').length}
            <small> 封</small>
          </strong>
          <Clock3 size={20} />
        </div>
        <div>
          <span>已完成处理</span>
          <strong>
            {tasks.filter((t) => ['accepted', 'simulated'].includes(t.status)).length}
            <small> 封</small>
          </strong>
          <Check size={20} />
        </div>
      </div>
      <section className="panel history-panel">
        <div className="history-tools">
          <h2>
            <Inbox size={18} /> 信件记录
          </h2>
          <div>
            <label className="search-field">
              <Search size={16} />
              <input
                aria-label="搜索信件"
                placeholder="搜索标题或收件人"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <select
              aria-label="筛选状态"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            >
              <option value="all">全部状态</option>
              {Object.entries(statusLabels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
        </div>
        {rows.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>这封信</th>
                  <th>状态</th>
                  <th>目标时间 · 北京</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((task) => (
                  <tr key={task.id} onClick={() => onDetail(task)}>
                    <td>
                      <div className="letter-row">
                        <span className="letter-icon">
                          <Mail size={18} />
                        </span>
                        <div>
                          <button
                            className="row-title"
                            onClick={(e) => {
                              e.stopPropagation();
                              onDetail(task);
                            }}
                          >
                            {task.subject}
                          </button>
                          <p>{task.recipient}</p>
                        </div>
                      </div>
                    </td>
                    <td>
                      <StatusBadge task={task} />
                    </td>
                    <td className="date-cell">{shortDate(task.targetAt)}</td>
                    <td>
                      <div className="record-actions">
                        <button
                          className="icon-button"
                          aria-label={'删除 ' + task.subject}
                          disabled={!canDeleteRecord(task.status)}
                          title={
                            canDeleteRecord(task.status)
                              ? '删除记录'
                              : '待寄请先取消，正在处理或待核实的信暂不能删除'
                          }
                          onClick={(e) => {
                            e.stopPropagation();
                            onDelete(task);
                          }}
                        >
                          <Trash2 size={16} />
                        </button>
                        <button
                          className="icon-button"
                          aria-label={'查看 ' + task.subject}
                          onClick={(e) => {
                            e.stopPropagation();
                            onDetail(task);
                          }}
                        >
                          <ArrowUpRight size={17} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            title={query || filter !== 'all' ? '没有找到这封信' : '信箱还安安静静的'}
            description={
              query || filter !== 'all'
                ? '换个关键词或状态再找找。'
                : '写下第一封信，让这里开始有故事。'
            }
            action="去写一封信"
            onAction={onWrite}
          />
        )}
        <div className="table-foot">显示最近 100 封信 · 服务接受提交，不等于最终进入收件箱。</div>
      </section>
    </>
  );
}
export function Templates({
  templates,
  onApply,
  onDelete,
  onWrite,
}: {
  templates: Template[];
  onApply: (item: Template) => void;
  onDelete: (id: string) => void;
  onWrite: () => void;
}) {
  return (
    <>
      <div className="page-title">
        <div>
          <p className="eyebrow">A SMALL PLACE TO BEGIN</p>
          <h1>灵感，先从一句话开始。</h1>
          <p>选一封作为开头，再写下属于你的那一部分。</p>
        </div>
      </div>
      <div className="section-caption">
        <BookOpen size={17} /> 为你准备的开场白
      </div>
      <div className="template-grid">
        {starters.map((item, index) => (
          <article className={'template-card tone-' + index} key={item.id}>
            <div className="template-number">
              LETTER No. 0{index + 1}
              <FileText size={21} strokeWidth={1.25} />
            </div>
            <h2>{item.name}</h2>
            <p>{item.body.slice(0, 64)}…</p>
            <button className="text-button" onClick={() => onApply(item)}>
              用这封信开始 <ArrowUpRight size={16} />
            </button>
          </article>
        ))}
      </div>
      <div className="section-caption personal-caption">
        <FileText size={17} /> 我保存的模板 <span>{templates.length}</span>
      </div>
      {templates.length ? (
        <div className="saved-templates">
          {templates.map((item) => (
            <article className="panel saved-template" key={item.id}>
              <div>
                <h3>{item.name}</h3>
                <p>{item.subject}</p>
              </div>
              <div>
                <button className="button secondary" onClick={() => onApply(item)}>
                  使用
                </button>
                <button
                  className="icon-button"
                  onClick={() => onDelete(item.id)}
                  aria-label={'删除模板 ' + item.name}
                >
                  <Trash2 size={16} />
                </button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <section className="panel">
          <EmptyState
            title="把常写的话，留在这里"
            description="写信时点击“存为模板”，下次就能接着用。"
            action="去写一封信"
            onAction={onWrite}
          />
        </section>
      )}
    </>
  );
}
export function Settings({
  config,
  onSaveProfile,
  onPause,
}: {
  config: SiteConfig;
  onSaveProfile: (name: string, replyEmail: string) => Promise<void>;
  onPause: () => void;
}) {
  const [nickname, setNickname] = useState(config.user?.senderName || '');
  const [replyEmail, setReplyEmail] = useState(config.user?.replyEmail || '');
  const [saving, setSaving] = useState(false);
  const [nameError, setNameError] = useState('');
  useEffect(() => {
    setNickname(config.user?.senderName || '');
    setReplyEmail(config.user?.replyEmail || '');
    setNameError('');
  }, [config.user?.id, config.user?.senderName, config.user?.replyEmail]);
  const previewUser = Boolean(
    config.user && ['local-demo', 'local-member'].includes(config.user.id),
  );
  const displayName = config.user?.id === 'local-demo' ? '体验访客' : config.user?.name;
  return (
    <>
      <div className="page-title">
        <div>
          <p className="eyebrow">YOUR LITTLE POST OFFICE</p>
          <h1>让邮局，按你的节奏。</h1>
          <p>账号、寄件身份，以及一点使用说明。</p>
        </div>
      </div>
      <div className="settings-grid">
        <section className="panel settings-card">
          <h2>
            <ShieldCheck size={18} /> 我的账号
          </h2>
          <div className="account-large">
            <span>{displayName?.slice(0, 1) || '访'}</span>
            <div>
              <h3>{displayName || '尚未登录'}</h3>
              <p>
                {previewUser
                  ? '欢迎来邮局坐坐'
                  : config.user
                    ? '@' + config.user.login
                    : '登录后开始寄信'}
              </p>
            </div>
          </div>
          <dl>
            <div>
              <dt>身份</dt>
              <dd>{config.user?.role === 'admin' ? '邮局管理员' : '寄信人'}</dd>
            </div>
            <div>
              <dt>登录方式</dt>
              <dd>{previewUser ? '访客预览' : 'GitHub'}</dd>
            </div>
          </dl>
          <p className="fine-print">
            <LockKeyhole size={13} /> 每位寄信人只能看到自己的信件与模板。
          </p>
        </section>
        <section className="panel settings-card">
          <h2>
            <Mail size={18} /> 寄件身份
          </h2>
          <label>发件邮箱</label>
          <p className="from-address">
            {config.from.match(/<([^>]+)>/)?.[1] || config.from || '尚未设置'}
          </p>
          <form
            className="sender-settings"
            onSubmit={async (e) => {
              e.preventDefault();
              if (saving || !config.user?.active) return;
              setSaving(true);
              setNameError('');
              try {
                await onSaveProfile(nickname, replyEmail);
              } catch (error) {
                setNameError((error as Error).message);
              } finally {
                setSaving(false);
              }
            }}
          >
            <div className="field">
              <label htmlFor="sender-name">寄件昵称</label>
              <input
                id="sender-name"
                value={nickname}
                maxLength={30}
                placeholder="例如：西柿"
                disabled={!config.user?.active || saving}
                onChange={(e) => {
                  setNickname(e.target.value);
                  setNameError('');
                }}
              />
              <p className="field-note">留空使用“柿间邮局”，修改仅影响之后新建的信。</p>
            </div>
            <div className="field">
              <label htmlFor="reply-email">回信邮箱（选填）</label>
              <input
                id="reply-email"
                type="email"
                value={replyEmail}
                maxLength={254}
                placeholder="填写你能收信的邮箱"
                disabled={!config.user?.active || saving}
                aria-describedby="reply-email-note"
                onChange={(e) => {
                  setReplyEmail(e.target.value);
                  setNameError('');
                }}
              />
              <p id="reply-email-note" className="field-note">
                不填写将无法收到回信；填写后，收件人点击“回复”会寄到这个邮箱。回信在你的邮箱查看，地址会向收件人展示。修改仅影响之后新建的信。
              </p>
            </div>
            <div className="sender-preview">
              <span>收件人看到</span>
              <strong>{senderDisplay(senderFrom(config.from, nickname.trim()))}</strong>
            </div>
            <button
              type="submit"
              className="button secondary"
              disabled={
                !config.user?.active ||
                saving ||
                (nickname.trim() === (config.user?.senderName || '') &&
                  replyEmail.trim() === (config.user?.replyEmail || ''))
              }
            >
              {saving ? '保存中…' : '保存寄件设置'}
            </button>
            {nameError && (
              <p role="alert" className="image-error">
                {nameError}
              </p>
            )}
          </form>
          <dl>
            <div>
              <dt>投递服务</dt>
              <dd>
                {config.mailMode === 'mock'
                  ? '预览模式'
                  : config.mailMode === 'resend'
                    ? 'Resend'
                    : '等待配置'}
              </dd>
            </div>
            <div>
              <dt>当前状态</dt>
              <dd>
                <span className={'service-dot ' + (config.paused ? 'off' : '')} />
                {config.paused
                  ? '暂停寄信'
                  : config.mailMode === 'disabled'
                    ? '尚未启用'
                    : config.mailMode === 'mock'
                      ? '仅预览，不发送邮件'
                      : '可以寄信'}
              </dd>
            </div>
          </dl>
          <p className="fine-print">发件邮箱由邮局统一配置，寄件昵称和收件人由你填写。</p>
        </section>
        {config.user?.role === 'admin' && (
          <section className="panel settings-card admin-card">
            <h2>
              <UserPlus size={18} /> 管理邮局
            </h2>
            <InviteManager />
            <div className="setting-action">
              <div>
                <h3>{config.paused ? '恢复寄信' : '让邮局休息一下'}</h3>
                <p>
                  {config.paused
                    ? '恢复后会继续处理到期的待寄信件。'
                    : '暂停创建与发送任务，已经提交的邮件无法撤回。'}
                </p>
              </div>
              <button className="button secondary" onClick={onPause}>
                {config.paused ? <Play size={15} /> : <Pause size={15} />}{' '}
                {config.paused ? '恢复' : '暂停'}
              </button>
            </div>
          </section>
        )}
        <section className="panel settings-card help-card">
          <h2>
            <CircleHelp size={18} /> 关于寄信
          </h2>
          <p>定时发送使用北京时间，到点后由邮局尽快处理。</p>
          <p>“服务已接受”表示邮件已提交给投递服务，收件箱是否收到仍需收件人确认。</p>
          <p>遇到“结果待核实”时，请先核对记录与收件箱。邮局不会自动重发结果不确定的信件。</p>
        </section>
      </div>
    </>
  );
}
