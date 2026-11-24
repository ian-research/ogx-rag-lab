#!/usr/bin/env python3
"""Run on a workstation already logged in to the TARGET offline cluster."""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from render import ROOT, checked_settings, settings
from probe import CASES, REQUEST_TIMEOUT

PROBE_TIMEOUT = len(CASES) * REQUEST_TIMEOUT + 60


def oc(*args, data=None, timeout=90, safe_stdout=False):
    result = subprocess.run(["oc", *map(str, args)], input=data, text=True,
                            capture_output=True, timeout=timeout)
    if result.returncode:
        # Never echo stdin: it may contain the MaaS key or a short-lived token.
        detail = result.stderr.strip() if data is None else "请求失败；为避免回显凭据，未打印请求正文。"
        # Only our probe program opts in: it prints fixed PASS/FAIL messages,
        # never the token, request headers or upstream response body.
        if data is not None and safe_stdout:
            summary = "\n".join(line for line in result.stdout.splitlines() if line.startswith(("PASS ", "FAIL ")))
            if summary:
                detail = summary
        raise RuntimeError(f"oc {args[0]} 失败：{detail}")
    return result.stdout.strip()


def get(kind, name=None, namespace=None, optional=False):
    args = ["get", kind]
    if name:
        args.append(name)
    if namespace:
        args.extend(["-n", namespace])
    if optional:
        args.append("--ignore-not-found")
    text = oc(*args, "-o", "json")
    return json.loads(text) if text else None


def apply(name):
    print(oc("apply", "-f", ROOT / "rendered" / name), flush=True)


def wait_for(predicate, description, timeout=600):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            print("PASS " + description, flush=True)
            return
        time.sleep(3)
    raise RuntimeError("等待超时：" + description)


def private_file(name, text):
    folder = ROOT / "private"
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    path = folder / name
    if path.is_symlink():
        raise ValueError("不写入符号链接：" + str(path))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    return path


def preflight(c):
    print("当前目标：" + oc("whoami", "--show-server"), flush=True)
    get("namespace", c["playground_namespace"])
    for crd in ("nemoguardrails.trustyai.opendatahub.io", "ogxservers.ogx.io"):
        get("crd", crd)
    # Feature discovery checks the actual schema instead of trusting a version string alone.
    oc("explain", "nemoguardrails.spec.nemoConfigs")
    oc("explain", "ogxservers.spec.tls.trust.caCertificates")
    if c["maas_ca_file"]:
        oc("explain", "nemoguardrails.spec.caBundleConfig")
    print("PASS 项目、CRD 与证书配置字段。请同时完成手册中的离线镜像和平台组件确认。", flush=True)


def install(c, key_file):
    preflight(c)
    ns, name = c["nemo_namespace"], c["nemo_name"]
    current = get("nemoguardrails", name, ns, optional=True)
    if current:
        raise ValueError("目标已有同名 NeMo 资源。本命令用于首次安装；规则更新请按手册操作，或选择新的命名空间。")
    if key_file:
        key = Path(key_file).read_text(encoding="utf-8").strip()
    else:
        key = getpass.getpass("目标 MaaS API key（输入不显示）：").strip()
    if not key or "\n" in key or key.startswith("Bearer "):
        raise ValueError("请输入原始 MaaS API key，不包含 Bearer 前缀或换行。")
    if not get("namespace", ns, optional=True):
        apply("00-namespace.yaml")
    apply("10-client-rbac.yaml")
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {
        "name": "tinyllama-maas-api-key", "namespace": ns}, "type": "Opaque", "stringData": {"token": key}}
    oc("apply", "-f", "-", data=json.dumps(secret))
    print("已配置目标 MaaS 凭据 Secret。", flush=True)
    if c["maas_ca_file"]:
        apply("21-maas-ca.yaml")
    apply("20-rules-configmap.yaml")
    apply("30-nemo.yaml")
    apply("40-playground-service-ca.yaml")
    expected = get("configmap", "nemo-demo-rules-config", ns)["data"]

    def loaded():
        dep = get("deployment", name, ns, optional=True)
        if not deployment_ready(dep):
            return False
        # Confirms ready pods actually read the final files, avoiding a stale-rollout race.
        program = "import json,pathlib; print(json.dumps({p.name:p.read_text() for p in pathlib.Path('/app/config/basic').iterdir() if p.is_file()}))"
        try:
            files = json.loads(oc("exec", "-n", ns, "deployment/" + name, "-c", "nemo-guardrails", "--", "python", "-c", program))
        except RuntimeError:
            return False
        return all(files.get(k) == v for k, v in expected.items())

    wait_for(loaded, "NeMo 已加载本次规则配置")
    print("NeMo 端点：https://" + name + "." + ns + ".svc.cluster.local/v1")
    print("下一步：运行 token，按手册在 Playground 创建端点与配置模型，再运行 trust 和 verify。")


