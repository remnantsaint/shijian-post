import base64
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import reserve_letter as reservations
from scripts.github_api import GitHubAPIError


# Small historical fixture from 58c0216; not an active mail template. v1 did
# not say whether number 0 referred to this short body or a later long body.
LEGACY_SHORT_BODY_0 = "\n\n".join((
    "嘿，收件箱里突然亮起一颗小星星。小龙娘把今天的信轻轻放在这里，想和你分享一个脑袋里刚冒出来的小故事。",
    "我幻想自己开了一家只在雨天营业的小书店。客人进门时，门铃不是叮当响，而是会小声念出一句诗。书架最上面放着没有结局的冒险故事，谁翻开它，谁就能为主角决定下一步。今天我把一页空白书签留给你：想去哪里探险，就从哪里开始。",
    "说起来，我挺喜欢那种‘原来如此’的瞬间。一个问题卡了很久，突然有一条线索接上，脑子里就像有小灯泡亮起来。即使今天没有解决全部问题，只要比昨天多明白一点点，也值得悄悄庆祝。",
    "好啦，信纸快写满了。我把一颗想象中的星星夹在最后一页，等你下次打开邮箱时，它还会在这里闪一下。——小龙娘 🐉",
))


class MemoryAPI:
    """Contents/ref simulation; no network, SMTP or real state writes."""

    repo = "test/mail-bot"

    def __init__(self, ledger=None, *, branch=True):
        self.ledger = copy.deepcopy(ledger)
        self.branch = branch
        self.revision = 1
        self.calls = []
        self.failures = []
        self.branch_race = False

    @property
    def sha(self):
        return str(self.revision).zfill(40)

    def request(self, method, path, data=None):
        self.calls.append((method, path, copy.deepcopy(data)))
        prefix = "/repos/" + self.repo
        if method == "GET" and path == prefix:
            return {"default_branch": "main"}
        if method == "GET" and path == prefix + "/git/ref/heads/main":
            return {"object": {"sha": "f" * 40}}
        if method == "GET" and path == prefix + "/git/ref/heads/" + reservations.BRANCH:
            if not self.branch:
                raise GitHubAPIError("Not found", status=404)
            return {"object": {"sha": "e" * 40}}
        if method == "POST" and path == prefix + "/git/refs":
            self.branch = True
            if self.branch_race:
                raise GitHubAPIError("Ref already created", status=422)
            return {"ref": data["ref"]}
        if method == "GET" and path == prefix + "/contents/" + reservations.PATH + "?ref=" + reservations.BRANCH:
            if not self.branch or self.ledger is None:
                raise GitHubAPIError("Not found", status=404)
            content = base64.b64encode(json.dumps(self.ledger).encode()).decode()
            return {"sha": self.sha, "encoding": "base64", "content": content}
        if method == "PUT" and path == prefix + "/contents/" + reservations.PATH:
            decoded = json.loads(base64.b64decode(data["content"]))
            if self.failures:
                status, concurrent = self.failures.pop(0)
                if concurrent:
                    # Another caller reserved precisely our first candidate.
                    self.ledger = decoded
                    self.revision += 1
                raise GitHubAPIError("do not log this secret body", status=status)
            if not self.branch:
                raise GitHubAPIError("Missing ref", status=422)
            expected = None if self.ledger is None else self.sha
            if data.get("sha") != expected:
                raise GitHubAPIError("SHA mismatch", status=409)
            self.ledger = decoded
            self.revision += 1
            return {"content": {"sha": self.sha}}
        raise AssertionError((method, path))

    def puts(self):
        return [call for call in self.calls if call[0] == "PUT"]


