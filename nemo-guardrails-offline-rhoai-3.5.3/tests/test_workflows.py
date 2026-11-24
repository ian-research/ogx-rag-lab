"""Regression checks for local setup, Playground recovery and credential validation.

All oc calls and HTTP responses are mocked; these tests do not contact a cluster.
"""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cluster
import probe
import render
from test_package import example


def ready_deployment():
    return {"metadata": {"generation": 2}, "spec": {"replicas": 1},
            "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1,
                       "readyReplicas": 1, "availableReplicas": 1}}


class SetupTests(unittest.TestCase):
    def test_missing_settings_explains_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "cp -n settings.example.json settings.json"):
                render.settings(Path(directory) / "settings.json")

    def test_missing_render_manifest_explains_next_step(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(render, "ROOT", Path(directory)), \
                patch.object(render, "settings", return_value=example()):
            with self.assertRaisesRegex(ValueError, "python3 scripts/render.py"):
                render.checked_settings()

    def test_changed_settings_require_new_render(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(render, "ROOT", Path(directory)), \
                patch.object(render, "settings", return_value=example()), \
                patch.object(render, "fingerprint", return_value="new"):
            folder = Path(directory) / "rendered"
            folder.mkdir()
            (folder / "manifest.json").write_text('{"fingerprint":"old"}')
            with self.assertRaisesRegex(ValueError, "render.py"):
                render.checked_settings()

    def test_invalid_config_shape_is_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            for value in (None, [], "example", 1):
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    with self.assertRaisesRegex(ValueError, "settings.json 字段"):
                        render.settings(path)

    def test_token_renewal_does_not_require_rendered_yaml(self):
        with patch.object(cluster, "settings", return_value=example()), \
                patch.object(cluster, "checked_settings") as checked, \
                patch.object(cluster, "token") as token, \
                patch.object(sys, "argv", ["cluster.py", "token"]):
            cluster.main()
            checked.assert_not_called()
            token.assert_called_once_with(example(), "720h")

    def test_trust_requires_current_rendered_yaml(self):
        with patch.object(cluster, "checked_settings", return_value=example()) as checked, \
                patch.object(cluster, "trust") as trust, \
                patch.object(sys, "argv", ["cluster.py", "trust"]):
            cluster.main()
            checked.assert_called_once_with()
            trust.assert_called_once_with(example())


class PlaygroundTests(unittest.TestCase):
    def test_missing_playground_stops_before_mutating(self):
        with patch.object(cluster, "get", return_value=None) as get, \
                patch.object(cluster, "apply") as apply, patch.object(cluster, "oc") as oc:
            with self.assertRaisesRegex(ValueError, "目标项目 llm"):
                cluster.trust(example())
            get.assert_called_once_with("ogxservers", "lsd-genai-playground", "llm", optional=True)
            apply.assert_not_called()
            oc.assert_not_called()

    def test_ready_requires_complete_rollout(self):
        self.assertTrue(cluster.deployment_ready(ready_deployment()))
        self.assertFalse(cluster.deployment_ready(None))
        for key, value in (("observedGeneration", 1), ("replicas", 2), ("updatedReplicas", 0),
                           ("readyReplicas", 0), ("availableReplicas", 0)):
            with self.subTest(key=key):
                dep = ready_deployment()
                dep["status"][key] = value
                self.assertFalse(cluster.deployment_ready(dep))
        dep = ready_deployment()
        dep["spec"]["replicas"] = 0
        dep["status"].update({k: 0 for k in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas")})
        self.assertFalse(cluster.deployment_ready(dep))

    def test_waits_until_phase_and_deployment_are_ready(self):
        phases = iter(("Initializing", "Initializing", "Ready", "Ready"))
        dep_calls = []

        def get(kind, name, namespace, optional=False):
            self.assertEqual(namespace, "llm")
            if kind == "ogxservers":
                return {"status": {"phase": next(phases)}}
            dep_calls.append(kind)
            dep = ready_deployment()
            if len(dep_calls) == 1:
                dep["status"]["readyReplicas"] = 0
            return dep

        with patch.object(cluster, "get", side_effect=get), patch.object(cluster.time, "sleep") as sleep, \
                patch("sys.stdout", new_callable=io.StringIO):
            cluster.wait_playground(example())
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(dep_calls, ["deployment", "deployment"])

    def test_failed_phase_stops_with_diagnostic_command(self):
        with patch.object(cluster, "get", return_value={"status": {"phase": "Failed"}}), \
                patch("sys.stdout", new_callable=io.StringIO), patch.object(cluster.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "oc describe ogxservers"):
                cluster.wait_playground(example())
            sleep.assert_not_called()

    def test_trust_preserves_tls_and_checks_resource_version(self):
        original = {"metadata": {"resourceVersion": "123"}, "spec": {"tls": {
            "identity": {"key": {"name": "key", "key": "key.pem"}},
            "trust": {"caCertificates": [{"name": "existing-ca", "key": "root.crt"}]}}}}
        expected = copy.deepcopy(original)
        with patch.object(cluster, "wait_playground"), patch.object(cluster, "apply"), \
                patch.object(cluster, "wait_for"), patch.object(cluster, "get", return_value=original), \
                patch.object(cluster, "private_file"), patch.object(cluster, "oc") as oc, \
                patch("sys.stdout", new_callable=io.StringIO):
            cluster.trust(example())
            args = oc.call_args.args
            self.assertEqual(args[:5], ("patch", "ogxservers", "lsd-genai-playground", "-n", "llm"))
            payload = json.loads(args[-1])
            self.assertEqual(payload[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "123"})
            self.assertEqual(payload[1]["value"]["identity"], expected["spec"]["tls"]["identity"])
            self.assertEqual(len(payload[1]["value"]["trust"]["caCertificates"]), 2)
            self.assertEqual(original, expected)


class VerifyTests(unittest.TestCase):
    def test_verifies_supplied_token_without_issuing_another(self):
        value = "SYNTHETIC_RENEWAL_TOKEN"
        with tempfile.TemporaryDirectory() as directory, patch.object(cluster, "wait_playground"), \
                patch.object(cluster, "oc", return_value="PASS") as oc, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            path = Path(directory) / "token.txt"
            path.write_text(value + "\n")
            cluster.verify(example(), path)
            oc.assert_called_once()
            self.assertEqual(oc.call_args.args[0], "exec")
            self.assertEqual(json.loads(oc.call_args.kwargs["data"])["token"], value)
            self.assertNotIn(value, str(oc.call_args.args) + output.getvalue())
            self.assertTrue(oc.call_args.kwargs["safe_stdout"])

    def test_default_verification_explicitly_uses_temporary_token(self):
        with patch.object(cluster, "wait_playground"), \
                patch.object(cluster, "oc", side_effect=["TEMP_TOKEN", "PASS"]) as oc, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            cluster.verify(example())
            self.assertIn("--duration=15m", oc.call_args_list[0].args)
            self.assertEqual(json.loads(oc.call_args_list[1].kwargs["data"])["token"], "TEMP_TOKEN")
            self.assertIn("15 分钟", output.getvalue())
            self.assertIn("不读取 Dashboard", output.getvalue())

    def test_missing_or_malformed_token_is_not_sent(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cluster, "wait_playground"), \
                patch.object(cluster, "oc") as oc:
            path = Path(directory) / "token.txt"
            with self.assertRaisesRegex(ValueError, "找不到"):
                cluster.verify(example(), path)
            for value in ("", "Bearer abc", "abc\ndef", "abc def"):
                with self.subTest(value=value):
                    path.write_text(value)
                    with self.assertRaises(ValueError):
                        cluster.verify(example(), path)
            oc.assert_not_called()

    def test_outer_timeout_covers_all_requests(self):
        self.assertGreater(cluster.PROBE_TIMEOUT, len(probe.CASES) * probe.REQUEST_TIMEOUT)

    def test_probe_failure_shows_status_without_sensitive_stderr(self):
        response = subprocess.CompletedProcess(["oc"], 1,
            stdout="PASS 测试标记\nFAIL 普通问答: HTTP 401\n", stderr="Authorization: Bearer PRIVATE_VALUE")
        with patch.object(cluster.subprocess, "run", return_value=response):
            with self.assertRaises(RuntimeError) as raised:
                cluster.oc("exec", data='{"token":"PRIVATE_VALUE"}', safe_stdout=True)
            self.assertIn("HTTP 401", str(raised.exception))
            self.assertNotIn("PRIVATE_VALUE", str(raised.exception))


class ProbeTests(unittest.TestCase):
    config = {"base_url": "https://nemo.example.test/v1", "model": "tinyllama", "token": "TEST_TOKEN"}

    def test_all_requests_are_independent_and_bounded(self):
        expected_inputs = iter(probe.CASES)

        def respond(request, timeout):
            label, text, ids = next(expected_inputs)
            payload = json.loads(request.data)
            self.assertEqual(payload["messages"], [{"role": "user", "content": text}])
            self.assertEqual(payload["max_tokens"], 128)
            self.assertFalse(payload["stream"])
            self.assertEqual(timeout, probe.REQUEST_TIMEOUT)
            content = "【NeMo Guardrails｜已拦截】\n" + " ".join(ids) if ids else "Paris is the capital of France."
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

        with patch.object(probe.urllib.request, "urlopen", side_effect=respond) as urlopen, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            probe.check(self.config)
            self.assertEqual(urlopen.call_count, 6)
            self.assertEqual(output.getvalue().count("PASS "), 6)
            self.assertNotIn("TEST_TOKEN", output.getvalue())

    def test_http_error_does_not_leak_response_or_credentials(self):
        error = urllib.error.HTTPError("https://example.test", 401, "PRIVATE_ERROR", {}, io.BytesIO(b"PRIVATE_BODY"))
        with patch.object(probe.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(SystemExit) as raised:
                probe.check(self.config)
            self.assertEqual(str(raised.exception), "FAIL 测试标记: HTTP 401")

    def test_malformed_response_has_actionable_failure(self):
        for value, expected in ((b"not json", "有效 JSON"), (b'{"choices":[]}', "缺少文本回答"),
                                (b'{"choices":[{"message":{"content":null}}]}', "缺少文本回答")):
            with self.subTest(value=value), patch.object(probe.urllib.request, "urlopen", return_value=io.BytesIO(value)):
                with self.assertRaisesRegex(SystemExit, expected):
                    probe.check(self.config)

    def test_unexpected_additional_rules_fail(self):
        content = "【NeMo Guardrails｜已拦截】 DEMO-000 DLP-201"
        value = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        with patch.object(probe.urllib.request, "urlopen", return_value=io.BytesIO(value)):
            with self.assertRaisesRegex(SystemExit, "未收到预期的规则编号"):
                probe.check(self.config)


if __name__ == "__main__":
    unittest.main()
