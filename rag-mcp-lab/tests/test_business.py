from datetime import date
import business_query as bq


def test_device_identity_and_no_record():
    a = bq.get_laptop_info('alice.johnson@company.com', date(2026, 11, 9))
    b = bq.get_laptop_info('john.doe@company.com', date(2026, 11, 9))
    assert a['serial_number'] != b['serial_number']
    assert a['assigned_date'] is None
    assert a['age_basis'] == 'purchase_date_not_assigned_date'
    assert bq.get_laptop_info('empty@example.invalid')['status'] == 'not_found'


def test_ticket_ownership_and_time_scope():
    from business_data import TICKETS
    rec = next(iter(TICKETS.values()))
    result = bq.get_ticket(rec['ticket_id'], rec['owner_email'])
    assert result['status'] == 'ok' and result['expected_completion'] is None
    assert result['temporal_scope'] == 'current_fixture_not_historical_snapshot'
    assert bq.get_ticket(rec['ticket_id'], 'john.doe@company.com' if rec['owner_email'] != 'john.doe@company.com' else 'alice.johnson@company.com')['status'] == 'forbidden'


def test_missing_and_future_purchase_do_not_create_duration():
    assert bq._age_text(None, date(2026, 1, 1)) is None
    assert bq._age_text(date(2027, 1, 1), date(2026, 1, 1)) is None
