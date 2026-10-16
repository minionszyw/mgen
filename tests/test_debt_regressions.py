"""已复现技术债的行为回归；只使用本地素材与模拟网络。"""

import base64
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

from mgen import cli, common, config, http
from mgen.image import client as image_client
from mgen.image import validation as image_validation
from mgen.video import client as video_client
from mgen.video import validation as video_validation


def video_payload():
    return {
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": "测试"}],
        "duration": 5,
        "resolution": "768P",
    }


def image_payload():
    return {
        "model": "gpt-6-astra",
        "input": "测试",
        "tools": [{"type": "image_generation", "model": "gpt-image-2.5-sunburst"}],
    }


def task(url="https://cdn.example/old.mp4"):
    return {"task": {"id": "123", "status": "succeeded", "content": {"url": url}}}


def response(data=None, status=200, chunks=(b"video",), headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = data
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.iter_content.return_value = iter(chunks)
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status))
    return result


class ConfigTests(unittest.TestCase):
    def test_multiple_loads_do_not_change_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            first = Path(directory) / "first.env"
            second = Path(directory) / "second.env"
            first.write_text("VIDEO_MODEL=one\nIMAGE_API_KEY=first-secret\n")
            second.write_text("VIDEO_MODEL=two\nIMAGE_API_KEY=second-secret\n")
            before = dict(os.environ)
            one = config.load_config(first)
            two = config.load_config(second)
            self.assertEqual(one.video.model, "one")
            self.assertEqual(two.video.model, "two")
            self.assertEqual(one.image.api_key, "first-secret")
            self.assertEqual(two.image.api_key, "second-secret")
            self.assertEqual(dict(os.environ), before)
            self.assertNotIn("first-secret", repr(one))
            with self.assertRaises(FrozenInstanceError):
                one.video.model = "changed"

    def test_precedence_and_explicit_empty_values(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "config.env"
            source.write_text("VIDEO_MODEL=dotenv-model\nVIDEO_BASE_URL=https://dotenv.example\n")
            with patch.dict(os.environ, {"VIDEO_MODEL": "environment-model"}, clear=True):
                loaded = config.load_config(source)
                args = cli.parser().parse_args(["video", "validate", "--model", "cli-model"])
                self.assertEqual(loaded.video.model, "environment-model")
                self.assertEqual(loaded.video.with_cli(args).model, "cli-model")
                empty = cli.parser().parse_args(["video", "run", "--base-url", ""])
                self.assertEqual(loaded.video.with_cli(empty).base_url, "")
            with patch.dict(os.environ, {"VIDEO_MODEL": ""}, clear=True):
                self.assertEqual(config.load_config(source).video.model, "")
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        cli.main(
                            ["--env-file", str(source), "video", "validate", "--prompt", "猫"]
                        ),
                        2,
                    )

    def test_two_cli_invocations_use_their_own_config(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            for model in ["MiniMax-H3", "MiniMax-H3-Max"]:
                source = Path(directory) / f"{model}.env"
                source.write_text(f"VIDEO_MODEL={model}\n")
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(
                        cli.main(
                            ["--env-file", str(source), "video", "validate", "--prompt", "猫"]
                        ),
                        0,
                    )
                self.assertEqual(json.loads(out.getvalue())["model"], model)
            self.assertNotIn("VIDEO_MODEL", os.environ)


class OutputTests(unittest.TestCase):
    def test_nested_json_redaction_preserves_types_and_original(self):
        original = {
            "nested": ["video-secret image-secret", {"data": "data:image/png;base64,YQ=="}],
            "count": 2,
            "ok": True,
            "empty": None,
        }
        before = copy.deepcopy(original)
        with (
            common.output_secrets(("video-secret", "image-secret")),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            common.emit(original)
        text = out.getvalue()
        self.assertNotIn("video-secret", text)
        self.assertNotIn("image-secret", text)
        self.assertNotIn("YQ==", text)
        saved = json.loads(text)
        self.assertEqual(saved["nested"][1]["data"], "[DATA URI]")
        self.assertEqual(saved["count"], 2)
        self.assertIs(saved["ok"], True)
        self.assertIsNone(saved["empty"])
        self.assertEqual(original, before)
        self.assertEqual(common.redact("video-secret"), "video-secret")

    def test_cli_uses_dotenv_secret_for_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / ".env"
            env.write_text("VIDEO_API_KEY=dotenv-secret\n")

            def handler(args, resolved):
                common.emit({"message": resolved.api_key})
                raise common.CliError("error " + resolved.api_key, 3)

            with (
                patch.object(cli.video, "execute", side_effect=handler),
                contextlib.redirect_stdout(io.StringIO()) as out,
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                self.assertEqual(cli.main(["--env-file", str(env), "video", "query", "123"]), 3)
            self.assertNotIn("dotenv-secret", out.getvalue() + err.getvalue())
            self.assertEqual(common.redact("dotenv-secret"), "dotenv-secret")


class ValidationTests(unittest.TestCase):
    def test_validators_leave_input_untouched_on_success_and_failure(self):
        for validate, source in [
            (video_validation.validate, video_payload()),
            (image_validation.validate, image_payload()),
        ]:
            before = copy.deepcopy(source)
            validated = validate(source)
            request = validated[0] if isinstance(validated, tuple) else validated
            self.assertEqual(source, before)
            self.assertEqual(json.loads(request.body), request.payload)
            self.assertIsNot(source, request.payload)
        source = video_payload()
        source["content"].append({"type": "image_url", "image_url": {"url": "missing-image.png"}})
        before = copy.deepcopy(source)
        with self.assertRaises(common.CliError):
            video_validation.validate(source)
        self.assertEqual(source, before)

    def test_video_data_uri_mime_and_skip_contract(self):
        raw = io.BytesIO()
        Image.new("RGB", (256, 256)).save(raw, format="PNG")
        value = "data:image/jpeg;base64," + base64.b64encode(raw.getvalue()).decode()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(common.CliError):
                video_validation.prepare_media(value, "image_url", False, directory, 1)
            self.assertEqual(
                video_validation.prepare_media(value, "image_url", True, directory, 1)[0], value
            )

    def test_prepared_bytes_are_reused(self):
        original = common.serialize
        with patch.object(common, "serialize", wraps=original) as encode:
            request, report = image_validation.validate(image_payload())
            client = image_client.ImageClient("https://api.example", "key")
            client.session = Mock()
            client.session.request.return_value = response({"id": "resp_123", "status": "queued"})
            client.create(request)
            self.assertEqual(encode.call_count, 1)
            self.assertEqual(report["request_bytes"], len(request.body))
            self.assertIs(client.session.request.call_args.kwargs["data"], request.body)

    def test_malformed_active_output_reports_protocol_error(self):
        for status in ("queued", "in_progress", "completed"):
            for output in (42, "bad", {}, [None], ["bad"]):
                data = {"id": "resp_test", "status": status, "output": output}
                with (
                    self.subTest(status=status, output=output),
                    self.assertRaises(common.CliError) as cm,
                ):
                    image_client.summary(data)
                self.assertEqual(cm.exception.code, 3)
        image_client.summary({"id": "resp_test", "status": "queued", "output": None})

    def test_response_metadata_and_required_result_fields(self):
        for changes in (
            {"error": "bad"},
            {"usage": []},
            {"incomplete_details": 1},
            {"output": [{}]},
            {"output": [{"type": "image_generation_call", "id": "ig_test"}]},
            {
                "output": [
                    {
                        "type": "image_generation_call",
                        "id": "ig_test",
                        "status": "completed",
                        "result": 1,
                    }
                ]
            },
        ):
            data = {"id": "resp_test", "status": "queued", **changes}
            with self.subTest(changes=changes), self.assertRaises(common.CliError) as cm:
                image_client.check_response(data)
            self.assertEqual(cm.exception.code, 3)
        client = video_client.VideoClient("https://api.example", "key")
        client.session = Mock()
        for changes in ({"content": None}, {"content": []}, {"content": {}}, {"error": []}):
            data = task()
            data["task"].update(changes)
            client.session.request.return_value = response(data)
            with self.subTest(changes=changes), self.assertRaises(common.CliError) as cm:
                client.query("123")
            self.assertEqual(cm.exception.code, 3)


class TransportTests(unittest.TestCase):
    def test_responses_closed_on_success_api_error_and_bad_json(self):
        for data, status, json_error in [
            ({"ok": True}, 200, False),
            ({"error": {"message": "bad"}}, 400, False),
            (None, 200, True),
            ([], 200, False),
        ]:
            client = http.JsonClient("https://api.example", "key")
            client.session = Mock()
            reply = response(data, status)
            if json_error:
                reply.json.side_effect = ValueError("bad json")
            client.session.request.return_value = reply
            try:
                client.request("POST", "/endpoint", {})
            except common.CliError as exc:
                self.assertEqual(exc.code, 3)
            reply.close.assert_called_once()

    @patch.object(http.time, "sleep")
    def test_retry_responses_closed_before_sleep_and_after_exhaustion(self, sleep):
        client = http.JsonClient("https://api.example", "key")
        replies = [response({}, 500) for _ in range(4)]
        client.session = Mock()
        client.session.request.side_effect = replies

        def observe(delay):
            for reply in replies[: sleep.call_count]:
                reply.close.assert_called_once()

        sleep.side_effect = observe
        with self.assertRaises(common.CliError):
            client.request("GET", "/endpoint")
        self.assertEqual(sleep.call_count, 3)
        for reply in replies:
            reply.close.assert_called_once()

    def test_provider_error_policies_are_independent(self):
        image = image_client.ImageClient("https://api.example", "key")
        video = video_client.VideoClient("https://api.example", "key")
        failure = {"status": "failed", "error": {"code": "moderation_blocked"}}
        self.assertFalse(image.is_api_error(failure))
        self.assertTrue(video.is_api_error(failure))
        self.assertFalse(image.retryable(429, {"error": {"code": "insufficient_quota"}}))
        self.assertTrue(video.retryable(429, {"error": {"code": "insufficient_quota"}}))
        image.close()
        video.close()


class DownloadTests(unittest.TestCase):
    @patch.object(video_client.time, "sleep")
    def test_retry_then_expired_link_can_refresh(self, sleep):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(video_client.requests, "Session") as factory,
        ):
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [response(status=500), response(status=403), response()]
            client = Mock(timeout=1)
            client.query.return_value = task("https://cdn.example/new.mp4")
            out = Path(directory) / "out.mp4"
            video_client.download(client, "123", task(), str(out), False)
            self.assertEqual(out.read_bytes(), b"video")
            client.query.assert_called_once_with("123")
            self.assertEqual(session.get.call_args.args[0], "https://cdn.example/new.mp4")
            self.assertEqual(sleep.call_count, 1)

    @patch.object(video_client.time, "sleep")
    def test_refresh_only_once_and_temporary_errors_have_separate_budget(self, sleep):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(video_client.requests, "Session") as factory,
        ):
            session = factory.return_value.__enter__.return_value
            client = Mock(timeout=1)
            client.query.return_value = task()
            session.get.side_effect = [response(status=404), response(status=404)]
            out = Path(directory) / "out.mp4"
            with self.assertRaises(common.CliError):
                video_client.download(client, "123", task(), str(out), False)
            self.assertEqual(client.query.call_count, 1)
            session.get.side_effect = [
                response(status=500),
                response(status=403),
                response(status=500),
                response(status=500),
                response(status=500),
            ]
            client.query.reset_mock()
            with self.assertRaises(common.CliError):
                video_client.download(client, "123", task(), str(out), False)
            self.assertEqual(session.get.call_count, 7)
            self.assertEqual(client.query.call_count, 1)
            self.assertFalse(out.exists())

    @patch.object(video_client.time, "sleep")
    def test_partial_file_removed_before_retry_and_original_protected(self, sleep):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(video_client.requests, "Session") as factory,
        ):
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [
                response(headers={"Content-Length": "100"}) for _ in range(4)
            ]
            out = Path(directory) / "out.mp4"
            out.write_bytes(b"original")

            def check(delay):
                self.assertEqual(list(Path(directory).glob(".mgen-*")), [])
                self.assertEqual(out.read_bytes(), b"original")

            sleep.side_effect = check
            with self.assertRaises(common.CliError):
                video_client.download(Mock(timeout=1), "123", task(), str(out), True)
            self.assertEqual(out.read_bytes(), b"original")
            self.assertEqual(list(Path(directory).glob(".mgen-*")), [])

    def test_bad_content_length_is_protocol_error(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(video_client.requests, "Session") as factory,
        ):
            factory.return_value.__enter__.return_value.get.return_value = response(
                headers={"Content-Length": "not-a-number"}
            )
            with self.assertRaises(common.CliError) as cm:
                video_client.download(
                    Mock(timeout=1), "123", task(), str(Path(directory) / "out.mp4"), False
                )
            self.assertEqual(cm.exception.code, 3)
