import { senderDisplay } from '../shared/sender';
import { bodyParts, type ImageInfo, type ImageInput } from '../shared/images';
import { ArrowUpRight, Check, Clock3, Mail, Send, X } from 'lucide-react';
import type { MailTask, SiteConfig } from '../shared/schema';
import { statusLabels, canDeleteRecord } from '../shared/schema';
export function Mark({ small = false }: { small?: boolean }) {
  return (
    <span className={'brand-mark ' + (small ? 'small' : '')}>
      <Mail size={small ? 19 : 25} strokeWidth={1.5} />
      <i />
    </span>
  );
}
export function Stamp() {
  return (
    <div className="stamp">
      <svg viewBox="0 0 70 70" aria-hidden="true">
        <circle cx="35" cy="35" r="27" />
        <circle cx="35" cy="35" r="21" />
        <path d="M23 30h24v17H23z M23 30l12 10 12-10" />
        <path d="M4 23h13 M1 31h14 M3 39h12 M5 47h13" />
      </svg>
      <span>SHIJIAN POST</span>
    </div>
  );
}
export function StatusBadge({ task }: { task: MailTask }) {
  return (
    <span className={'status ' + task.status}>
      <i />
      {statusLabels[task.status]}
    </span>
  );
}
export function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value));
}
export function shortDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value));
}
export function Modal({
  title,
  children,
  onClose,
}: {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
}) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-heading">
          <h2>{title}</h2>
          <button className="icon-button" onClick={onClose} aria-label="关闭">
            <X size={20} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}
