"""归并差异底稿核心引擎（纯函数、无副作用）。

设计约束（对应业务复核要求）：
- 只读取两侧导出明细，按归并键把同键的主源/从源行配成对照对；
- 底稿逐字段记录两侧的原始值与比对结论，**绝不改写任何源值**；
- 一个归并键在主源和从源各有多行时，用全排列挑出字段吻合度最高的配对，
  配不上的原行进入「待确认」队列，不允许悄悄并入；
- 每个比对单元格都带原行位置（批次、文件版本、行号），复核人员可凭此
  回到真实数据源定位应当纠正的原行和需要重做的批次。

引擎本身不接触文件系统，输入是已经读入内存的两份导出描述，输出是普通
dict/list，方便单测与序列化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
from typing import Any

# 比对结论（单元格级）
VERDICT_EQUAL = "相同值"        # 两侧同字段值一致，可直通
VERDICT_DIFFERENT = "差异值"    # 两侧都有值但不一致，需人工裁定
VERDICT_MISSING = "缺失值"      # 一侧缺该字段/值为空

# 原行处理去向（纠错处理路由）
ROUTE_STRAIGHT = "直通免纠错"
ROUTE_DIFF_CORRECTION = "差异纠错"
ROUTE_PENDING = "待确认原行"

# 配对结果状态
ROW_STATUS_EQUAL = "全部一致"
ROW_STATUS_PARTIAL = "部分差异"
ROW_STATUS_PENDING = "待确认"

# 单元格复核裁定
RESOLUTION_OPEN = "待复核"
RESOLUTION_PRIMARY = "取主源"
RESOLUTION_SECONDARY = "取从源"
RESOLUTION_MANUAL = "人工修正值"


@dataclass
class SourceBundle:
    """一侧导出明细的内存描述（只读快照，构造后不再回写源文件）。"""

    side: str                          # primary / secondary
    export_id: int
    batch_no: str                      # 批次号，定位“该重做的批次”
    file_version: str                  # 文件版本标识
    file_path: str
    sha256: str                        # 导出文件内容指纹
    columns: list[str]
    rows: list[dict[str, Any]]         # 已转成 列->值 的字典，值统一为字符串或 None
    api_name: str = ""
    row_positions: list[dict[str, Any]] = field(default_factory=list)

    def row_ref(self, row_index: int) -> dict[str, Any]:
        """返回某原行的可追溯位置信息。"""
        position = self.row_positions[row_index] if row_index < len(self.row_positions) else {}
        return {
            "side": self.side,
            "export_id": self.export_id,
            "batch_no": self.batch_no,
            "file_version": self.file_version,
            "file_path": self.file_path,
            "sha256": self.sha256,
            "row_index": row_index,
            "row_number": position.get("row_number", row_index + 2),  # 含表头，数据首行通常是第2行
            "source_row_id": position.get("source_row_id", ""),
        }


def normalize_cell(value: Any) -> str | None:
    """导出明细里的值统一按字符串比较；空串/None 视为缺失。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def bundle_from_export(side: str, export: dict[str, Any], sha256: str, file_path: str) -> SourceBundle:
    """把 data_manager 读出的导出产物转成只读 SourceBundle。"""
    columns = list(export.get("columns", []))
    raw_rows = export.get("rows", [])
    rows: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_rows):
        values = list(raw) + [""] * max(0, len(columns) - len(raw))
        row = {col: normalize_cell(values[col_idx]) for col_idx, col in enumerate(columns)}
        rows.append(row)
        positions.append({
            "row_number": idx + 2,
            "source_row_id": row.get("id") or row.get("ID") or "",
        })
    return SourceBundle(
        side=side,
        export_id=int(export.get("export_id", 0)),
        batch_no=str(export.get("batch_no") or export.get("task_no") or f"EXPORT-{export.get('export_id', 0):06d}"),
        file_version=str(export.get("file_version") or export.get("_created_at", "") or f"v{export.get('export_id', 0)}"),
        file_path=file_path,
        sha256=sha256,
        columns=columns,
        rows=rows,
        api_name=str(export.get("api_name", "")),
        row_positions=positions,
    )