def token(c, duration):
    issued = json.loads(oc("create", "token", "nemo-playground-client", "-n", c["nemo_namespace"],
                           "--duration=" + duration, "-o", "json"))
    status = issued.get("status", {})
    value, expiry = status.get("token"), status.get("expirationTimestamp")
    if not isinstance(value, str) or not value.strip() or not isinstance(expiry, str):
        raise ValueError("集群未返回有效 token 或实际到期时间；未更新本地 token 文件。")
    try:
        expires_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            raise ValueError("timezone missing")
    except ValueError:
        raise ValueError("集群返回的到期时间格式无效；未更新本地 token 文件。") from None
    info = {"requested_duration": duration, "expirationTimestamp": expiry,
            "expires_at_beijing": expires_at.astimezone(timezone(timedelta(hours=8))).isoformat()}
    path = private_file("nemo-playground-token.txt", value + "\n")
    info_path = private_file("nemo-playground-token-info.json", json.dumps(info, ensure_ascii=False, indent=2) + "\n")
    print("NeMo 访问 token 已写入：" + str(path))
    print("申请有效期：" + duration + "；实际到期时间以集群签发结果为准。")
    print("实际到期时间（UTC）：" + expiry)
    print("实际到期时间（北京时间）：" + info["expires_at_beijing"])
    print("到期记录已保存：" + str(info_path))
    print("新 token 不会自动替换 Playground 中的旧凭据；请按手册第 8.3 节创建替代端点并切换 Playground，API key 不加 Bearer。")


def merged_trust(spec):
    tls = copy.deepcopy(spec.get("tls") or {})
    trust = tls.setdefault("trust", {})
    refs = trust.setdefault("caCertificates", [])
    ref = {"name": "nemo-service-ca", "key": "service-ca.crt"}
    if ref not in refs:
        refs.append(ref)
    return tls


def require_playground(c):
    ns, name = c["playground_namespace"], c["ogx_name"]
    obj = get("ogxservers", name, ns, optional=True)
    if not obj:
        raise ValueError(
            f"目标项目 {ns} 中没有 OGXServer/{name}。请先确认 oc 当前集群，并在该项目的 Playground 完成 Configure；"
            f"使用 oc get ogxservers -n {ns} 核对名称。若名称不同，修改 settings.json 后重新运行 render.py。"
            "不会自动改用其他项目的 Playground。"
        )
    return obj


def deployment_ready(dep):
    if not dep:
        return False
    desired = dep.get("spec", {}).get("replicas", 1)
    status = dep.get("status", {})
    return desired > 0 and (
        status.get("observedGeneration", 0) >= dep["metadata"].get("generation", 1)
        and all(status.get(key, 0) == desired for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
    )


def wait_playground(c):
    ns, name = c["playground_namespace"], c["ogx_name"]
    require_playground(c)
    print(f"等待 {ns}/{name} 初始化及 Deployment 就绪（最长 600 秒）。", flush=True)

    def ready():
        obj = require_playground(c)
        phase = obj.get("status", {}).get("phase", "Unknown")
        if phase == "Failed":
            raise RuntimeError(f"OGXServer/{name} 处于 Failed；请检查 oc describe ogxservers {name} -n {ns}。")
        return phase == "Ready" and deployment_ready(get("deployment", name, ns, optional=True))

    wait_for(ready, f"Playground {ns}/{name} 已就绪", 600)


def trust(c):
    ns, name = c["playground_namespace"], c["ogx_name"]
    # Check the intended target before creating or changing any resources.
    wait_playground(c)
    apply("40-playground-service-ca.yaml")
    wait_for(lambda: bool(get("configmap", "nemo-service-ca", ns).get("data", {}).get("service-ca.crt")), "Service CA 注入完成", 120)
    obj = get("ogxservers", name, ns)
    private_file("ogx-before-trust.json", json.dumps(obj, ensure_ascii=False, indent=2))
    tls = merged_trust(obj["spec"])
    patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
             {"op": "add", "path": "/spec/tls", "value": tls}]
    oc("patch", "ogxservers", name, "-n", ns, "--type=json", "-p", json.dumps(patch))
    print("已保留现有 CA 引用并添加 NeMo Service CA。", flush=True)
    host = c["nemo_name"] + "." + c["nemo_namespace"] + ".svc.cluster.local"
    code = "import socket,ssl,sys; s=socket.create_connection((sys.argv[1],443),timeout=10); t=ssl.create_default_context().wrap_socket(s,server_hostname=sys.argv[1]); print('TLS_OK'); t.close()"

    def trusted():
        try:
            return "TLS_OK" in oc("exec", "-n", ns, "deployment/" + name, "--", "python", "-c", code, host)
        except RuntimeError:
            return False
    wait_for(trusted, "Playground 到 NeMo 的 TLS 验证通过", 300)


