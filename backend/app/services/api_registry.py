"""API注册领域服务：管理数据API的注册、版本控制和生命周期。

跨模块数据流：
- 注册后写共享快照 → query_route 消费路由规则
- 注册后写共享快照 → cache_layer 消费缓存策略
- 注册后写共享快照 → subscription 消费订阅审批
- 注册后写共享快照 → rate_limiter 消费频率限制
- 变更触发 version_compat 版本过渡检查
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ApiRegistryService(BaseModuleService):
    MODULE = "api_registry"
    ENTITY = "数据API"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["API编号", "API名称", "接口路径", "请求方法", "数据源", "返回格式", "缓存策略", "API版本"]
    REQUIRED_FIELDS = ["API编号", "API名称", "接口路径"]
    STATUS_ORDER = ["草稿", "待上线", "已上线", "已下线"]
    ACTION_RULES = {"提交上线": "待上线", "确认上线": "已上线", "标记下线": "已下线"}
    NEGATIVE_ACTIONS = []

    def _publish_to_shared(self, api_id: int, api_data: dict[str, Any]) -> None:
        """将API信息发布到共享数据目录，供下游模块消费。
        
        联动增强：API上线同时自动创建默认路由规则、缓存策略和SLA监控指标。
        """
        from app.services.data_manager import write_api_snapshot
        from app.store import store as s

        api_name = api_data.get("API名称", "")
        api_version = api_data.get("API版本", "v1")

        publish_data = {
            "api_id": api_id,
            "api_no": api_data.get("API编号", ""),
            "api_name": api_name,
            "endpoint": api_data.get("接口路径", ""),
            "method": api_data.get("请求方法", ""),
            "data_source": api_data.get("数据源", ""),
            "response_format": api_data.get("返回格式", ""),
            "cache_policy": api_data.get("缓存策略", ""),
            "api_version": api_version,
            "status": api_data.get("status", ""),
        }
        write_api_snapshot(api_id, publish_data)

        # 联动增强：自动创建默认路由规则
        route_rows = s.rows("query_route")
        route_id = max((int(r.get("id", 0)) for r in route_rows), default=0) + 1
        route_rows.append({
            "id": route_id,
            "规则编号": f"ROUTE-{route_id:04d}",
            "关联API": api_name,
            "路由条件": "default",
            "目标数据源": api_data.get("数据源", "default"),
            "优先权重": "100",
            "降级策略": "直连",
            "超时阈值": "30000",
            "规则版本": api_version,
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "_auto_created": True,
        })

        # 联动增强：自动创建默认缓存策略
        cache_rows = s.rows("cache_layer")
        cache_id = max((int(r.get("id", 0)) for r in cache_rows), default=0) + 1
        cache_rows.append({
            "id": cache_id,
            "策略编号": f"CACHE-{cache_id:04d}",
            "关联API": api_name,
            "缓存层级": "L1",
            "TTL秒数": "300",
            "最大条目": "10000",
            "淘汰算法": "LRU",
            "预热策略": "按需加载",
            "缓存版本": api_version,
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "_auto_created": True,
        })

        # 联动增强：自动创建默认SLA监控指标
        sla_rows = s.rows("sla_monitor")
        sla_id = max((int(r.get("id", 0)) for r in sla_rows), default=0) + 1
        sla_rows.append({
            "id": sla_id,
            "指标编号": f"SLA-{sla_id:04d}",
            "关联API": api_name,
            "可用率目标": "99.9",
            "P99延迟": "200",
            "错误率上限": "1.0",
            "监控周期": "60",
            "当前状态": "正常",
            "最近告警": "",
            "status": "监控中",
            "pending": False,
            "abnormal": False,
            "_auto_created": True,
        })

    def _unpublish_from_shared(self, api_id: int) -> None:
        """标记API为下线，通知下游。"""
        from app.services.data_manager import remove_api_snapshot
        remove_api_snapshot(api_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "确认上线":
            self._publish_to_shared(entry_id, entry)
        elif action == "标记下线":
            self._unpublish_from_shared(entry_id)

        # 写模块快照给下游版本兼容检查
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())

        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        """输出API注册全量数据集供跨模块消费。"""
        rows = store.rows(self.MODULE)
        api_list = []
        for row in rows:
            api_list.append({
                "id": row.get("id"),
                "api_no": row.get("API编号"),
                "api_name": row.get("API名称"),
                "endpoint": row.get("接口路径"),
                "data_source": row.get("数据源"),
                "api_version": row.get("API版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(api_list),
            "apis": api_list,
            "by_status": {
                status: sum(1 for r in api_list if r.get("status") == status)
                for status in self.STATUS_ORDER
            },
            "online_count": sum(1 for r in api_list if r.get("status") == "已上线"),
        }