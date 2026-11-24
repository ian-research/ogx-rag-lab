#!/usr/bin/env python3
"""Render offline YAML using only the Python standard library. No cluster access."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
KEYS = {"playground_namespace", "nemo_namespace", "nemo_name", "ogx_name", "maas_base_url", "model_id", "maas_ca_file"}
OPTIONAL_KEYS = {"response_language"}


def settings(path=None):
    path = Path(path) if path else ROOT / "settings.json"
    if not path.is_file():
        raise ValueError(
            f"缺少配置文件：{path}。请在包根目录运行 cp -n settings.example.json settings.json，"
            "填写目标集群的完整 MaaS API 基址及资源名称。首次安装或执行 trust 前还需运行 python3 scripts/render.py。"
        )
    config = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(config, dict) or not KEYS <= set(config) or set(config) - KEYS - OPTIONAL_KEYS
            or any(not isinstance(v, str) for v in config.values())):
        raise ValueError("settings.json 字段须符合配置样例，且值均为字符串；response_language 可省略。")
    config.setdefault("response_language", "zh-CN")
    if config["response_language"] not in ("zh-CN", "zh-TW"):
        raise ValueError("response_language 只支持 zh-CN（简体）或 zh-TW（台湾繁体）。")
    for key in ("playground_namespace", "nemo_namespace", "nemo_name", "ogx_name"):
        if len(config[key]) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", config[key]):
            raise ValueError(f"无效的 Kubernetes 名称：{key}")
    url = urlsplit(config["maas_base_url"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("maas_base_url 必须是无凭据、无查询参数的 HTTP(S) 地址。")
    if not url.path.endswith("/v1") or "REPLACE" in config["maas_base_url"]:
        raise ValueError("请替换目标集群地址；maas_base_url 必须以 /v1 结尾。")
    if any(c.isspace() for c in config["maas_base_url"]) or not config["model_id"].strip():
        raise ValueError("模型地址不能包含空白，model_id 不能为空。")
    if config["maas_ca_file"] and url.scheme != "https":
        raise ValueError("仅 HTTPS 上游需要 maas_ca_file。")
    return config


def ca_text(config):
    if not config["maas_ca_file"]:
        return ""
    path = Path(config["maas_ca_file"])
    if not path.is_absolute():
        path = ROOT / path
    text = path.read_text(encoding="utf-8")
    if "-----BEGIN CERTIFICATE-----" not in text or "PRIVATE KEY" in text:
        raise ValueError("maas_ca_file 必须是 CA 证书 PEM，不能包含私钥。")
    return text


def fingerprint(config):
    h = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    for folder in ("templates", "rules"):
        for path in sorted((ROOT / folder).iterdir()):
            if path.is_file():
                h.update(path.name.encode())
                h.update(path.read_bytes())
    h.update(ca_text(config).encode())
    return h.hexdigest()


def render(config, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    values = {key.upper(): json.dumps(value, ensure_ascii=False) for key, value in config.items()}
    for path in sorted((ROOT / "templates").glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        if path.name == "20-rules-configmap.yaml":
            # The complete template is reviewable; rules/ remains the editable source.
            text = text.split("  actions.py: |", 1)[0]
            for filename in ("actions.py", "demo_rules.co"):
                text += "  " + filename + ": |\n"
                source = (ROOT / "rules" / filename).read_text(encoding="utf-8")
                if filename == "actions.py":
                    language = config.get("response_language", "zh-CN")
                    if language not in ("zh-CN", "zh-TW"):
                        raise ValueError("response_language 只支持 zh-CN 或 zh-TW。")
                    default = 'RESPONSE_LANGUAGE = "zh-CN"'
                    if source.count(default) != 1:
                        raise ValueError("rules/actions.py 的 RESPONSE_LANGUAGE 默认声明必须保留为 zh-CN；请在 settings.json 选择语言。")
                    source = source.replace(default, "RESPONSE_LANGUAGE = " + json.dumps(language))
                text += "".join("    " + line + "\n" for line in source.splitlines())
        for key, value in values.items():
            text = text.replace("@@" + key + "@@", value)
        if "@@" in text:
            raise ValueError(f"未替换的模板字段：{path.name}")
        if path.name == "30-nemo.yaml" and config["maas_ca_file"]:
            text += '  caBundleConfig:\n    configMapName: nemo-maas-ca\n    configMapKeys: ["ca.crt"]\n'
        (out / path.name).write_text(text, encoding="utf-8")
    if config["maas_ca_file"]:
        cm = 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: nemo-maas-ca\n  namespace: ' + values["NEMO_NAMESPACE"] + '\ndata:\n  ca.crt: |\n'
        cm += "".join("    " + line + "\n" for line in ca_text(config).splitlines())
        (out / "21-maas-ca.yaml").write_text(cm, encoding="utf-8")
    else:
        (out / "21-maas-ca.yaml").unlink(missing_ok=True)
    (out / "manifest.json").write_text(json.dumps({"fingerprint": fingerprint(config), "settings": config}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def checked_settings():
    config = settings()
    manifest_path = ROOT / "rendered/manifest.json"
    if not manifest_path.is_file():
        raise ValueError("尚未生成部署文件，请先在包根目录运行 python3 scripts/render.py，再执行 install 或 trust。")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or manifest.get("fingerprint") != fingerprint(config):
        raise ValueError("参数或规则已变更，请先运行 python3 scripts/render.py。")
    return config


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "rendered")
    args = parser.parse_args()
    try:
        config = settings(args.settings)
        render(config, args.output)
        print(f"已生成 {args.output}；未连接集群，未包含真实凭据。")
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc))
