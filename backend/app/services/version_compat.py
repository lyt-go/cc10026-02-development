"""版本兼容领域服务：管理API版本的字段映射、过渡和废弃管理。

跨模块数据流：
- 消费 api_registry 快照 → API版本变更触发兼容检查
- 被 sdk_config 消费 → SDK生成需要版本兼容信息
- 过渡截止后 → 写共享快照通知旧版本淘汰
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class VersionCompatService(BaseModuleService):
    MODULE = "version_compat"
    ENTITY = "版本映射"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["映射编号", "关联API", "旧版本", "新版本", "字段重命名", "默认值填充", "过渡截止", "兼容状态"]
    REQUIRED_FIELDS = ["映射编号", "关联API", "旧版本"]
    STATUS_ORDER = ["草稿", "过渡中", "已完成", "已过期"]
    ACTION_RULES = {"启动过渡": "过渡中", "确认完成": "已完成", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def get_compat_mapping(self, api_name: str, old_version: str) -> dict[str, Any]:
        """获取旧版本到新版本的字段映射。"""
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("关联API") == api_name and row.get("旧版本") == old_version:
                return {
                    "mapping_no": row.get("映射编号"),
                    "old_version": row.get("旧版本"),
                    "new_version": row.get("新版本"),
                    "field_rename": row.get("字段重命名"),
                    "default_fill": row.get("默认值填充"),
                    "transition_deadline": row.get("过渡截止"),
                    "status": row.get("status"),
                }
        return {}

    def update_sdk_config(self, api_name: str, new_version: str) -> None:
        """版本变更后更新SDK配置。"""
        from app.services import service_for
        sdk_svc = service_for("sdk_config")
        if sdk_svc:
            rows = store.rows("sdk_config")
            for row in rows:
                if row.get("关联API") == api_name:
                    row["配置版本"] = new_version
                    break

    def disable_old_version_subscriptions(self, api_name: str, old_version: str) -> int:
        """过渡期间禁用旧版本订阅，防止订阅方继续使用旧版本API。

        联动增强：扫描 subscription 表，停用订阅旧版本API的申请。
        返回停用数量。
        """
        from app.store import store as s
        sub_rows = s.rows("subscription")
        disabled_count = 0
        for row in sub_rows:
            if row.get("订阅API") == api_name and row.get("status") in ("已通过", "待审批"):
                # 检查是否使用了旧版本
                if old_version in (row.get("API版本", ""), row.get("数据范围", "")):
                    row["status"] = "已驳回"
                    row["驳回原因"] = f"旧版本 {old_version} 已进入过渡期，请迁移至新版本"
                    row["abnormal"] = True
                    row["pending"] = False
                    disabled_count += 1
        return disabled_count

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "启动过渡":
            # 联动增强：过渡期间禁用旧版本订阅
            old_version = entry.get("旧版本", "")
            api_name = entry.get("关联API", "")
            if old_version and api_name:
                disabled = self.disable_old_version_subscriptions(api_name, old_version)
                entry["_disabled_subscriptions"] = disabled
            # 同时更新SDK配置为新版本
            self.update_sdk_config(api_name, entry.get("新版本", ""))

        if action == "结束过渡":
            # 过渡结束自动更新SDK配置
            self.update_sdk_config(entry.get("关联API", ""), entry.get("新版本", ""))
            new_action = "确认完成"
            result = super().run_action(entry_id, new_action)

            from app.services.data_manager import write_module_snapshot
            write_module_snapshot(self.MODULE, self.domain_snapshot())
            return result

        result = super().run_action(entry_id, action)

        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def domain_snapshot(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        compat_list = []
        for row in rows:
            compat_list.append({
                "id": row.get("id"),
                "mapping_no": row.get("映射编号"),
                "api": row.get("关联API"),
                "old_version": row.get("旧版本"),
                "new_version": row.get("新版本"),
                "field_rename": row.get("字段重命名"),
                "default_fill": row.get("默认值填充"),
                "deadline": row.get("过渡截止"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(compat_list),
            "compat_mappings": compat_list,
            "transitioning_count": sum(1 for r in compat_list if r.get("status") == "过渡中"),
        }