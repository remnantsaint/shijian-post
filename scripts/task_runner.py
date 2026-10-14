"""Thin adapters around the single mail task lifecycle and existing transports."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid
from email.message import EmailMessage

from .github_api import GitHubAPI, GitHubAPIError
from .task_queue import TaskError, TaskStore, Tasks, new_task, repeat_rule, target_time, TERMINAL
from .time_utils import utc_now, parse_utc, format_utc, format_beijing, DeadlineExpired, check_deadline
from . import send_mail, send_letter

UNIFIED_WORKFLOW_NAME='邮件机器人（统一入口）'

# Default mode A: schedule delivery through GitHub. A blank timing is immediate
# unless the prepared letter explicitly specifies a time. Mode B (a delayed
# Foxmail reminder after immediate SMTP delivery) has no verified remote API.
PREPARED_TIMING = ''  # 'now', 'after:3d', zoned ISO time, or 'random:5m:730d'.
PREPARED_MODE = 'A'  # A=GitHub delivery; B requires external reminder integration.


def custom_default_timing():
    return PREPARED_TIMING.strip() or 'now'


def custom_delivery_mode():
    mode = PREPARED_MODE.strip().upper() or 'A'
    if mode not in {'A', 'B'}:
        raise TaskError('预设发送模式无效，仅支持 A 或 B。')
    return mode


def api_from_environment():
    token=os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:raise TaskError('缺少安全绑定的 GitHub 身份。')
    return GitHubAPI(os.environ.get('GITHUB_REPOSITORY',''),token)


def inputs():
    with open(os.environ['GITHUB_EVENT_PATH'],encoding='utf-8') as file:raw=file.read(512*1024+1)
    if len(raw)>512*1024:raise TaskError('触发内容超过限制。')
    data=json.loads(raw)
    result=data.get('inputs') or {}
    if not isinstance(result,dict):raise TaskError('触发参数无效。')
    return result


def write_output(key,value):
    path=os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path,'a',encoding='utf-8') as stream:stream.write(f'{key}={value}\n')


def private_payload(api,rid,subject,body):
    from .comic_mail import draft_release
    raw=json.dumps({'subject':subject,'body':body},ensure_ascii=False).encode('utf-8')
    if len(raw)>512*1024:raise TaskError('邮件正文超过私有内容文件限制。')
    release=draft_release(api)
    asset=api.upload_release_asset(release,f'mailbot-task-{rid}.json','application/json',raw)
    if (type(asset.get('id')) is not int or asset.get('size')!=len(raw)
            or asset.get('state')!='uploaded' or asset.get('name')!=f'mailbot-task-{rid}.json'):
        raise TaskError('私有内容保存未获确认，禁止重试或发送。')
    return {'asset_id':asset['id'],'release_id':release,'sha256':hashlib.sha256(raw).hexdigest()}


def private_image(api,rid,path):
    from .comic_mail import draft_release
    probe=EmailMessage()
    send_mail.add_image_attachment(probe,path)
    attachment=next(probe.iter_attachments())
    data=attachment.get_payload(decode=True);mime=attachment.get_content_type()
    extension={'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif'}[mime]
    release=draft_release(api)
    asset=api.upload_release_asset(release,f'mailbot-task-{rid}.{extension}',mime,data)
    if asset.get('state')!='uploaded' or asset.get('size')!=len(data) or type(asset.get('id')) is not int:
        raise TaskError('图片快照保存未获确认。')
    return {'asset_id':asset['id'],'release_id':release,'sha256':hashlib.sha256(data).hexdigest(),
            'mime':mime,'size':len(data)}


def prepare(api,*,event=None,now=None):
    injected_now=now
    now=now or utc_now()
    event=inputs() if event is None else event
    action=event.get('action','查看状态')
    if os.environ.get('GITHUB_EVENT_NAME')=='schedule':
        from .migrate_tasks import migrate
        migrate(api)
        return 'poll'
    if action in {'查看状态','运行代码检查'}:return None
    from .migrate_tasks import migrate
    migrate(api)
    store=TaskStore(api); tasks=Tasks(store)
    if action in {'关闭计划','取消任务'}:
        rid=event.get('task_id') or None
        if action=='取消任务' and not rid:raise TaskError('取消任务需填写任务编号。')
        count=tasks.cancel(rid,repeating_only=action=='关闭计划')
        print(f'已关闭 {count} 个任务；历史保留，正在处理的邮件不能撤回。')
        return None
    kinds={'发送测试邮件':'test','发送随机来信':'random','发送小龙娘来信':'dragon',
           '发送自定义邮件':'custom','发送漫画邮件':'comic','安排一次':'dragon',
           '循环随机发送':'dragon','指定时间发送':'custom','周期发送':'custom'}
    if action not in kinds:raise TaskError('未知操作，未创建任务。')
    if action=='周期发送' and not event.get('repeat_rule'):raise TaskError('周期发送必须填写重复规则，例如 every:2h。')
    if action=='指定时间发送' and not event.get('timing'):raise TaskError('指定时间发送必须填写目标时间。')
    kind=kinds[action]
    if action=='发送自定义邮件' and custom_delivery_mode()=='B':
        raise TaskError('B 延迟提醒尚未接入 Foxmail 自动提醒接口；未发送邮件，也未创建任务。请选择 A。')
    timing=event.get('timing') or (custom_default_timing() if action=='发送自定义邮件' else 'now')
    repeat_text=event.get('repeat_rule') or ''
    # Preserve old API invocations without preserving a separate scheduler.
    if action in {'安排一次','循环随机发送'} and not event.get('timing'):
        units={'分钟':'m','小时':'h','天':'d'}
        timing=f"random:{event.get('min_interval',1)}{units.get(event.get('min_unit'),'m')}:{event.get('max_interval',90)}{units.get(event.get('max_unit'),'d')}"
    if action=='循环随机发送' and not repeat_text:repeat_text=timing if timing.startswith('random:') else 'random:1m:90d'
    if event.get('send_delay')=='3 分钟内随机发送' and not event.get('timing'):timing='random:1s:180s'
    target=target_time(timing,now=now)
    repeat=repeat_rule(repeat_text)
    expires=format_utc(parse_utc(event['expires_at'])) if event.get('expires_at') else None
    rid=event.get('request_id') or str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"mail-bot:{api.repo.casefold()}:run:{os.environ.get('GITHUB_RUN_ID','')}") )
    content={'kind':kind}
    subject=body=None
    if kind in {'custom','comic'}:
        # Reuse existing validation, without displaying plaintext step envs.
        subject,body=send_mail.custom_content()
    if kind=='comic':
        from .comic_mail import Requests, adopt, digest_payload
        owner=os.environ.get('GITHUB_REPOSITORY_OWNER','')
        if not owner or any(os.environ.get(k,'').casefold()!=owner.casefold() for k in ('GITHUB_ACTOR','GITHUB_TRIGGERING_ACTOR')):
            raise TaskError('漫画任务仅接受仓库所有者的授权请求。')
        rid=event.get('request_id') or adopt(api,event.get('image_path',''),subject if event.get('custom_subject') else '',body)
        artifact,_=Requests(api).load(rid)
        if artifact is None or artifact['status']!='dispatching':raise TaskError('附件已处理或尚未准备完成，禁止重发。')
        original_subject=event.get('custom_subject','')
        if digest_payload(original_subject,body,artifact['image_sha256'])!=artifact['payload_sha256']:
            raise TaskError('附件与邮件内容不匹配，未创建任务。')
        content['image_request_id']=rid
        content['attachment']={'asset_id':artifact['asset_id'],'release_id':artifact['release_id'],
                               'sha256':artifact['image_sha256'],'mime':artifact['mime'],'size':artifact['size']}
    elif event.get('image_path'):
        content['image_path']=event['image_path']
    staging=subject is not None or bool(content.get('image_path'))
    task=new_task(rid,content,target,repeat,expires,now=now,status='staging' if staging else 'queued')
    task['origin']={'run_id':os.environ.get('GITHUB_RUN_ID',''),'attempt':int(os.environ.get('GITHUB_RUN_ATTEMPT','1'))}
    existing=store.get(rid)
    if existing is not None and existing['status']=='blocked' and existing.get('reason')=='legacy_payload_unavailable' and kind=='comic':
        def resume(records):
            old=records[rid]
            if old['status']!='blocked':raise TaskError('任务已被其他运行处理。')
            old.update(status='staging',content=content,target_at=target,repeat=repeat,expires_at=expires)
            return old
        store.update(resume)
    else:tasks.create(task)
    if staging:
        prepared={}
        try:
            if subject is not None:prepared['payload']=private_payload(api,rid,subject,body)
            if content.get('image_path'):prepared['attachment']=private_image(api,rid,content['image_path'])
            def ready(records):
                task=records[rid]
                if task['status']!='staging':raise TaskError('内容准备任务不再由本次运行持有。')
                task['content'].update(prepared)
                task['status']='queued'
                return task
            store.update(ready)
        except Exception:
            def failed(records):
                records[rid]['content'].update(prepared)
                if records[rid]['status']=='staging':records[rid].update(status='failed',reason='content_preparation_failed',finished_at=format_utc(now))
                return records[rid]
            try:store.update(failed)
            except Exception:pass
            raise
    print(f'任务 {rid} 已保存，目标北京时间 {format_beijing(parse_utc(target))}；Actions 可能延迟，不保证准点。')
    write_output('created_task_id',rid)
    return rid if parse_utc(target)<=(injected_now or utc_now()) else None


def deliver(api,task,*,smtp_sender=None):
    """Content and image adapters only; task claim/results live in task_queue."""
    from .comic_mail import Requests, RELEASE_TAG, image_type, MAX_BYTES
    kind=task['content']['kind']
    directory=None
    smtp_started=False
    previous=dict(os.environ)
    try:
        os.environ.update(IMAGE_PATH='',IMAGE_PATHS='',MAIL_FROM_NAME='',MAIL_DEADLINE=task.get('expires_at') or '')
        if kind in {'custom','comic'}:
            payload=task['content']['payload']
            release=api.request('GET',f"/repos/{api.repo}/releases/{payload['release_id']}")
            if release.get('draft') is not True or release.get('tag_name')!=RELEASE_TAG:raise TaskError('私有正文存储不再是草稿。')
            raw=api.download_release_asset(payload['asset_id'],512*1024)
            if hashlib.sha256(raw).hexdigest()!=payload['sha256']:raise TaskError('私有正文校验失败。')
            try:
                message=json.loads(raw)
                if not isinstance(message,dict) or set(message)!={'subject','body'}:raise ValueError
            except (ValueError,TypeError):raise TaskError('私有正文文件格式无效，未提交 SMTP。') from None
            fd,event_path=tempfile.mkstemp(prefix='mail-task-',suffix='.json')
            with os.fdopen(fd,'w',encoding='utf-8') as stream:json.dump({'inputs':{'custom_subject':message['subject'],'custom_body':message['body']}},stream,ensure_ascii=False)
            os.environ.update(GITHUB_EVENT_PATH=event_path,MESSAGE_TYPE='custom',GITHUB_ACTIONS='true')
        elif kind in {'random','dragon'}:
            from .reserve_letter import reserve
            _,subject,body=reserve(api)
            from .issue_relay import render
            body += render(task.get('issue_relay', []))
            os.environ.update(MAIL_SUBJECT=subject,MAIL_BODY=body,MESSAGE_TYPE='random')
        else:os.environ['MESSAGE_TYPE']='test'
        if task['content'].get('attachment'):
            artifact=task['content']['attachment']
            release=api.request('GET',f"/repos/{api.repo}/releases/{artifact['release_id']}")
            if release.get('draft') is not True or release.get('tag_name')!=RELEASE_TAG:raise TaskError('附件不再位于草稿存储。')
            data=api.download_release_asset(artifact['asset_id'],MAX_BYTES)
            if hashlib.sha256(data).hexdigest()!=artifact['sha256'] or len(data)!=artifact['size']:raise TaskError('附件内容校验失败。')
            if kind=='comic' and image_type(data)[1]!=artifact['mime']:raise TaskError('漫画图片类型校验失败。')
            images=send_mail.REPOSITORY_ROOT/'images'
            if images.is_symlink() or not images.is_dir():raise TaskError('附件临时目录无效。')
            directory=Path(tempfile.mkdtemp(prefix='.task-',dir=images))
            extension={'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif'}[artifact['mime']]
            image=directory/f'attachment.{extension}';image.write_bytes(data)
            os.environ['IMAGE_PATH']=str(image.relative_to(send_mail.REPOSITORY_ROOT))
        check_deadline(task.get('expires_at'),utc_now())
        smtp_started=True
        if kind=='dragon' and smtp_sender is None:
            os.environ['MAIL_FROM_NAME']='小龙娘'
            metadata={};accepted=send_letter.send(metadata=metadata)
            return {'accepted_at':accepted,'has_image':bool(metadata.get('attachment_count'))}
        return (smtp_sender or send_mail.send)()
    except DeadlineExpired:
        raise
    except Exception as exc:
        if not smtp_started and not isinstance(exc,(TaskError,GitHubAPIError)):
            raise TaskError('内容或附件准备失败，尚未调用 SMTP；核验文案预留与附件配置。') from None
        raise
    finally:
        if kind in {'custom','comic'} and 'event_path' in locals():
            try:Path(event_path).unlink()
            except OSError:print('临时正文清理失败；不改变发送结果。',file=sys.stderr)
        if directory:
            try:shutil.rmtree(directory)
            except OSError:print('临时图片清理失败；不改变发送结果。',file=sys.stderr)
        os.environ.clear();os.environ.update(previous)


def run(api,selection='poll',*,sender=None,now=None):
    store=TaskStore(api); engine=Tasks(store,now=now)
    def active(claim):
        try:run=api.request('GET',f"/repos/{api.repo}/actions/runs/{claim['run_id']}")
        except GitHubAPIError:return None
        return run.get('status')!='completed'
    engine.recover(active)
    tasks=store.load()[0]['tasks']
    candidates=[t for t in tasks.values() if t['status']=='queued' and (selection=='poll' or t['id']==selection)]
    candidates.sort(key=lambda t:parse_utc(t['target_at']))
    failed=False
    for task in candidates[:5]:
        if parse_utc(task['target_at'])>(now or utc_now)():continue
        claimed=engine.claim(task['id'],os.environ.get('GITHUB_RUN_ID',''),int(os.environ.get('GITHUB_RUN_ATTEMPT','1')))
        if claimed is None:continue
        result=None
        try:
            from .issue_relay import DAILY_KINDS, collect, reserve as reserve_relay
            if claimed['content']['kind'] in DAILY_KINDS:
                try:
                    claimed = reserve_relay(store, claimed, collect(api))
                except (TaskError, GitHubAPIError):
                    raise
                except Exception:
                    raise TaskError('留言准备失败，尚未调用 SMTP；未转达，不自动重试本任务。') from None
            result=(sender or (lambda task:deliver(api,task)))(claimed)
            if not isinstance(result,dict):raise ValueError('missing receipt')
            parse_utc(result['accepted_at'])
            status,reason='accepted',''
        except Exception as exc:
            status='expired' if isinstance(exc,DeadlineExpired) else 'failed' if isinstance(exc,(TaskError,GitHubAPIError,send_mail.ConfigurationError)) else send_mail.failure_details(exc)[0]
            reason='content_or_smtp_failed' if status=='failed' else 'smtp_result_uncertain'
            if isinstance(exc,GitHubAPIError):explanation=f'内容或附件 API 失败（HTTP {exc.status or "连接错误"}），未自动重试。'
            elif isinstance(exc,(TaskError,DeadlineExpired)):explanation=str(exc)
            else:explanation=send_mail.failure_details(exc)[1]
            print(explanation,file=sys.stderr)
            result=None
            failed=True
        try:engine.finish(claimed,status,result['accepted_at'] if result else None,reason)
        except Exception:
            failed=True
            reason='持久结果保存未获确认；禁止重发，先核验收件箱与运行摘要。'
            print('结果写入未获确认；禁止重发，先核验收件箱与摘要。',file=sys.stderr)
        if status=='expired':print('任务超过截止时间，未提交邮件。')
        else:send_mail.append_send_summary(status,result,reason)
        print(f"任务 {task['id']}：{status}；不等于最终投递。")
    return 1 if failed else 0


def cleanup(api):
    from .comic_mail import Requests
    store=TaskStore(api)
    engine=Tasks(store)
    for task in list(store.load()[0]['tasks'].values()):
        interrupted=task.get('interrupted_claim')
        if interrupted and not (interrupted['run_id']==os.environ.get('GITHUB_RUN_ID')
                                and interrupted['attempt']==int(os.environ.get('GITHUB_RUN_ATTEMPT','1'))):
            try:owner=api.request('GET',f"/repos/{api.repo}/actions/runs/{interrupted['run_id']}")
            except GitHubAPIError:continue
            if owner.get('status')!='completed':continue
        if (task['status']=='sending' and task['claim']['run_id']==os.environ.get('GITHUB_RUN_ID')
                and task['claim']['attempt']==int(os.environ.get('GITHUB_RUN_ATTEMPT','1'))):
            try:task=engine.finish(task,'uncertain',reason='interrupted_step')
            except Exception:
                print('中断结果保存未确认；保留附件与占用，不重发。',file=sys.stderr)
                continue
        if task['status'] not in TERMINAL or task.get('cleanup')=='deleted':continue
        assets=[]
        if task['content'].get('payload'):assets.append(task['content']['payload']['asset_id'])
        if task['content'].get('attachment'):assets.append(task['content']['attachment']['asset_id'])
        elif task['content'].get('image_request_id'):
            artifact,_=Requests(api).load(task['content']['image_request_id'])
            if artifact and artifact.get('asset_id'):assets.append(artifact['asset_id'])
        try:
            for aid in assets:
                try:api.request('DELETE',f'/repos/{api.repo}/releases/assets/{aid}')
                except GitHubAPIError as exc:
                    if exc.status!=404:raise
            def cleaned(records):
                if records[task['id']]['status'] in TERMINAL:records[task['id']]['cleanup']='deleted'
                return records[task['id']]
            store.update(cleaned)
        except Exception:print('任务附件清理未完成；发送记录保留，不因清理失败重发。',file=sys.stderr)


def main(argv=None):
    command=(argv or sys.argv[1:] or ['status'])[0]
    try:
        api=api_from_environment()
        if command=='prepare':
            if os.environ.get('GITHUB_EVENT_NAME') == 'push':
                from .file_requests import prepare as prepare_file
                selected=prepare_file(api)
            else:
                selected=prepare(api)
            write_output('execute','true' if selected else 'false')
            write_output('task_id',selected or '')
        elif command=='run':return run(api,os.environ.get('TASK_ID') or 'poll')
        elif command=='cleanup':cleanup(api)
        elif command in {'migrate','preview-migration'}:
            from .migrate_tasks import migrate
            print(json.dumps(migrate(api,dry_run=command=='preview-migration'),ensure_ascii=False))
        elif command=='status':
            for task in TaskStore(api).load()[0]['tasks'].values():
                print(json.dumps({k:task.get(k) for k in ('id','status','target_at','repeat','expires_at','last_result','accepted_at','cleanup')},ensure_ascii=False))
        else:raise TaskError('未知任务操作。')
    except (TaskError, send_mail.ConfigurationError) as exc:
        print(f'任务操作停止：{exc}',file=sys.stderr);return 1
    except GitHubAPIError as exc:
        print(f'任务 API 结果未获确认（HTTP {exc.status or "网络错误"}）；不自动重试发送。',file=sys.stderr);return 1
    except Exception as exc:
        from .comic_mail import ComicError
        if isinstance(exc, ComicError):
            print(f'漫画附件处理停止：{exc}',file=sys.stderr)
        else:
            # Exception messages may contain credentials, URLs or mail content.
            # Only report fixed categories, never raw messages or tracebacks.
            category = ('参数类型或字段错误' if isinstance(exc, (TypeError, KeyError, AttributeError))
                        else '参数值或状态格式错误' if isinstance(exc, ValueError)
                        else '本地文件访问错误' if isinstance(exc, OSError)
                        else '程序内部异常')
            print(f'任务处理失败（{category}）；未输出底层信息，禁止自动重试发送。',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':sys.exit(main())
