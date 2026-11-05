"""Audit saved results without calling the server or pretending keywords are facts."""
import argparse
import hashlib
import json
import re
from pathlib import Path


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def citation_check(answer, sources, required):
    # Supported form: [1], [1][2], [1, 2], [1，2]. Other forms need review.
    groups = re.findall(r"\[(\d+(?:\s*[,，]\s*\d+)*)\]", answer)
    cited = [int(n) for group in groups for n in re.findall(r"\d+", group)]
    valid = {s.get("number", s.get("n")) for s in sources}
    if not cited:
        return ("FAIL" if required else "N/A"), cited
    return ("PASS" if all(n in valid for n in cited) else "FAIL"), cited


def audit(question, record, annotation=None):
    annotation = annotation or {}
    response = record.get("response", record)
    http = record.get("http")
    status = response.get("status", response.get("actual_status"))
    version = response.get("version") or {}
    label = version.get("version_label", response.get("actual_version"))
    sources = response.get("sources") or []
    answer = response.get("answer") or ""
    fact_count = len(question.get("required_facts", []))
    verified = annotation.get("facts_covered")
    # Every entry must be human-confirmed against top 3. Never infer from evidence_keys.
    if verified is not None:
        if len(verified) != fact_count or any(type(v) is not bool for v in verified):
            raise ValueError(f"{question['case_id']}: facts_covered must contain {fact_count} booleans")
        if not annotation.get("reviewer") or not annotation.get("notes"):
            raise ValueError("Fact annotations require reviewer and notes")
    coverage = "N/A" if not fact_count else (
        sum(verified) / fact_count if verified is not None else "REVIEW")
    relevant = annotation.get("relevant_source_numbers")
    if relevant is not None:
        if not annotation.get("reviewer") or not annotation.get("notes"):
            raise ValueError("Evidence annotations require reviewer and notes")
        available = {s.get("number", s.get("n")) for s in sources[:3]}
        if any(n not in available for n in relevant):
            raise ValueError("Evidence annotation must refer to the returned top 3")
    hit = "N/A" if not fact_count else (
        ("PASS" if relevant else "FAIL") if relevant is not None else "REVIEW")
    ref, cited = citation_check(answer, sources, question["expected_citation"])
    expected_version = question.get("expected_version_label")
    version_match = "N/A"
    if question["expected_status"] == "ok":
        version_match = "PASS" if http == 200 and status == "ok" and label == expected_version else "FAIL"
    expected_id = question.get("expected_version_id")
    identity = "NOT_CHECKED"
    if expected_id:
        identity = "PASS" if version.get("version_id") == expected_id else "FAIL"
    return {
        "case_id": question["case_id"],
        "dataset_review_status": question.get("dataset_review_status", "PROVISIONAL"),
        "dataset_note": question.get("review_note", ""),
        "status_match": "PASS" if http == 200 and status == question["expected_status"] else "FAIL",
        "version_label_match": version_match, "version_identity_match": identity,
        "hit3": hit, "coverage3": coverage, "fact_count": fact_count,
        "ref_exists": ref, "cited_numbers": cited,
        "fact_accuracy": "PENDING", "fabrication": "PENDING",
        "refusal_appropriateness": "PENDING", "ref_supports": "PENDING",
        "input_is_legacy_summary": "response" not in record,
        "notes": annotation.get("notes", "No semantic score inferred from keywords."),
        "reviewer": annotation.get("reviewer"),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--questions", required=True, type=Path)
    p.add_argument("--results", required=True, type=Path)
    p.add_argument("--annotations", type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    qs, rs = read_jsonl(args.questions), read_jsonl(args.results)
    if len({q['case_id'] for q in qs}) != len(qs) or len({r['case_id'] for r in rs}) != len(rs):
        raise ValueError("Duplicate case IDs")
    records = {r['case_id']: r for r in rs}
    if set(records) != {q['case_id'] for q in qs}:
        raise ValueError("Question/result case sets differ")
    annotations = json.loads(args.annotations.read_text()) if args.annotations else {}
    result_hash = hashlib.sha256(args.results.read_bytes()).hexdigest()
    if args.annotations and annotations.get('__results_sha256__') != result_hash:
        raise ValueError('Annotations belong to another response file; re-review the new responses')
    result = [audit(q, records[q['case_id']], annotations.get(q['case_id'])) for q in qs]
    with args.output.open('x') as f:
        json.dump({"questions_sha256": hashlib.sha256(args.questions.read_bytes()).hexdigest(),
                   "results_sha256": result_hash,
                   "kind": "offline_audit_not_new_inference", "cases": result}, f, ensure_ascii=False, indent=2)
    print(f"Audited {len(result)} saved requests; semantic fields remain pending human review.")


if __name__ == '__main__':
    main()
