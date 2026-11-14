"""Installer behaviour with stubbed systemctl/podman: refusal, late failure, no collateral stops."""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _stub(bindir: pathlib.Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _env(tmp: pathlib.Path, *, api_enable_fails: bool = False) -> dict:
    bindir = tmp / "bin"
    bindir.mkdir()
    calls = tmp / "calls.log"
    _stub(bindir, "systemctl", f'echo "$@" >> {calls}\n'
           + ('if [ "$1" = "enable" ] && [[ "$*" == *article07-api* ]]; then exit 23; fi\n' if api_enable_fails else "")
           + "exit 0\n")
    _stub(bindir, "podman", "exit 0\n")
    _stub(bindir, "python3.12", "exit 0\n")
    _stub(bindir, "curl", 'case "$*" in *api/health*) echo \'{"status":"ok"}\';; *api/evidence*) printf 401;; *) :;; esac\nexit 0\n')
    _stub(bindir, "ss", "exit 0\n")
    _stub(bindir, "getenforce", "echo Disabled\n")
    app = tmp / "app"
    (app / ".venv" / "bin").mkdir(parents=True)
    py = app / ".venv" / "bin" / "python"
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    (app / "requirements.lock").write_text("x\n")
    (app / "deploy").mkdir()
    for name in ("runtime07.env.in", "article07-api.service.in", "article07-prometheus.service.in", "prometheus.yml.in"):
        (app / "deploy" / name).write_text("STATE_DIR=@STATE_DIR@\nAPI_PORT=@API_PORT@\n")
    (app / "deploy" / "article07-api.service.in").write_text("ExecStart=@APP_DIR@ @API_PORT@ @CONFIG_PATH@\n")
    (app / "deploy" / "article07-prometheus.service.in").write_text("ExecStart=@PROM_IMAGE@ @PROM_PORT@ @STATE_DIR@\n")
    (app / "deploy" / "prometheus.yml.in").write_text("port: @METRICS_PORT@\n")
    state = tmp / "state"
    (state / "private").mkdir(parents=True)
    (state / "private" / "admins.json").write_text("{}")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env.update({
        "ALLOW_NONROOT": "1",
        "SKIP_PREFLIGHT": "1",
        "APP_DIR": str(app),
        "STATE_DIR": str(state),
        "CONFIG_PATH": str(tmp / "etc" / "runtime.env"),
        "UNIT_DIR": str(tmp / "units"),
        "PROM_URL": "http://127.0.0.1:9090",
    })
    (tmp / "units").mkdir()
    return env


@pytest.mark.skipif(not pathlib.Path("/bin/bash").exists(), reason="bash required")
def test_refuses_existing_unit_without_replace(tmp_path):
    env = _env(tmp_path)
    env["SKIP_PREFLIGHT"] = "0"
    unit = pathlib.Path(env["UNIT_DIR"]) / "article07-api.service"
    unit.write_text("old\n")
    proc = subprocess.run(["bash", str(ROOT / "scripts/install07.sh")], env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "already exists" in (proc.stdout + proc.stderr)
    assert unit.read_text() == "old\n"
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert "stop article07" not in calls


@pytest.mark.skipif(not pathlib.Path("/bin/bash").exists(), reason="bash required")
def test_late_failure_reports_error_and_cleans_created_files(tmp_path):
    env = _env(tmp_path, api_enable_fails=True)
    proc = subprocess.run(["bash", str(ROOT / "scripts/install07.sh")], env=env, capture_output=True, text=True)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert not (pathlib.Path(env["CONFIG_PATH"])).exists()
    assert not (pathlib.Path(env["UNIT_DIR"]) / "article07-api.service").exists()
    assert not (pathlib.Path(env["UNIT_DIR"]) / "article07-prometheus.service").exists()


@pytest.mark.skipif(not pathlib.Path("/bin/bash").exists(), reason="bash required")
def test_preflight_failure_writes_nothing(tmp_path):
    env = _env(tmp_path)
    env["SKIP_PREFLIGHT"] = "0"
    env["STATE_DIR"] = str(tmp_path / "missing-state")
    proc = subprocess.run(["bash", str(ROOT / "scripts/install07.sh")], env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "state dir missing" in (proc.stdout + proc.stderr)
    assert not (tmp_path / "etc" / "runtime.env").exists()
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert "stop" not in calls


def test_replace_failure_restores_prometheus_config(tmp_path):
    """E: a failed replace must put the previous prometheus.yml back byte for byte."""
    env = _env(tmp_path, api_enable_fails=True)
    units = pathlib.Path(env["UNIT_DIR"])
    for name in ("article07-api.service", "article07-prometheus.service"):
        (units / name).write_text(f"# old {name}\n")
    state = pathlib.Path(env["STATE_DIR"])
    (state / "prometheus-config").mkdir()
    sentinel = "old-config: keep-me\n"
    (state / "prometheus-config" / "prometheus.yml").write_text(sentinel)
    env["ARTICLE07_REPLACE"] = "1"
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "install07.sh")],
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0
    assert (state / "prometheus-config" / "prometheus.yml").read_text() == sentinel
    assert (units / "article07-api.service").read_text() == "# old article07-api.service\n"
