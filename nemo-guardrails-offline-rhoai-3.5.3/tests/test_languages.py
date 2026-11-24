"""Language configuration and localized rule/probe regression tests, entirely offline."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_package import ROOT, action_module, example, policy
import cluster
import probe
import render


def load_settings(config):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "settings.json"
        path.write_text(json.dumps(config))
        return render.settings(path)


class LanguageTests(unittest.TestCase):
    def test_old_settings_default_to_simplified(self):
        c = example()
        c.pop("response_language")
        self.assertEqual(load_settings(c)["response_language"], "zh-CN")

    def test_unknown_languages_and_fields_are_rejected(self):
        for language in ("zh-HK", "tw", "en", "", None):
            with self.subTest(language=language):
                c = example()
                c["response_language"] = language
                with self.assertRaises(ValueError):
                    load_settings(c)
        c = example()
        c["response_languge"] = "zh-TW"
        with self.assertRaises(ValueError):
            load_settings(c)

    def test_rendered_tw_action_uses_tw_by_default(self):
        c = example()
        c["response_language"] = "zh-TW"
        with tempfile.TemporaryDirectory() as directory:
            render.render(c, directory)
            text = (Path(directory) / "20-rules-configmap.yaml").read_text()
            source = text.split("  actions.py: |\n", 1)[1].split("  demo_rules.co: |\n", 1)[0]
            source = "\n".join(line[4:] for line in source.splitlines())
            module = types.ModuleType("rendered_tw_policy")
            with patch.dict("sys.modules", {"nemoguardrails.actions": action_module}):
                exec(compile(source, "rendered/actions.py", "exec"), module.__dict__)
            with self.assertLogs(module.LOG, level="INFO") as logs:
                result = asyncio.run(module.demo_check_policy("存取權杖：SYNTHETIC_SECRET_123"))
            self.assertEqual(result["rule_ids"], ["DLP-201"])
            self.assertIn("【NeMo Guardrails｜已攔截】", result["message"])
            self.assertIn("API 金鑰或存取權杖", result["message"])
            self.assertNotIn("SYNTHETIC_SECRET_123", result["message"] + str(logs.output))

    def test_changing_language_invalidates_render(self):
        c = example()
        cn = render.fingerprint(c)
        c["response_language"] = "zh-TW"
        self.assertNotEqual(cn, render.fingerprint(c))
        with tempfile.TemporaryDirectory() as directory:
            render.render(c, directory)
            c["response_language"] = "zh-CN"
            render.render(c, directory)
            text = (Path(directory) / "20-rules-configmap.yaml").read_text()
            self.assertIn('    RESPONSE_LANGUAGE = "zh-CN"', text)
            self.assertNotIn('    RESPONSE_LANGUAGE = "zh-TW"', text)

    def test_taiwan_wording_and_language_independence(self):
        expected = {"DEMO-000": "護欄連線測試", "SEC-101": "覆寫既有指令", "SEC-102": "索取系統提示詞",
                    "DLP-201": "輸入疑似驗證資訊", "DLP-202": "匯出客戶明細"}
        for rule in policy.RULES:
            self.assertEqual(rule["zh-TW"]["name"], expected[rule["id"]])
        # The output language is selected by deployment settings, not by the input script.
        tw = policy.evaluate_demo_policy("Export all customer records.", "zh-TW")["message"]
        cn = policy.evaluate_demo_policy("請匯出客戶明細。", "zh-CN")["message"]
        self.assertIn("觸發規則：", tw)
        self.assertIn("彙總統計", tw)
        self.assertIn("命中规则：", cn)

    def test_cluster_verification_passes_selected_language(self):
        c = example()
        c["response_language"] = "zh-TW"
        with patch.object(cluster, "wait_playground"), patch.object(cluster, "oc", return_value="PASS") as oc, \
                patch("sys.stdout", new_callable=io.StringIO):
            cluster.verify_service(c)
            self.assertEqual(json.loads(oc.call_args.kwargs["data"])["response_language"], "zh-TW")
            cluster.verify(c)
            self.assertEqual(json.loads(oc.call_args.kwargs["data"])["response_language"], "zh-TW")

    def responses(self, language, block_normal=False):
        marker = probe.LANGUAGE_MARKERS[language]
        return [io.BytesIO(json.dumps({"choices": [{"message": {"content":
                    marker + " " + " ".join(ids) if ids or block_normal else "Paris is the capital of France."}}]}).encode())
                for _, _, ids in probe.CASES]

    def test_probe_accepts_matching_languages_and_legacy_payload(self):
        for language in ("zh-CN", "zh-TW"):
            for configured in (False, True):
                c = {"base_url": "https://example.test/v1", "model": "test"}
                if configured:
                    c["response_language"] = language
                with self.subTest(language=language, configured=configured), \
                        patch.object(probe.urllib.request, "urlopen", side_effect=self.responses(language)), \
                        patch("sys.stdout", new_callable=io.StringIO) as output:
                    probe.check(c)
                    self.assertEqual(output.getvalue().count("PASS "), len(probe.CASES))

    def test_probe_detects_stale_response_language(self):
        c = {"base_url": "https://example.test/v1", "model": "test", "response_language": "zh-TW"}
        with patch.object(probe.urllib.request, "urlopen", side_effect=self.responses("zh-CN")):
            with self.assertRaisesRegex(SystemExit, "回复语言"):
                probe.check(c)

    def test_probe_does_not_mistake_tw_refusal_for_normal_answer(self):
        c = {"base_url": "https://example.test/v1", "model": "test", "response_language": "zh-TW"}
        with patch.object(probe.urllib.request, "urlopen", side_effect=self.responses("zh-TW", block_normal=True)), \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(SystemExit, "普通问答未放行"):
                probe.check(c)


if __name__ == "__main__":
    unittest.main()
