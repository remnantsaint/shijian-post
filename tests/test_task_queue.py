import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone
import uuid
import smtplib

from scripts.task_queue import TaskStore, Tasks, TaskError, new_task, target_time, repeat_rule, PATH
from scripts import task_runner
from scripts.migrate_tasks import migrate
from scripts.github_api import GitHubAPIError
from test_comic_mail import FakeAPI, png

NOW=datetime(2026,10,20,12,0,tzinfo=timezone.utc)


class TaskAPI(FakeAPI):
    def __init__(self):
        super().__init__();self.queue=None;self.qsha=None;self.old_plan=None
        self.conflict=False;self.uncertain_write=False;self.active=True

    def request(self,method,path,data=None):
        if '/issues?' in path:
            return []
        if '/contents/'+PATH in path:
            self.calls.append((method,path,copy.deepcopy(data)))
            if method=='GET':
                if self.queue is None:raise GitHubAPIError('not found',status=404)
                return {'sha':self.qsha,'encoding':'base64','content':base64.b64encode(json.dumps(self.queue).encode()).decode()}
            if self.conflict:
                self.conflict=False;raise GitHubAPIError('conflict',status=409)
            if self.uncertain_write:raise GitHubAPIError('unknown write')
            if data.get('sha')!=self.qsha:raise GitHubAPIError('conflict',status=409)
            proposed=json.loads(base64.b64decode(data['content']))
            if self.fail_terminal_save and any(task['status'] in {'accepted','failed','uncertain'} for task in proposed['tasks'].values()):
                raise GitHubAPIError('private lost result')
            self.queue=proposed
            self.version+=1;self.qsha=str(self.version)
            return {'content':{'sha':self.qsha}}
        if '/contents/.mail-bot/random.json' in path:
            if self.old_plan is None:raise GitHubAPIError('missing',status=404)
            return {'content':base64.b64encode(json.dumps(self.old_plan).encode()).decode()}
        if path.split('?')[0].endswith('/contents/.mail-bot/image-requests'):
            return [{'type':'file','name':rid+'.json'} for rid in self.records]
        if '/actions/runs/' in path:return {'status':'in_progress' if self.active else 'completed'}
        return super().request(method,path,data)

    def upload_release_asset(self,release,name,mime,data):
        aid=20+len(self.assets)
        metadata={'id':aid,'name':name,'size':len(data),'state':'uploaded','content_type':mime}
        self.assets[aid]={'metadata':metadata,'data':data}
        self.calls.append(('UPLOAD',name,None))
        return metadata


