"""归并差异底稿服务：底稿生命周期、复核留痕与确认后归并发布。

与 data_merge.merge_sources 的区别：
- 旧链路提交即按冲突策略出结果，没有复核数据；
- 本服务先把两侧导出固化成差异底稿，冲突策略只能“预填裁定建议”，
  必须等复核人员逐格确认、待确认原行逐条处置后才能归并发布。

所有底稿/发布产物写到 data/ 下，源导出文件 data/exports/ 全程只读。
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services import merge_review as engine
from app.services.data_manager import DATA_ROOT, _ensure_dir, _read_json, _write_json

WORKSHEET_DIR = DATA_ROOT / "merge_worksheets"
PUBLISHED_DIR = DATA_ROOT / "merge_published"
INDEX_PATH = WORKSHEET_DIR / "_index.json"

TZ = timezone(timedelta(hours=8))

STATUS_REVIEW = "待复核"
STATUS_CONFIRMED = "已确认"
STATUS_PUBLISHED = "已发布"

# 待确认原行的处置方式
PENDING_DROP = "放弃该行"
PENDING_KEEP = "手工保留"
PENDING_DECISIONS = {PENDING_DROP, PENDING_KEEP}

CONFLICT_POLICIES = {"主源优先": engine.RESOLUTION_PRIMARY,
                     "从源覆盖": engine.RESOLUTION_SECONDARY}


def _now() -> str:
    return datetime.now(TZ).isoformat()


def _sha256_file(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _next_id(directory: Path, prefix_glob: str) -> int:
    _ensure_dir(directory)
    return max((int(f.stem) for f in directory.glob(prefix_glob)), default=0) + 1


def _worksheet_path(worksheet_id: int) -> Path:
    return WORKSHEET_DIR / f"{worksheet_id:06d}.json"


def _load_index() -> list[dict[str, Any]]:
    return _read_json(INDEX_PATH, []) or []


def _save_index(entries: list[dict[str, Any]]) -> None:
    _write_json(INDEX_PATH, entries)


class MergeWorksheetService:
    """底稿登记、复核、确认与发布的应用服务。"""

    # ---------- 构造与打开 ----------

    def create_from_exports(self, primary_export_id: int, secondary_export_id: int,
                            merge_key: str, rule_id: int | None = None) -> dict[str, Any]:
        """读取两份导出产物，生成归并差异底稿并落盘（不改动源导出）。"""
        from app.services.data_manager import read_export

        if primary_export_id == secondary_export_id:
            raise ValueError("主源和从源必须是两份不同的导出")
        primary_export = read_export(primary_export_id)
        secondary_export = read_export(secondary_export_id)
        if not primary_export or not secondary_export:
            raise ValueError("指定的导出产物不存在，无法建立底稿")

        primary_path = DATA_ROOT / "exports" / f"{primary_export_id:06d}.json"
        secondary_path = DATA_ROOT / "exports" / f"{secondary_export_id:06d}.json"
        primary_hash = _sha256_file(primary_path)
        secondary_hash = _sha256_file(secondary_path)
        if not primary_hash or not secondary_hash:
            raise ValueError("导出文件缺失或不可读，已中止建底稿以保证溯源完整")

        primary_bundle = engine.bundle_from_export("primary", primary_export, primary_hash, str(primary_path))
        secondary_bundle = engine.bundle_from_export("secondary", secondary_export, secondary_hash, str(secondary_path))
        review = engine.build_review(primary_bundle, secondary_bundle, merge_key, rule_id)
        input_pair_id = engine.fingerprint_pair(review["primary_source"], review["secondary_source"])

        worksheet_id = _next_id(WORKSHEET_DIR, "[0-9]*.json")
        worksheet = {
            "worksheet_id": worksheet_id,
            "status": STATUS_REVIEW,
            "rule_id": rule_id,
            "merge_key": merge_key,
            "fields": review["fields"],
            "input_pair_id": input_pair_id,
            "primary_source": review["primary_source"],
            "secondary_source": review["secondary_source"],
            "review": review,
            "summary": review["summary"],
            "created_at": _now(),
            "confirmed_at": None,
            "published_id": None,
        }
        _write_json(_worksheet_path(worksheet_id), worksheet)

        index = _load_index()
        index.append({
            "worksheet_id": worksheet_id,
            "input_pair_id": input_pair_id,
            "status": STATUS_REVIEW,
            "rule_id": rule_id,
            "merge_key": merge_key,
            "primary_export_id": primary_export_id,
            "secondary_export_id": secondary_export_id,
            "primary_sha256": primary_hash,
            "secondary_sha256": secondary_hash,
            "created_at": worksheet["created_at"],
        })
        _save_index(index)
        return self.get_worksheet(worksheet_id)

    def list_worksheets(self) -> list[dict[str, Any]]:
        """列出全部底稿及其输入对身份，便于区分不同的一对输入。"""
        entries = _load_index()
        for entry in entries:
            entry["reopen_check"] = self._reopen_check(entry)
        return entries

    def get_worksheet(self, worksheet_id: int) -> dict[str, Any]:
        worksheet = _read_json(_worksheet_path(worksheet_id))
        if not worksheet:
            raise KeyError(f"归并差异底稿 {worksheet_id} 不存在")
        worksheet["reopen_check"] = self._reopen_check({
            "worksheet_id": worksheet_id,
            "input_pair_id": worksheet["input_pair_id"],
            "primary_sha256": worksheet["primary_source"]["sha256"],
            "secondary_sha256": worksheet["secondary_source"]["sha256"],
        })
        return worksheet

    def _reopen_check(self, index_entry: dict[str, Any]) -> dict[str, Any]:
        """重新打开时核对当前磁盘上的两份导出是否还是底稿记录的那一对。"""
        worksheet_id = index_entry["worksheet_id"]
        worksheet = _read_json(_worksheet_path(worksheet_id))
        if not worksheet:
            return {"available": False, "reason": "底稿文件缺失"}

        result: dict[str, Any] = {
            "available": True,
            "stored_pair_id": worksheet["input_pair_id"],
            "current_pair_id": worksheet["input_pair_id"],
            "primary_changed": False,
            "secondary_changed": False,
            "primary_missing": False,
            "secondary_missing": False,
            "pair_drifted": False,
        }
        for side, flag_missing in (("primary_source", "primary_missing"),
                                   ("secondary_source", "secondary_missing")):
            meta = worksheet[side]
            path = Path(meta["file_path"])
            if not path.exists():
                result[flag_missing] = True
                result[f"{meta['side']}_current_sha256"] = ""
                continue
            current_hash = _sha256_file(path)
            result[f"{meta['side']}_current_sha256"] = current_hash
            if current_hash != meta["sha256"]:
                result[f"{meta['side']}_changed"] = True

        current_pair = engine.fingerprint_pair(
            {"export_id": worksheet["primary_source"]["export_id"],
             "sha256": result.get("primary_current_sha256", "")},
            {"export_id": worksheet["secondary_source"]["export_id"],
             "sha256": result.get("secondary_current_sha256", "")},
        )
        result["current_pair_id"] = current_pair
        result["pair_drifted"] = current_pair != worksheet["input_pair_id"]
        return result

    # ---------- 复核操作（只改底稿，不碰源值） ----------

    def _mutable_worksheet(self, worksheet_id: int) -> dict[str, Any]:
        worksheet = _read_json(_worksheet_path(worksheet_id))
        if not worksheet:
            raise KeyError(f"归并差异底稿 {worksheet_id} 不存在")
        if worksheet["status"] != STATUS_REVIEW:
            raise ValueError(f"底稿已{worksheet['status']}，复核结论不可再改")
        return worksheet

    def resolve_cell(self, worksheet_id: int, pair_id: str, field_name: str,
                     resolution: str, manual_value: str | None = None) -> dict[str, Any]:
        """裁定一个差异单元格：取主源/取从源/人工修正值。"""
        if resolution not in (engine.RESOLUTION_PRIMARY, engine.RESOLUTION_SECONDARY,
                              engine.RESOLUTION_MANUAL):
            raise ValueError(f"不支持的裁定方式：{resolution}")
        worksheet = self._mutable_worksheet(worksheet_id)
        cell, pair = self._find_pair_cell(worksheet, pair_id, field_name)
        if cell["verdict"] == engine.VERDICT_EQUAL:
            raise ValueError("相同值单元格无需裁定，保持直通即可")
        cell["resolution"] = resolution
        if resolution == engine.RESOLUTION_MANUAL:
            if manual_value is None or str(manual_value).strip() == "":
                raise ValueError("人工修正值不能为空")
            cell["manual_value"] = str(manual_value)
            cell["resolved_value"] = str(manual_value)
        else:
            cell["manual_value"] = None
            cell["resolved_value"] = (cell["primary_value"] if resolution == engine.RESOLUTION_PRIMARY
                                      else cell["secondary_value"])
        pair["row_status"] = engine.ROW_STATUS_PARTIAL
        self._refresh_summary(worksheet)
        _write_json(_worksheet_path(worksheet_id), worksheet)
        return self.get_worksheet(worksheet_id)

    def apply_conflict_policy(self, worksheet_id: int, policy: str) -> dict[str, Any]:
        """把冲突策略批量预填到尚未裁定的差异单元格（仍需人工确认，不会直接归并）。"""
        if policy not in CONFLICT_POLICIES:
            raise ValueError(f"冲突策略仅支持：{'、'.join(CONFLICT_POLICIES)}")
        target = CONFLICT_POLICIES[policy]
        worksheet = self._mutable_worksheet(worksheet_id)
        prefilled = 0
        for group in worksheet["review"]["key_groups"]:
            for pair in group["pairs"]:
                for cell in pair["cells"]:
                    if cell["resolution"] != engine.RESOLUTION_OPEN:
                        continue
                    cell["resolution"] = target
                    cell["resolved_value"] = (cell["primary_value"] if target == engine.RESOLUTION_PRIMARY
                                               else cell["secondary_value"])
                    prefilled += 1
        worksheet["policy_prefill"] = {"policy": policy, "prefilled_cells": prefilled, "at": _now()}
        self._refresh_summary(worksheet)
        _write_json(_worksheet_path(worksheet_id), worksheet)
        return self.get_worksheet(worksheet_id)

    def decide_pending_row(self, worksheet_id: int, pending_id: str, decision: str) -> dict[str, Any]:
        """处置一条配不上对的待确认原行：手工保留或放弃该行。"""
        if decision not in PENDING_DECISIONS:
            raise ValueError(f"待确认原行处置仅支持：{'、'.join(PENDING_DECISIONS)}")
        worksheet = self._mutable_worksheet(worksheet_id)
        pending = self._find_pending(worksheet, pending_id)
        pending["handling"] = decision
        for cell in pending["cells"]:
            if decision == PENDING_KEEP and cell["resolution"] == engine.RESOLUTION_OPEN:
                cell["resolution"] = engine.RESOLUTION_MANUAL
                cell["manual_value"] = (cell["primary_value"] if pending["side"] == "primary"
                                         else cell["secondary_value"])
                cell["resolved_value"] = cell["manual_value"]
            if decision == PENDING_DROP and cell["resolution"] == engine.RESOLUTION_OPEN:
                cell["resolution"] = "放弃"
        self._refresh_summary(worksheet)
        _write_json(_worksheet_path(worksheet_id), worksheet)
        return self.get_worksheet(worksheet_id)

    def confirm_worksheet(self, worksheet_id: int, reviewer: str = "复核员") -> dict[str, Any]:
        """确认底稿：所有差异与待确认原行都必须已有处置，且输入对未发生漂移。"""
        worksheet = self._mutable_worksheet(worksheet_id)
        reopen = self._reopen_check({"worksheet_id": worksheet_id,
                                     "input_pair_id": worksheet["input_pair_id"]})
        if reopen.get("pair_drifted"):
            raise ValueError("两侧导出已变更，当前输入对与底稿不一致，请基于新导出重建底稿后再确认")
        progress = engine.review_progress(worksheet["review"])
        if not progress["can_confirm"]:
            raise ValueError(
                "仍有未完成的复核项："
                f"差异配对 {len(progress['open_pair_ids'])} 条、待确认原行 {len(progress['open_pending_ids'])} 条"
            )
        worksheet["status"] = STATUS_CONFIRMED
        worksheet["confirmed_at"] = _now()
        worksheet["reviewer"] = reviewer
        _write_json(_worksheet_path(worksheet_id), worksheet)
        self._update_index_status(worksheet_id, STATUS_CONFIRMED)
        return self.get_worksheet(worksheet_id)

    # ---------- 确认后归并发布 ----------

    def publish_merge(self, worksheet_id: int) -> dict[str, Any]:
        """根据确认后的底稿选出两份导出重做归并，发布带溯源列的明细。"""
        worksheet = _read_json(_worksheet_path(worksheet_id))
        if not worksheet:
            raise KeyError(f"归并差异底稿 {worksheet_id} 不存在")
        if worksheet["status"] != STATUS_CONFIRMED:
            raise ValueError("只有已确认的底稿才能归并发布")
        reopen = self._reopen_check({"worksheet_id": worksheet_id,
                                     "input_pair_id": worksheet["input_pair_id"]})
        if reopen.get("pair_drifted"):
            raise ValueError("源导出在确认后发生变更，为保证发布明细可回溯已中止，请重建底稿")

        fields = worksheet["fields"]
        merged_rows: list[dict[str, Any]] = []
        correction_routes: list[dict[str, Any]] = []
        for group in worksheet["review"]["key_groups"]:
            for pair in group["pairs"]:
                row = {cell["field"]: cell["resolved_value"] for cell in pair["cells"]}
                row.update(self._provenance_columns(worksheet, pair["route"],
                                                     pair["primary_ref"], pair["secondary_ref"]))
                merged_rows.append(row)
                correction_routes.append({
                    "pair_id": pair["pair_id"],
                    "route": pair["route"],
                    "merge_key_value": pair["merge_key_value"],
                    "primary_ref": pair["primary_ref"],
                    "secondary_ref": pair["secondary_ref"],
                })
            for pending in group["pending_rows"]:
                if pending["handling"] != PENDING_KEEP:
                    correction_routes.append({
                        "pending_id": pending["pending_id"],
                        "route": engine.ROUTE_PENDING,
                        "decision": PENDING_DROP,
                        "merge_key_value": pending["merge_key_value"],
                        "source_ref": pending["source_ref"],
                    })
                    continue
                row = {cell["field"]: cell["resolved_value"] for cell in pending["cells"]
                       if cell["resolution"] != "放弃"}
                ref = pending["source_ref"]
                row.update(self._provenance_columns(worksheet, engine.ROUTE_PENDING,
                                                     ref if pending["side"] == "primary" else {},
                                                     ref if pending["side"] == "secondary" else {}))
                merged_rows.append(row)
                correction_routes.append({
                    "pending_id": pending["pending_id"],
                    "route": engine.ROUTE_PENDING,
                    "decision": PENDING_KEEP,
                    "merge_key_value": pending["merge_key_value"],
                    "source_ref": ref,
                })

        published_id = _next_id(PUBLISHED_DIR, "[0-9]*.json")
        provenance_cols = ["归并底稿号", "输入对指纹", "纠错处理",
                           "主源批次", "主源文件版本", "主源原行行号",
                           "从源批次", "从源文件版本", "从源原行行号"]
        artifact = {
            "published_id": published_id,
            "worksheet_id": worksheet_id,
            "input_pair_id": worksheet["input_pair_id"],
            "merge_key": worksheet["merge_key"],
            "columns": [*fields, *provenance_cols],
            "rows": [[str(row.get(col, "") or "") for col in [*fields, *provenance_cols]]
                     for row in merged_rows],
            "row_count": len(merged_rows),
            "correction_routes": correction_routes,
            "source_pair": {
                "primary": worksheet["primary_source"],
                "secondary": worksheet["secondary_source"],
            },
            "published_at": _now(),
        }
        _ensure_dir(PUBLISHED_DIR)
        _write_json(PUBLISHED_DIR / f"{published_id:06d}.json", artifact)
        self._write_published_csv(published_id, artifact)

        worksheet["status"] = STATUS_PUBLISHED
        worksheet["published_id"] = published_id
        _write_json(_worksheet_path(worksheet_id), worksheet)
        self._update_index_status(worksheet_id, STATUS_PUBLISHED)
        return artifact

    def verify_published(self, published_id: int) -> dict[str, Any]:
        """把发布明细回溯到底稿引用的那对输入：重算文件指纹并逐行核对溯源。"""
        artifact = _read_json(PUBLISHED_DIR / f"{published_id:06d}.json")
        if not artifact:
            raise KeyError(f"发布明细 {published_id} 不存在")
        worksheet = _read_json(_worksheet_path(artifact["worksheet_id"]))
        checks: dict[str, Any] = {
            "published_id": published_id,
            "worksheet_id": artifact["worksheet_id"],
            "stored_pair_id": artifact["input_pair_id"],
            "files": [],
            "row_trace_ok": True,
            "pair_intact": True,
            "ok": False,
        }
        for side_meta in artifact["source_pair"].values():
            path = Path(side_meta["file_path"])
            current_hash = _sha256_file(path)
            file_check = {
                "side": side_meta["side"],
                "export_id": side_meta["export_id"],
                "batch_no": side_meta["batch_no"],
                "file_version": side_meta["file_version"],
                "file_path": side_meta["file_path"],
                "recorded_sha256": side_meta["sha256"],
                "current_sha256": current_hash,
                "unchanged": current_hash == side_meta["sha256"] and bool(current_hash),
            }
            checks["files"].append(file_check)
            if not file_check["unchanged"]:
                checks["pair_intact"] = False

        valid_refs = set()
        for group in worksheet["review"]["key_groups"]:
            for pair in group["pairs"]:
                valid_refs.add((pair["primary_ref"]["row_number"], pair["secondary_ref"]["row_number"]))
            for pending in group["pending_rows"]:
                valid_refs.add((pending["source_ref"]["row_number"], -1))
                valid_refs.add((-1, pending["source_ref"]["row_number"]))
        columns = artifact["columns"]
        idx_p = columns.index("主源原行行号")
        idx_s = columns.index("从源原行行号")
        bad_rows = []
        for line_no, row in enumerate(artifact["rows"], start=2):
            p_no = int(row[idx_p] or -1)
            s_no = int(row[idx_s] or -1)
            if (p_no, s_no) not in valid_refs:
                bad_rows.append(line_no)
                checks["row_trace_ok"] = False
        checks["bad_row_numbers"] = bad_rows
        checks["ok"] = checks["pair_intact"] and checks["row_trace_ok"]
        return checks

    # ---------- 内部工具 ----------

    def _find_pair_cell(self, worksheet: dict[str, Any], pair_id: str, field_name: str):
        for group in worksheet["review"]["key_groups"]:
            for pair in group["pairs"]:
                if pair["pair_id"] == pair_id:
                    for cell in pair["cells"]:
                        if cell["field"] == field_name:
                            return cell, pair
        raise KeyError(f"底稿中找不到配对 {pair_id} 的字段 {field_name}")

    def _find_pending(self, worksheet: dict[str, Any], pending_id: str) -> dict[str, Any]:
        for group in worksheet["review"]["key_groups"]:
            for pending in group["pending_rows"]:
                if pending["pending_id"] == pending_id:
                    return pending
        raise KeyError(f"底稿中找不到待确认原行 {pending_id}")

    def _refresh_summary(self, worksheet: dict[str, Any]) -> None:
        review = worksheet["review"]
        route_counts = {engine.ROUTE_STRAIGHT: 0, engine.ROUTE_DIFF_CORRECTION: 0,
                        engine.ROUTE_PENDING: 0}
        for group in review["key_groups"]:
            for pair in group["pairs"]:
                route_counts[pair["route"]] += 1
            route_counts[engine.ROUTE_PENDING] += len(group["pending_rows"])
        worksheet["summary"]["route_counts"] = route_counts
        worksheet["summary"]["straight_count"] = route_counts[engine.ROUTE_STRAIGHT]
        worksheet["summary"]["diff_correction_count"] = route_counts[engine.ROUTE_DIFF_CORRECTION]
        worksheet["summary"]["pending_count"] = route_counts[engine.ROUTE_PENDING]
        review_progress = engine.review_progress(review)
        worksheet["summary"]["open_diff_cell_count"] = len(review_progress["open_pair_ids"]) + sum(
            1 for group in review["key_groups"]
            for pair in group["pairs"]
            for cell in pair["cells"] if cell["resolution"] == engine.RESOLUTION_OPEN
        )

    def _provenance_columns(self, worksheet: dict[str, Any], route: str,
                            primary_ref: dict[str, Any], secondary_ref: dict[str, Any]) -> dict[str, Any]:
        return {
            "归并底稿号": worksheet["worksheet_id"],
            "输入对指纹": worksheet["input_pair_id"],
            "纠错处理": route,
            "主源批次": primary_ref.get("batch_no", ""),
            "主源文件版本": primary_ref.get("file_version", ""),
            "主源原行行号": primary_ref.get("row_number", ""),
            "从源批次": secondary_ref.get("batch_no", ""),
            "从源文件版本": secondary_ref.get("file_version", ""),
            "从源原行行号": secondary_ref.get("row_number", ""),
        }

    def _write_published_csv(self, published_id: int, artifact: dict[str, Any]) -> None:
        path = PUBLISHED_DIR / f"{published_id:06d}.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(artifact["columns"])
            writer.writerows(artifact["rows"])

    def _update_index_status(self, worksheet_id: int, status: str) -> None:
        index = _load_index()
        for entry in index:
            if entry["worksheet_id"] == worksheet_id:
                entry["status"] = status
        _save_index(index)


# 单例，路由层直接使用
worksheet_service = MergeWorksheetService()
