import base64
import contextlib
import copy
from email import policy
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import smtplib
import struct
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import uuid
import zlib

from scripts import comic_mail as comic
from scripts.github_api import GitHubAPI, GitHubAPIError


def png(width=2, height=2):
    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data)))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * width) * height))
            + chunk(b'IEND', b''))


JPEG = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK+ZP3E//Z')
WEBP = base64.b64decode('UklGRjwAAABXRUJQVlA4IDAAAADQAQCdASoCAAIAAUAmJaACdLoB+AADsAD+8ut//NgVzXPv9//S4P0uD9Lg/9KQAAA=')


class FakeAPI:
    repo = 'owner/mail-bot'

    def __init__(self):
        self.records = {}
        self.calls = []
        self.release = {'id': 10, 'draft': True, 'tag_name': comic.RELEASE_TAG}
        self.assets = {}
        self.version = 0
        self.fail_dispatch = False
        self.fail_cleanup = False
        self.fail_terminal_save = False
        self.tamper = False

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        if '/contents/.mail-bot/image-requests/' in path:
            rid = path.split('/')[-1].split('.json')[0]
            if method == 'GET':
                if rid not in self.records:
                    raise GitHubAPIError('private raw body', status=404)
                record, sha = self.records[rid]
                return {'sha': sha, 'content': base64.b64encode(json.dumps(record).encode()).decode()}
            record = json.loads(base64.b64decode(data['content']))
            previous = self.records.get(rid)
            if data.get('sha') != (previous[1] if previous else None):
                raise GitHubAPIError('private collision', status=409)
            if self.fail_terminal_save and record['status'] in {'accepted','failed','uncertain'}:
                raise GitHubAPIError('private uncertain save')
            self.version += 1
            sha = str(self.version)
            self.records[rid] = (record, sha)
            return {'content': {'sha': sha}}
        if path.endswith('/git/ref/heads/mail-bot-state'):
            return {'object': {'sha': 'head'}}
        if path.endswith('/releases?per_page=100'):
            return [copy.deepcopy(self.release)]
        if path.endswith('/releases/10'):
            return copy.deepcopy(self.release)
        if path.endswith('/releases/10/assets?per_page=100'):
            return [copy.deepcopy(asset['metadata']) for asset in self.assets.values()]
        if method == 'DELETE' and '/releases/assets/' in path:
            if self.fail_cleanup:
                raise GitHubAPIError('private cleanup detail', status=403)
            asset_id = int(path.rsplit('/', 1)[1])
            if asset_id not in self.assets:
                raise GitHubAPIError('absent', status=404)
            del self.assets[asset_id]
            return None
        if path == '/repos/' + self.repo:
            return {'default_branch': 'main'}
        if path.endswith('/dispatches'):
            if self.fail_dispatch:
                raise GitHubAPIError('private uncertain dispatch')
            return None
        raise AssertionError((method, path))

    def upload_release_asset(self, rid, name, mime, data):
        self.calls.append(('UPLOAD', name, None))
        asset = {'id': 20, 'name': name, 'size': len(data), 'state': 'uploaded', 'content_type': mime}
        self.assets[20] = {'metadata': asset, 'data': data}
        return copy.deepcopy(asset)

    def download_release_asset(self, aid, max_bytes):
        self.calls.append(('DOWNLOAD', str(aid), None))
        return b'corrupt private image' if self.tamper else self.assets[aid]['data']


class ComicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'images').mkdir()
        self.image = self.root / 'comic.png'
        self.image.write_bytes(png())
        self.rid = str(uuid.uuid4())
        self.api = FakeAPI()
        self.event = self.root / 'event.json'
        self.event.write_text(json.dumps({'inputs': {'action': comic.ACTION, 'request_id': self.rid,
                                                    'custom_subject': '漫画来信', 'custom_body': '小澪的中文漫画正文。'}}))
        self.summary = self.root / 'summary.md'
        self.env = patch.dict(os.environ, {
            'GITHUB_EVENT_PATH': str(self.event), 'GITHUB_ACTIONS': 'true',
            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_STEP_SUMMARY': str(self.summary), 'GITHUB_OUTPUT': str(self.root / 'output'),
            'QQ_EMAIL': 'sender@qq.com', 'MAIL_TO': 'receiver@example.com',
            'QQ_SMTP_AUTH_CODE': 'private-test-code',
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.console = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.console)
        self.stderr = contextlib.redirect_stderr(self.console)
        self.stdout.__enter__(); self.stderr.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.addCleanup(self.stderr.__exit__, None, None, None)

    def upload(self):
        return comic.upload(self.api, self.image, '漫画来信', '小澪的中文漫画正文。', self.rid)

    def record(self):
        return self.api.records[self.rid][0]

    def test_upload_uses_binary_draft_asset_and_dispatches_only_metadata(self):
        self.upload()
        self.assertEqual(self.record()['status'], 'dispatching')
        dispatch = next(data for method, path, data in self.api.calls if path.endswith('/dispatches'))
        self.assertEqual(dispatch['inputs']['request_id'], self.rid)
        self.assertEqual(dispatch['inputs']['action'], comic.ACTION)
        self.assertNotIn('image_path', dispatch['inputs'])
        record = json.dumps(self.record())
        self.assertNotIn('漫画来信', record)
        self.assertNotIn('中文漫画正文', record)
        self.assertEqual(self.api.assets[20]['data'], self.image.read_bytes())
        with self.assertRaises(comic.ComicError):
            self.upload()
        self.assertEqual(sum(method == 'UPLOAD' for method, _, _ in self.api.calls), 1)

    @patch('scripts.send_mail.smtplib.SMTP_SSL')
    def test_end_to_end_real_mime_exact_png_utf8_receipt_and_cleanup(self, smtp):
        smtp.return_value.send_message.return_value = {}
        self.upload()
        with patch('scripts.send_mail.REPOSITORY_ROOT', self.root):
            self.assertEqual(comic.receive(self.api, root=self.root), 0)
        message = smtp.return_value.send_message.call_args.args[0]
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        attachment = list(parsed.iter_attachments())[0]
        self.assertEqual(attachment.get_content_type(), 'image/png')
        self.assertEqual(attachment.get_payload(decode=True), self.image.read_bytes())
        self.assertIn('小澪的中文漫画正文。', parsed.get_body().get_content())
        self.assertIn('写信时间（北京时间 UTC+8）', parsed.get_body().get_content())
        self.assertEqual(self.record()['status'], 'accepted')
        self.assertNotIn('中文漫画正文', self.summary.read_text())
        self.assertFalse(list((self.root / 'images').rglob('attachment.png')))
        comic.cleanup(self.api)
        self.assertEqual(self.record()['status'], 'accepted')
        self.assertEqual(self.record()['cleanup'], 'deleted')
        self.assertFalse(self.api.assets)
        with self.assertRaises(comic.ComicError):
            comic.receive(self.api, root=self.root)
        smtp.return_value.send_message.assert_called_once()

    def test_lost_dispatch_response_blocks_repeat(self):
        self.api.fail_dispatch = True
        with self.assertRaises(GitHubAPIError):
            self.upload()
        self.assertEqual(self.record()['status'], 'dispatching')
        with self.assertRaises(comic.ComicError):
            self.upload()
        self.assertEqual(sum(path.endswith('/dispatches') for _, path, _ in self.api.calls), 1)

    def test_payload_tampering_does_not_claim_or_call_smtp(self):
        self.upload()
        event = json.loads(self.event.read_text())
        event['inputs']['custom_body'] = 'tampered private text'
        self.event.write_text(json.dumps(event))
        sender = unittest.mock.Mock()
        with self.assertRaises(comic.ComicError):
            comic.receive(self.api, sender=sender, root=self.root)
        sender.assert_not_called()
        self.assertEqual(self.record()['status'], 'dispatching')

    def test_bad_download_and_published_storage_fail_before_smtp(self):
        for cause in ('tamper','published'):
            self.api = FakeAPI(); self.rid = str(uuid.uuid4())
            event = json.loads(self.event.read_text()); event['inputs']['request_id'] = self.rid
            self.event.write_text(json.dumps(event)); self.upload()
            if cause == 'tamper': self.api.tamper = True
            else: self.api.release['draft'] = False
            sender = unittest.mock.Mock()
            self.assertEqual(comic.receive(self.api, sender=sender, root=self.root), 1)
            sender.assert_not_called()
            self.assertEqual(self.record()['status'], 'failed')

    def test_concurrent_duplicate_claim_never_reaches_sender(self):
        self.upload()
        original = self.api.request
        def request(method,path,data=None):
            if method == 'PUT' and '/image-requests/' in path:
                record = json.loads(base64.b64decode(data['content']))
                if record['status'] == 'claimed':
                    raise GitHubAPIError('conflict private', status=409)
            return original(method,path,data)
        sender = unittest.mock.Mock()
        with patch.object(self.api,'request',side_effect=request), self.assertRaises(GitHubAPIError):
            comic.receive(self.api,sender=sender,root=self.root)
        sender.assert_not_called()

    def test_smtp_outcomes_no_automatic_retry(self):
        for error, expected in ((smtplib.SMTPAuthenticationError(535,b'private-test-code'),'failed'),
                                (smtplib.SMTPServerDisconnected('private-test-code'),'uncertain')):
            self.api = FakeAPI(); self.rid = str(uuid.uuid4())
            event = json.loads(self.event.read_text()); event['inputs']['request_id'] = self.rid
            self.event.write_text(json.dumps(event)); self.upload()
            sender = unittest.mock.Mock(side_effect=error)
            self.assertEqual(comic.receive(self.api,sender=sender,root=self.root),1)
            self.assertEqual(self.record()['status'], expected)
            with self.assertRaises(comic.ComicError):
                comic.receive(self.api,sender=sender,root=self.root)
            sender.assert_called_once()
        self.assertNotIn('private-test-code',self.console.getvalue())

    def test_cleanup_failure_does_not_change_acceptance(self):
        self.upload()
        sender = unittest.mock.Mock(return_value={'accepted_at':'2026-10-13T01:02:03Z','has_image':True})
        self.assertEqual(comic.receive(self.api,sender=sender,root=self.root),0)
        self.api.fail_cleanup = True
        comic.cleanup(self.api)
        self.assertEqual(self.record()['status'],'accepted')
        self.assertEqual(self.record()['cleanup'],'pending')
        self.assertNotIn('private cleanup detail',self.console.getvalue())

    def test_lost_final_record_does_not_allow_resend(self):
        self.upload(); self.api.fail_terminal_save=True
        sender = unittest.mock.Mock(return_value={'accepted_at':'2026-10-13T01:02:03Z','has_image':True})
        self.assertEqual(comic.receive(self.api,sender=sender,root=self.root),1)
        self.assertEqual(self.record()['status'],'claimed')
        self.assertIn('SMTP 已接受',self.summary.read_text())
        self.assertIn('持久结果保存未获确认',self.summary.read_text())
        with self.assertRaises(comic.ComicError):
            comic.receive(self.api,sender=sender,root=self.root)
        sender.assert_called_once()

    def test_interrupted_claim_becomes_uncertain_in_cleanup(self):
        self.upload()
        record,sha=comic.Requests(self.api).load(self.rid)
        record.update(status='claimed',run_id='123',run_attempt='1')
        comic.Requests(self.api).save(record,sha)
        comic.cleanup(self.api)
        self.assertEqual(self.record()['status'],'uncertain')
        self.assertEqual(self.record()['cleanup'],'deleted')

    def test_different_rerun_cannot_clean_other_attempt(self):
        self.upload()
        record,sha=comic.Requests(self.api).load(self.rid)
        record.update(status='claimed',run_id='123',run_attempt='1')
        comic.Requests(self.api).save(record,sha)
        with patch.dict(os.environ,{'GITHUB_RUN_ATTEMPT':'2'}):
            comic.cleanup(self.api)
        self.assertIn(20,self.api.assets)
        self.assertEqual(self.record()['status'],'claimed')

    def test_invalid_files_and_paths_rejected_before_remote_writes(self):
        for invalid in (b'<svg/>',b'\x89PNG\r\n\x1a\nhtml',b'GIF89a',b'\xff\xd8fake\xff\xd9',b'x'*(comic.MAX_BYTES+1)):
            self.image.write_bytes(invalid)
            with self.assertRaises(comic.ComicError): self.upload()
        self.image.write_bytes(png())
        link=self.root/'link.png';link.symlink_to(self.image)
        with self.assertRaises(comic.ComicError):
            comic.upload(self.api,link,'title','body',self.rid)
        self.assertFalse(self.api.calls)

    @patch('scripts.send_mail.smtplib.SMTP_SSL')
    def test_real_jpeg_and_webp_mime_attachments_round_trip(self, smtp):
        smtp.return_value.send_message.return_value = {}
        for data, extension, mime in ((JPEG,'jpg','image/jpeg'),(WEBP,'webp','image/webp')):
            with self.subTest(mime=mime):
                self.api = FakeAPI(); self.rid = str(uuid.uuid4())
                self.image = self.root / ('comic.' + extension); self.image.write_bytes(data)
                event = json.loads(self.event.read_text()); event['inputs']['request_id'] = self.rid
                self.event.write_text(json.dumps(event)); self.upload()
                with patch('scripts.send_mail.REPOSITORY_ROOT',self.root):
                    self.assertEqual(comic.receive(self.api,root=self.root),0)
                message = smtp.return_value.send_message.call_args.args[0]
                attachment = list(message.iter_attachments())[0]
                self.assertEqual(attachment.get_content_type(),mime)
                self.assertEqual(attachment.get_payload(decode=True),data)
                comic.cleanup(self.api)
                self.assertFalse(self.api.assets)

    def test_png_crc_size_and_uuid_validation(self):
        self.assertEqual(comic.image_type(png()),('png','image/png'))
        altered=bytearray(png());altered[-1]^=1
        for data in (bytes(altered),png(9000,1),png()+b'payload'):
            with self.assertRaises(comic.ComicError):comic.image_type(data)
        for rid in ('../private','ABC','',str(uuid.uuid4()).upper()):
            with self.assertRaises(comic.ComicError):comic.request_id(rid)

    def test_temp_symlink_refused_before_smtp(self):
        self.upload();(self.root/'images'/'.mail-bot-tmp').symlink_to(self.root)
        sender=unittest.mock.Mock()
        self.assertEqual(comic.receive(self.api,sender=sender,root=self.root),1)
        sender.assert_not_called()

    def test_non_actions_receiver_and_secret_arguments_fail_closed(self):
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}), self.assertRaises(comic.ComicError):
            comic.receive(self.api,root=self.root)
        self.assertFalse(self.api.calls)
        self.assertEqual(comic.main(['--token','private-credential']),1)
        self.assertNotIn('private-credential',self.console.getvalue())