class TasksTests(unittest.TestCase):
    def setUp(self):
        self.api=TaskAPI();self.store=TaskStore(self.api);self.now=NOW
        self.engine=Tasks(self.store,now=lambda:self.now,choose=lambda low,high:high)
        self.rid=str(uuid.uuid4())
        self.task=new_task(self.rid,{'kind':'test'},NOW.isoformat(),now=NOW)

    def test_immediate_specified_random_use_same_record_and_clock(self):
        self.assertEqual(target_time('now',now=NOW),NOW.isoformat().replace('+00:00','Z'))
        self.assertEqual(target_time('after:2m',now=NOW),(NOW+timedelta(minutes=2)).isoformat().replace('+00:00','Z'))
        self.assertEqual(target_time('2026-10-20T20:00:00+08:00',now=NOW),target_time('now',now=NOW))
        self.assertEqual(target_time('random:1m:2h',now=NOW,choose=lambda lo,hi:hi),(NOW+timedelta(hours=2)).isoformat().replace('+00:00','Z'))
        with self.assertRaises(TaskError):target_time('2026-10-20 20:00:00',now=NOW)
        with self.assertRaises(TaskError):repeat_rule('random:2d:1d')
        with self.assertRaises(TaskError):repeat_rule('every:91d')

    def test_claim_before_send_and_reruns_cannot_claim_again(self):
        self.engine.create(self.task)
        first=self.engine.claim(self.rid,'123')
        self.assertEqual(first['status'],'sending')
        self.assertIsNone(self.engine.claim(self.rid,'124'))
        self.engine.finish(first,'accepted',NOW.isoformat())
        self.assertIsNone(self.engine.claim(self.rid,'123',2))
        with self.assertRaises(TaskError):self.engine.create(self.task)

    def test_definite_cas_conflict_retries_state_but_uncertain_write_does_not(self):
        self.api.conflict=True;self.engine.create(self.task)
        writes=[call for call in self.api.calls if call[0]=='PUT' and PATH in call[1]]
        self.assertEqual(len(writes),2)
        self.api.uncertain_write=True
        before=len(self.api.calls)
        with self.assertRaises(GitHubAPIError):self.engine.claim(self.rid,'123')
        writes=[call for call in self.api.calls[before:] if call[0]=='PUT']
        self.assertEqual(len(writes),1)

    def test_missing_write_acknowledgement_never_exposes_a_sending_lease(self):
        self.engine.create(self.task)
        original=self.api.request
        def request(method,path,data=None):
            result=original(method,path,data)
            return None if method=='PUT' and PATH in path else result
        with patch.object(self.api,'request',side_effect=request),self.assertRaises(TaskError):
            self.engine.claim(self.rid,'123')
        self.assertEqual(self.store.get(self.rid)['status'],'sending')

    def test_future_tasks_do_not_claim_early_late_tasks_send_once(self):
        self.task['target_at']=(NOW+timedelta(hours=1)).isoformat()
        self.engine.create(self.task)
        self.assertIsNone(self.engine.claim(self.rid,'123'))
        self.now=NOW+timedelta(days=5)
        claimed=self.engine.claim(self.rid,'124')
        self.assertIsNotNone(claimed)
        self.engine.finish(claimed,'accepted',self.now.isoformat())
        self.assertIsNone(self.engine.claim(self.rid,'125'))

    def test_expiry_is_distinct_from_action_lateness(self):
        self.task['expires_at']=(NOW+timedelta(minutes=1)).isoformat()
        self.engine.create(self.task);self.now=NOW+timedelta(hours=1)
        self.assertIsNone(self.engine.claim(self.rid,'123'))
        self.assertEqual(self.store.get(self.rid)['status'],'expired')

    def test_fixed_repeat_skips_missed_periods_without_drift_or_burst(self):
        self.task['repeat']=repeat_rule('every:1h');self.engine.create(self.task)
        self.now=NOW+timedelta(hours=3,minutes=10)
        claimed=self.engine.claim(self.rid,'123')
        next_task=self.engine.finish(claimed,'accepted',self.now.isoformat())
        self.assertEqual(next_task['status'],'queued')
        self.assertEqual(next_task['target_at'],(NOW+timedelta(hours=4)).isoformat().replace('+00:00','Z'))
        self.assertEqual(next_task['sequence'],1)
        self.assertIsNone(self.engine.claim(self.rid,'124'))

    def test_random_repeat_only_generates_next_target_after_confirmed_acceptance(self):
        self.task['repeat']=repeat_rule('random:1m:2h');self.engine.create(self.task)
        claimed=self.engine.claim(self.rid,'123')
        after=self.engine.finish(claimed,'accepted',NOW.isoformat())
        self.assertEqual(after['target_at'],(NOW+timedelta(hours=2)).isoformat().replace('+00:00','Z'))
        self.now=NOW+timedelta(hours=2)
        claimed=self.engine.claim(self.rid,'124')
        self.engine.finish(claimed,'uncertain',reason='disconnected')
        self.assertEqual(self.store.get(self.rid)['status'],'uncertain')
        self.assertIsNone(self.engine.claim(self.rid,'125'))

    def test_recovery_waits_for_live_owner_and_never_resends_abandoned_attempt(self):
        self.engine.create(self.task);self.engine.claim(self.rid,'123')
        self.engine.recover(lambda claim:True)
        self.assertEqual(self.store.get(self.rid)['status'],'sending')
        self.engine.recover(lambda claim:None)
        self.assertEqual(self.store.get(self.rid)['status'],'sending')
        self.engine.recover(lambda claim:False)
        self.assertEqual(self.store.get(self.rid)['status'],'uncertain')
        self.assertIsNone(self.engine.claim(self.rid,'124'))

    def test_cancelled_claim_cannot_be_overwritten_or_repeated(self):
        self.engine.create(self.task);claimed=self.engine.claim(self.rid,'123')
        self.engine.cancel(self.rid)
        with self.assertRaises(TaskError):self.engine.finish(claimed,'accepted',NOW.isoformat())
        self.assertEqual(self.store.get(self.rid)['status'],'uncertain')

    def test_pause_repeating_does_not_cancel_independent_one_shot(self):
        self.engine.create(self.task)
        repeat=new_task(str(uuid.uuid4()),{'kind':'random'},NOW.isoformat(),repeat_rule('every:2h'))
        self.engine.create(repeat)
        self.assertEqual(self.engine.cancel(repeating_only=True),1)
        self.assertEqual(self.store.get(self.rid)['status'],'queued')
        self.assertEqual(self.store.get(repeat['id'])['status'],'cancelled')

    def test_recorded_deadline_expiry_does_not_repeat(self):
        self.task['repeat']=repeat_rule('every:1h');self.engine.create(self.task)
        claim=self.engine.claim(self.rid,'123')
        expired=self.engine.finish(claim,'expired',reason='deadline_before_data')
        self.assertEqual(expired['status'],'expired')
        self.assertIsNone(self.engine.claim(self.rid,'124'))

    def test_migration_preserves_disabled_plan_and_history_idempotently(self):
        history={'run_id':'45','run_attempt':1,'occurred_at':NOW.isoformat(),'status':'accepted','time_source':'smtp_accepted'}
        self.api.old_plan={'version':1,'enabled':False,'mode':'repeat','state':'disabled',
                           'min_minutes':120,'max_minutes':360,'next_send_at':None,'claim_run_id':None,
                           'claim_run_attempt':None,'last_send':history,'history':[history]}
        original=copy.deepcopy(self.api.old_plan)
        self.assertEqual(migrate(self.api,dry_run=True)['imported'],1)
        self.assertIsNone(self.api.queue)
        self.assertEqual(migrate(self.api)['imported'],1)
        task=next(iter(self.api.queue['tasks'].values()))
        self.assertEqual(task['status'],'cancelled')
        self.assertEqual(task['legacy']['state'],original)
        self.assertEqual(task['accepted_at'],history['occurred_at'])
        self.assertEqual(self.api.old_plan,original)
        self.assertEqual(migrate(self.api)['imported'],0)

    def test_migration_never_retries_legacy_sending_plan(self):
        self.api.old_plan={'version':1,'enabled':True,'mode':'once','state':'sending',
                           'min_minutes':1,'max_minutes':2,'next_send_at':NOW.isoformat(),
                           'claim_run_id':'123','claim_run_attempt':1,'last_send':None,'history':[]}
        migrate(self.api)
        self.assertEqual(next(iter(self.api.queue['tasks'].values()))['status'],'uncertain')


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'images').mkdir()
        self.event=self.root/'event.json';self.api=TaskAPI()
        self.env=patch.dict(os.environ,{'GITHUB_REPOSITORY':'owner/mail-bot','GITHUB_RUN_ID':'123',
            'GITHUB_RUN_ATTEMPT':'1','GITHUB_ACTIONS':'true','GITHUB_EVENT_NAME':'workflow_dispatch',
            'GITHUB_EVENT_PATH':str(self.event),'GITHUB_REPOSITORY_OWNER':'owner',
            'GITHUB_ACTOR':'owner','GITHUB_TRIGGERING_ACTOR':'owner',
            'QQ_EMAIL':'sender@qq.com','MAIL_TO':'receiver@example.com','QQ_SMTP_AUTH_CODE':'private-test-code',
            'GITHUB_STEP_SUMMARY':str(self.root/'summary')},clear=True)
        self.env.start();self.addCleanup(self.env.stop)
        self.console=io.StringIO()
        capture=contextlib.redirect_stdout(self.console);capture.__enter__();self.addCleanup(capture.__exit__,None,None,None)
        error=contextlib.redirect_stderr(self.console);error.__enter__();self.addCleanup(error.__exit__,None,None,None)

    def event_write(self,**values):
        self.event.write_text(json.dumps({'inputs':values}))

    def test_blank_custom_timing_is_immediate_and_nonrepeating(self):
        self.event_write(action='发送自定义邮件', custom_subject='title', custom_body='body')
        with patch.object(task_runner, 'PREPARED_TIMING', ''), patch.object(task_runner, 'PREPARED_MODE', 'A'):
            rid=task_runner.prepare(self.api,now=NOW)
        task=TaskStore(self.api).get(rid)
        self.assertEqual(task['target_at'],NOW.isoformat().replace('+00:00','Z'))
        self.assertIsNone(task['repeat'])

    def test_prepared_time_and_manual_override(self):
        self.event_write(action='发送自定义邮件',custom_body='body')
        with patch.object(task_runner,'PREPARED_TIMING','after:3d'), patch.object(task_runner,'PREPARED_MODE','A'):
            self.assertIsNone(task_runner.prepare(self.api,now=NOW))
        task=next(iter(self.api.queue['tasks'].values()))
        self.assertEqual(task['target_at'],(NOW+timedelta(days=3)).isoformat().replace('+00:00','Z'))
        self.assertIsNone(task['repeat'])
        self.event_write(action='发送自定义邮件',timing='now',custom_body='body',request_id=str(uuid.uuid4()))
        with patch.object(task_runner,'PREPARED_TIMING','after:3d'), patch.object(task_runner,'PREPARED_MODE','A'):
            rid=task_runner.prepare(self.api,now=NOW)
        self.assertEqual(TaskStore(self.api).get(rid)['target_at'],NOW.isoformat().replace('+00:00','Z'))

    def test_unavailable_reminder_mode_fails_without_creating_task(self):
        self.event_write(action='发送自定义邮件',custom_body='body')
        with patch.object(task_runner,'PREPARED_MODE','B'),self.assertRaises(TaskError):
            task_runner.prepare(self.api,now=NOW)
        self.assertFalse(TaskStore(self.api).load()[0]['tasks'])

    @patch('scripts.send_mail.smtplib.SMTP_SSL')
    def test_future_custom_payload_private_then_unified_delivery_and_cleanup(self,smtp):
        smtp.return_value.send_message.return_value={}
        self.event_write(action='指定时间发送',timing=(NOW+timedelta(hours=1)).isoformat(),
                         custom_subject='私有测试标题',custom_body='私有中文测试正文')
        self.assertIsNone(task_runner.prepare(self.api,now=NOW))
        task=next(iter(TaskStore(self.api).load()[0]['tasks'].values()))
        self.assertNotIn('私有中文测试正文',json.dumps(self.api.queue,ensure_ascii=False))
        sender=lambda t:task_runner.deliver(self.api,t)
        with patch('scripts.send_mail.REPOSITORY_ROOT',self.root):
            self.assertEqual(task_runner.run(self.api,now=lambda:NOW,sender=sender),0)
            smtp.return_value.send_message.assert_not_called()
            self.assertEqual(task_runner.run(self.api,now=lambda:NOW+timedelta(hours=2),sender=sender),0)
        smtp.return_value.send_message.assert_called_once()
        self.assertIn('私有中文测试正文',smtp.return_value.send_message.call_args.args[0].get_content())
        self.assertEqual(TaskStore(self.api).get(task['id'])['status'],'accepted')
        task_runner.cleanup(self.api)
        self.assertFalse(self.api.assets)
        self.assertEqual(TaskStore(self.api).get(task['id'])['cleanup'],'deleted')

    @patch('scripts.send_mail.smtplib.SMTP_SSL')
    def test_browser_comic_and_custom_use_identical_lifecycle(self,smtp):
        smtp.return_value.send_message.return_value={}
        self.api.assets[20]={'metadata':{'id':20,'name':'comic.png','size':len(png()),'state':'uploaded','content_type':'image/png'},'data':png()}
        self.event_write(action='发送漫画邮件',image_path='draft:comic.png',custom_subject='漫画',custom_body='漫画正文')
        rid=task_runner.prepare(self.api,now=NOW)
        with patch('scripts.send_mail.REPOSITORY_ROOT',self.root):
            self.assertEqual(task_runner.run(self.api,rid,now=lambda:NOW),0)
        message=smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(list(message.iter_attachments())[0].get_payload(decode=True),png())
        self.assertEqual(TaskStore(self.api).get(rid)['status'],'accepted')
        task_runner.cleanup(self.api)
        self.assertFalse(self.api.assets)
        with self.assertRaises(Exception):task_runner.prepare(self.api,now=NOW)
        smtp.return_value.send_message.assert_called_once()

    def test_smtp_uncertainty_and_cleanup_failure_never_resend(self):
        self.event_write(action='发送测试邮件')
        rid=task_runner.prepare(self.api,now=NOW)
        sender=Mock(side_effect=smtplib.SMTPServerDisconnected('private-test-code'))
        self.assertEqual(task_runner.run(self.api,rid,now=lambda:NOW,sender=sender),1)
        self.assertEqual(TaskStore(self.api).get(rid)['status'],'uncertain')
        self.assertEqual(task_runner.run(self.api,rid,now=lambda:NOW,sender=sender),0)
        sender.assert_called_once()
        self.assertNotIn('private-test-code',self.console.getvalue())

    def test_status_does_not_create_state_or_send(self):
        self.event_write(action='查看状态')
        self.assertIsNone(task_runner.prepare(self.api,now=NOW))
        self.assertIsNone(self.api.queue)
        self.assertFalse(self.api.calls)

    @patch('scripts.send_mail.smtplib.SMTP_SSL')
    def test_delayed_repo_image_is_snapshotted_not_replaced_by_later_commit(self,smtp):
        smtp.return_value.send_message.return_value={}
        original=png();(self.root/'images'/'picture.png').write_bytes(original)
        self.event_write(action='发送测试邮件',timing=(NOW+timedelta(hours=1)).isoformat(),image_path='images/picture.png')
        with patch('scripts.send_mail.REPOSITORY_ROOT',self.root):
            task_runner.prepare(self.api,now=NOW)
            (self.root/'images'/'picture.png').write_bytes(png(3,3))
            task_runner.run(self.api,now=lambda:NOW+timedelta(hours=2))
        message=smtp.return_value.send_message.call_args.args[0]
        self.assertEqual(list(message.iter_attachments())[0].get_payload(decode=True),original)

    def test_periodic_action_without_rule_cannot_send_immediately(self):
        self.event_write(action='周期发送',custom_body='body')
        with self.assertRaises(TaskError):task_runner.prepare(self.api,now=NOW)
        self.assertFalse(TaskStore(self.api).load()[0]['tasks'])

    def test_private_payload_failure_is_recorded_without_smtp_or_duplicate_upload(self):
        self.event_write(action='发送自定义邮件',custom_subject='title',custom_body='body')
        with patch.object(self.api,'upload_release_asset',side_effect=GitHubAPIError('unknown upload')):
            with self.assertRaises(GitHubAPIError):task_runner.prepare(self.api,now=NOW)
        self.assertEqual(next(iter(self.api.queue['tasks'].values()))['status'],'failed')
        with self.assertRaises(TaskError):task_runner.prepare(self.api,now=NOW)

    @patch('scripts.send_mail.smtplib.SMTP_SSL')
    def test_reservation_failure_is_failed_not_uncertain_and_never_opens_smtp(self,smtp):
        from scripts.reserve_letter import ReservationError
        self.event_write(action='发送随机来信')
        rid=task_runner.prepare(self.api,now=NOW)
        with patch('scripts.reserve_letter.reserve',side_effect=ReservationError('素材耗尽')):
            self.assertEqual(task_runner.run(self.api,rid,now=lambda:NOW),1)
        self.assertEqual(TaskStore(self.api).get(rid)['status'],'failed')
        smtp.assert_not_called()

    def test_cancelled_live_sender_assets_are_not_deleted_by_another_run(self):
        task=new_task(str(uuid.uuid4()),{'kind':'custom','payload':{'asset_id':20,'release_id':10,'sha256':'a'*64}},NOW.isoformat())
        tasks=Tasks(TaskStore(self.api),now=lambda:NOW);tasks.create(task)
        tasks.claim(task['id'],'999');tasks.cancel(task['id'])
        self.api.assets[20]={'metadata':{},'data':b'private'}
        task_runner.cleanup(self.api)
        self.assertIn(20,self.api.assets)
        self.api.active=False
        task_runner.cleanup(self.api)
        self.assertNotIn(20,self.api.assets)

    def test_cleanup_failure_cannot_change_accepted_task_or_repeat_smtp(self):
        task=new_task(str(uuid.uuid4()),{'kind':'custom','payload':{'asset_id':20,'release_id':10,'sha256':'a'*64}},NOW.isoformat())
        tasks=Tasks(TaskStore(self.api),now=lambda:NOW);tasks.create(task)
        claim=tasks.claim(task['id'],'123');tasks.finish(claim,'accepted',NOW.isoformat())
        self.api.assets[20]={'metadata':{},'data':b'private'};self.api.fail_cleanup=True
        task_runner.cleanup(self.api)
        self.assertEqual(TaskStore(self.api).get(task['id'])['status'],'accepted')
        self.assertIsNone(tasks.claim(task['id'],'124'))