def _key_of(row: dict[str, Any], merge_key: str) -> str:
    value = normalize_cell(row.get(merge_key))
    return value if value is not None else ""


def _compare_fields(primary_row: dict[str, Any], secondary_row: dict[str, Any],
                    fields: list[str], merge_key: str) -> list[dict[str, Any]]:
    """逐字段比对一对同键原行，返回单元格级结论。"""
    cells: list[dict[str, Any]] = []
    for field_name in fields:
        primary_value = primary_row.get(field_name)
        secondary_value = secondary_row.get(field_name)
        if field_name == merge_key:
            verdict = VERDICT_EQUAL
        elif primary_value is None or secondary_value is None:
            verdict = VERDICT_MISSING
        elif primary_value == secondary_value:
            verdict = VERDICT_EQUAL
        else:
            verdict = VERDICT_DIFFERENT
        cells.append({
            "field": field_name,
            "is_merge_key": field_name == merge_key,
            "primary_value": primary_value,
            "secondary_value": secondary_value,
            "verdict": verdict,
            "resolution": RESOLUTION_OPEN if verdict in (VERDICT_DIFFERENT, VERDICT_MISSING) else RESOLUTION_PRIMARY,
            "resolved_value": primary_value if verdict == VERDICT_EQUAL else None,
            "manual_value": None,
        })
    return cells


def _score_pairing(primary_row: dict[str, Any], secondary_row: dict[str, Any],
                   fields: list[str], merge_key: str) -> tuple[int, int]:
    """给一组配对打分：(非键相同字段数, 非键总字段数)，供全排列挑最优。"""
    equal = 0
    compared = 0
    for field_name in fields:
        if field_name == merge_key:
            continue
        compared += 1
        p_value = primary_row.get(field_name)
        s_value = secondary_row.get(field_name)
        if p_value is not None and s_value is not None and p_value == s_value:
            equal += 1
    return equal, compared


def _best_pairing(primary_rows: list[dict[str, Any]], secondary_rows: list[dict[str, Any]],
                  fields: list[str], merge_key: str) -> list[tuple[int, int]]:
    """在同键多行之间找吻合度最高的一一配对。

    返回 (主源行下标, 从源行下标) 列表。行数少的一侧全部参与排列，
    分数先比“相同字段数”，平局保持较小行号优先，结果稳定可复核。
    """
    if not primary_rows or not secondary_rows:
        return []
    short_on_primary = len(primary_rows) <= len(secondary_rows)
    short_rows = primary_rows if short_on_primary else secondary_rows
    long_rows = secondary_rows if short_on_primary else primary_rows
    short_size = len(short_rows)

    best: list[tuple[int, int]] = []
    best_score: tuple[int, ...] = ()
    # 行数可控（同键多行的复核场景通常个位数），全排列保证全局最优；
    # 超过 120 种排列时退化为贪心，避免极端输入爆炸。
    choices = list(permutations(range(len(long_rows)), short_size))
    if len(choices) > 120:
        greedy: list[int] = []
        used: set[int] = set()
        for short_idx in range(short_size):
            candidates = [
                (_score_pairing(short_rows[short_idx], long_rows[long_idx], fields, merge_key)[0], -long_idx)
                for long_idx in range(len(long_rows)) if long_idx not in used
            ]
            candidates.sort(reverse=True)
            pick = -candidates[0][1]
            used.add(pick)
            greedy.append(pick)
        choices = [tuple(greedy)]

    for arrangement in choices:
        pairs: list[tuple[int, int]] = []
        scores: list[int] = []
        for short_idx, long_idx in enumerate(arrangement):
            if short_on_primary:
                p_idx, s_idx = short_idx, long_idx
            else:
                p_idx, s_idx = long_idx, short_idx
            pairs.append((p_idx, s_idx))
            scores.append(_score_pairing(primary_rows[p_idx], secondary_rows[s_idx], fields, merge_key)[0])
        ranking = tuple(sorted(scores, reverse=True))
        if ranking > best_score:
            best_score = ranking
            best = sorted(pairs)
    return best


