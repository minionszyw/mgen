import base64
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

from mgen import cli, common, config, files
from mgen.video import cli as vcli
from mgen.video import client as vc
from mgen.video import validation as vv


def payload(**kw):
    return {
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": "海边日落"}],
        "resolution": "768P",
        "duration": 5,
        **kw,
    }


def media(kind, role, url="https://example.com/media"):
    return {"type": kind, kind: {"url": url}, "role": role}


def response(data=None, status=200, chunks=(b"video",), headers=None):
    r = Mock()
    r.status_code = status
    r.json.return_value = data
    r.headers = headers or {}
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    r.iter_content.return_value = iter(chunks)
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(status))
    return r


def task(status="succeeded", **kw):
    return {
        "task": {
            "id": "123",
            "status": status,
            "content": {"url": "https://cdn.example/video.mp4"},
            **kw,
        }
    }


class ValidationTests(unittest.TestCase):
    def test_defaults_and_models(self):
        self.assertEqual(vv.validate(payload()).payload["ratio"], "16:9")
        for model, low, good, bad in [
            ("MiniMax-H3", 4, "2K", "480P"),
            ("MiniMax-H3-Max", 5, "480P", "2K"),
        ]:
            for duration in [low, 15]:
                vv.validate(payload(model=model, duration=duration, resolution=good))
            for overrides in [
                {"duration": low - 1},
                {"duration": 16},
                {"duration": True},
                {"resolution": bad},
            ]:
                with self.assertRaises(vv.CliError):
                    vv.validate(payload(model=model, **overrides))

    def test_bad_types_and_text(self):
        for value in ["", " ", "x" * 7001, None, False]:
            with self.assertRaises(vv.CliError):
                vv.validate(payload(content=[{"type": "text", "text": value}]))
        for overrides in [
            {"model": []},
            {"ratio": []},
            {"extra": []},
            {"aigc_watermark": "false"},
            {"unknown": 1},
            {"ratio": "adaptive"},
        ]:
            with self.assertRaises(vv.CliError):
                vv.validate(payload(**overrides))
        vv.validate(payload(content=[{"type": "text", "text": "x" * 7000}]))

    def test_scenarios_and_role_limits(self):
        base = payload()["content"]
        combinations = [
            [media("image_url", "first_frame")],
            [media("image_url", "last_frame")],
            [media("image_url", "first_frame"), media("image_url", "last_frame")],
            [
                media("image_url", "reference_image"),
                media("video_url", "reference_video"),
                media("audio_url", "reference_audio"),
            ],
        ]
        for items in combinations:
            result = vv.validate(payload(content=base + items), skip=True)
            self.assertEqual(result.payload["ratio"], "adaptive")
        invalid = [
            [media("image_url", "first_frame"), media("image_url", "reference_image")],
            [media("image_url", "first_frame")] * 2,
            [media("image_url", "reference_image")] * 10,
            [media("video_url", "reference_video")] * 4,
            [media("audio_url", "reference_audio")] * 4,
            [media("audio_url", "first_frame")],
        ]
        for items in invalid:
            with self.assertRaises(vv.CliError):
                vv.validate(payload(content=base + items), skip=True)

    def test_images_and_local_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            Image.new("RGB", (256, 256)).save(path)
            p = payload(
                content=payload()["content"] + [media("image_url", "last_frame", str(path))]
            )
            result = vv.validate(p)
            self.assertTrue(
                result.payload["content"][1]["image_url"]["url"].startswith(
                    "data:image/png;base64,"
                )
            )
            Image.new("RGB", (255, 256)).save(path)
            with self.assertRaises(vv.CliError):
                vv.inspect_media(path, "image_url")
            Image.new("RGB", (256, 1000)).save(path)
            with self.assertRaises(vv.CliError):
                vv.inspect_media(path, "image_url")

    def test_data_uri_and_platform_file(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "a.png"
            Image.new("RGB", (256, 256)).save(p)
            uri = "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
            self.assertEqual(vv.prepare_media(uri, "image_url", False, directory, 1)[0], uri)
            for uri in [
                "data:image/png;base64,====",
                "data:audio/wav;base64,YQ==",
                "mm_file://123",
            ]:
                with self.assertRaises(vv.CliError):
                    vv.prepare_media(uri, "image_url", False, directory, 1)
            self.assertEqual(
                vv.prepare_media("mm_file://123", "image_url", True, directory, 1)[0],
                "mm_file://123",
            )

    def test_audio_video_properties(self):
        video = {
            "format": {"duration": "5", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 256,
                    "height": 256,
                    "avg_frame_rate": "24/1",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }
        with patch.object(vv, "probe", return_value=video):
            self.assertEqual(vv.inspect_media("x", "video_url"), (5, "video/mp4"))
            video["streams"][0]["avg_frame_rate"] = "23/1"
            with self.assertRaises(vv.CliError):
                vv.inspect_media("x", "video_url")
            video["streams"][0]["avg_frame_rate"] = "24/1"
            video["streams"][0]["codec_name"] = "vp9"
            with self.assertRaises(vv.CliError):
                vv.inspect_media("x", "video_url")
        audio = {
            "format": {"duration": "2", "format_name": "mp3"},
            "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
        }
        with patch.object(vv, "probe", return_value=audio):
            self.assertEqual(vv.inspect_media("x", "audio_url"), (2, "audio/mp3"))
            audio["format"]["duration"] = "nan"
            with self.assertRaises(vv.CliError):
                vv.inspect_media("x", "audio_url")

    def test_totals_and_request_size(self):
        items = payload()["content"] + [media("video_url", "reference_video")] * 2
        with patch.object(vv, "prepare_media", return_value=("https://example.com/v", 8)):
            with self.assertRaises(vv.CliError):
                vv.validate(payload(content=items))
        with patch.object(common, "serialize", return_value=b"x" * 65), patch.object(vv, "MB", 1):
            with self.assertRaises(vv.CliError):
                vv.validate(payload())

    def test_extra(self):
        vv.validate(payload(model="MiniMax-H3-Max", extra={"prompt_expansion_mode": "quality"}))
        for extra in [
            {"prompt_expansion_mode": "balance"},
            {"prompt_expansion_mode": True},
            {"other": "x"},
        ]:
            with self.assertRaises(vv.CliError):
                vv.validate(payload(model="MiniMax-H3-Max", extra=extra))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = vc.VideoClient("https://api.example", "secret", 1)
        self.client.session = Mock()

    @patch.object(vc.time, "sleep")
    def test_get_retries_post_does_not(self, sleep):
        self.client.session.request.side_effect = [
            response(status=429, headers={"Retry-After": "2"}),
            response(task()),
        ]
        self.assertEqual(self.client.query("123")["task"]["status"], "succeeded")
        sleep.assert_called_once_with(2)
        self.client.session.request.reset_mock(side_effect=True)
        self.client.session.request.return_value = response({"error": {"message": "bad"}}, 500)
        with self.assertRaises(vc.CliError):
            self.client.create(payload())
        self.assertEqual(self.client.session.request.call_count, 1)

    def test_create_and_protocol(self):
        self.client.session.request.return_value = response({"task_id": "123"})
        self.assertEqual(self.client.create(payload())["task_id"], "123")
        for data in [
            {"task": {}},
            task([]),
            task("surprise"),
            {"task": {"id": "other", "status": "queued"}},
            [],
        ]:
            self.client.session.request.return_value = response(data)
            with self.assertRaises(vc.CliError):
                self.client.query("123")
        r = response()
        r.json.side_effect = ValueError()
        self.client.session.request.return_value = r
        with self.assertRaises(vc.CliError):
            self.client.query("123")

    @patch.object(vc.time, "sleep")
    def test_wait_terminal(self, sleep):
        self.client.query = Mock(side_effect=[task("queued"), task("running"), task()])
        self.assertEqual(self.client.wait("123", 0.01, 1)["task"]["status"], "succeeded")
        for status in ["failed", "cancelled"]:
            self.client.query = Mock(return_value=task(status))
            with self.assertRaises(vc.CliError) as cm:
                self.client.wait("123", 0.01, 1)
            self.assertEqual(cm.exception.code, 4)

    def test_timeout(self):
        with patch.object(vc.time, "monotonic", return_value=2):
            with self.assertRaises(vc.CliError) as cm:
                self.client.query("123", deadline=1)
            self.assertEqual(cm.exception.code, 5)

    def test_post_network_failure_and_invalid_id(self):
        self.client.session.request.side_effect = requests.Timeout("timeout")
        with self.assertRaises(vc.CliError):
            self.client.create(payload())
        self.assertEqual(self.client.session.request.call_count, 1)
        for value in ["", "../escape", "/tmp/a", "a?b", None]:
            with self.assertRaises(vc.CliError):
                vc.require_task_id(value)


class DownloadTests(unittest.TestCase):
    def test_missing_output_url(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(vc.CliError) as cm:
                vc.download(
                    Mock(timeout=1),
                    "123",
                    task(content=None),
                    str(Path(directory) / "out.mp4"),
                    False,
                )
            self.assertEqual(cm.exception.code, 3)

    @patch.object(vc.time, "sleep")
    def test_download_and_no_credentials(self, sleep):
        client = Mock(timeout=1)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vc.requests, "Session") as factory,
        ):
            session = factory.return_value.__enter__.return_value
            session.get.return_value = response(headers={"Content-Length": "5"})
            out = Path(directory) / "out.mp4"
            self.assertEqual(vc.download(client, "123", task(), str(out), False)["bytes"], 5)
            self.assertEqual(out.read_bytes(), b"video")
            self.assertNotIn("headers", session.get.call_args.kwargs)
            with self.assertRaises(vc.CliError):
                vc.download(client, "123", task(), str(out), False)
            session.get.return_value = response(chunks=(b"new",))
            vc.download(client, "123", task(), str(out), True)
            self.assertEqual(out.read_bytes(), b"new")

    @patch.object(vc.time, "sleep")
    def test_refresh_and_partial_cleanup(self, sleep):
        client = Mock(timeout=1)
        client.query.return_value = task()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vc.requests, "Session") as factory,
        ):
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = [response(status=403), response()]
            vc.download(client, "123", task(), str(Path(directory) / "out.mp4"), False)
            client.query.assert_called_once_with("123")
            session.get.side_effect = None
            session.get.return_value = response(headers={"Content-Length": "100"})
            out = Path(directory) / "bad.mp4"
            with self.assertRaises(vc.CliError):
                vc.download(client, "123", task(), str(out), False)
            self.assertFalse(out.exists())
            self.assertEqual(list(Path(directory).glob(".mgen-*")), [])

    def test_remote_size(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(vc.requests, "Session") as factory,
        ):
            session = factory.return_value.__enter__.return_value
            session.get.return_value = response(chunks=(b"12345",))
            with self.assertRaises(vc.CliError):
                files.fetch_media("https://example.com/a", Path(directory) / "a", 4, 1)
            self.assertNotIn("headers", session.get.call_args.kwargs)


