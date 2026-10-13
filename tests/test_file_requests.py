import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from scripts import file_requests
from scripts.task_queue import TaskStore, TaskError
from test_task_queue import TaskAPI


class FileRequestTests(unittest.TestCase):
    def setUp(self):
        self.rid = str(uuid.uuid4())
        self.path = 'mail-requests/'+self.rid+'.json'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'mail-requests').mkdir()
        self.message = {'version':1,'request_id':self.rid,'action':'发送自定义邮件',
                        'subject':'中文标题','body':'中文正文 $(never-execute)','timing':'now'}
        (self.root/self.path).write_text(json.dumps(self.message))
        self.api = TaskAPI()
        self.api.repo = 'xurenqi/mail-bot'
        self.event = {'repository':{'full_name':self.api.repo},'sender':{'login':'xurenqi'},
                      'before':'a'*40,'after':'b'*40,'ref':'refs/heads/main'}
        self.event_file = self.root/'event.json'
        self.event_file.write_text(json.dumps(self.event))
        self.env = patch.dict(os.environ, {'GITHUB_REPOSITORY_OWNER':'xurenqi',
            'GITHUB_ACTOR':'xurenqi','GITHUB_TRIGGERING_ACTOR':'xurenqi',
            'GITHUB_SHA':'b'*40,'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'push',
            'GITHUB_EVENT_PATH':str(self.event_file),'GITHUB_ACTIONS':'true','GITHUB_RUN_ID':'123'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        original = self.api.request
        self.files = [{'filename':self.path,'status':'added'}]
        def request(method,path,data=None):
            if '/compare/' in path:
                return {'files':copy.deepcopy(self.files)}
            return original(method,path,data)
        self.api.request = request

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return file_requests.prepare(self.api,root=self.root)

    def test_new_file_uses_existing_queue_and_never_sends_during_prepare(self):
        with patch('scripts.send_mail.send') as sender:
            self.assertEqual(self.prepare(), self.rid)
            sender.assert_not_called()
        task = TaskStore(self.api).get(self.rid)
        self.assertEqual(task['status'],'queued')
        self.assertEqual(task['content']['kind'],'custom')
        self.assertEqual(os.environ['GITHUB_EVENT_PATH'],str(self.event_file))

    def test_repeat_push_skips_existing_task_without_overwriting(self):
        self.prepare()
        previous = copy.deepcopy(self.api.queue)
        self.assertIsNone(self.prepare())
        self.assertEqual(previous,self.api.queue)

    def test_future_timestamp_is_queued_without_immediate_execution(self):
        self.message['timing'] = '2099-01-01T00:00:00+08:00'
        (self.root/self.path).write_text(json.dumps(self.message))
        self.assertIsNone(self.prepare())
        self.assertEqual(TaskStore(self.api).get(self.rid)['status'],'queued')

    def test_non_owner_wrong_branch_force_push_and_wrong_repo_rejected(self):
        for change in ({'sender':{'login':'other'}},{'ref':'refs/heads/dev'},
                       {'forced':True},{'repository':{'full_name':'other/repo'}}):
            with self.subTest(change=change), self.assertRaises(TaskError):
                file_requests.added_requests(self.api,{**self.event,**change})
        with patch.dict(os.environ,{'GITHUB_TRIGGERING_ACTOR':'other'}), self.assertRaises(TaskError):
            file_requests.added_requests(self.api,self.event)

    def test_modified_deleted_and_renamed_files_never_trigger(self):
        for status in ('modified','removed','renamed'):
            self.files[0]['status'] = status
            self.assertIsNone(self.prepare())

    def test_truncated_and_multi_request_push_fail_closed(self):
        for files in ([self.files[0]]*300,[self.files[0]]*2):
            self.files = files
            with self.assertRaises(TaskError):
                self.prepare()

    def test_invalid_uuid_and_traversal_rejected(self):
        for name in ('mail-requests/../bad.json','mail-requests/send.json'):
            self.files = [{'filename':name,'status':'added'}]
            with self.assertRaises(TaskError):
                self.prepare()

    def test_symlinks_never_read(self):
        file = self.root/self.path
        file.unlink()
        file.symlink_to(self.event_file)
        with self.assertRaises(TaskError):
            self.prepare()

    def test_empty_body_unknown_fields_and_id_mismatch_rejected(self):
        for change in ({'body':''},{'command':'execute'},{'request_id':str(uuid.uuid4())},
                       {'repeat_rule':'every:1m'},{'version':True},{'timing':{}},
                       {'action':'取消任务'},{'expires_at':'2026-10-14'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                file_requests.validate({**self.message,**change},self.rid)

    def test_duplicate_json_fields_and_size_limit_rejected(self):
        file = self.root/self.path
        file.write_text('{"version":1,"version":2}')
        with self.assertRaises(TaskError):
            self.prepare()
        file.write_text('x'*(file_requests.LIMIT+1))
        with self.assertRaises(TaskError):
            self.prepare()

    def test_daily_actions_use_relay_enabled_existing_kinds(self):
        for action,kind in (('发送随机来信','random'),('发送小龙娘来信','dragon')):
            rid = str(uuid.uuid4())
            event = file_requests.validate({'version':1,'request_id':rid,'action':action},rid)
            self.assertEqual(event['action'], action)
            self.assertNotIn('custom_body',event)

    def test_zero_before_and_unmatched_head_are_rejected(self):
        for change in ({'before':'0'*40},{'after':'c'*40}):
            with self.assertRaises(TaskError):
                file_requests.added_requests(self.api,{**self.event,**change})


if __name__ == '__main__':
    unittest.main()
