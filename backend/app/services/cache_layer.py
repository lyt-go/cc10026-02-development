"""缓存分层领域服务：管理数据API缓存策略、TTL和预热配置。

跨模块数据流：
- 消费 api_registry 快照 → 关联API获取缓存策略
- 被 query_route 消费 → 路由规则与缓存层级联动
- 被 query_optimize 消费 → 优化规则参考缓存命中率
- 缓存失效 → 触发过期淘汰和预热
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CacheLayerService(BaseModuleService):
    MODULE = "cache_layer"
    ENTITY = "缓存策略"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["策略编号", "关联API", "缓存层级", "TTL秒数", "最大条目", "淘汰算法", "预热策略", "缓存版本"]
    REQUIRED_FIELDS = ["策略编号", "关联API", "缓存层级"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def get_cache_config(self, api_name: str) -> dict[str, Any]:
        """获取指定API的缓存配置，供其他模块消费。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("关联API") == api_name and row.get("status") == "已生效":
                return {
                    "policy_no": row.get("策略编号"),
                    "tier": row.get("缓存层级"),
                    "ttl_seconds": self._safe_int(row.get("TTL秒数")),
                    "max_entries": self._safe_int(row.get("最大条目")),
                    "eviction_algorithm": row.get("淘汰算法"),
                    "warmup_policy": row.get("预热策略"),
                    "version": row.get("缓存版本"),
                }
        return {}

    def evict_expired(self, api_name: str) -> int:
        """淘汰指定API的过期缓存条目，返回淘汰数量。"""
        config = self.get_cache_config(api_name)
        if not config:
            return 0
        ttl = config.get("ttl_seconds", 0)
        if ttl <= 0:
            return 0
        # 读取当前缓存层快照
        from app.services.data_manager import read_module_snapshot
        snap = read_module_snapshot("cache_layer")
        cached = snap.get("caches", [])
        expired = [c for c in cached if c.get("api") == api_name and c.get("age", 0) > ttl]
        # 写入淘汰日志
        from app.services.data_manager import write_module_snapshot
        remaining = [c for c in cached if c not in expired]
        snap["caches"] = remaining
        snap["_evicted_at"] = self._now_iso()
        snap["_evicted_count"] = len(expired)
        write_module_snapshot(self.MODULE, snap)
        return len(expired)

    def warmup_cache(self, api_name: str, entry_count: int) -> dict[str, Any]:
        """预热缓存：批量查询完成后将热点数据写入缓存层。

        跨模块：供 batch_query 调用，在批量查询完成后自动预热。
        返回预热结果。
        """
        config = self.get_cache_config(api_name)
        if not config:
            return {"ok": False, "message": "无生效缓存策略"}

        from app.services.data_manager import read_module_snapshot, write_module_snapshot
        snap = read_module_snapshot("cache_layer")
        if not snap:
            snap = {"module": self.MODULE, "caches": []}

        warmup_entry = {
            "api": api_name,
            "tier": config.get("tier", "L1"),
            "ttl": config.get("ttl_seconds", 300),
            "entries": entry_count,
            "warmup_policy": config.get("warmup_policy", "按需加载"),
            "created_at": self._now_iso(),
            "age": 0,
        }
        snap["caches"].append(warmup_entry)
        snap["_warmup_at"] = self._now_iso()
        snap["_warmup_entries"] = entry_count
        write_module_snapshot(self.MODULE, snap)
        return {"ok": True, "api": api_name, "entries": entry_count}

    def _now_iso(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).isoformat()

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "标记生效":
            # 预写缓存快照
            from app.services.data_manager import read_module_snapshot, write_module_snapshot
            snap = read_module_snapshot("cache_layer")
            if not snap:
                snap = {"module": self.MODULE, "caches": []}
            snap["caches"].append({
                "api": entry.get("关联API"),
                "tier": entry.get("缓存层级"),
                "ttl": entry.get("TTL秒数"),
                "created_at": self._now_iso(),
            })
            write_module_snapshot(self.MODULE, snap)

        result = super().run_action(entry_id, action)
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        cache_list = []
        for row in rows:
            cache_list.append({
                "id": row.get("id"),
                "policy_no": row.get("策略编号"),
                "api": row.get("关联API"),
                "tier": row.get("缓存层级"),
                "ttl": row.get("TTL秒数"),
                "max_entries": row.get("最大条目"),
                "eviction": row.get("淘汰算法"),
                "warmup": row.get("预热策略"),
                "version": row.get("缓存版本"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(cache_list),
            "caches": cache_list,
            "active_count": sum(1 for r in cache_list if r.get("status") == "已生效"),
        }