"""Owner-only, one-shot public request files; no SMTP or shell execution here."""
import json
import os
from pathlib import Path
import re
import tempfile

from .comic_mail import content, request_id
from .task_queue import TaskError, TaskStore, target_time
from .time_utils import parse_utc

PREFIX = 'mail-requests/'
PATTERN = re.compile(r'mail-requests/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json')
ACTIONS = {'发送自定义邮件', '发送随机来信', '发送小龙娘来信'}
LIMIT = 128 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TaskError('请求 JSON 包含重复字段；未发送。')
        result[key] = value
    return result


def validate(data, rid):
    request_id(rid)
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data.get('version') != 1
            or set(data) - {'version','request_id','action','subject','body','timing','expires_at'}
            or data.get('request_id') != rid or data.get('action') not in ACTIONS):
        raise TaskError('请求文件字段、版本、编号或操作无效；未发送。')
    event = {'action':data['action'], 'request_id':rid,
             'timing':data.get('timing','now'), 'expires_at':data.get('expires_at','')}
    if not isinstance(event['timing'], str) or not isinstance(event['expires_at'], str):
        raise TaskError('请求时间字段必须是字符串；未发送。')
    target_time(event['timing'])
    if event['expires_at']:
        parse_utc(event['expires_at'])
    if data['action'] == '发送自定义邮件':
        subject, body = content(data.get('subject'), data.get('body'))
        if not subject.strip():
            raise TaskError('请求标题不能为空；未发送。')
        event.update(custom_subject=subject, custom_body=body)
    elif 'subject' in data or 'body' in data:
        raise TaskError('日常来信请求不接受自定义正文；未发送。')
    return event


def added_requests(api, event):
    owner = os.environ.get('GITHUB_REPOSITORY_OWNER','')
    if (os.environ.get('GITHUB_EVENT_NAME') != 'push'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or event.get('ref') != 'refs/heads/main'
            or event.get('repository',{}).get('full_name') != api.repo
            or event.get('sender',{}).get('login','').casefold() != owner.casefold()
            or not owner or any(os.environ.get(k,'').casefold() != owner.casefold()
                                for k in ('GITHUB_ACTOR','GITHUB_TRIGGERING_ACTOR'))
            or event.get('forced') or event.get('deleted')):
        raise TaskError('请求文件只接受仓库所有者直接推送到 main；未发送。')
    before, after = event.get('before',''), event.get('after','')
    if (not all(isinstance(x,str) and re.fullmatch('[0-9a-f]{40}',x) for x in (before,after))
            or before == '0'*40 or after != os.environ.get('GITHUB_SHA')):
        raise TaskError('无法校验推送范围；未发送。')
    diff = api.request('GET',f'/repos/{api.repo}/compare/{before}...{after}')
    files = diff.get('files')
    if not isinstance(files,list) or len(files) >= 300:
        raise TaskError('推送文件列表不完整或超过限制；未发送。')
    paths = [x['filename'] for x in files if x.get('status') == 'added'
             and isinstance(x.get('filename'),str) and x['filename'].startswith(PREFIX)
             and x['filename'].endswith('.json')]
    if len(paths) > 1:
        raise TaskError('每次推送只允许一个新邮件请求；未发送。')
    if paths and not PATTERN.fullmatch(paths[0]):
        raise TaskError('请求文件名必须是规范的小写 UUID.json；未发送。')
    return paths


def prepare(api, *, root=None):
    from . import task_runner
    root = Path(root or Path(__file__).resolve().parents[1])
    original = Path(os.environ['GITHUB_EVENT_PATH'])
    with original.open(encoding='utf-8') as stream:
        raw = stream.read(1024*1024+1)
    if len(raw) > 1024*1024:
        raise TaskError('推送事件超过读取限制；未发送。')
    paths = added_requests(api, json.loads(raw))
    if not paths:
        return None
    path = paths[0]
    rid = PATTERN.fullmatch(path)[1]
    if TaskStore(api).get(rid) is not None:
        print('请求编号已有任务记录；只读跳过，不重复发送。')
        return None
    directory, file = root/'mail-requests', root/path
    if directory.is_symlink() or file.is_symlink():
        raise TaskError('请求路径不能是符号链接；未发送。')
    descriptor = os.open(file, os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0))
    import stat
    with os.fdopen(descriptor,'r',encoding='utf-8') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise TaskError('请求必须是普通文件；未发送。')
        raw = stream.read(LIMIT+1)
    if len(raw.encode('utf-8')) > LIMIT:
        raise TaskError('请求文件超过读取限制；未发送。')
    event = validate(json.loads(raw,object_pairs_hook=unique_object),rid)
    # Existing custom-content adapter reads a bounded event file, never env logs.
    with tempfile.TemporaryDirectory(prefix='mail-request-') as temporary:
        local_event = Path(temporary)/'event.json'
        local_event.write_text(json.dumps({'inputs':event},ensure_ascii=False),encoding='utf-8')
        local_event.chmod(0o600)
        previous = os.environ.get('GITHUB_EVENT_PATH')
        try:
            os.environ['GITHUB_EVENT_PATH'] = str(local_event)
            return task_runner.prepare(api,event=event)
        finally:
            os.environ['GITHUB_EVENT_PATH'] = previous