def build_review(primary: SourceBundle, secondary: SourceBundle, merge_key: str,
                 rule_id: int | None = None) -> dict[str, Any]:
    """构造一份归并差异底稿（只读两侧数据，不修改任何源值）。"""
    if merge_key not in primary.columns:
        raise ValueError(f"主源导出缺少归并键列：{merge_key}")
    if merge_key not in secondary.columns:
        raise ValueError(f"从源导出缺少归并键列：{merge_key}")

    # 字段顺序：主源列在前，从源独有列追加，保证两边同键值在同一组字段下对照
    fields = list(primary.columns)
    for col in secondary.columns:
        if col not in fields:
            fields.append(col)

    primary_groups: dict[str, list[int]] = {}
    secondary_groups: dict[str, list[int]] = {}
    for idx, row in enumerate(primary.rows):
        primary_groups.setdefault(_key_of(row, merge_key), []).append(idx)
    for idx, row in enumerate(secondary.rows):
        secondary_groups.setdefault(_key_of(row, merge_key), []).append(idx)

    all_keys = sorted(set(primary_groups) | set(secondary_groups))
    key_groups: list[dict[str, Any]] = []
    counters = {ROUTE_STRAIGHT: 0, ROUTE_DIFF_CORRECTION: 0, ROUTE_PENDING: 0}

    for key in all_keys:
        p_indices = primary_groups.get(key, [])
        s_indices = secondary_groups.get(key, [])
        p_rows = [primary.rows[i] for i in p_indices]
        s_rows = [secondary.rows[i] for i in s_indices]
        pair_specs = _best_pairing(p_rows, s_rows, fields, merge_key)
        paired_primary: set[int] = set()
        paired_secondary: set[int] = set()

        pair_records: list[dict[str, Any]] = []
        for local_p, local_s in pair_specs:
            global_p = p_indices[local_p]
            global_s = s_indices[local_s]
            paired_primary.add(global_p)
            paired_secondary.add(global_s)
            cells = _compare_fields(primary.rows[global_p], secondary.rows[global_s], fields, merge_key)
            diff_cells = [c for c in cells if c["verdict"] in (VERDICT_DIFFERENT, VERDICT_MISSING)]
            equal_count = sum(1 for c in cells if c["verdict"] == VERDICT_EQUAL)
            if not diff_cells:
                row_status = ROW_STATUS_EQUAL
                route = ROUTE_STRAIGHT
            else:
                row_status = ROW_STATUS_PARTIAL
                route = ROUTE_DIFF_CORRECTION
            counters[route] += 1
            pair_records.append({
                "pair_id": f"{key}#P{global_p + 1}-S{global_s + 1}",
                "merge_key_value": key,
                "row_status": row_status,
                "route": route,
                "equal_field_count": equal_count,
                "diff_field_count": len(diff_cells),
                "primary_ref": primary.row_ref(global_p),
                "secondary_ref": secondary.row_ref(global_s),
                "cells": cells,
            })

        pending_records: list[dict[str, Any]] = []
        for global_p in p_indices:
            if global_p in paired_primary:
                continue
            pending_records.append(_pending_record("primary", key, primary, global_p, fields, merge_key, counters))
        for global_s in s_indices:
            if global_s in paired_secondary:
                continue
            pending_records.append(_pending_record("secondary", key, secondary, global_s, fields, merge_key, counters))

        key_groups.append({
            "merge_key": merge_key,
            "merge_key_value": key,
            "primary_row_count": len(p_indices),
            "secondary_row_count": len(s_indices),
            "pairs": pair_records,
            "pending_rows": pending_records,
        })

    summary = {
        "key_count": len(all_keys),
        "pair_count": sum(len(group["pairs"]) for group in key_groups),
        "pending_count": sum(len(group["pending_rows"]) for group in key_groups),
        "straight_count": counters[ROUTE_STRAIGHT],
        "diff_correction_count": counters[ROUTE_DIFF_CORRECTION],
        "route_counts": dict(counters),
        "open_diff_cell_count": _count_open_cells(key_groups),
    }
    return {
        "schema_version": 1,
        "rule_id": rule_id,
        "merge_key": merge_key,
        "fields": fields,
        "primary_source": _source_meta(primary),
        "secondary_source": _source_meta(secondary),
        "key_groups": key_groups,
        "summary": summary,
    }


