#!/usr/bin/env python3
"""Offline checks of saved application responses; never contacts a service.

This cannot prove a missing database-count observation or an unrecorded HTTP
response. It checks the snapshots and exact response fields that were saved.
"""
import argparse,hashlib,json
from pathlib import Path

DOC_SNAPSHOT_FIELDS={'status','policy_id','filename','version_label',
                     'valid_from','valid_to','heading','excerpt_sha256'}

def audit(data):
    log=data['http_log']
    def entry(kind):
        found=[row for row in log if row.get('kind')==kind]
        if len(found)!=1:raise ValueError(f'Need exactly one saved {kind} response')
        return found[0]
    ask=entry('ask')['response'];fresh=entry('revise')['response']['draft']
    ticket=entry('ticket')['response']['ticket'];payload=ticket['payload']
    canonical_text=payload['content_canonical'];canonical=json.loads(canonical_text)
    submit=entry('submit')['response'];duplicate=entry('submit-duplicate')['response']
    checks={}
    checks['required_responses_ok']=all(entry(kind)['status']==200 for kind in (
        'ask','revise','ticket','submit','submit-duplicate'))
    checks['v2_is_newer']=fresh['version']>ask['draft']['version']
    checks['old_confirmation_rejected']=entry('submit-old')['status']==409
    checks['ticket_pending_review']=ticket['status']==payload['ticket_status']=='pending_review'
    checks['same_ticket_on_repeat']=bool(submit.get('ticket_no')) and submit['ticket_no']==duplicate.get('ticket_no')==ticket['ticket_no']
    checks['canonical_hash']=hashlib.sha256(canonical_text.encode()).hexdigest()==payload['content_hash']==fresh['content_hash']
    checks['same_draft_version']=canonical['draft_id']==fresh['draft_id']==payload['draft_ref'] and canonical['version']==fresh['version']==payload['version']
    checks['same_owner']=bool(canonical['user_id']) and canonical['user_id']==payload['submitted_by']
    checks['same_asset']=all(canonical['asset'].get(k)==v for k,v in fresh['asset'].items()) and payload['asset_id']==ticket['asset_id']==fresh['asset']['asset_id']
    checks['same_reason']=canonical['reason']==fresh['reason']==payload['reason_text']==ticket['reason']
    checks['same_facts']=canonical['facts']==fresh['facts']
    checks['same_missing']=canonical['missing']==fresh['missing'] and payload['declared_missing']==[x['item'] for x in fresh['missing']]
    checks['same_basis']=canonical['basis']==fresh['basis']
    snapshot=payload.get('doc_snapshot')
    checks['same_document_snapshot']=(isinstance(snapshot,dict)
        and set(snapshot)==DOC_SNAPSHOT_FIELDS
        and all(snapshot[k]==fresh['basis'].get(k) for k in DOC_SNAPSHOT_FIELDS))
    return {'checks':checks,'all_snapshot_checks_pass':all(checks.values()),
            'ticket_no':ticket['ticket_no'],'draft_id':fresh['draft_id'],
            'declared_missing':payload['declared_missing'],
            'basis_status':fresh['basis']['status'],
            'scope':'saved responses only; not a database-count or authorization test'}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('record',type=Path);ap.add_argument('--out',type=Path,required=True)
    a=ap.parse_args()
    if a.out.exists():raise SystemExit('Refusing to overwrite audit output')
    raw=a.record.read_bytes();result=audit(json.loads(raw))
    result['input_file_sha256']=hashlib.sha256(raw).hexdigest()
    # Exclusive creation also protects against a second process using this path.
    with a.out.open('x',encoding='utf-8') as handle:
        handle.write(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['all_snapshot_checks_pass'] else 1)
if __name__=='__main__':main()
