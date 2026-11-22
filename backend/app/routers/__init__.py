"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import merge_worksheet, resource

# 归并底稿是固定前缀接口，必须排在通用 /api/{module} 动态路由之前注册，
# 否则 merge-worksheets 会被当成模块名抢走匹配。
ROUTERS = [merge_worksheet, resource]
