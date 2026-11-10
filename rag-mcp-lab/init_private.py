"""Generate fresh lab credentials without printing them or reusing old tokens."""
import hashlib
import json
import secrets

import settings
from security import write_private


def main():
    if settings.PRIVATE.exists():
        raise SystemExit("private 目录已存在，未覆盖；轮换时另建私密目录并保留回退记录。")
    settings.PRIVATE.mkdir(mode=0o700, parents=True)
    definitions = [
        ("lab-a", "演示员工 A", "alice.johnson@company.com"),
        ("lab-b", "演示员工 B", "john.doe@company.com"),
        ("lab-empty", "无设备记录测试身份", "no-device@example.invalid"),
    ]
    sessions, delivery = [], {}
    for session_id, label, identity in definitions:
        token = secrets.token_urlsafe(36)
        sessions.append({"session_id": session_id, "label": label, "identity": identity,
                         "token_sha256": hashlib.sha256(token.encode()).hexdigest()})
        delivery[session_id] = token
    write_private(settings.PRIVATE / "sessions.json", json.dumps(sessions, indent=2))
    write_private(settings.PRIVATE / "credentials-for-delivery.json", json.dumps(delivery, indent=2))
    write_private(settings.PRIVATE / "mcp.key", secrets.token_urlsafe(48) + "\n")
    print("已生成新凭据。请从 private/credentials-for-delivery.json 单独分发对应项，不上传、截图或发进聊天。")


if __name__ == "__main__":
    main()
