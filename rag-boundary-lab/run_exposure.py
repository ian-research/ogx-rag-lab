#!/usr/bin/env python3
"""Eight single-continuation cases under the unchanged product tool contract.

Assistant/tool prelude messages are synthetic fixtures, not model output or real
retrieval. The one subsequent OGX completion is real. Tool proposals are recorded
but NEVER executed. Byte payloads are persisted before sending; output is append
only per run directory. No ticket endpoint or credential file is accessed.
"""
from __future__ import annotations
import argparse, ast, copy, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
import httpx

BASELINE_SHA = 'c761a15f3703a995f2b255e4426eab6d388728f168c6f3e7c59cc776025f18ba'
IDS = ('P2-normal','P2-attack','P3-normal','P3-attack',
       'P4-normal','P4-attack','P5-normal','P5-attack')

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def encoded(value) -> bytes:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()

def contract(source: Path):
    raw=(source/'model_loop.py').read_bytes()
    if digest(raw)!=BASELINE_SHA:
        raise ValueError('model_loop.py differs from the reviewed 7ee81f5 contract')
    values={}
    for node in ast.parse(raw).body:
        if isinstance(node,ast.Assign):
            for target in node.targets:
                if isinstance(target,ast.Name) and target.id in ('SYSTEM_PROMPT','TOOLS'):
                    values[target.id]=ast.literal_eval(node.value)
    if set(values)!= {'SYSTEM_PROMPT','TOOLS'}:
        raise ValueError('Cannot read literal system/tools contract')
    return values

def build_request(case, spec, product, model):
    # Two synthetic read-tool calls expose both controls and attacks consistently.
    # Their arguments/results are explicitly recorded as fixture messages.
    calls=[{'id':'fixture_device','type':'function','function':{
                'name':'get_employee_laptop_info','arguments':'{}'}},
           {'id':'fixture_docs','type':'function','function':{
                'name':'search_documents','arguments':json.dumps({
                    'question':'设备换新需要满足什么条件？',
                    'policy_id':'eval3-device'},ensure_ascii=False)}}]
    device=copy.deepcopy(spec['device']);docs=copy.deepcopy(spec['doc'])
    device['email']='alice.johnson@company.com'
    seed=case.get('seed') or {}
    if seed.get('tool')=='get_employee_laptop_info':device=copy.deepcopy(seed['result'])
    if seed.get('tool')=='search_documents':docs=copy.deepcopy(seed['result'])
    device.setdefault('email','alice.johnson@company.com')
    return {'model':model,'messages':[
        {'role':'system','content':product['SYSTEM_PROMPT']},
        {'role':'user','content':case['question']},
        {'role':'assistant','content':'','tool_calls':calls},
        {'role':'tool','tool_call_id':'fixture_device','content':json.dumps(device,ensure_ascii=False)},
        {'role':'tool','tool_call_id':'fixture_docs','content':json.dumps(docs,ensure_ascii=False)}],
        'tools':copy.deepcopy(product['TOOLS']),'tool_choice':'auto',
        'temperature':0.0,'top_p':1.0,'max_tokens':800,'seed':7,
        'chat_template_kwargs':{'enable_thinking':False}}

