"""Create the lab credentials once. Re-running refuses to overwrite."""
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIVATE = Path(os.getenv("PRIVATE_DIR", str(ROOT / "private")))

PEOPLE = [
    ("lab-a", "演示员工 A", "alice.johnson@company.com"),
    ("lab-b", "演示员工 B", "john.doe@company.com"),
    ("lab-empty", "无设备记录", "no-device@example.invalid"),
]


def main():
    if PRIVATE.exists() and any(PRIVATE.iterdir()):
        raise SystemExit("private/ already exists; refusing to overwrite")
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    sessions, delivery = [], {}
    for slug, label, identity in PEOPLE:
        token = secrets.token_urlsafe(36)
        delivery[slug] = token
        sessions.append({"session_id": secrets.token_hex(8), "label": label,
                         "identity": identity,
                         "token_sha256": __import__("hashlib").sha256(token.encode()).hexdigest()})
    def write(name, text):
        fd = os.open(PRIVATE / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    write("sessions.json", json.dumps(sessions, ensure_ascii=False, indent=1))
    write("credentials-for-delivery.json", json.dumps(delivery, ensure_ascii=False, indent=1))
    write("action.key", secrets.token_urlsafe(48))
    print("private credentials created for", ", ".join(slug for slug, _, _ in PEOPLE))


if __name__ == "__main__":
    main()