class CliTests(unittest.TestCase):
    def test_cli_offline_and_conflict(self):
        with (
            patch.dict(os.environ, {"VIDEO_MODEL": "MiniMax-H3"}, clear=True),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(cli.main(["video", "validate", "--prompt", "日落"]), 0)
            self.assertTrue(json.loads(out.getvalue())["valid"])
        args = cli.parser().parse_args(
            ["video", "create", "--request-json", "request.json", "--prompt", "x"]
        )
        with self.assertRaises(common.CliError):
            vcli.build_payload(args, config.DEFAULT_VIDEO.with_cli(args))

    def test_environment_and_json_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("VIDEO_MODEL=MiniMax-H3-Max\nVIDEO_API_KEY=dotenv-secret\n")
            request = Path(directory) / "r.json"
            data = payload()
            del data["model"]
            request.write_text(json.dumps(data))
            with (
                patch.dict(os.environ, {"VIDEO_MODEL": "MiniMax-H3"}, clear=True),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "--env-file",
                            str(env),
                            "video",
                            "validate",
                            "--request-json",
                            str(request),
                        ]
                    ),
                    0,
                )
                self.assertEqual(json.loads(out.getvalue())["model"], "MiniMax-H3")
                self.assertIsNone(os.getenv("VIDEO_API_KEY"))
        with common.output_secrets(("secret",)):
            self.assertNotIn("secret", common.redact("secret data:image/png;base64,YQ=="))