export function Paper({
  subject,
  body,
  recipient,
  from,
  large = false,
  images = [],
  imageUrl,
}: {
  subject: string;
  body: string;
  recipient: string;
  from: string;
  large?: boolean;
  images?: (ImageInfo | ImageInput)[];
  imageUrl?: (id: string) => string | undefined;
}) {
  return (
    <article className={'letter-paper ' + (large ? 'large' : '')}>
      <div className="paper-top">
        <span>一封来自柿间邮局的信</span>
        <Stamp />
      </div>
      <div className="paper-to">
        <span>TO</span> {recipient || '那个你想念的人'}
      </div>
      <h3>{subject || '把想说的话，认真寄出去。'}</h3>
      <div className={'paper-body ' + (!body ? 'placeholder' : '')}>
        {body
          ? bodyParts(body).map((part, index) => {
              if ('text' in part) return <span key={index}>{part.text}</span>;
              const img = images.find((i) => i.id === part.imageId);
              const url =
                img &&
                ('content' in img
                  ? `data:${img.contentType};base64,${img.content}`
                  : imageUrl?.(img.id));
              return url ? (
                <img key={index} className="paper-image" src={url} alt={img!.name} />
              ) : (
                <span key={index} className="cleared-image">
                  照片：{img?.name || '图片'}
                  <small>原图已清理</small>
                </span>
              );
            })
          : '有些话，写下来才刚刚好。\n\n写一封问候，分享一个小小的发现，\n或者把此刻的心情，寄给未来。\n\n不必很长，真诚就好。'}
      </div>
      <div className="paper-sign">
        <span className="handwritten">见字如面</span>
        <span>通过 {from.match(/<([^>]+)>/)?.[1] || from || '柿间邮局'} 寄出</span>
      </div>
      <div className="paper-bottom">
        <span>MADE WITH A LITTLE THOUGHT</span>
        <Mail size={15} />
      </div>
    </article>
  );
}
export function TaskDetail({
  task,
  config,
  onClose,
  onCancel,
  onDelete,
}: {
  task: MailTask;
  config: SiteConfig;
  onClose: () => void;
  onCancel: (task: MailTask) => void;
  onDelete: (task: MailTask) => void;
}) {
  return (
    <Modal title="信件详情" onClose={onClose}>
      <div className="detail-meta">
        <StatusBadge task={task} />
        <span>{formatTime(task.createdAt)} 写下</span>
      </div>
      <Paper
        subject={task.subject}
        body={task.body}
        recipient={task.recipient}
        from={task.senderFrom || config.from}
        images={task.images}
        imageUrl={(id) => (task.imagesAvailable ? `/api/tasks/${task.id}/images/${id}` : undefined)}
        large
      />
      <dl className="task-facts">
        <div>
          <dt>发件人</dt>
          <dd>{senderDisplay(task.senderFrom || config.from)}</dd>
        </div>
        <div>
          <dt>目标时间</dt>
          <dd>{formatTime(task.targetAt)}（北京时间）</dd>
        </div>
        <div>
          <dt>任务编号</dt>
          <dd className="mono">{task.id}</dd>
        </div>
        {task.finishedAt && (
          <div>
            <dt>记录时间</dt>
            <dd>{formatTime(task.finishedAt)}</dd>
          </div>
        )}
        {task.reason && (
          <div>
            <dt>备注</dt>
            <dd>{task.reason}</dd>
          </div>
        )}
      </dl>
      {task.images?.length > 0 && !task.imagesAvailable && (
        <p className="fine-print">
          {task.status === 'accepted'
            ? '网站已清理图片原图，邮件里仍包含图片。'
            : '网站已清理图片原图，记录仅保留图片信息。'}
        </p>
      )}
      {task.status === 'accepted' && (
        <p className="fine-print">邮件服务已接受提交，最终是否进入收件箱仍以收件人邮箱为准。</p>
      )}
      {canDeleteRecord(task.status) && (
        <button className="button danger" onClick={() => onDelete(task)}>
          删除这封信的记录
        </button>
      )}
      {task.status === 'queued' && (
        <button className="button danger" onClick={() => onCancel(task)}>
          取消这封待寄的信
        </button>
      )}
    </Modal>
  );
}
export function EmptyState({
  title,
  description,
  action,
  onAction,
}: {
  title: string;
  description: string;
  action?: string;
  onAction?: () => void;
}) {
  return (
    <div className="empty-state">
      <div className="empty-art">
        <span className="orbit" />
        <Mail size={32} strokeWidth={1.25} />
        <i>✦</i>
      </div>
      <h3>{title}</h3>
      <p>{description}</p>
      {action && (
        <button className="text-button" onClick={onAction}>
          {action}
          <ArrowUpRight size={16} />
        </button>
      )}
    </div>
  );
}
export function SendResult({
  task,
  onClose,
  onHistory,
}: {
  task: MailTask;
  onClose: () => void;
  onHistory: () => void;
}) {
  const success = ['accepted', 'simulated'].includes(task.status);
  return (
    <Modal
      title={
        task.status === 'queued'
          ? '信已放进待寄信箱'
          : task.status === 'simulated'
            ? '信件预览已完成'
            : success
              ? '这份心意，已经启程'
              : '这封信需要你看一下'
      }
      onClose={onClose}
    >
      <div className={'result-icon ' + (!success && task.status !== 'queued' ? 'warning' : '')}>
        {task.status === 'queued' ? <Clock3 /> : success ? <Check /> : <Mail />}
      </div>
      <div className="result-copy">
        <StatusBadge task={task} />
        <h3>{task.subject}</h3>
        <p>收件人：{task.recipient}</p>
        <p>
          {task.status === 'queued'
            ? '会在 ' + formatTime(task.targetAt) + ' 后尽快寄出。定时检查可能有少量延迟。'
            : task.status === 'simulated'
              ? '预览已完成，这封信未实际发送。'
              : task.status === 'accepted'
                ? '邮件服务已接受这封信，请以收件箱的实际收到情况为准。'
                : task.reason}
        </p>
      </div>
      <button className="button primary full" onClick={onHistory}>
        去信件记录看看 <Send size={16} />
      </button>
    </Modal>
  );
}