class BinaryAPITests(unittest.TestCase):
    def test_redirect_does_not_forward_authentication(self):
        api=GitHubAPI('owner/repo','private-token')
        headers={'Location':'https://release-assets.githubusercontent.com/path?signature=private'}
        redirect=urllib.error.HTTPError('https://api.github.com',302,'redirect',headers,None)
        response=unittest.mock.MagicMock()
        response.__enter__.return_value=response
        response.read.return_value=b'image'
        with patch.object(api._opener,'open',side_effect=[redirect,response]) as opened:
            self.assertEqual(api.download_release_asset(20),b'image')
        first=opened.call_args_list[0].args[0]; second=opened.call_args_list[1].args[0]
        self.assertEqual(first.get_header('Authorization'),'Bearer private-token')
        self.assertIsNone(second.get_header('Authorization'))

    def test_external_or_insecure_redirects_rejected_without_second_request(self):
        for location in ('https://evil.example/a','http://release-assets.githubusercontent.com/a',
                         'https://private@release-assets.githubusercontent.com/a',
                         'https://release-assets.githubusercontent.com:444/a'):
            api=GitHubAPI('owner/repo','private-token')
            error=urllib.error.HTTPError('https://api.github.com',302,'private raw error',{'Location':location},None)
            with patch.object(api._opener,'open',side_effect=error) as opened:
                with self.assertRaises(GitHubAPIError):api.download_release_asset(20)
            opened.assert_called_once()

    def test_upload_uses_fixed_tls_host_no_redirect_and_bounded_binary(self):
        api=GitHubAPI('owner/repo','private-token')
        rid=str(uuid.uuid4())
        response=unittest.mock.MagicMock();response.__enter__.return_value=response
        response.read.return_value=b'{"id":20}'
        with patch.object(api._opener,'open',return_value=response) as opened:
            api.upload_release_asset(10,'mailbot-'+rid+'.png','image/png',png())
        request=opened.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://uploads.github.com/repos/owner/repo/releases/10/assets?'))
        self.assertEqual(request.data,png())
        with self.assertRaises(GitHubAPIError):
            api.upload_release_asset(10,'../private.png','image/png',png())


if __name__=='__main__':unittest.main()