class IntegrationTests(unittest.TestCase):
    def test_full_run_over_http(self):
        observed = []
        queries = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, kind="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                observed.append((self.path, self.headers.get("Authorization")))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.server.received_payload = body
                self.send(b'{"task_id":"123"}')

            def do_GET(self):
                observed.append((self.path, self.headers.get("Authorization")))
                if self.path == "/result.mp4":
                    self.send(b"complete-video", "video/mp4")
                else:
                    queries.append(1)
                    data = task("queued" if len(queries) == 1 else "succeeded")
                    data["task"]["content"]["url"] = (
                        f"http://127.0.0.1:{self.server.server_port}/result.mp4"
                    )
                    self.send(json.dumps(data).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {"VIDEO_API_KEY": "test-key"}, clear=True),
            ):
                output = Path(directory) / "result.mp4"
                with contextlib.redirect_stdout(io.StringIO()) as stdout:
                    result = cli.main(
                        [
                            "video",
                            "run",
                            "--base-url",
                            f"http://127.0.0.1:{server.server_port}",
                            "--prompt",
                            "海边日落",
                            "--interval",
                            ".001",
                            "--output",
                            str(output),
                        ]
                    )
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(stdout.getvalue())["bytes"], 14)
                self.assertEqual(output.read_bytes(), b"complete-video")
                self.assertEqual(server.received_payload["ratio"], "16:9")
                self.assertEqual(observed[0], ("/v2/video_generation", "Bearer test-key"))
                self.assertEqual(observed[1], ("/v2/query/video_generation/123", "Bearer test-key"))
                self.assertEqual(observed[-1], ("/result.mp4", None))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
