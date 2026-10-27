"""为已有 Word 文档创建新的后台处理任务；成功前保留旧版本可查询。"""
import argparse
import json
import time
import uuid
from pathlib import Path

import state


def enqueue(document_id):
    state.init()
    with state.db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        old = connection.execute("SELECT * FROM jobs WHERE id=?", (document_id,)).fetchone()
        if old is None:
            raise ValueError("文档 ID 不存在")
        if old["status"] != "ready":
            raise ValueError("此命令只用于已经成功入库的文档")
        if Path(old["filename"]).suffix.lower() not in {".docx", ".doc"}:
            raise ValueError("本次重建仅适用于 DOCX/DOC")
        if not Path(old["path"]).is_file():
            raise ValueError("原始文件已不存在")
        pending = connection.execute(
            "SELECT id FROM jobs WHERE filename=? AND sha256=? AND status IN ('queued','parsing','chunking','embedding')",
            (old["filename"], old["sha256"])).fetchone()
        if pending:
            return {"id": pending["id"], "message": "该文件已有待处理任务，复用现有任务"}
        newest = connection.execute(
            "SELECT id FROM jobs WHERE filename=? AND status='ready' ORDER BY created DESC LIMIT 1",
            (old["filename"],)).fetchone()
        if newest["id"] != document_id:
            raise ValueError("存在更新的成功版本，请使用该版本的文档 ID，以免恢复过期内容")
        new_id = str(uuid.uuid4())
        now = time.time()
        # 原文件只读复用；新任务有独立 artifacts 目录和向量库，不复用旧分块。
        connection.execute(
            "INSERT INTO jobs(id,filename,sha256,path,created,updated) VALUES(?,?,?,?,?,?)",
            (new_id, old["filename"], old["sha256"], old["path"], now, now))
    return {"id": new_id, "previous_id": document_id, "filename": old["filename"],
            "message": "新任务已排队；成功后默认问答切换到同名新版本"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document_id")
    args = parser.parse_args()
    print(json.dumps(enqueue(args.document_id), ensure_ascii=False, indent=2))
