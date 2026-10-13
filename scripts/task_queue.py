"""One task ledger and one at-most-once lifecycle for every mail entry point."""
import base64
import copy
import json
import random
import re
import uuid
from datetime import timedelta

from .github_api import GitHubAPIError
from .state_store import STATE_BRANCH, StateBranch
from .time_utils import format_utc, parse_utc, utc_now

PATH = '.mail-bot/tasks.json'
MAX_SECONDS = 90 * 86400
STATES = {'staging', 'blocked', 'queued', 'sending', 'accepted', 'failed',
          'uncertain', 'cancelled', 'expired'}
TERMINAL = {'accepted', 'failed', 'uncertain', 'cancelled', 'expired'}


class TaskError(ValueError):
    """Static safe diagnostics only."""


def task_id(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise TaskError('任务编号必须为规范的小写 UUID。') from None
    return value


def duration(value):
    match = re.fullmatch(r'([1-9][0-9]{0,8})(s|m|h|d)', value or '')
    if not match:
        raise TaskError('间隔格式为正整数加 s/m/h/d，例如 5m、2h、30d。')
    seconds = int(match[1]) * {'s':1, 'm':60, 'h':3600, 'd':86400}[match[2]]
    if seconds > MAX_SECONDS:
        raise TaskError('间隔不能超过 90 天。')
    return seconds


def repeat_rule(value):
    if not value:
        return None
    parts = value.split(':')
    if len(parts) == 2 and parts[0] == 'every':
        return {'kind':'fixed', 'seconds':duration(parts[1])}
    if len(parts) == 3 and parts[0] == 'random':
        low, high = duration(parts[1]), duration(parts[2])
        if low > high:
            raise TaskError('最短间隔不能大于最长间隔。')
        return {'kind':'random', 'min_seconds':low, 'max_seconds':high}
    raise TaskError('重复规则为 every:2h 或 random:5m:30d，留空表示不重复。')


def target_time(value, *, now=None, choose=None):
    now = now or utc_now()
    if not value or value == 'now':
        return format_utc(now)
    if value.startswith('after:'):
        return format_utc(now+timedelta(seconds=duration(value[6:])))
    rule = repeat_rule(value) if value.startswith('random:') else None
    if rule:
        seconds = (choose or random.SystemRandom().randint)(rule['min_seconds'], rule['max_seconds'])
        return format_utc(now + timedelta(seconds=seconds))
    try:
        return format_utc(parse_utc(value))
    except ValueError:
        raise TaskError('目标时间必须带时区，例如 2026-10-20T20:00:00+08:00；也可填写 now 或 random:5m:2d。') from None


def next_target(task, accepted_at, *, choose=None):
    rule = task['repeat']
    accepted = parse_utc(accepted_at)
    if rule['kind'] == 'random':
        seconds = (choose or random.SystemRandom().randint)(rule['min_seconds'],rule['max_seconds'])
        return format_utc(accepted + timedelta(seconds=seconds))
    period = rule['seconds']
    anchor = parse_utc(task['target_at'])
    # Skip missed periods rather than sending a backlog or drifting with cron.
    periods = max(1, int((accepted-anchor).total_seconds() // period) + 1)
    return format_utc(anchor + timedelta(seconds=period * periods))


def validate(task):
    try:
        task_id(task['id'])
        if task['status'] not in STATES or task['content']['kind'] not in {'test','random','dragon','custom','comic'}:
            raise ValueError
        parse_utc(task['created_at'])
        if task.get('accepted_at'):parse_utc(task['accepted_at'])
        if set(task['content'])-{'kind','payload','attachment','image_path','image_request_id'}:raise ValueError
        for name in ('payload','attachment'):
            reference=task['content'].get(name)
            if reference:
                if any(type(reference.get(key)) is not int or reference[key]<1 for key in ('asset_id','release_id')):raise ValueError
                if not re.fullmatch(r'[0-9a-f]{64}',reference.get('sha256','')):raise ValueError
                if name=='attachment' and (reference.get('mime') not in {'image/png','image/jpeg','image/webp','image/gif'}
                                            or type(reference.get('size')) is not int or not 0<reference['size']<=5*1024*1024):raise ValueError
        if type(task['sequence']) is not int or task['sequence'] < 0:
            raise ValueError
        if task.get('target_at') is not None:
            parse_utc(task['target_at'])
        elif task['status'] in {'queued','sending'}:
            raise ValueError
        if task.get('expires_at') is not None:
            parse_utc(task['expires_at'])
        rule = task.get('repeat')
        if rule:
            if rule['kind'] == 'fixed':
                values = [rule['seconds']]
            elif rule['kind'] == 'random':
                values = [rule['min_seconds'],rule['max_seconds']]
                if values[0] > values[1]: raise ValueError
            else: raise ValueError
            if any(type(x) is not int or not 1 <= x <= MAX_SECONDS for x in values):
                raise ValueError
        if task['status'] == 'sending' and (not task.get('claim') or not task['claim']['run_id'].isdigit()
                                           or type(task['claim']['attempt']) is not int or task['claim']['attempt'] < 1):
            raise ValueError
        if not isinstance(task.get('history',[]),list) or len(task.get('history',[])) > 20:
            raise ValueError
        for record in task.get('history',[]):
            parse_utc(record['occurred_at'])
            if record['status'] not in STATES or not str(record['run_id']).isdigit():raise ValueError
            attempt=record.get('attempt',record.get('run_attempt'))
            if type(attempt) is not int or attempt<1:raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise TaskError('任务记录或时间规则无效；已阻止发送。') from None
    return task


def new_task(rid, content, target_at, repeat=None, expires_at=None, *, now=None, status='queued'):
    task = {'id':task_id(rid), 'content':copy.deepcopy(content), 'target_at':target_at,
            'repeat':repeat, 'expires_at':expires_at, 'status':status, 'sequence':0,
            'created_at':format_utc(now or utc_now()), 'claim':None, 'history':[], 'cleanup':'pending'}
    validate(task)
    if expires_at and target_at and parse_utc(expires_at) < parse_utc(target_at):
        raise TaskError('截止时间不能早于目标时间。')
    return task


class TaskStore:
    """Reuse Contents SHA compare-and-swap; retries only definite 409 conflicts."""
    def __init__(self, api):
        self.api = api

    def load(self):
        try:
            file = self.api.request('GET',f'/repos/{self.api.repo}/contents/{PATH}?ref={STATE_BRANCH}')
        except GitHubAPIError as exc:
            if exc.status == 404: return {'version':1,'tasks':{}}, None
            raise
        try:
            data = json.loads(base64.b64decode(file['content']))
            if type(data['version']) is not int or data['version'] != 1 or not isinstance(data['tasks'],dict): raise ValueError
            if not isinstance(file.get('sha'),str) or not file['sha']:raise ValueError
            for rid, task in data['tasks'].items():
                validate(task)
                if rid != task['id']: raise ValueError
            return data, file['sha']
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise TaskError('任务账本无法校验；禁止覆盖或发送。') from None

    def update(self, change, metadata=None):
        StateBranch(self.api).ensure_branch()
        for attempt in range(4):
            data, sha = self.load()
            result = change(data['tasks'])  # Pure transformation; no SMTP/API side effects.
            if metadata: data.update(metadata)
            for task in data['tasks'].values(): validate(task)
            raw = (json.dumps(data,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8')
            if len(raw) > 900 * 1024:
                raise TaskError('任务账本超过状态文件容量；需要保留去重记录并安排归档，未发送。')
            payload = {'message':'Update unified mail task metadata','branch':STATE_BRANCH,
                       'content':base64.b64encode(raw).decode('ascii')}
            if sha is not None: payload['sha'] = sha
            try:
                acknowledged=self.api.request('PUT',f'/repos/{self.api.repo}/contents/{PATH}',payload)
                if (not isinstance(acknowledged,dict) or not isinstance(acknowledged.get('content'),dict)
                        or not isinstance(acknowledged['content'].get('sha'),str) or not acknowledged['content']['sha']):
                    raise TaskError('状态写入缺少有效确认；禁止发送或自动重试。')
                return copy.deepcopy(result)
            except GitHubAPIError as exc:
                if exc.status != 409 or attempt == 3: raise

    def get(self, rid):
        return self.load()[0]['tasks'].get(task_id(rid))


class Tasks:
    def __init__(self, store, *, now=None, choose=None):
        self.store, self.now, self.choose = store, now or utc_now, choose

    def create(self, task):
        def change(tasks):
            if task['id'] in tasks:
                raise TaskError('任务已存在；禁止重跑或覆盖，请查看原任务结果。')
            tasks[task['id']] = copy.deepcopy(task)
            return task
        return self.store.update(change)

    def claim(self, rid, run_id, attempt=1):
        now = self.now()
        if not str(run_id).isdigit() or type(attempt) is not int or attempt < 1:
            raise TaskError('缺少有效运行身份；未发送。')
        def change(tasks):
            task = tasks[rid]
            if task['status'] != 'queued': return None
            if task.get('expires_at') and parse_utc(task['expires_at']) < now:
                task.update(status='expired',finished_at=format_utc(now),last_result='expired')
                return None
            if parse_utc(task['target_at']) > now: return None
            task.update(status='sending',claim={'run_id':str(run_id),'attempt':attempt,'sequence':task['sequence']})
            return task
        return self.store.update(change)

    def finish(self, claimed, result, accepted_at=None, reason=''):
        if result not in {'accepted','failed','uncertain','expired'}: raise TaskError('发送结果类型无效。')
        ended = format_utc(self.now())
        def change(tasks):
            task = tasks[claimed['id']]
            if task['status'] != 'sending' or task['claim'] != claimed['claim']:
                raise TaskError('本次运行不再持有任务；未覆盖结果或重发。')
            entry = {**task['claim'],'status':result,'occurred_at':accepted_at or ended,
                     'time_source':'smtp_accepted' if accepted_at else 'result_recorded'}
            task['history'] = (task['history']+[entry])[-20:]
            task.update(status=result,last_result=result,claim=None,finished_at=ended,reason=reason)
            if accepted_at: task['accepted_at'] = format_utc(parse_utc(accepted_at))
            if result == 'accepted' and task['repeat']:
                target = next_target(task,task['accepted_at'],choose=self.choose)
                if not task.get('expires_at') or parse_utc(target) <= parse_utc(task['expires_at']):
                    task.update(status='queued',target_at=target,sequence=task['sequence']+1)
            return task
        return self.store.update(change)

    def cancel(self, rid=None, *, repeating_only=False):
        def change(tasks):
            count = 0
            for task in tasks.values():
                if rid is not None and task['id'] != rid: continue
                if repeating_only and not task['repeat']: continue
                if task['status'] not in {'queued','sending','blocked','staging'}: continue
                if task['status']=='sending':task['interrupted_claim']=copy.deepcopy(task['claim'])
                task.update(status='uncertain' if task['status']=='sending' else 'cancelled',
                            last_result='uncertain' if task['status']=='sending' else 'cancelled',
                            finished_at=format_utc(self.now()),reason='user_cancelled',claim=None)
                count += 1
            return count
        return self.store.update(change)

    def recover(self, run_active):
        for task in list(self.store.load()[0]['tasks'].values()):
            if task['status'] == 'sending' and run_active(task['claim']) is False:
                self.finish(task,'uncertain',reason='interrupted_run')
