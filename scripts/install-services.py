"""Install local systemd definitions; never start services or overwrite secrets."""
import argparse
import datetime
import getpass
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path('/root/rag-lab')
REPO = ROOT / 'ogx-rag-lab'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace', action='store_true', help='back up and replace existing differing units')
    args = parser.parse_args()
    if os.geteuid() != 0 or Path(__file__).resolve().parent.parent != REPO:
        raise SystemExit('请在服务器 /root/rag-lab/ogx-rag-lab 中以 root 执行。')
    required = [ROOT / p for p in (
        '.venv-vllm/bin/vllm', '.venv-ogx/bin/ogx', '.venv-ingest/bin/python',
        'ogx/config.yaml', 'models/Qwen3.5-9B/config.json',
        'models/bge-small-zh-v1.5/config.json', 'rag-upload-lab/app.py',
        'rag-upload-lab/worker.py')]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise SystemExit('请先完成安装，缺少：\n' + '\n'.join(missing))
    subprocess.run(['podman', 'container', 'exists', 'rag-postgres'], check=True)
    system = Path('/etc/systemd/system')
    source = REPO / 'deploy/systemd'
    pairs = [(p, system / p.name) for p in source.glob('*.service')]
    pairs += [(source / 'rag-stack.target', system / 'rag-stack.target')]
    for name in ('rag-upload-api', 'rag-upload-worker'):
        pairs += [(REPO / 'rag-upload-lab/deploy' / f'{name}.service', system / f'{name}.service'),
                  (source / 'app-dependencies.conf', system / f'{name}.service.d/20-rag-dependencies.conf')]
    pairs += [(source / 'api-readiness.conf', system / 'rag-upload-api.service.d/30-rag-readiness.conf')]
    changed = [dst for src, dst in pairs if dst.exists() and dst.read_bytes() != src.read_bytes()]
    if changed and not args.replace:
        raise SystemExit('已有不同配置，请先比较；确认迁移后用 --replace（自动备份）：\n' + '\n'.join(map(str, changed)))
    config = Path('/etc/rag-lab')
    config.mkdir(mode=0o700, exist_ok=True)
    secret = config / 'ogx.env'
    if not secret.exists():
        password = getpass.getpass('请输入已有 rag_app 数据库密码（不会修改数据库密码）：')
        if not password or '\n' in password or '\r' in password or '\x00' in password:
            raise SystemExit('密码不能为空或含换行/NUL。')
        if password != getpass.getpass('再次输入：'):
            raise SystemExit('两次输入不一致，未保存。')
        # systemd EnvironmentFile quoting, not shell quoting. No variable expansion.
        escaped = password.replace('\\', '\\\\').replace('"', '\\"')
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(f'PGVECTOR_PASSWORD="{escaped}"\n')
    if secret.stat().st_uid != 0 or secret.stat().st_mode & 0o077:
        raise SystemExit('/etc/rag-lab/ogx.env 应归 root 所有且权限为 600，请先修正。')
    env = config / 'vllm.env'
    if not env.exists():
        shutil.copyfile(REPO / 'configs/vllm.env.example', env)
        env.chmod(0o600)
    backup = ROOT / 'service-backups' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for src, dst in pairs:
        if dst in changed:
            saved = backup / dst.relative_to(system)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, saved)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        dst.chmod(0o644)
    subprocess.run(['systemd-analyze', 'verify', str(system / 'rag-stack.target')], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print('配置已安装，未启动或重启服务。现有其他 drop-in 保留，请用 systemctl cat 核对合并结果。')
    if changed:
        print(f'原配置备份：{backup}')
    print('确认无手动启动的进程占用端口后执行：systemctl enable --now rag-stack.target')


if __name__ == '__main__':
    main()
