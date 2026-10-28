"""Read-only validation of existing version metadata before deploying fixes."""
import sqlite3
import sys
from pathlib import Path
import version_rules as rules


def check(path):
    if not path.exists():
        print('新数据目录，无需检查历史记录')
        return
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        versions = [dict(row) for row in conn.execute('SELECT * FROM versions')]
    errors, published = [], []
    for version in versions:
        try:
            start = rules.normalize_date(version['valid_from'], 'valid_from')
            end = rules.normalize_date(version['valid_to'], 'valid_to')
            if not start or (end and end <= start):
                raise ValueError('无效有效区间')
            if version['publication_status'] == 'published':
                if version['job_status'] != 'ready' or not version['store_id']:
                    raise ValueError('已发布但未入库就绪')
                published.append(version)
        except (rules.RuleError, ValueError) as error:
            errors.append(f"{version['version_id']}: {error}")
    for i, a in enumerate(published):
        for b in published[i + 1:]:
            if (a['policy_id'], a['scope']) == (b['policy_id'], b['scope']) and rules.overlaps(a['valid_from'], a['valid_to'], b['valid_from'], b['valid_to']):
                errors.append(f"有效期重叠: {a['version_id']} / {b['version_id']}")
    if errors:
        raise SystemExit('\n'.join(errors) + '\n请先核对历史元数据，检查器不会自动修改。')
    print(f'历史记录检查通过: {len(versions)} 个版本')


if __name__ == '__main__':
    check(Path(sys.argv[1] if len(sys.argv) > 1 else '/root/rag-lab/rag-version-lab/data/version.db'))
