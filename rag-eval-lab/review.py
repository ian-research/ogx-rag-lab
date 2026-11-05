"""Prepare human review, summarize confirmed fields, and select a six-case recheck.

Only standard-library code. No model calls, keyword-based grades, or combined score.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from evaluate import audit, read_jsonl

FIELDS = {
    'fact_accuracy': {'PASS', 'PARTIAL', 'FAIL', 'REVIEW', 'N/A'},
    'fabrication': {'NONE', 'MINOR', 'MAJOR', 'REVIEW'},
    'refusal_appropriateness': {'APPROPRIATE', 'OVER_CAUTIOUS', 'UNDER_REFUSED', 'N/A'},
    'ref_supports': {'FULL', 'PARTIAL', 'NO', 'N/A'},
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n')


def inputs(run):
    qs = read_jsonl(run / 'questions.jsonl')
    rs = read_jsonl(run / 'responses.jsonl')
    ids = [q['case_id'] for q in qs]
    if not ids or len(ids) != len(set(ids)) or len(rs) != len(ids):
        raise ValueError('Empty, incomplete, or duplicate run')
    if len({r['case_id'] for r in rs}) != len(rs) or set(ids) != {r['case_id'] for r in rs}:
        raise ValueError('Question and response IDs differ')
    hashes = {'questions_sha256': sha(run / 'questions.jsonl'),
              'responses_sha256': sha(run / 'responses.jsonl')}
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['question_sha256'] != hashes['questions_sha256']:
        raise ValueError('Run questions do not match the collector manifest')
    records = {r['case_id']: r for r in rs}
    for q in qs:
        r = records[q['case_id']]
        if r['request'] != {k: q[k] for k in ('question', 'scope', 'as_of', 'policy_id')}:
            raise ValueError(f"Request mismatch: {q['case_id']}")
        # Transport failures remain reviewable; valid JSON objects must be preserved exactly.
        try:
            parsed = json.loads(r['raw_body'])
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed != r['response']:
            raise ValueError(f"Raw response mismatch: {q['case_id']}")
    return qs, records, hashes, manifest


def prepare(run, docs, output):
    qs, records, hashes, manifest = inputs(run)
    document_hashes = manifest['environment']['documents']
    for name, expected in document_hashes.items():
        if Path(name).name != name or sha(docs / name) != expected:
            raise ValueError(f'Document mismatch: {name}')
    output.mkdir(parents=True, exist_ok=False)
    # Link to source documents using paths relative to the generated review packet.
    import os
    relative_docs = Path(os.path.relpath(docs.resolve(), output.resolve())).as_posix()
    markdown = ['# 逐题评审材料', '', '只评这轮已经保存的响应，不重新提问替换个别答案。',
                '请在 review.json 填写评分、逐事实依据、理由、评审人和时间；规则见仓库 RUBRIC.md。',
                'PENDING/null 表示待评，N/A 表示不适用；程序检查不等于语义评分。', '',
                '## 完整实验原文', '']
    markdown += [f'- [{name}]({relative_docs}/{name})' for name in document_hashes]
    reviews, checks = [], []
    for q in qs:
        r = records[q['case_id']]
        response = r['response']
        boundary = q['expected_status'] == 'no_data'
        insufficient = q['category'] == 'insufficient'
        check = audit(q, r)
        checks.append(check)
        scores = dict.fromkeys(FIELDS, 'PENDING')
        if boundary:
            scores.update(fact_accuracy='N/A', refusal_appropriateness='N/A', ref_supports='N/A')
        elif insufficient:
            scores['fact_accuracy'] = 'N/A'
        else:
            scores['refusal_appropriateness'] = 'N/A'
        reviews.append({'case_id': q['case_id'], **scores,
                        'boundary_message_accuracy': 'PENDING' if boundary else 'N/A',
                        'evidence': [{'fact': f['text'], 'covered': None, 'source_numbers': []}
                                     for f in q['required_facts']],
                        'relevant_source_numbers': None if q['required_facts'] else [],
                        'reviewer': '', 'reviewed_at': '', 'notes': ''})
        markdown += ['', f"## {q['case_id']} · {q['question']}", '',
                     f"文档：{q['policy_id']}；范围：{q['scope']}；日期：{q['as_of']}。",
                     f"预期状态：{q['expected_status']}；预期版本：{q.get('expected_version_label')}。",
                     '', '### 评分参照（不是模型输出）', '']
        markdown += [f"- F{i}（{'关键' if f['key'] else '次要'}）：{f['text']}"
                     for i, f in enumerate(q['required_facts'], 1)] or ['本题没有正向必需事实。']
        markdown += ['', '### 实际模型回答或系统提示', '', response.get('answer', '(无答案)'),
                     '', f"HTTP：{r['http']}；状态：{response.get('status')}；请求错误：{r.get('transport_error')}。",
                     '', '### 本次完整来源', '']
        for source in response.get('sources', []):
            name = source.get('source', source.get('filename'))
            if document_hashes.get(name) != source.get('file_sha256'):
                raise ValueError(f"Source hash mismatch: {q['case_id']}/{name}")
            markdown += [f"#### [{source['number']}] {name}", '', source['text'], '',
                         f"片段：{source.get('chunk_id')}；版本：{source.get('version_id')}。", '']
        if not response.get('sources'):
            markdown.append('本次无来源片段。')
    save(output / 'review.json', {**hashes, 'review_mode': 'human_review', 'cases': reviews})
    save(output / 'automatic-checks.json', {**hashes, 'cases': checks})
    (output / 'review.md').write_text('\n'.join(markdown) + '\n', encoding='utf-8')
    return {'cases': len(reviews), 'output': str(output)}


def validate_review(qs, records, hashes, review):
    if any(review.get(k) != v for k, v in hashes.items()):
        raise ValueError('Review belongs to another question/response file')
    cases = review['cases']
    if len(cases) != len(qs) or {c['case_id'] for c in cases} != {q['case_id'] for q in qs}:
        raise ValueError('Review case IDs are incomplete or duplicated')
    by_id = {c['case_id']: c for c in cases}
    for q in qs:
        c = by_id[q['case_id']]
        for field, allowed in FIELDS.items():
            if c[field] not in allowed:
                raise ValueError(f"{q['case_id']}: unfinished/invalid {field}")
        if not all(isinstance(c.get(k), str) and c[k].strip() for k in ('reviewer', 'reviewed_at', 'notes')):
            raise ValueError('Each review needs reviewer, time and notes')
        boundary = q['expected_status'] == 'no_data'
        insufficient = q['category'] == 'insufficient'
        if (boundary or insufficient) != (c['fact_accuracy'] == 'N/A'):
            raise ValueError('fact_accuracy applicability mismatch')
        if insufficient == (c['refusal_appropriateness'] == 'N/A'):
            raise ValueError('refusal applicability mismatch')
        if boundary and (c['ref_supports'] != 'N/A' or c['boundary_message_accuracy'] not in ('准确', '不准确', '待复核')):
            raise ValueError('Boundary cases require a separate message check')
        if not boundary and c['boundary_message_accuracy'] != 'N/A':
            raise ValueError('Unexpected boundary-message grade')
        facts = q['required_facts']
        if len(c['evidence']) != len(facts):
            raise ValueError('Evidence denominator must match required facts')
        available = {s['number'] for s in records[q['case_id']]['response'].get('sources', [])[:3]}
        for fact, entry in zip(facts, c['evidence']):
            if entry['fact'] != fact['text'] or type(entry['covered']) is not bool:
                raise ValueError('Unfinished or misaligned fact review')
            nums = entry['source_numbers']
            if any(type(n) is not int or n not in available for n in nums):
                raise ValueError('Evidence must refer to returned top-three sources')
            if entry['covered'] and not nums:
                raise ValueError('A covered fact requires source numbers')
        relevant = c['relevant_source_numbers']
        if not isinstance(relevant, list) or any(type(n) is not int or n not in available for n in relevant):
            raise ValueError('Review relevant sources explicitly; use [] for none')
        if any(e['covered'] and not set(e['source_numbers']) <= set(relevant) for e in c['evidence']):
            raise ValueError('Supporting sources must also be marked relevant')
    return by_id


def summarize(run, review_path):
    qs, records, hashes, _ = inputs(run)
    review = json.loads(review_path.read_text(encoding='utf-8'))
    scores = validate_review(qs, records, hashes, review)
    groups = defaultdict(list)
    coverage = Counter()
    covered = total = 0
    for q in qs:
        c = scores[q['case_id']]
        groups['boundary_no_data' if q['expected_status'] == 'no_data' else
               'insufficient' if q['category'] == 'insufficient' else 'answerable'].append(c)
        if c['evidence']:
            n = sum(e['covered'] for e in c['evidence'])
            coverage['full' if n == len(c['evidence']) else 'partial' if n else 'zero'] += 1
            covered += n
            total += len(c['evidence'])
    paired = defaultdict(list)
    for q in qs:
        if q.get('pair_id'):
            paired[q['pair_id']].append(scores[q['case_id']])
    differences = {}
    for key, rows in paired.items():
        if len(rows) == 2:
            differences[key] = [f for f in FIELDS if rows[0][f] != rows[1][f]]
    return {**hashes, 'review_sha256': sha(review_path), 'kind': 'separate_dimensions_no_total_score',
            'groups': {k: {'count': len(rows), **{f: dict(Counter(r[f] for r in rows)) for f in FIELDS}}
                       for k, rows in groups.items()},
            'fact_coverage': {'cases': dict(coverage), 'covered_facts': covered, 'required_facts': total},
            'boundary_messages': dict(Counter(c['boundary_message_accuracy'] for c in groups['boundary_no_data'])),
            'paired_field_differences': differences}


def select(run, run_id=None):
    qs, records, hashes, _ = inputs(run)
    run_id = run_id or run.name
    seed_input = '\n'.join(['scheme-b-six-review-v1', run_id, hashes['questions_sha256'], hashes['responses_sha256']])
    seed = hashlib.sha256(seed_input.encode()).hexdigest()
    pairs = defaultdict(list)
    for q in qs:
        if q['category'] == 'paired_knowledge':
            pairs[q['pair_id']].append(q['case_id'])
    if any(len(v) != 2 for v in pairs.values()):
        raise ValueError('Each basic pair must contain two cases')
    pools = {'paired_knowledge': sorted(pairs)}
    for category in ('cross_chunk', 'insufficient', 'boundary'):
        pools[category] = sorted(q['case_id'] for q in qs if q['category'] == category)
    quotas = {'paired_knowledge': 1, 'cross_chunk': 2, 'insufficient': 1, 'boundary': 1}
    ranking, selected = {}, []
    for category, pool in pools.items():
        if len(pool) < quotas[category]:
            raise ValueError(f'Not enough cases in {category}')
        ranking[category] = sorted([{'unit': unit, 'digest': hashlib.sha256(f'{seed}|{category}|{unit}'.encode()).hexdigest()}
                                    for unit in pool], key=lambda row: (row['digest'], row['unit']))
        for row in ranking[category][:quotas[category]]:
            selected.extend(sorted(pairs[row['unit']]) if category == 'paired_knowledge' else [row['unit']])
    return {**hashes, 'protocol': 'scheme-b-six-review-v1', 'run_id': run_id, 'seed': seed,
            'seed_input': seed_input, 'quotas': quotas, 'ranked_candidates': ranking,
            'selected_case_ids': selected,
            'limitation': 'Selection is deterministic, not semantic grading. Reproducibility does not establish independent or blind review.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--run', type=Path, required=True)
    prep.add_argument('--docs', type=Path, default=Path('dataset/docs'))
    prep.add_argument('--output-dir', type=Path, required=True)
    summ = sub.add_parser('summary')
    summ.add_argument('--run', type=Path, required=True)
    summ.add_argument('--review', type=Path, required=True)
    summ.add_argument('--output', type=Path, required=True)
    sample = sub.add_parser('sample')
    sample.add_argument('--run', type=Path, required=True)
    sample.add_argument('--run-id', help='Original run ID if the example directory has been renamed')
    sample.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.command == 'prepare':
        result = prepare(a.run, a.docs, a.output_dir)
    else:
        result = summarize(a.run, a.review) if a.command == 'summary' else select(a.run, a.run_id)
        save(a.output, result)
    print(json.dumps(result if a.command != 'summary' else result['groups'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