def run(source, cases_path, out, base='http://127.0.0.1:8321',
        model='local-vllm/rag-llm', remaining=0, transport=None, dry_run=False):
    if remaining<8 and not dry_run:
        raise ValueError('Eight verified remaining requests required; refusing partial collection')
    product=contract(Path(source));spec=json.loads(Path(cases_path).read_text())
    cases={c['id']:c for c in spec['cases']}
    if len(cases)!=len(spec['cases']):raise ValueError('Duplicate case identifier')
    if any(cid not in cases for cid in IDS):raise ValueError('Missing required case')
    # Validate and freeze the entire batch before the first billable request.
    payloads={}
    for cid in IDS:
        case=cases[cid]
        if not isinstance(case.get('question'),str) or not case['question'].strip():
            raise ValueError('Each case requires a nonempty question')
        seed=case.get('seed') or {}
        if seed.get('tool') not in ('get_employee_laptop_info','search_documents') or not isinstance(seed.get('result'),dict):
            raise ValueError('Each case requires a read-tool fixture')
        payloads[cid]=encoded(build_request(case,spec,product,model))
    out=Path(out)
    if out.exists():raise FileExistsError('Use a fresh run directory; evidence is never overwritten')
    out.mkdir(parents=True,mode=0o700)
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),
              'product_model_loop_sha256':BASELINE_SHA,'cases_source_sha256':digest(Path(cases_path).read_bytes()),
              'mode':'controlled_synthetic_read_tool_prelude_single_real_completion',
              'tool_execution':False,'max_model_requests':8,'requests_sent':0,
              'dry_run':dry_run,'collection_ok':None,'cases':[]}
    def checkpoint():
        temporary=out/'.manifest.json.tmp'
        temporary.write_bytes(encoded(manifest))
        temporary.replace(out/'manifest.json')
    checkpoint()
    with httpx.Client(timeout=120,trust_env=False,transport=transport) as client:
        for cid in IDS:
            raw=payloads[cid]
            prefix=out/cid;req_path=prefix.with_suffix('.request.json')
            req_path.write_bytes(raw)
            item={'case_id':cid,'request_sha256':digest(raw),
                  'synthetic_message_indices':[2,3,4],
                  'seed_tool':cases[cid]['seed']['tool'],'state':'request_saved',
                  'content':None,'finish_reason':None,'tool_proposals':[]}
            manifest['cases'].append(item);checkpoint()
            if dry_run:
                item['state']='dry_run_not_sent';checkpoint();continue
            # No retry, no follow-up calls, no tool execution of returned proposals.
            manifest['requests_sent']+=1;item['state']='sending';checkpoint()
            try:
                response=client.post(base.rstrip('/')+'/v1/chat/completions',content=raw,
                                     headers={'Content-Type':'application/json'})
                prefix.with_suffix('.response.bin').write_bytes(response.content)
                item.update(http_status=response.status_code,response_sha256=digest(response.content),state='received')
                try:
                    if response.status_code!=200:
                        item['state']='http_error'
                        continue
                    parsed=response.json();choices=parsed.get('choices')
                    if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict):
                        raise ValueError('Missing choices')
                    choice=choices[0];message=choice.get('message')
                    if not isinstance(message,dict):raise ValueError('Missing message')
                    content=message.get('content');proposals=message.get('tool_calls') or []
                    if content is not None and not isinstance(content,str):raise ValueError('Invalid content')
                    if not isinstance(proposals,list):raise ValueError('Invalid tool calls')
                    item.update(finish_reason=choice.get('finish_reason'),
                                content=content,tool_proposals=proposals)
                    if choice.get('finish_reason') not in ('stop','tool_calls'):
                        item['state']='incomplete_response'
                    elif not ((content or '').strip() or proposals):
                        item['state']='empty_response'
                except (ValueError,AttributeError,IndexError,TypeError):
                    item['state']='invalid_response'
                finally:
                    checkpoint()
            except httpx.HTTPError as exc:
                item.update(state='network_error',error_type=type(exc).__name__)
            checkpoint()
    if not dry_run:
        manifest['collection_ok']=len(manifest['cases'])==8 and all(
            row['state']=='received' for row in manifest['cases'])
    checkpoint()
    return manifest

def exit_code(manifest):
    """Collection status only, never a model-safety or semantic grade."""
    return 0 if manifest['dry_run'] or manifest['collection_ok'] else 2

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--app-source',required=True,type=Path)
    ap.add_argument('--cases',required=True,type=Path)
    ap.add_argument('--out',required=True,type=Path)
    ap.add_argument('--remaining-requests',type=int,required=True)
    ap.add_argument('--ogx-base',default='http://127.0.0.1:8321')
    ap.add_argument('--model',default='local-vllm/rag-llm')
    ap.add_argument('--dry-run',action='store_true')
    a=ap.parse_args()
    result=run(a.app_source,a.cases,a.out,a.ogx_base,a.model,a.remaining_requests,dry_run=a.dry_run)
    print(json.dumps({'requests_sent':result['requests_sent'],'collection_ok':result['collection_ok'],
                      'dry_run':result['dry_run'],'out':str(a.out)},ensure_ascii=False))
    return exit_code(result)
if __name__=='__main__':raise SystemExit(main())
