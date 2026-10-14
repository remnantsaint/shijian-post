import { templateText } from '../shared/images';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowDownToLine,
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronRight,
  Copy,
  Feather,
  Github,
  Inbox,
  LogOut,
  Menu,
  Mail,
  Settings2,
  ShieldCheck,
  X,
} from 'lucide-react';
import type { MailTask, SiteConfig, Template } from '../shared/schema';
import Composer, { emptyDraft, type Draft } from './Composer';
import { EmptyState, Mark, Modal, SendResult, TaskDetail } from './components';
import { History, Settings, Templates } from './Panels';
import Unsubscribe from './Unsubscribe';
import { api, ApiError, post } from './api';
type Page = 'compose' | 'history' | 'templates' | 'settings';
const pageNames: Record<Page, string> = {
  compose: '写一封信',
  history: '信件记录',
  templates: '模板信纸',
  settings: '邮局设置',
};
export default function App() {
  return window.location.pathname.startsWith('/unsubscribe/') ? <Unsubscribe /> : <Workbench />;
}
function Workbench() {
  const [config, setConfig] = useState<SiteConfig | null>(null);
  const [loadError, setLoadError] = useState('');
  const [page, setPage] = useState<Page>('compose');
  const [draft, setDraft] = useState<Draft>({ ...emptyDraft });
  const [tasks, setTasks] = useState<MailTask[]>([]);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [authOpen, setAuthOpen] = useState(false);
  const [invite, setInvite] = useState(
    () =>
      new URLSearchParams(location.search).get('invite') ||
      sessionStorage.getItem('manyou-invite') ||
      '',
  );
  const [authError, setAuthError] = useState('');
  const [mobileOpen, setMobileOpen] = useState(false);
  const [result, setResult] = useState<MailTask | null>(null);
  const [detail, setDetail] = useState<MailTask | null>(null);
  const [saveOpen, setSaveOpen] = useState(false);
  const [templateName, setTemplateName] = useState('');
  const [invitation, setInvitation] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const requestRef = useRef<{ id: string; input: unknown } | null>(null);
  const configRequest = useRef(0);
  const refreshConfig = useCallback(async () => {
    const sequence = ++configRequest.current;
    try {
      const data = await api<SiteConfig>('/config');
      if (sequence === configRequest.current) {
        setConfig(data);
        setLoadError('');
      }
      return data;
    } catch (e) {
      if (sequence === configRequest.current) setLoadError((e as Error).message);
      return null;
    }
  }, []);
  const loadData = useCallback(async (silent = false) => {
    try {
      const [mailData, templateData] = await Promise.all([
        api<{ tasks: MailTask[] }>('/tasks'),
        api<{ templates: Template[] }>('/templates'),
      ]);
      setTasks(mailData.tasks);
      setTemplates(templateData.templates);
    } catch (e) {
      if (!silent) setNotice((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void refreshConfig();
    const key = new URLSearchParams(location.search).get('authError');
    if (key) {
      setAuthError(
        key === 'setup'
          ? '登录暂不可用，请稍后再试或联系邮局管理员。'
          : key === 'state'
            ? '登录验证已失效，请重新开始。'
            : 'GitHub 登录未完成，请重试。',
      );
      setAuthOpen(true);
      history.replaceState(null, '', location.pathname);
    }
  }, [refreshConfig]);
  const userId = config?.user?.id;
  const active = config?.user?.active;
  useEffect(() => {
    if (userId && active) void loadData();
    else {
      setTasks([]);
      setTemplates([]);
    }
    if (userId && !active) setAuthOpen(true);
  }, [userId, active, loadData]);
  useEffect(() => {
    if (!userId || !active || !tasks.some((t) => ['queued', 'sending'].includes(t.status))) return;
    const timer = setInterval(() => {
      if (!document.hidden) void loadData(true);
    }, 15000);
    return () => clearInterval(timer);
  }, [tasks, userId, active, loadData]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(''), 6000);
    return () => clearTimeout(timer);
  }, [notice]);
  const requireLogin = () => {
    if (!config?.user?.active) {
      setAuthOpen(true);
      return false;
    }
    return true;
  };
  const navigate = (next: Page) => {
    setPage(next);
    setMobileOpen(false);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };
  const newLetter = () => {
    if (uncertain && !confirm('上一封信的结果尚未确认，请先核对记录。确定另外写一封新信吗？'))
      return;
    setDraft({ ...emptyDraft });
    requestRef.current = null;
    setUncertain(false);
    navigate('compose');
  };
  const send = async () => {
    if (!requireLogin() || busy || uncertain) return;
    if (!draft.body.trim() || draft.body.length > 20000) {
      setNotice('请填写正文，且控制在 20,000 字以内');
      return;
    }
    if (draft.timing === 'schedule' && !draft.sendAt) {
      setNotice('请选择一个发送时间');
      return;
    }
    if (
      draft.timing === 'schedule' &&
      new Date(draft.sendAt + ':00+08:00').getTime() <= Date.now()
    ) {
      setNotice('请选择未来的发送时间');
      return;
    }
    if (!requestRef.current) {
      const id = crypto.randomUUID();
      requestRef.current = {
        id,
        input: {
          id,
          recipient: draft.recipient,
          subject: draft.subject,
          body: draft.body,
          images: draft.images,
          ...(draft.timing === 'schedule'
            ? { sendAt: new Date(draft.sendAt + ':00+08:00').toISOString() }
            : {}),
        },
      };
    }
    setBusy(true);
    try {
      const response = await post<{ task: MailTask }>('/tasks', requestRef.current.input);
      setResult(response.task);
      requestRef.current = null;
      if (['queued', 'accepted', 'simulated'].includes(response.task.status))
        setDraft({ ...emptyDraft });
      await loadData(true);
    } catch (e) {
      const error = e as ApiError;
      setNotice(error.message);
      if (
        error.status === 0 ||
        (error.status >= 500 && error.status !== 503) ||
        error.status === 409
      )
        setUncertain(true);
      else requestRef.current = null;
    } finally {
      setBusy(false);
    }
  };
  const saveSenderName = async (senderName: string) => {
    await post('/profile', { senderName });
    await refreshConfig();
    setNotice('寄件昵称已保存，将用于之后新建的信');
  };
  const cancelTask = async (task: MailTask) => {
    if (!confirm('取消这封尚未开始寄出的信？')) return;
    try {
      await post('/tasks/' + task.id + '/cancel');
      setDetail(null);
      setNotice('这封信已取消，记录会保留');
      await loadData();
    } catch (e) {
      setNotice((e as Error).message);
    }
  };
  const openSave = () => {
    if (!requireLogin()) return;
    if (!draft.subject.trim() || !templateText(draft.body)) {
      setNotice('先写好标题和正文，就能存成模板');
      return;
    }
    setTemplateName(draft.subject.slice(0, 40));
    setSaveOpen(true);
  };
  const saveTemplate = async () => {
    if (!templateName.trim()) return;
    setBusy(true);
    try {
      await post('/templates', {
        name: templateName,
        subject: draft.subject,
        body: templateText(draft.body),
      });
      setSaveOpen(false);
      setNotice(draft.images.length ? '文字已保存为模板，图片仅用于这封信' : '这份灵感已经保存');
      await loadData(true);
    } catch (e) {
      setNotice((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const applyTemplate = (item: Template) => {
    if (uncertain) {
      setNotice('请先核对上一封信的记录，或点击“写一封新信”');
      return;
    }
    setDraft((old) => ({ ...old, subject: item.subject, body: item.body, images: [] }));
    navigate('compose');
    setNotice('已放进信纸，接着写属于你的内容吧');
  };
  const deleteTemplate = async (id: string) => {
    if (!confirm('删除这个模板？已经寄出的信件不受影响。')) return;
    try {
      await api('/templates/' + id, { method: 'DELETE' });
      await loadData();
      setNotice('模板已删除');
    } catch (e) {
      setNotice((e as Error).message);
    }
  };
  const demo = async (role: 'admin' | 'member' = 'admin') => {
    setBusy(true);
    try {
      await post('/auth/demo', { role });
      const data = await refreshConfig();
      if (!data?.user) throw new Error('连接暂未恢复，请再试一次');
      setAuthOpen(false);
      setNotice('欢迎来到柿间邮局，当前为预览模式，不发送邮件');
    } catch (e) {
      setAuthError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const activate = async () => {
    setBusy(true);
    try {
      await post('/auth/invite', { code: invite.trim() });
      sessionStorage.removeItem('manyou-invite');
      await refreshConfig();
      setAuthOpen(false);
      setNotice('你的邮局通行证已开通');
    } catch (e) {
      setAuthError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const logout = async () => {
    try {
      await post('/auth/logout');
      setDraft({ ...emptyDraft });
      setDetail(null);
      requestRef.current = null;
      setUncertain(false);
      await refreshConfig();
      setNotice('已退出登录');
    } catch (e) {
      setNotice((e as Error).message);
    }
  };
  const createInvite = async () => {
    try {
      const data = await post<{ code: string }>('/admin/invites');
      setInvitation(data.code);
    } catch (e) {
      setNotice((e as Error).message);
    }
  };
  const togglePause = async () => {
    if (!config) return;
    try {
      await post('/admin/settings', { paused: !config.paused });
      await refreshConfig();
      setNotice(config.paused ? '邮局重新营业啦' : '邮局已暂停，已经提交的邮件无法撤回');
    } catch (e) {
      setNotice((e as Error).message);
    }
  };
  const copyInvite = async () => {
    try {
      await navigator.clipboard.writeText(invitation);
      setNotice('邀请码已复制');
    } catch {
      setNotice('复制未完成，请手动选择并复制邀请码');
    }
  };
  if (!config)
    return (
      <div className="loading-screen">
        <Mark />
        <h1>柿间邮局</h1>
        {loadError ? (
          <>
            <p role="alert">{loadError}</p>
            <p className="fine-print">邮局暂时未能连接，请稍后重试。</p>
            <button className="button primary" onClick={() => void refreshConfig()}>
              重新连接
            </button>
          </>
        ) : (
          <p>
            <span className="spinner dark" /> 邮局正在开门…
          </p>
        )}
      </div>
    );
  const displayName = config.user?.id === 'local-demo' ? '体验访客' : config.user?.name;
  const nav = [
    { id: 'compose' as const, icon: Feather },
    { id: 'history' as const, icon: Inbox },
    { id: 'templates' as const, icon: BookOpen },
    { id: 'settings' as const, icon: Settings2 },
  ];
  return (
    <div className="app-shell">
      <aside className={'sidebar ' + (mobileOpen ? 'open' : '')}>
        <a className="brand" href="/" aria-label="柿间邮局首页">
          <Mark />
          <div>
            <strong>
              柿间邮局<span>SHIJIAN POST</span>
            </strong>
            <small>把今天，寄给某一天。</small>
          </div>
        </a>
        <button
          className="mobile-close icon-button"
          onClick={() => setMobileOpen(false)}
          aria-label="收起菜单"
        >
          <X />
        </button>
        <div className="nav-caption">我的邮局</div>
        <nav>
          {nav.map((item) => (
            <button
              key={item.id}
              className={page === item.id ? 'nav-item active' : 'nav-item'}
              onClick={() => navigate(item.id)}
            >
              <item.icon size={19} strokeWidth={1.6} />
              <span>{pageNames[item.id]}</span>
              {item.id === 'history' && tasks.some((t) => t.status === 'queued') && (
                <b>{tasks.filter((t) => t.status === 'queued').length}</b>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-quote">
          <span>“</span>
          <p>
            总有一些话，
            <br />
            值得慢慢写，
            <br />
            认真寄。
          </p>
          <div className="quote-line" />
          <small>LETTERS BRING US CLOSER</small>
        </div>
        <div className="sidebar-bottom">
          <span className="tiny-dot" /> {config.paused ? '邮局暂时休息' : '邮局营业中'}
          <small>一封信，也是一份陪伴。</small>
        </div>
      </aside>
      {mobileOpen && (
        <button
          className="mobile-scrim"
          aria-label="收起菜单"
          onClick={() => setMobileOpen(false)}
        />
      )}
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="mobile-menu icon-button"
              onClick={() => setMobileOpen(true)}
              aria-label="展开菜单"
            >
              <Menu size={20} />
            </button>
            <span>我的邮局</span>
            <ChevronRight size={14} />
            <strong>{pageNames[page]}</strong>
          </div>
          <div className="topbar-right">
            {config.user ? (
              <>
                <span className="avatar">{displayName?.slice(0, 1)}</span>
                <span className="user-name">{displayName}</span>
                <button
                  className="icon-button"
                  onClick={() => void logout()}
                  aria-label="退出登录"
                  title="退出登录"
                >
                  <LogOut size={17} />
                </button>
              </>
            ) : (
              <button className="button secondary small-button" onClick={() => setAuthOpen(true)}>
                <Github size={15} /> 登录邮局
              </button>
            )}
          </div>
        </header>
        <main className="workspace">
          {config.mailMode === 'disabled' && (
            <div className="environment-note warning">
              <ShieldCheck size={15} />
              <span>邮局正在准备中，暂时无法寄信。</span>
            </div>
          )}
          {uncertain && (
            <div className="environment-note warning" role="alert">
              <Inbox size={15} />
              <span>上一封信的提交结果待确认，请先到记录里核对，避免重复寄出。</span>
              <button
                onClick={() => {
                  navigate('history');
                  void loadData();
                }}
              >
                核对记录
              </button>
            </div>
          )}
          {page === 'compose' && (
            <Composer
              draft={draft}
              onChange={setDraft}
              onSubmit={() => void send()}
              busy={busy}
              config={config}
              onTemplates={() => navigate('templates')}
              onSave={openSave}
              uncertain={uncertain}
              onHistory={() => {
                navigate('history');
                void loadData();
              }}
            />
          )}
          {page === 'history' &&
            (config.user?.active ? (
              <History tasks={tasks} onDetail={setDetail} onWrite={newLetter} />
            ) : (
              <section className="panel guest-panel">
                <EmptyState
                  title="你的信件，都在这里"
                  description="登录邮局，查看属于你的寄信记录。"
                  action="登录邮局"
                  onAction={() => setAuthOpen(true)}
                />
              </section>
            ))}
          {page === 'templates' && (
            <Templates
              templates={templates}
              onApply={applyTemplate}
              onDelete={(id) => void deleteTemplate(id)}
              onWrite={() => navigate('compose')}
            />
          )}
          {page === 'settings' && (
            <Settings
              config={config}
              onSaveName={saveSenderName}
              onPause={() => void togglePause()}
              onInvite={() => void createInvite()}
            />
          )}
          <footer className="workspace-footer">
            <span>柿间邮局 · 把今天的话，寄给某一天。</span>
            <span>
              <ShieldCheck size={13} /> 心意有记录，隐私有边界。
            </span>
          </footer>
        </main>
      </div>
      {notice && (
        <div className="toast" role="status">
          <Check size={17} />
          <span>{notice}</span>
          <button onClick={() => setNotice('')} aria-label="关闭提示">
            <X size={15} />
          </button>
        </div>
      )}
      {authOpen && (
        <Modal
          title={config.user && !config.user.active ? '开通你的邮局通行证' : '欢迎来到柿间邮局'}
          onClose={() => setAuthOpen(false)}
        >
          <div className="auth-intro">
            <Mark />
            <h3>把今天的话，寄给某一天。</h3>
            <p>
              {config.user
                ? '你已经以 @' + config.user.login + ' 登录，输入管理员给你的邀请码即可开始寄信。'
                : '使用 GitHub 账号登录。首次使用需要管理员邀请。'}
            </p>
          </div>
          {config.user && !config.user.active ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void activate();
              }}
            >
              <div className="field">
                <label htmlFor="invite">邀请码</label>
                <input
                  id="invite"
                  value={invite}
                  onChange={(e) => setInvite(e.target.value)}
                  placeholder="粘贴管理员分享的邀请码"
                  required
                />
              </div>
              <button className="button primary full" disabled={busy}>
                开通通行证 <ArrowUpRight size={16} />
              </button>
            </form>
          ) : (
            <>
              <a
                className={'button primary full ' + (!config.githubConfigured ? 'disabled' : '')}
                href={config.githubConfigured ? '/api/auth/github' : undefined}
                onClick={() => {
                  if (invite) sessionStorage.setItem('manyou-invite', invite);
                }}
              >
                <Github size={18} /> 使用 GitHub 登录
              </a>
              {!config.githubConfigured && (
                <p className="fine-print">
                  GitHub 登录暂不可用。
                  {config.demoAvailable ? '你可以先预览邮局。' : '请联系邮局管理员。'}
                </p>
              )}
              {config.demoAvailable && (
                <>
                  <button
                    className="button secondary full demo-button"
                    disabled={busy}
                    onClick={() => void demo()}
                  >
                    <Feather size={17} /> 预览邮局 · 不发送邮件
                  </button>
                  <button
                    className="button secondary full demo-button"
                    disabled={busy}
                    onClick={() => void demo('member')}
                  >
                    <Mail size={17} /> 普通寄信人预览 · 不发送邮件
                  </button>
                </>
              )}
            </>
          )}
          {authError && (
            <p className="form-error" role="alert">
              {authError}
            </p>
          )}
          <p className="auth-note">
            <ShieldCheck size={13} /> 只获取基本 GitHub 身份，不读取你的仓库。
          </p>
        </Modal>
      )}
      {saveOpen && (
        <Modal title="保存一份写信灵感" onClose={() => setSaveOpen(false)}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void saveTemplate();
            }}
          >
            <div className="field">
              <label htmlFor="template-name">模板名称</label>
              <input
                id="template-name"
                required
                maxLength={40}
                value={templateName}
                onChange={(e) => setTemplateName(e.target.value)}
              />
            </div>
            <p className="fine-print">保存当前标题与正文，收件人和发送时间不会存进模板。</p>
            <button className="button primary full" disabled={busy}>
              <ArrowDownToLine size={16} /> 保存模板
            </button>
          </form>
        </Modal>
      )}
      {invitation && (
        <Modal title="邀请一个朋友来寄信" onClose={() => setInvitation('')}>
          <p className="modal-copy">
            这个邀请码可使用一次，七天内有效。朋友用 GitHub 登录后，粘贴它即可开通。
          </p>
          <textarea className="invite-code" value={invitation} readOnly aria-label="生成的邀请码" />
          <button className="button primary full" onClick={() => void copyInvite()}>
            <Copy size={16} /> 复制邀请码
          </button>
        </Modal>
      )}
      {result && (
        <SendResult
          task={result}
          onClose={() => setResult(null)}
          onHistory={() => {
            setResult(null);
            navigate('history');
          }}
        />
      )}
      {detail && (
        <TaskDetail
          task={tasks.find((t) => t.id === detail.id) || detail}
          config={config}
          onClose={() => setDetail(null)}
          onCancel={(task) => void cancelTask(task)}
        />
      )}
    </div>
  );
}