class ReserveLetterTests(unittest.TestCase):
    def test_all_original_1152_bodies_match_frozen_unique_mapping(self):
        current = [reservations.body_digest(reservations.letter(n)[1])
                   for n in range(reservations.LEGACY_TOTAL)]
        self.assertEqual(tuple(current), tuple(row[-1] for row in reservations.legacy_digests()))
        self.assertEqual(len(set(current)), 1152)
        self.assertEqual(len(reservations.candidates()), 1152)

    def test_missing_branch_is_initialized_before_first_reservation(self):
        api = MemoryAPI(branch=False)
        stable_id, _, body = reservations.reserve(api, choose=lambda values: values[0])
        methods = [method for method, _, _ in api.calls]
        self.assertIn("POST", methods)
        self.assertLess(methods.index("POST"), methods.index("PUT"))
        self.assertTrue(api.branch)
        self.assertEqual(stable_id, reservations.reservation_id(reservations.body_digest(body)))
        self.assertEqual(api.ledger["version"], 2)

    def test_concurrent_branch_creation_is_verified_and_used(self):
        api = MemoryAPI(branch=False)
        api.branch_race = True
        reservations.reserve(api, choose=lambda values: values[0])
        ref_reads = [call for call in api.calls if call[0] == "GET" and call[1].endswith("heads/mail-bot-state")]
        self.assertEqual(len(ref_reads), 2)
        self.assertEqual(len(api.puts()), 1)

    def test_v1_migration_keeps_original_numbers_and_digests(self):
        api = MemoryAPI({"version": 1, "used": [386, 7]})
        reservations.reserve(api, choose=lambda values: values[0])
        self.assertEqual(api.ledger["legacy_used"], [386, 7])
        self.assertEqual(api.ledger["legacy_catalog"], "dragon-letters-v1")
        self.assertEqual([entry["body_sha256"] for entry in api.ledger["used"][:6]],
                         list(reservations.legacy_digests()[386]) + list(reservations.legacy_digests()[7]))
        self.assertEqual(len(api.ledger["used"]), 7)

    def test_v1_migration_reserves_every_known_historical_rendering(self):
        expected = (reservations.body_digest(LEGACY_SHORT_BODY_0.replace("\n\n", "\\n\\n")),
                    reservations.body_digest(LEGACY_SHORT_BODY_0),
                    reservations.body_digest(reservations.letter(0)[1]))
        self.assertEqual(reservations.legacy_digests()[0], expected)
        migrated = reservations.validate_ledger({"version": 1, "used": [0]})
        self.assertEqual([entry["body_sha256"] for entry in migrated["used"]], list(expected))

    def test_old_58c_short_body_cannot_be_reserved_after_v1_migration(self):
        api = MemoryAPI({"version": 1, "used": [0]})
        short_digest = reservations.body_digest(LEGACY_SHORT_BODY_0)
        other_title, other_body = reservations.letter(1)
        other_digest = reservations.body_digest(other_body)
        # A future catalog could reintroduce the old short body under any index.
        def choose(values):
            self.assertNotIn(short_digest, values)
            return values[0]
        with mock.patch.object(reservations, "candidates", return_value={short_digest: 9000, other_digest: 1}), mock.patch.object(
                reservations, "letter", side_effect=lambda number: (
                    ("旧短文标题", LEGACY_SHORT_BODY_0) if number == 9000 else (other_title, other_body))):
            _, _, body = reservations.reserve(api, choose=choose)
        self.assertEqual(body, other_body)
        self.assertEqual(api.ledger["legacy_used"], [0])
        self.assertIn(short_digest, [entry["body_sha256"] for entry in api.ledger["used"]])

    def test_appending_scene_cannot_reuse_old_386_body_as_new_434(self):
        old_body = reservations.letter(386)[1]
        expanded_scenes = reservations.SCENES + ["新增场景"]
        api = MemoryAPI({"version": 1, "used": [386]})
        with mock.patch.object(reservations, "SCENES", expanded_scenes):
            self.assertEqual(reservations.letter(434)[1], old_body)
            repeated_digest = reservations.body_digest(old_body)
            def choose(values):
                self.assertNotIn(repeated_digest, values)
                return values[0]
            _, _, body = reservations.reserve(api, choose=choose)
            self.assertNotEqual(body, old_body)
        self.assertEqual(api.ledger["legacy_used"], [386])

    def test_title_change_does_not_make_old_body_new(self):
        api = MemoryAPI({"version": 1, "used": [0]})
        old_digest = reservations.legacy_digests()[0][-1]
        with mock.patch.object(reservations, "TITLES", ["全新标题"]):
            def choose(values):
                self.assertNotIn(old_digest, values)
                return values[0]
            reservations.reserve(api, choose=choose)

    def test_outer_whitespace_matches_smtp_body_and_cannot_make_body_new(self):
        old_body = reservations.letter(0)[1]
        openings = reservations.OPENINGS.copy()
        closings = reservations.CLOSINGS.copy()
        openings[0] = "  " + openings[0]
        closings[0] += "  \n"
        with mock.patch.object(reservations, "OPENINGS", openings), mock.patch.object(
                reservations, "CLOSINGS", closings):
            self.assertEqual(reservations.letter(0)[1], old_body)

    def test_v2_records_continue_without_resetting_legacy_history(self):
        api = MemoryAPI({"version": 1, "used": [1, 386]})
        first_id, _, _ = reservations.reserve(api, choose=lambda values: values[0])
        first_ledger = copy.deepcopy(api.ledger)
        second_id, _, _ = reservations.reserve(api, choose=lambda values: values[0])
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(api.ledger["legacy_used"], [1, 386])
        self.assertEqual(api.ledger["used"][:-1], first_ledger["used"])

    def test_cas_conflict_reloads_and_selects_a_different_body(self):
        api = MemoryAPI({"version": 1, "used": []})
        api.failures = [(409, True)]
        stable_id, _, _ = reservations.reserve(api, choose=lambda values: values[0])
        writes = api.puts()
        self.assertEqual(len(writes), 2)
        first = json.loads(base64.b64decode(writes[0][2]["content"]))["used"][-1]["id"]
        self.assertNotEqual(stable_id, first)
        self.assertEqual(len(api.ledger["used"]), 2)
        self.assertNotEqual(writes[0][2]["sha"], writes[1][2]["sha"])

    def test_first_file_creation_conflict_422_reloads_reserved_body(self):
        api = MemoryAPI()
        api.failures = [(422, True)]
        stable_id, _, _ = reservations.reserve(api, choose=lambda values: values[0])
        self.assertEqual(len(api.puts()), 2)
        self.assertNotIn("sha", api.puts()[0][2])
        self.assertIn("sha", api.puts()[1][2])
        self.assertEqual(stable_id, api.ledger["used"][-1]["id"])
        self.assertNotEqual(stable_id, api.ledger["used"][0]["id"])

    def test_repeated_conflicts_are_bounded_and_do_not_export(self):
        api = MemoryAPI()
        api.failures = [(409, True)] * reservations.MAX_ATTEMPTS
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "github-env"
            with mock.patch.object(reservations, "GitHubAPI", return_value=api), mock.patch.dict(
                    reservations.os.environ, {"GITHUB_ENV": str(env), "GITHUB_REPOSITORY": api.repo,
                                              "GH_TOKEN": "test-token"}), mock.patch.object(
                    reservations, "export_for_actions") as export:
                with self.assertRaises(reservations.ReservationError):
                    reservations.main()
            export.assert_not_called()
            self.assertFalse(env.exists())
        self.assertEqual(len(api.puts()), reservations.MAX_ATTEMPTS)

    def test_unknown_or_unsafe_write_failures_are_never_retried(self):
        for status in (None, 403, 422, 500):
            with self.subTest(status=status):
                api = MemoryAPI()
                api.failures = [(status, False)]
                with self.assertRaises(GitHubAPIError):
                    reservations.reserve(api, choose=lambda values: values[0])
                self.assertEqual(len(api.puts()), 1)

    def test_unknown_ledger_version_blocks_writes(self):
        for version in (3, "1", True, None):
            with self.subTest(version=version):
                api = MemoryAPI({"version": version, "used": []})
                with self.assertRaises(reservations.ReservationError):
                    reservations.reserve(api)
                self.assertEqual(api.puts(), [])

    def test_invalid_legacy_numbers_are_not_reset(self):
        for used in ([1152], [-1], [True], [1, 1], "1"):
            with self.subTest(used=used):
                api = MemoryAPI({"version": 1, "used": used})
                with self.assertRaises(reservations.ReservationError):
                    reservations.reserve(api)
                self.assertEqual(api.puts(), [])

    def test_missing_legacy_digest_and_inconsistent_identity_fail_closed(self):
        baseline = reservations.validate_ledger({"version": 1, "used": [386]})
        for mutate in (lambda ledger: ledger["used"].clear(),
                       lambda ledger: ledger["used"][0].update(id="changed"),
                       lambda ledger: ledger["used"].append(ledger["used"][0].copy()),
                       lambda ledger: ledger.update(body="private text")):
            ledger = copy.deepcopy(baseline)
            mutate(ledger)
            with self.subTest(ledger=ledger):
                with self.assertRaises(reservations.ReservationError):
                    reservations.validate_ledger(ledger)

    def test_frozen_mapping_reordering_is_detected(self):
        snapshot = {"version": 2, "catalog": reservations.LEGACY_CATALOG,
                    "revisions": list(reservations.LEGACY_REVISIONS),
                    "body_sha256": [list(row) for row in reservations.legacy_digests()]}
        snapshot["body_sha256"][0], snapshot["body_sha256"][1] = snapshot["body_sha256"][1], snapshot["body_sha256"][0]
        reservations.legacy_digests.cache_clear()
        try:
            with mock.patch.object(Path, "read_text", return_value=json.dumps(snapshot)):
                with self.assertRaises(reservations.ReservationError):
                    reservations.legacy_digests()
        finally:
            reservations.legacy_digests.cache_clear()

    def test_unknown_or_incomplete_legacy_snapshot_is_rejected(self):
        baseline = {"version": 2, "catalog": reservations.LEGACY_CATALOG,
                    "revisions": list(reservations.LEGACY_REVISIONS),
                    "body_sha256": [list(row) for row in reservations.legacy_digests()]}
        for mutate in (lambda data: data.update(version=3),
                       lambda data: data["revisions"].reverse(),
                       lambda data: data["body_sha256"][0].pop()):
            snapshot = copy.deepcopy(baseline)
            mutate(snapshot)
            reservations.legacy_digests.cache_clear()
            try:
                with mock.patch.object(Path, "read_text", return_value=json.dumps(snapshot)):
                    with self.assertRaises(reservations.ReservationError):
                        reservations.legacy_digests()
            finally:
                reservations.legacy_digests.cache_clear()

    def test_exhaustion_does_not_reset_or_write_ledger(self):
        api = MemoryAPI({"version": 1, "used": list(range(1152))})
        with self.assertRaises(reservations.ReservationError):
            reservations.reserve(api)
        self.assertEqual(api.puts(), [])
        self.assertEqual(len(api.ledger["used"]), 1152)

    def test_invalid_catalog_is_rejected_before_github_mutation(self):
        for name, values in (("SCENES", []), ("THOUGHTS", [None]),
                             ("TITLES", ["标题\nMAIL_TO=x"]), ("SCENES", ["不合法\x00正文"])):
            api = MemoryAPI(branch=False)
            with self.subTest(name=name), mock.patch.object(reservations, name, values):
                with self.assertRaises(reservations.ReservationError):
                    reservations.reserve(api)
            self.assertEqual(api.calls, [])

    def test_ledger_and_commit_message_contain_no_body_or_title(self):
        api = MemoryAPI()
        _, title, body = reservations.reserve(api, choose=lambda values: values[0])
        serialized = json.dumps(api.ledger, ensure_ascii=False)
        self.assertNotIn(title, serialized)
        self.assertNotIn(body, serialized)
        self.assertNotIn(body.splitlines()[0], serialized)
        self.assertNotIn(title, api.puts()[0][2]["message"])

    def test_export_keeps_utf8_body_and_masks_every_nonblank_line(self):
        body = "中文 100%\n\n第二段 🐉"
        title = "私密标题"
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "github-env"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                reservations.export_for_actions("body-sha256:test", title, body, env)
            content = env.read_text(encoding="utf-8")
        self.assertIn("MAIL_SUBJECT=" + title, content)
        self.assertIn(body, content)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "::add-mask::" + title)
        self.assertEqual(lines[1], "::add-mask::中文 100%25")
        self.assertEqual(lines[2], "::add-mask::第二段 🐉")
        self.assertNotIn(title, lines[-1])
        self.assertNotIn("第二段", lines[-1])
        self.assertNotIn("body-sha256:test", output.getvalue())

    def test_action_mask_escapes_command_control_characters(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            reservations.mask_for_actions("x%\r\n::warning::private")
        self.assertEqual(output.getvalue(), "::add-mask::x%25%0D%0A::warning::private\n")

    def test_cli_error_never_echoes_credentials_or_private_api_text(self):
        for error, expected in (
                (GitHubAPIError("password=private-code body=private-letter", status=403), "HTTP 403"),
                (GitHubAPIError("password=private-code body=private-letter"), "结果不确定"),
                (RuntimeError("password=private-code body=private-letter"), "内部错误")):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with mock.patch.object(reservations, "main", side_effect=error), contextlib.redirect_stderr(output):
                    result = reservations.cli()
                self.assertEqual(result, 1)
                self.assertIn(expected, output.getvalue())
                self.assertNotIn("private", output.getvalue())

    def test_cli_reports_fixed_reservation_reason_clearly(self):
        try:
            reservations.validate_ledger({"version": 3, "used": []})
        except reservations.ReservationError as error:
            output = io.StringIO()
            with mock.patch.object(reservations, "main", side_effect=error), contextlib.redirect_stderr(output):
                self.assertEqual(reservations.cli(), 1)
        self.assertIn("不支持的文案账本版本", output.getvalue())

    def test_missing_export_configuration_does_not_consume_reservation(self):
        with mock.patch.dict(reservations.os.environ, {}, clear=True), mock.patch.object(
                reservations, "GitHubAPI") as factory:
            with self.assertRaises(reservations.ReservationError):
                reservations.main()
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
