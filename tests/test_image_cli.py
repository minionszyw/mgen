import base64
import contextlib
import copy
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

from media_cli import cli, common, config, http
from media_cli.image import cli as commands
from media_cli.image import client, validation


def payload(**kwargs):
    return {
        "model": "gpt-6-astra",
        "input": "画一只猫",
        "tools": [{"type": "image_generation", "model": "gpt-image-2.5-sunburst"}],
        **kwargs,
    }


def png(mode="RGB", size=(256, 256)):
    out = io.BytesIO()
    Image.new(mode, size).save(out, format="PNG")
    return out.getvalue()


def data_uri(raw):
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def result(status="completed", **kwargs):
    return {
        "id": "resp_123",
        "object": "response",
        "status": status,
        "error": None,
        "output": [
            {
                "type": "image_generation_call",
                "id": "ig_123",
                "status": "completed",
                "result": base64.b64encode(png()).decode(),
                "revised_prompt": "画一只可爱的猫",
            }
        ],
        **kwargs,
    }


def response(data, status=200, headers=None):
    r = Mock(status_code=status, headers=headers or {})
    r.json.return_value = data
    return r


class ValidationTests(unittest.TestCase):
    def test_defaults_and_edit_context(self):
        request, report = validation.validate(payload())
        p = request.payload
        self.assertTrue(p["store"])
        self.assertTrue(p["background"])
        self.assertEqual(p["tools"][0]["output_format"], "png")
        self.assertEqual(report["image_count"], 0)
        self.assertEqual(payload()["input"], "画一只猫")
        p["tools"][0]["action"] = "edit"
        with self.assertRaises(common.CliError):
            validation.validate(p)
        p["previous_response_id"] = "resp_previous"
        validation.validate(p)

    def test_request_shape_and_types(self):
        invalid = [
            payload(input=" "),
            payload(input=[]),
            payload(model="gpt-image-2.5-flare"),
            payload(model=[]),
            payload(store=False),
            payload(background=False),
            payload(stream=True),
            payload(tools=[]),
            payload(tool_choice="auto"),
            payload(previous_response_id="../x"),
            payload(input=[{"role": "assistant", "content": []}]),
        ]
        for p in invalid:
            with self.subTest(payload=p), self.assertRaises(common.CliError):
                validation.validate(p)

    def test_tool_options_and_sizes(self):
        for size in ["1024x1024", "1536x1024", "3840x2160", "2160x3840"]:
            p = payload()
            p["tools"][0].update(size=size, quality="max")
            validation.validate(p)
        for changes in [
            {"size": "256x256"},
            {"size": "1025x1024"},
            {"size": "3840x3840"},
            {"size": "4096x2048"},
            {"size": "3072x512"},
            {"size": []},
            {"quality": []},
            {"model": "gpt-image-1"},
            {"partial_images": 1},
            {"input_fidelity": "high"},
            {"output_format": "png", "output_compression": 50},
            {"output_compression": True},
            {"output_format": "webp", "output_compression": 101},
            {"background": "transparent", "output_format": "jpeg"},
        ]:
            p = payload()
            p["tools"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(common.CliError):
                validation.validate(p)
        p = payload()
        p["tools"][0].update(background="transparent", output_format="webp", output_compression=0)
        validation.validate(p)

    def test_local_remote_and_file_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cat.png"
            path.write_bytes(png())
            args = cli.parser().parse_args(
                [
                    "image",
                    "validate",
                    "--prompt",
                    "修改猫",
                    "--image",
                    str(path),
                    "--image-file-id",
                    "file-test",
                ]
            )
            p, report = validation.validate(
                commands.build_payload(args, config.DEFAULT_IMAGE.with_cli(args))
            )
            self.assertTrue(
                p.payload["input"][0]["content"][1]["image_url"].startswith(
                    "data:image/png;base64,"
                )
            )
            self.assertEqual(report["unchecked"], ["input_image[1]"])

            def fetch(url, target, limit, timeout):
                Path(target).write_bytes(png())

            with patch.object(validation, "fetch_media", side_effect=fetch):
                item, info = validation.prepare_image(
                    {"image_url": "https://example.com/cat"}, directory, 1
                )
                self.assertEqual(item["image_url"], "https://example.com/cat")
                self.assertEqual(info["size"], (256, 256))

    def test_corrupt_mime_and_animation(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in [
                "data:image/png;base64,====",
                "data:image/png;base64,YQ==",
                "data:image/jpeg;base64," + base64.b64encode(png()).decode(),
            ]:
                with self.assertRaises(common.CliError):
                    validation.prepare_image({"image_url": value}, directory, 1)
            path = Path(directory) / "animated.gif"
            Image.new("RGB", (16, 16), "red").save(
                path, save_all=True, append_images=[Image.new("RGB", (16, 16), "blue")]
            )
            with self.assertRaises(common.CliError):
                validation.inspect_image(path)

    def test_masks(self):
        def masked(mask_raw, first=png()):
            p = payload(
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "编辑"},
                            {"type": "input_image", "image_url": data_uri(first)},
                        ],
                    }
                ]
            )
            p["tools"][0]["input_image_mask"] = {"image_url": data_uri(mask_raw)}
            return p

        validation.validate(masked(png("RGBA")))
        for raw in [png("RGB"), png("RGBA", (128, 256))]:
            with self.assertRaises(common.CliError):
                validation.validate(masked(raw))
        p = payload()
        p["tools"][0]["input_image_mask"] = {"file_id": "file-mask"}
        with self.assertRaises(common.CliError):
            validation.validate(p)
        p["input"] = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "编辑"},
                    {"type": "input_image", "file_id": "file-input"},
                ],
            }
        ]
        _, report = validation.validate(p)
        self.assertIn("mask_format_and_size_match", report["unchecked"])

    def test_payload_and_count_limits(self):
        with patch.object(validation, "MAX_PAYLOAD", 1):
            with self.assertRaises(common.CliError):
                validation.validate(payload())
        p = payload(
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "编辑"},
                        {"type": "input_image", "file_id": "file-input"},
                        {"type": "input_image", "file_id": "file-other"},
                    ],
                }
            ]
        )
        with patch.object(validation, "MAX_IMAGES", 1), self.assertRaises(common.CliError):
            validation.validate(p)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(common.CliError):
                validation.prepare_image({"image_url": data_uri(png())}, directory, 1, limit=10)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = client.ImageClient("https://api.example/v1", "test-secret", 1)
        self.client.session = Mock()

    def test_create_protocol_and_post_no_retry(self):
        self.client.session.request.return_value = response(result("queued", output=[]))
        self.assertEqual(self.client.create(payload())["id"], "resp_123")
        self.client.session.request.side_effect = requests.Timeout("test-secret")
        self.client.session.request.reset_mock()
        with self.assertRaises(common.CliError) as cm:
            self.client.create(payload())
        self.assertNotIn("test-secret", str(cm.exception))
        self.assertEqual(self.client.session.request.call_count, 1)
        for data in [result("unknown"), result(id="../escape"), result(output=None), {}, []]:
            with self.assertRaises(common.CliError):
                client.check_response(data)

    @patch.object(http.time, "sleep")
    def test_get_retry_and_nonretryable_errors(self, sleep):
        self.client.session.request.side_effect = [response({}, 500), response(result())]
        self.assertEqual(self.client.query("resp_123")["status"], "completed")
        sleep.assert_called_once()
        for error in [
            {"code": "insufficient_quota"},
            {"type": "image_generation_user_error", "code": "moderation_blocked"},
        ]:
            self.client.session.request.reset_mock()
            self.client.session.request.side_effect = None
            self.client.session.request.return_value = response(
                {"error": error}, 429, {"x-request-id": "req-test"}
            )
            with self.assertRaises(common.CliError) as cm:
                self.client.query("resp_123")
            self.assertIn("req-test", str(cm.exception))
            self.assertEqual(self.client.session.request.call_count, 1)

    @patch.object(client.time, "sleep")
    def test_wait_and_failure_status(self, sleep):
        self.client.query = Mock(side_effect=[result("in_progress"), result()])
        self.assertEqual(
            self.client.wait("resp_123", 0.01, 1, initial=result("queued"))["status"], "completed"
        )
        for state in ["failed", "cancelled", "incomplete"]:
            data = result(state, error={"code": "moderation_blocked"})
            # HTTP 200 的 response.error 属于任务失败，不是传输错误。
            self.client.session.request.side_effect = None
            self.client.session.request.return_value = response(data)
            other = client.ImageClient("https://api.example/v1", "key", 1)
            other.session = self.client.session
            self.assertEqual(other.query("resp_123")["status"], state)
            with self.assertRaises(common.CliError) as cm:
                other.wait("resp_123", 0.01, 1, initial=data)
            self.assertEqual(cm.exception.code, 4)
        with patch.object(client.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(common.CliError) as cm:
                self.client.wait("resp_123", 0.01, 1, initial=result("queued"))
            self.assertEqual(cm.exception.code, 5)


class SaveTests(unittest.TestCase):
    def test_all_images_actual_format_and_manifest(self):
        data = result()
        second = copy.deepcopy(data["output"][0])
        second["id"] = "ig_456"
        buffer = io.BytesIO()
        Image.new("RGB", (32, 32)).save(buffer, format="JPEG")
        second["result"] = base64.b64encode(buffer.getvalue()).decode()
        data["output"].append(second)
        with tempfile.TemporaryDirectory() as directory:
            saved = client.save_images(data, directory)
            self.assertEqual(len(saved["images"]), 2)
            self.assertEqual(Path(saved["images"][1]["path"]).suffix, ".jpeg")
            manifest = Path(saved["manifest"]).read_text()
            self.assertNotIn(data["output"][0]["result"], manifest)
            self.assertIn("revised_prompt", manifest)
            with self.assertRaises(common.CliError):
                client.save_images(data, directory)
            client.save_images(data, directory, True)

    def test_corrupt_result_and_missing_image(self):
        for output in [[], [{"type": "message"}]]:
            with self.assertRaises(common.CliError) as cm:
                client.decode_outputs(result(output=output))
            self.assertEqual(cm.exception.code, 4)
        with tempfile.TemporaryDirectory() as directory:
            for value in ["====", base64.b64encode(b"not image").decode()]:
                data = result()
                bad = copy.deepcopy(data["output"][0])
                bad.update(id="ig_bad", result=value)
                data["output"].append(bad)
                with self.assertRaises(common.CliError):
                    client.save_images(data, directory)
                self.assertEqual(list(Path(directory).iterdir()), [])
        data = result()
        data["output"][0]["id"] = "../escape"
        with self.assertRaises(common.CliError):
            client.decode_outputs(data)

    def test_partial_save_reports_paths(self):
        data = result()
        second = copy.deepcopy(data["output"][0])
        second["id"] = "ig_456"
        data["output"].append(second)
        real_write = client.atomic_write

        def write(path, raw, overwrite):
            if Path(path).name == "ig_456.png":
                raise OSError("disk full")
            real_write(path, raw, overwrite)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(client, "atomic_write", side_effect=write),
        ):
            with self.assertRaises(common.CliError) as cm:
                client.save_images(data, directory)
            self.assertIn("ig_123.png", str(cm.exception))
            self.assertTrue((Path(directory) / "ig_123.png").exists())
            self.assertFalse((Path(directory) / "result.json").exists())
            self.assertEqual(list(Path(directory).glob(".media-cli-*")), [])