def verify_service(c):
    source = (ROOT / "scripts/probe.py").read_text(encoding="utf-8")
    ns, name = c["nemo_namespace"], c["nemo_name"]
    payload = {"model": c["model_id"], "base_url": "http://127.0.0.1:8000/v1",
               "response_language": c.get("response_language", "zh-CN")}
    print("验证 NeMo 服务内部规则与上游模型。", flush=True)
    print(oc("exec", "-i", "-n", ns, "deployment/" + name, "-c", "nemo-guardrails", "--", "python", "-c", source,
             data=json.dumps(payload), timeout=PROBE_TIMEOUT, safe_stdout=True), flush=True)


def verify(c, token_file=None):
    # Never issue a token for a missing or still-initializing Playground.
    wait_playground(c)
    source = (ROOT / "scripts/probe.py").read_text(encoding="utf-8")
    ns, name = c["nemo_namespace"], c["nemo_name"]
    payload = {"model": c["model_id"], "response_language": c.get("response_language", "zh-CN")}
    payload["base_url"] = "https://" + name + "." + ns + ".svc.cluster.local/v1"
    if token_file is None:
        payload["token"] = oc("create", "token", "nemo-playground-client", "-n", ns, "--duration=15m")
        print("本次使用另行签发的 15 分钟临时 token。", flush=True)
    else:
        path = Path(token_file)
        if not path.is_file():
            raise ValueError(f"找不到待验证的 token 文件：{path}。请先运行 token 命令生成。")
        value = path.read_text(encoding="utf-8").strip()
        if not value or any(char.isspace() for char in value) or value.startswith("Bearer "):
            raise ValueError("token 文件必须只包含一个原始 token，不带 Bearer 前缀。")
        payload["token"] = value
        print("本次使用指定文件中的 token，不签发替代 token。", flush=True)
    print("验证 Playground 到 NeMo 的 HTTPS、RBAC 和规则返回。", flush=True)
    print(oc("exec", "-i", "-n", c["playground_namespace"], "deployment/" + c["ogx_name"], "--", "python", "-c", source,
             data=json.dumps(payload), timeout=PROBE_TIMEOUT, safe_stdout=True), flush=True)
    print("此验证不读取 Dashboard 保存的端点凭据；最后仍需在 Playground 新聊天中验收。", flush=True)


def inventory(c):
    output = {"captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pods": []}
    for ns in dict.fromkeys(["redhat-ods-operator", "redhat-ods-applications", c["nemo_namespace"], c["playground_namespace"]]):
        pods = get("pods", namespace=ns)
        for pod in pods.get("items", []):
            spec, status = pod["spec"], pod.get("status", {})
            ids = {x["name"]: x.get("imageID", "") for x in status.get("containerStatuses", []) + status.get("initContainerStatuses", [])}
            output["pods"].append({"namespace": ns, "pod": pod["metadata"]["name"], "images": [
                {"container": x["name"], "image": x["image"], "imageID": ids.get(x["name"], "")}
                for x in spec.get("initContainers", []) + spec.get("containers", [])]})
    path = ROOT / "rendered/image-inventory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("镜像与运行摘要已写入 " + str(path))
    print("这是已部署 Pod 清单，不代替产品完整离线镜像清单。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("preflight", "install", "token", "wait-playground", "trust", "verify-service", "verify", "inventory"))
    parser.add_argument("--maas-key-file", type=Path)
    parser.add_argument("--duration", default="720h", help="NeMo 端点 token 的申请有效期，默认 720h（30 天）；实际由集群决定。")
    parser.add_argument("--token-file", type=Path, help="仅用于 verify：验证此文件中的 token；省略时签发 15 分钟临时 token。")
    args = parser.parse_args()
    if args.token_file is not None and args.stage != "verify":
        parser.error("--token-file 只适用于 verify")
    # Only commands applying rendered YAML depend on the render manifest.
    c = checked_settings() if args.stage in ("install", "trust") else settings()
    if args.stage == "install":
        install(c, args.maas_key_file)
    elif args.stage == "token":
        token(c, args.duration)
    elif args.stage == "verify":
        verify(c, args.token_file)
    else:
        globals()[args.stage.replace("-", "_")](c)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(str(exc))
