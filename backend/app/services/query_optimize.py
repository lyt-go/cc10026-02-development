"""查询优化领域服务：管理查询优化规则、索引建议和预聚合策略。

跨模块数据流：
- 消费 api_registry 快照 → 关联API加载优化规则
- 消费 cache_layer 快照 → 缓存命中率影响优化策略
- 消费 query_route 快照 → 路由权重影响并行策略
- 优化结果 → 写共享快照供 batch_query 消费
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class QueryOptimizeService(BaseModuleService):
    MODULE = "query_optimize"
    ENTITY = "优化规则"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["规则编号", "关联API", "索引建议", "分区裁剪", "预聚合", "并行策略", "优化收益", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "关联API", "索引建议"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def get_optimization_plan(self, api_name: str) -> dict[str, Any]:
        """获取指定API的优化计划。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("关联API") == api_name and row.get("status") == "已生效":
                return {
                    "rule_no": row.get("规则编号"),
                    "index_hint": row.get("索引建议"),
                    "partition_prune": row.get("分区裁剪"),
                    "pre_aggregation": row.get("预聚合"),
                    "parallel_strategy": row.get("并行策略"),
                    "estimated_benefit": row.get("优化收益"),
                }
        return {}

    def estimate_improvement(self, api_name: str) -> float:
        """估算优化收益百分比。"""
        plan = self.get_optimization_plan(api_name)
        if not plan:
            return 0.0

        benefit_str = plan.get("estimated_benefit", "0%")
        try:
            return float(benefit_str.replace("%", "")) / 100.0
        except (ValueError, AttributeError):
            return 0.0

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "标记生效":
            from app.services.data_manager import read_api_snapshot
            api_name = entry.get("关联API", "")
            registry = read_api_snapshot()
            for api_data in (registry.values() if isinstance(registry, dict) else []):
                if isinstance(api_data, dict) and api_data.get("api_name") == api_name:
                    break
            else:
                return None, "关联API尚未注册，无法标记生效优化规则"

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        opt_list = []
        for row in rows:
            opt_list.append({
                "id": row.get("id"),
                "rule_no": row.get("规则编号"),
                "api": row.get("关联API"),
                "index_hint": row.get("索引建议"),
                "partition": row.get("分区裁剪"),
                "pre_agg": row.get("预聚合"),
                "parallel": row.get("并行策略"),
                "benefit": row.get("优化收益"),
                "version": row.get("规则版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(opt_list),
            "optimizations": opt_list,
            "active_count": sum(1 for r in opt_list if r.get("status") == "已生效"),
        }