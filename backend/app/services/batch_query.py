"""批量查询领域服务：管理批量API查询任务的排队、执行和结果汇总。

跨模块数据流：
- 消费 api_registry 快照 → 批量查询API合法性校验
- 消费 query_optimize 快照 → 应用每个子查询的优化规则
- 消费 cache_layer 快照 → 批量查询结果缓存策略
- 批量结果 → 写入共享快照供下游消费
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class BatchQueryService(BaseModuleService):
    MODULE = "batch_query"
    ENTITY = "批量任务"
    OPERATION_TERM = "查询"
    LIST_FIELDS = ["任务编号", "关联API", "查询清单", "并发度", "失败策略", "结果汇总", "超时控制", "任务状态"]
    REQUIRED_FIELDS = ["任务编号", "关联API", "查询清单"]
    STATUS_ORDER = ["排队中", "执行中", "已完成", "部分完成"]
    ACTION_RULES = {"发起执行": "执行中", "确认完成": "已完成", "标记部分完成": "部分完成"}
    NEGATIVE_ACTIONS = []

    def execute_batch(self, batch_id: int) -> dict[str, Any]:
        """执行批量查询任务。

        跨模块增强：
        - 调用 query_optimize 获取优化计划
        - 完成后触发 cache_layer 预热
        """
        entry = store.find(self.MODULE, batch_id)
        if entry is None:
            return {"ok": False, "message": "批量任务不存在"}

        from app.services.data_manager import write_module_snapshot
        api_name = entry.get("关联API", "")
        query_list_str = entry.get("查询清单", "")
        concurrency = 1
        try:
            concurrency = int(entry.get("并发度", "1") or 1)
        except (ValueError, TypeError):
            concurrency = 1

        # 联动增强：调用 query_optimize 获取优化计划
        from app.services import service_for
        opt_svc = service_for("query_optimize")
        opt_plan = {}
        if opt_svc and hasattr(opt_svc, "get_optimization_plan"):
            opt_plan = opt_svc.get_optimization_plan(api_name)

        # 解析查询清单
        queries = [q.strip() for q in query_list_str.split(",") if q.strip()]
        results = []

        for query in queries:
            latency = 45.0 + len(query) * 2
            # 联动增强：应用优化计划的收益估算
            if opt_plan:
                benefit = 0.0
                try:
                    benefit_str = opt_plan.get("estimated_benefit", "0%")
                    benefit = float(str(benefit_str).replace("%", "")) / 100.0
                except (ValueError, AttributeError):
                    pass
                latency = latency * (1.0 - benefit)

            result = {
                "query": query,
                "api": api_name,
                "rows": 10,
                "latency_ms": round(latency, 2),
                "status": "success",
                "optimized": bool(opt_plan),
            }
            results.append(result)

        # 汇总结果
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["结果汇总"] = f"共{len(results)}个查询完成"

        write_module_snapshot(self.MODULE, {
            "batch_id": batch_id,
            "task_no": entry.get("任务编号"),
            "api": api_name,
            "query_count": len(results),
            "completed_count": sum(1 for r in results if r.get("status") == "success"),
            "optimization_plan": opt_plan.get("rule_no", "none") if opt_plan else "none",
            "results": results,
        })

        # 联动增强：触发 cache_layer 预热
        cache_svc = service_for("cache_layer")
        if cache_svc and hasattr(cache_svc, "warmup_cache"):
            cache_svc.warmup_cache(api_name, len(results))

        return {"ok": True, "batch_id": batch_id, "query_count": len(results), "results": results}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "发起执行":
            result = self.execute_batch(entry_id)
            if not result.get("ok"):
                return None, result.get("message", "批量查询失败")
            entry["status"] = "已完成"
            entry["pending"] = False
            return entry, f"批量查询完成，共{result.get('query_count', 0)}个查询"

        result = super().run_action(entry_id, action)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        batch_list = []
        for row in rows:
            batch_list.append({
                "id": row.get("id"),
                "task_no": row.get("任务编号"),
                "api": row.get("关联API"),
                "queries": row.get("查询清单"),
                "concurrency": row.get("并发度"),
                "failure_policy": row.get("失败策略"),
                "results": row.get("结果汇总"),
                "timeout": row.get("超时控制"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(batch_list),
            "batches": batch_list,
            "completed_count": sum(1 for r in batch_list if r.get("status") == "已完成"),
        }