class CliTests(unittest.TestCase):
    def test_validation_config_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "config.env"
            env.write_text("IMAGE_MODEL=gpt-6-astra\nIMAGE_TOOL_MODEL=gpt-image-2.5-flare\n")
            with (
                patch.dict(os.environ, {"IMAGE_MODEL": "custom-mainline"}, clear=True),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(
                    cli.main(["--env-file", str(env), "image", "validate", "--prompt", "cat"]), 0
                )
                report = json.loads(out.getvalue())
                self.assertEqual(report["model"], "custom-mainline")
                self.assertEqual(report["tool_model"], "gpt-image-2.5-flare")
            request = Path(directory) / "request.json"
            p = payload()
            del p["model"]
            del p["tools"][0]["model"]
            request.write_text(json.dumps(p))
            with (
                patch.dict(os.environ, {}, clear=True),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                self.assertEqual(cli.main(["image", "validate", "--request-json", str(request)]), 0)
            args = cli.parser().parse_args(
                ["image", "create", "--request-json", str(request), "--prompt", "x"]
            )
            with self.assertRaises(common.CliError):
                commands.build_payload(args, config.DEFAULT_IMAGE.with_cli(args))
        with common.output_secrets(("image-secret", "video-secret")):
            self.assertNotIn(
                "secret", common.redact("image-secret video-secret data:image/png;base64,YQ==")
            )

    def test_invalid_argument_and_missing_key(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["image", "run", "--prompt", "cat", "--timeout", "nan"])
        self.assertEqual(cm.exception.code, 2)
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                cli.main(["--env-file", "/missing/env", "image", "run", "--prompt", "cat"]), 2
            )


class IntegrationTests(unittest.TestCase):
    def test_background_edit_run_and_restore(self):
        observed = []
        requests_seen = []
        queries = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, content_type="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                observed.append((self.path, self.headers.get("Authorization")))
                requests_seen.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                self.send(json.dumps(result("queued", output=[])).encode())

            def do_GET(self):
                observed.append((self.path, self.headers.get("Authorization")))
                if self.path == "/reference.png":
                    self.send(png(), "image/png")
                else:
                    queries.append(1)
                    self.send(
                        json.dumps(
                            result("in_progress" if len(queries) == 1 else "completed")
                        ).encode()
                    )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {"IMAGE_API_KEY": "test-key"}, clear=True),
            ):
                args = [
                    "image",
                    "run",
                    "--base-url",
                    base + "/v1",
                    "--prompt",
                    "修改猫",
                    "--action",
                    "edit",
                    "--image",
                    base + "/reference.png",
                    "--interval",
                    ".001",
                    "--output-dir",
                    directory,
                ]
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.main(args), 0)
                    saved = json.loads(out.getvalue())
                self.assertEqual(Path(saved["images"][0]["path"]).read_bytes(), png())
                self.assertEqual(observed[0], ("/reference.png", None))
                self.assertIn(("/v1/responses", "Bearer test-key"), observed)
                self.assertIn(("/v1/responses/resp_123", "Bearer test-key"), observed)
                self.assertTrue(requests_seen[0]["background"])
                self.assertTrue(requests_seen[0]["store"])
                self.assertEqual(requests_seen[0]["tool_choice"], {"type": "image_generation"})
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(
                        cli.main(["image", "query", "resp_123", "--base-url", base + "/v1"]), 0
                    )
                    self.assertNotIn("result", json.loads(out.getvalue())["output"][0])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        cli.main(
                            [
                                "image",
                                "download",
                                "resp_123",
                                "--base-url",
                                base + "/v1",
                                "--output-dir",
                                directory + "/restored",
                            ]
                        ),
                        0,
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
