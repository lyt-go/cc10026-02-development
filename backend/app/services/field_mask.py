"""字段脱敏领域服务：管理敏感字段的脱敏规则和掩码算法。

跨模块数据流：
- 消费 api_registry 快照 → 关联API自动加载脱敏规则
- 消费 data_filter 快照 → 过滤后的字段进入脱敏流程
- 被 result_format 消费 → 脱敏后的字段进入格式化
- 脱敏规则生效/失效 → 写共享快照通知下游
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FieldMaskService(BaseModuleService):
    MODULE = "field_mask"
    ENTITY = "脱敏规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "目标字段", "脱敏算法", "脱敏范围", "保留前缀", "保留后缀", "例外条件", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "目标字段", "脱敏算法"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def mask_fields(self, api_name: str, data_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """对指定数据行的敏感字段应用脱敏。"""
        rows = store.rows(self.MODULE)
        active_rules = [r for r in rows if r.get("status") == "已生效"]

        for row in data_rows:
            for rule in active_rules:
                field = rule.get("目标字段", "")
                algo = rule.get("脱敏算法", "")
                if not field or not algo:
                    continue
                prefix_n = self._safe_int(rule.get("保留前缀", "0"))
                suffix_n = self._safe_int(rule.get("保留后缀", "0"))

                if field in row and row[field] is not None:
                    raw_value = str(row[field])

                    if "例外条件" in rule and rule["例外条件"]:
                        exception_keyword = rule["例外条件"]
                        if exception_keyword.casefold() in raw_value.casefold():
                            continue

                    if algo == "全掩码":
                        row[field] = "*" * len(raw_value)
                    elif algo == "前缀保留":
                        if len(raw_value) > prefix_n:
                            row[field] = raw_value[:prefix_n] + "*" * (len(raw_value) - prefix_n)
                    elif algo == "前后保留":
                        if len(raw_value) > prefix_n + suffix_n:
                            row[field] = raw_value[:prefix_n] + "*" * (len(raw_value) - prefix_n - suffix_n) + raw_value[-suffix_n:]
                    elif algo == "哈希脱敏":
                        import hashlib
                        row[field] = hashlib.sha256(raw_value.encode()).hexdigest()[:16]

        return data_rows

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        mask_list = []
        for row in rows:
            mask_list.append({
                "id": row.get("id"),
                "rule_no": row.get("规则编号"),
                "field": row.get("目标字段"),
                "algo": row.get("脱敏算法"),
                "scope": row.get("脱敏范围"),
                "prefix": row.get("保留前缀"),
                "suffix": row.get("保留后缀"),
                "except": row.get("例外条件"),
                "version": row.get("规则版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(mask_list),
            "masks": mask_list,
            "active_count": sum(1 for r in mask_list if r.get("status") == "已生效"),
        }