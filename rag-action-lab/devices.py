"""Read-only device lookup from the upstream fixed fixture.

Source: rh-ai-quickstart/it-self-service-agent @ 7fc221dc7991a3d51432af4202fff6dae94b8d93,
mock-employee-data/src/mock_employee_data/data.py, MOCK_EMPLOYEE_DATA (14 records, unchanged).
Purchase date is not an assignment date; the fixture has no assigned_date field.
"""
import json
from datetime import date
from pathlib import Path

DATA = json.loads((Path(__file__).parent / "upstream_devices.json").read_text(encoding="utf-8"))


def lookup(email: str):
    record = DATA.get(email.lower())
    if record is None:
        return {"status": "no_record"}
    purchase = record.get("purchase_date") or ""
    age = None
    if purchase:
        try:
            born = date.fromisoformat(purchase)
            today = date.today()
            age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        except ValueError:
            age = None
    return {
        "status": "ok",
        "name": record.get("name"),
        "email": record.get("email"),
        "asset_id": record.get("asset_tag"),
        "model": record.get("laptop_model"),
        "serial": record.get("laptop_serial_number"),
        "purchase_date": purchase or None,
        "assigned_date": None,
        "assigned_date_status": "台账未提供",
        "usage_duration_status": "未知",
        "usage_duration_reason": "台账没有领用日期，购买日期不能当作实际使用时长",
        "purchase_age": age,
        "purchase_age_basis": "purchase_date",
        "purchase_age_is_usage_duration": False,
        "warranty_expiry": record.get("warranty_expiry"),
    }
