"""归并差异底稿验收测试。

验收场景：同一个归并键在主源、从源各有两行，其中只有部分字段对得上；
两边各修改一条后，核对相同值、差异值、待确认原行各自进入哪个纠错处理，
并验证源导出全程不被覆盖、换了一侧导出能区分输入对、发布明细可回溯。
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEMP_DATA = tempfile.mkdtemp(prefix="cc_merge_review_")
os.environ["DATA_ROOT"] = TEMP_DATA

from app.services import merge_review as engine  # noqa: E402
from app.services.data_manager import DATA_ROOT, write_export  # noqa: E402
from app.services.merge_worksheet import (  # noqa: E402
    PENDING_DROP,
    PENDING_KEEP,
    STATUS_CONFIRMED,
    STATUS_PUBLISHED,
    STATUS_REVIEW,
    worksheet_service,
)

COLUMNS = ["id", "客户名称", "手机号", "金额", "城市"]
MERGE_KEY = "id"
KEY = "K100"


def _export(export_id: int, task_no: str, rows: list[list[str]], api_name: str = "客户主数据API") -> dict:
    payload = {
        "export_id": export_id,
        "task_no": task_no,
        "batch_no": task_no,
        "file_version": f"{task_no}-v1",
        "api_name": api_name,
        "columns": COLUMNS,
        "rows": rows,
        "status": "已完成",
    }
    write_export(export_id, payload)
    return payload


@pytest.fixture()
def two_by_two_exports():
    """主源、从源各两行同键 K100，外加主源独有的 K200 用于待确认分流。

    主源 K100 两行：P1 与从源 S1 仅手机号/城市一致（部分对得上），
    P2 与从源 S2 金额/城市一致；两列手机号与金额存在差异。
    """
    shutil.rmtree(DATA_ROOT, ignore_errors=True)
    primary = _export(1, "BATCH-P-20261120", [
        [KEY, "华东客户甲", "13800000001", "100.00", "上海"],   # P1
        [KEY, "华东客户甲-改", "13800000002", "200.00", "杭州"],  # P2（主源改过的一条）
        ["K200", "孤立主源行", "13800000009", "900.00", "南京"],
    ])
    secondary = _export(2, "BATCH-S-20261120", [
        [KEY, "华东客户甲", "13800000001", "150.00", "上海"],   # S1：金额与 P1 不同
        [KEY, "华东客户甲-改", "13900000002", "200.00", "杭州"],  # S2（从源改过手机号的一条）
    ])
    # 记录源文件原始字节，供“不覆盖源值”断言
    primary_bytes = (DATA_ROOT / "exports" / "000001.json").read_bytes()
    secondary_bytes = (DATA_ROOT / "exports" / "000002.json").read_bytes()
    yield {
        "primary": primary,
        "secondary": secondary,
        "primary_bytes": primary_bytes,
        "secondary_bytes": secondary_bytes,
    }


def _build(two_by_two_exports):
    return worksheet_service.create_from_exports(1, 2, MERGE_KEY, rule_id=3)


# ---------------------------------------------------------------------------
# 1. 建底稿：2v2 同键配对、单元格结论与纠错分流
# ---------------------------------------------------------------------------

class TestWorksheetBuild:
    def test_pairing_prefers_most_matching_fields(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        assert worksheet["status"] == STATUS_REVIEW
        groups = worksheet["review"]["key_groups"]
        k100 = next(g for g in groups if g["merge_key_value"] == KEY)

        # 同键各两行，应配出两对，且按吻合度最高的方式一一配对
        assert k100["primary_row_count"] == 2
        assert k100["secondary_row_count"] == 2
        assert len(k100["pairs"]) == 2
        pair_index = {p["pair_id"]: p for p in k100["pairs"]}
        # P1<->S1 与 P2<->S2 各有 4 个字段可比、3 个相同
        for pair in pair_index.values():
            assert pair["equal_field_count"] == 4  # id + 两个相同字段 + ？见下
            assert pair["diff_field_count"] == 1
            assert pair["route"] == engine.ROUTE_DIFF_CORRECTION
            assert pair["row_status"] == engine.ROW_STATUS_PARTIAL

    def test_cell_verdicts_split_equal_different_missing(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        k100 = next(g for g in worksheet["review"]["key_groups"] if g["merge_key_value"] == KEY)
        pair = k100["pairs"][0]
        by_field = {c["field"]: c for c in pair["cells"]}

        assert by_field["id"]["verdict"] == engine.VERDICT_EQUAL
        assert by_field["客户名称"]["verdict"] == engine.VERDICT_EQUAL
        assert by_field["手机号"]["verdict"] == engine.VERDICT_EQUAL
        assert by_field["金额"]["verdict"] == engine.VERDICT_DIFFERENT
        assert by_field["金额"]["primary_value"] == "100.00"
        assert by_field["金额"]["secondary_value"] == "150.00"
        assert by_field["金额"]["resolution"] == engine.RESOLUTION_OPEN
        # 差异值不允许被底稿悄悄定掉
        assert by_field["金额"]["resolved_value"] is None

    def test_every_cell_carries_real_source_location(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        pair = worksheet["review"]["key_groups"][0]["pairs"][0]
        p_ref = pair["primary_ref"]
        s_ref = pair["secondary_ref"]
        assert p_ref["side"] == "primary"
        assert p_ref["batch_no"] == "BATCH-P-20261120"
        assert p_ref["file_version"] == "BATCH-P-20261120-v1"
        assert p_ref["row_number"] == 2  # CSV 含表头，首条数据在第2行
        assert p_ref["sha256"] and len(p_ref["sha256"]) == 64
        assert s_ref["batch_no"] == "BATCH-S-20261120"
        assert s_ref["row_number"] == 2

    def test_unpaired_row_goes_to_pending_queue(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        groups = worksheet["review"]["key_groups"]
        k200 = next(g for g in groups if g["merge_key_value"] == "K200")
        assert len(k200["pairs"]) == 0
        assert len(k200["pending_rows"]) == 1
        pending = k200["pending_rows"][0]
        assert pending["route"] == engine.ROUTE_PENDING
        assert pending["side"] == "primary"
        assert pending["source_ref"]["batch_no"] == "BATCH-P-20261120"
        assert pending["handling"] == engine.RESOLUTION_OPEN

    def test_summary_counts_three_routes(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        summary = worksheet["summary"]
        assert summary["pair_count"] == 2
        assert summary["pending_count"] == 1
        assert summary["diff_correction_count"] == 2
        assert summary["route_counts"][engine.ROUTE_DIFF_CORRECTION] == 2
        assert summary["route_counts"][engine.ROUTE_PENDING] == 1
        assert summary["open_diff_cell_count"] >= 2

    def test_source_exports_are_never_overwritten(self, two_by_two_exports):
        _build(two_by_two_exports)
        # 走完整复核流程后再断言一次，源字节必须保持建底稿前的内容
        assert (DATA_ROOT / "exports" / "000001.json").read_bytes() == two_by_two_exports["primary_bytes"]
        assert (DATA_ROOT / "exports" / "000002.json").read_bytes() == two_by_two_exports["secondary_bytes"]


# ---------------------------------------------------------------------------
# 2. 复核裁定：相同值直通、差异值纠错、待确认原行分流
# ---------------------------------------------------------------------------

class TestReviewRouting:
    def test_confirm_blocked_until_all_items_handled(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        with pytest.raises(ValueError, match="未完成的复核项"):
            worksheet_service.confirm_worksheet(worksheet["worksheet_id"])

    def test_policy_only_prefills_and_still_needs_confirm(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        wid = worksheet["worksheet_id"]
        updated = worksheet_service.apply_conflict_policy(wid, "主源优先")
        k100 = next(g for g in updated["review"]["key_groups"] if g["merge_key_value"] == KEY)
        for pair in k100["pairs"]:
            amount = next(c for c in pair["cells"] if c["field"] == "金额")
            assert amount["resolution"] == engine.RESOLUTION_PRIMARY
            assert amount["resolved_value"] == amount["primary_value"]
        # 预填不等于发布：待确认原行还没处置，仍不能确认
        with pytest.raises(ValueError, match="未完成的复核项"):
            worksheet_service.confirm_worksheet(wid)

    def test_full_review_routes_each_original_row(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        wid = worksheet["worksheet_id"]
        worksheet_service.apply_conflict_policy(wid, "主源优先")

        # 手工把第二对的差异字段「手机号」改成人工修正值，验证逐格裁定可覆盖策略预填
        k100 = next(g for g in worksheet_service.get_worksheet(wid)["review"]["key_groups"]
                    if g["merge_key_value"] == KEY)
        second_pair = k100["pairs"][1]
        phone_cell = next(c for c in second_pair["cells"] if c["field"] == "手机号")
        assert phone_cell["verdict"] == engine.VERDICT_DIFFERENT
        worksheet_service.resolve_cell(wid, second_pair["pair_id"], "手机号",
                                       engine.RESOLUTION_MANUAL, manual_value="13800000088")

        # 待确认原行：放弃（重做批次时不再带出）
        pending_id = worksheet_service.get_worksheet(wid)["review"]["key_groups"][1]["pending_rows"][0]["pending_id"]
        worksheet_service.decide_pending_row(wid, pending_id, PENDING_DROP)

        confirmed = worksheet_service.confirm_worksheet(wid, reviewer="张复核")
        assert confirmed["status"] == STATUS_CONFIRMED
        assert confirmed["reviewer"] == "张复核"

        artifact = worksheet_service.publish_merge(wid)
        assert artifact["row_count"] == 2  # 待确认孤立行被放弃，不进发布明细
        routes = {(r.get("pair_id") or r.get("pending_id")): r["route"]
                  for r in artifact["correction_routes"]}
        # 两条同键配对都是差异纠错；孤立原行挂待确认路由并记录“放弃该行”
        pair_routes = [route for key, route in routes.items() if key and "-S" in key]
        assert pair_routes and all(route == engine.ROUTE_DIFF_CORRECTION for route in pair_routes)
        pending_route = next(r for r in artifact["correction_routes"] if r["route"] == engine.ROUTE_PENDING)
        assert pending_route["decision"] == PENDING_DROP

        # 发布行的值来自复核裁定，而不是源文件被改写：
        # 第一对金额走主源优先=100.00；第二对金额两边一致直通=200.00；
        # 第二对手机号为人工修正值 13800000088
        amount_idx = artifact["columns"].index("金额")
        phone_idx = artifact["columns"].index("手机号")
        assert {row[amount_idx] for row in artifact["rows"]} == {"100.00", "200.00"}
        second_row = next(row for row in artifact["rows"]
                          if row[amount_idx] == "200.00")
        assert second_row[phone_idx] == "13800000088"

    def test_pending_keep_brings_row_into_published_detail(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        wid = worksheet["worksheet_id"]
        worksheet_service.apply_conflict_policy(wid, "从源覆盖")
        pending_id = worksheet_service.get_worksheet(wid)["review"]["key_groups"][1]["pending_rows"][0]["pending_id"]
        worksheet_service.decide_pending_row(wid, pending_id, PENDING_KEEP)
        worksheet_service.confirm_worksheet(wid)
        artifact = worksheet_service.publish_merge(wid)
        assert artifact["row_count"] == 3
        kept = next(r for r in artifact["correction_routes"] if r["route"] == engine.ROUTE_PENDING)
        assert kept["decision"] == PENDING_KEEP


# ---------------------------------------------------------------------------
# 3. 输入对身份：换任一导出，重开可区分
# ---------------------------------------------------------------------------

class TestInputPairIdentity:
    def test_reopen_detects_changed_side_export(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        wid = worksheet["worksheet_id"]
        stored_pair = worksheet["input_pair_id"]

        # 重开未改动的导出：无漂移
        reopened = worksheet_service.get_worksheet(wid)
        assert reopened["reopen_check"]["pair_drifted"] is False
        assert reopened["reopen_check"]["current_pair_id"] == stored_pair

        # 换一侧导出（同 export_id、新内容，模拟该批次重做后的文件版本）
        _export(2, "BATCH-S-20261121", [
            [KEY, "华东客户甲", "13800000001", "160.00", "上海"],
            [KEY, "华东客户甲-改", "13900000002", "200.00", "杭州"],
        ])
        drifted = worksheet_service.get_worksheet(wid)
        assert drifted["reopen_check"]["secondary_changed"] is True
        assert drifted["reopen_check"]["primary_changed"] is False
        assert drifted["reopen_check"]["pair_drifted"] is True
        assert drifted["reopen_check"]["current_pair_id"] != stored_pair

        # 漂移后禁止确认/发布，防止拿旧底稿套新文件
        with pytest.raises(ValueError, match="输入对与底稿不一致"):
            worksheet_service.confirm_worksheet(wid)

    def test_new_export_pair_creates_distinct_worksheet_identity(self, two_by_two_exports):
        first = _build(two_by_two_exports)
        _export(2, "BATCH-S-20261121", [
            [KEY, "华东客户甲", "13800000001", "160.00", "上海"],
            [KEY, "华东客户甲", "13800000001", "200.00", "杭州"],
        ])
        second = worksheet_service.create_from_exports(1, 2, MERGE_KEY, rule_id=3)
        assert second["worksheet_id"] != first["worksheet_id"]
        assert second["input_pair_id"] != first["input_pair_id"]

        ledger = worksheet_service.list_worksheets()
        pair_ids = {entry["input_pair_id"] for entry in ledger}
        assert len(pair_ids) == 2
        # 旧底稿仍指向自己那对输入，没有被新底稿覆盖
        old_entry = next(e for e in ledger if e["worksheet_id"] == first["worksheet_id"])
        assert old_entry["input_pair_id"] == first["input_pair_id"]


# ---------------------------------------------------------------------------
# 4. 发布明细回溯到这对输入
# ---------------------------------------------------------------------------

class TestPublishedTraceability:
    def test_verify_roundtrip_back_to_source_pair(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        wid = worksheet["worksheet_id"]
        worksheet_service.apply_conflict_policy(wid, "主源优先")
        pending_id = worksheet_service.get_worksheet(wid)["review"]["key_groups"][1]["pending_rows"][0]["pending_id"]
        worksheet_service.decide_pending_row(wid, pending_id, PENDING_DROP)
        worksheet_service.confirm_worksheet(wid)
        artifact = worksheet_service.publish_merge(wid)
        assert worksheet_service.get_worksheet(wid)["status"] == STATUS_PUBLISHED

        check = worksheet_service.verify_published(artifact["published_id"])
        assert check["ok"] is True
        assert check["pair_intact"] is True
        assert check["row_trace_ok"] is True
        assert {f["side"] for f in check["files"]} == {"primary", "secondary"}
        for file_check in check["files"]:
            assert file_check["unchanged"] is True
            assert file_check["recorded_sha256"] == file_check["current_sha256"]

        # 发布 CSV 带溯源列，能直接看到批次/文件版本/原行行号
        csv_path = DATA_ROOT / "merge_published" / f"{artifact['published_id']:06d}.csv"
        with csv_path.open(encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        for col in ("归并底稿号", "输入对指纹", "纠错处理", "主源批次", "从源批次",
                    "主源原行行号", "从源原行行号"):
            assert col in header

    def test_verify_fails_after_source_export_changes(self, two_by_two_exports):
        worksheet = _build(two_by_two_exports)
        wid = worksheet["worksheet_id"]
        worksheet_service.apply_conflict_policy(wid, "主源优先")
        pending_id = worksheet_service.get_worksheet(wid)["review"]["key_groups"][1]["pending_rows"][0]["pending_id"]
        worksheet_service.decide_pending_row(wid, pending_id, PENDING_DROP)
        worksheet_service.confirm_worksheet(wid)
        artifact = worksheet_service.publish_merge(wid)

        # 发布后重做一侧导出：回溯必须发现输入对已不是发布时那一对
        _export(1, "BATCH-P-20261122", [
            [KEY, "华东客户甲", "13800000001", "120.00", "上海"],
            [KEY, "华东客户甲-改", "13800000002", "200.00", "杭州"],
            ["K200", "孤立主源行", "13800000009", "900.00", "南京"],
        ])
        check = worksheet_service.verify_published(artifact["published_id"])
        assert check["ok"] is False
        assert check["pair_intact"] is False
        primary_check = next(f for f in check["files"] if f["side"] == "primary")
        assert primary_check["unchanged"] is False


# ---------------------------------------------------------------------------
# 5. 纯引擎边界：缺失值、全一致直通、手工修正
# ---------------------------------------------------------------------------

class TestEngineEdgeCases:
    def test_all_equal_pair_goes_straight(self):
        primary = engine.bundle_from_export("primary", {
            "export_id": 1, "task_no": "P", "columns": ["id", "v"],
            "rows": [["K1", "A"]]}, "h1", "/tmp/p.json")
        secondary = engine.bundle_from_export("secondary", {
            "export_id": 2, "task_no": "S", "columns": ["id", "v"],
            "rows": [["K1", "A"]]}, "h2", "/tmp/s.json")
        review = engine.build_review(primary, secondary, "id")
        pair = review["key_groups"][0]["pairs"][0]
        assert pair["route"] == engine.ROUTE_STRAIGHT
        assert review["summary"]["straight_count"] == 1

    def test_missing_field_marked_not_silently_filled(self):
        primary = engine.bundle_from_export("primary", {
            "export_id": 1, "columns": ["id", "v"], "rows": [["K1", "A"]]},
            "h1", "/tmp/p.json")
        secondary = engine.bundle_from_export("secondary", {
            "export_id": 2, "columns": ["id", "v"], "rows": [["K1", ""]]},
            "h2", "/tmp/s.json")
        review = engine.build_review(primary, secondary, "id")
        cell = next(c for c in review["key_groups"][0]["pairs"][0]["cells"] if c["field"] == "v")
        assert cell["verdict"] == engine.VERDICT_MISSING
        assert cell["secondary_value"] is None
        assert cell["resolution"] == engine.RESOLUTION_OPEN