def _pending_record(side: str, key: str, bundle: SourceBundle, row_index: int,
                    fields: list[str], merge_key: str, counters: dict[str, int]) -> dict[str, Any]:
    """构造一条配不上对、必须等待人工确认的原行记录（值仍逐字段留痕）。"""
    row = bundle.rows[row_index]
    cells = []
    for field_name in fields:
        value = row.get(field_name)
        if field_name == merge_key:
            verdict = VERDICT_EQUAL
        elif value is None:
            verdict = VERDICT_MISSING
        else:
            verdict = VERDICT_DIFFERENT  # 对侧无配对行，该值无法被对侧印证
        cells.append({
            "field": field_name,
            "is_merge_key": field_name == merge_key,
            "primary_value": value if side == "primary" else None,
            "secondary_value": value if side == "secondary" else None,
            "verdict": verdict,
            "resolution": RESOLUTION_OPEN,
            "resolved_value": None,
            "manual_value": None,
        })
    counters[ROUTE_PENDING] += 1
    return {
        "pending_id": f"{key}#{side[0].upper()}{row_index + 1}",
        "side": side,
        "merge_key_value": key,
        "row_status": ROW_STATUS_PENDING,
        "route": ROUTE_PENDING,
        "source_ref": bundle.row_ref(row_index),
        "cells": cells,
        "handling": RESOLUTION_OPEN,   # 待确认：补配对 / 放弃该行
    }


def _source_meta(bundle: SourceBundle) -> dict[str, Any]:
    return {
        "side": bundle.side,
        "export_id": bundle.export_id,
        "batch_no": bundle.batch_no,
        "file_version": bundle.file_version,
        "file_path": bundle.file_path,
        "sha256": bundle.sha256,
        "api_name": bundle.api_name,
        "columns": bundle.columns,
        "row_count": len(bundle.rows),
    }


def _count_open_cells(key_groups: list[dict[str, Any]]) -> int:
    total = 0
    for group in key_groups:
        for pair in group["pairs"]:
            total += sum(1 for cell in pair["cells"] if cell["resolution"] == RESOLUTION_OPEN)
        for pending in group["pending_rows"]:
            total += sum(1 for cell in pending["cells"] if cell["resolution"] == RESOLUTION_OPEN)
    return total


def review_progress(review: dict[str, Any]) -> dict[str, Any]:
    """统计底稿复核进度，供确认前校验是否还有未裁定项。"""
    open_pairs: list[str] = []
    open_pending: list[str] = []
    for group in review["key_groups"]:
        for pair in group["pairs"]:
            if any(cell["resolution"] == RESOLUTION_OPEN for cell in pair["cells"]):
                open_pairs.append(pair["pair_id"])
        for pending in group["pending_rows"]:
            if pending["handling"] == RESOLUTION_OPEN:
                open_pending.append(pending["pending_id"])
    return {
        "open_pair_ids": open_pairs,
        "open_pending_ids": open_pending,
        "can_confirm": not open_pairs and not open_pending,
    }


def fingerprint_pair(primary_meta: dict[str, Any], secondary_meta: dict[str, Any]) -> str:
    """一对输入的身份指纹：任一侧导出换了文件，指纹就不同。"""
    import hashlib
    import json

    payload = {
        "primary": {"export_id": primary_meta["export_id"], "sha256": primary_meta["sha256"]},
        "secondary": {"export_id": secondary_meta["export_id"], "sha256": secondary_meta["sha256"]},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
