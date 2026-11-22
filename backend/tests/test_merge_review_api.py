"""归并差异底稿 HTTP 路由端到端冒烟测试。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEMP_DATA = tempfile.mkdtemp(prefix="cc_merge_api_")
os.environ["DATA_ROOT"] = TEMP_DATA

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.data_manager import DATA_ROOT, write_export  # noqa: E402
from app.services.merge_worksheet import PENDING_DROP  # noqa: E402

client = TestClient(app)

COLUMNS = ["id", "客户名称", "手机号", "金额", "城市"]


def _seed_exports():
    shutil.rmtree(DATA_ROOT, ignore_errors=True)
    write_export(1, {"export_id": 1, "task_no": "BATCH-P", "batch_no": "BATCH-P",
                     "file_version": "BATCH-P-v1", "api_name": "客户API",
                     "columns": COLUMNS,
                     "rows": [["K1", "甲", "138", "100", "上海"],
                              ["K1", "甲二", "139", "200", "杭州"]]})
    write_export(2, {"export_id": 2, "task_no": "BATCH-S", "batch_no": "BATCH-S",
                     "file_version": "BATCH-S-v1", "api_name": "客户API",
                     "columns": COLUMNS,
                     "rows": [["K1", "甲", "138", "150", "上海"],
                              ["K1", "甲二", "130", "200", "杭州"]]})


def test_full_http_flow():
    _seed_exports()

    # 选项
    options = client.get("/api/merge-worksheets/meta/options").json()
    assert "主源优先" in options["conflict_policies"]

    # 建底稿
    resp = client.post("/api/merge-worksheets", json={
        "primary_export_id": 1, "secondary_export_id": 2,
        "merge_key": "id", "rule_id": 3})
    assert resp.status_code == 200, resp.text
    worksheet = resp.json()
    wid = worksheet["worksheet_id"]
    assert worksheet["summary"]["pair_count"] == 2

    # 同一份导出不能同时做主从
    bad = client.post("/api/merge-worksheets", json={
        "primary_export_id": 1, "secondary_export_id": 1, "merge_key": "id"})
    assert bad.status_code == 400

    # 列表台账
    listed = client.get("/api/merge-worksheets").json()
    assert listed["total"] == 1
    assert listed["items"][0]["input_pair_id"] == worksheet["input_pair_id"]

    # 策略预填
    resp = client.post(f"/api/merge-worksheets/{wid}/apply-policy", json={"policy": "主源优先"})
    assert resp.status_code == 200

    # 相同值单元格拒绝裁定
    pair = worksheet["review"]["key_groups"][0]["pairs"][0]
    bad_cell = client.post(f"/api/merge-worksheets/{wid}/resolve-cell", json={
        "pair_id": pair["pair_id"], "field": "客户名称", "resolution": "取主源"})
    assert bad_cell.status_code == 400

    # 本场景没有待确认原行，策略预填后全部差异都已裁定，可以确认
    confirmed = client.post(f"/api/merge-worksheets/{wid}/confirm", json={"reviewer": "李复核"})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "已确认"

    # 发布
    published = client.post(f"/api/merge-worksheets/{wid}/publish")
    assert published.status_code == 200
    artifact = published.json()
    assert artifact["row_count"] == 2
    assert "归并底稿号" in artifact["columns"]

    # 回溯核对
    verify = client.get(f"/api/merge-worksheets/published/{artifact['published_id']}/verify")
    assert verify.status_code == 200
    assert verify.json()["ok"] is True

    # 已发布底稿不可再改裁定
    locked = client.post(f"/api/merge-worksheets/{wid}/apply-policy", json={"policy": "从源覆盖"})
    assert locked.status_code == 400


def test_pending_decision_http():
    _seed_exports()
    # 再造一份只含孤立键的从源，制造待确认原行
    write_export(3, {"export_id": 3, "task_no": "BATCH-S2", "batch_no": "BATCH-S2",
                     "file_version": "BATCH-S2-v1", "api_name": "客户API",
                     "columns": COLUMNS,
                     "rows": [["K9", "孤", "135", "1", "北京"]]})
    worksheet = client.post("/api/merge-worksheets", json={
        "primary_export_id": 1, "secondary_export_id": 3, "merge_key": "id"}).json()
    wid = worksheet["worksheet_id"]
    pending_ids = [
        pending["pending_id"]
        for group in worksheet["review"]["key_groups"]
        for pending in group["pending_rows"]
    ]
    assert pending_ids  # 主源 K1 两行 + 从源 K9 一行，全部配不上对

    blocked = client.post(f"/api/merge-worksheets/{wid}/confirm", json={})
    assert blocked.status_code == 400

    for pending_id in pending_ids:
        resp = client.post(f"/api/merge-worksheets/{wid}/pending-decision",
                           json={"pending_id": pending_id, "decision": PENDING_DROP})
        assert resp.status_code == 200
    # 待确认项清零后方可确认（该场景没有同键配对，无需策略预填）
    assert client.post(f"/api/merge-worksheets/{wid}/confirm", json={}).status_code == 200
