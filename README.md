# 数据服务开放与订阅管理平台

面向数据API注册、查询路由、订阅审批、用量计量与SLA保障的企业数据服务开放平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/MergeWorksheetView.vue  归并差异底稿复核视图
│   ├── src/api/merge-worksheet.ts        底稿接口封装与类型
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/merge_worksheet.py  归并差异底稿接口（复核/确认/发布/回溯）
│   ├── app/services/merge_review.py    差异底稿比对引擎（纯函数、只读源值）
│   ├── app/services/merge_worksheet.py 底稿生命周期与确认后归并发布
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 归并差异底稿（复核后再归并）

旧链路提交归并即直接套用冲突策略，没有可复核的数据。现在两侧导出先固化成
**归并差异底稿**，复核完成并确认后才允许重做归并、发布明细：

1. `POST /api/merge-worksheets` 选两份导出与归并键建底稿。建底稿时记录两侧文件的
   SHA-256 与输入对指纹；同键多行按字段吻合度最高的方式一一配对，配不上的原行进
   「待确认原行」队列。
2. 底稿逐字段给出「相同值 / 差异值 / 缺失值」结论，每个单元格都带原行位置
   （批次号、文件版本、CSV 行号、文件指纹）。比对与复核全程只读 `data/exports/`，
   不覆盖任何源值。
3. 冲突策略只做**预填建议**（`/apply-policy`），差异单元格可逐格改判取主源、取从源
   或人工修正值；待确认原行须逐条选择手工保留或放弃。未处置完不能确认。
4. 换任一侧导出后重开底稿，`reopen_check` 会用当前文件指纹与底稿指纹比对，识别
   输入对漂移并阻止确认/发布；台账中不同的一对输入有不同指纹可区分。
5. `POST /{id}/publish` 按确认后的底稿选出那两份导出重做归并，发布明细带
   「归并底稿号、输入对指纹、纠错处理、两侧批次/文件版本/原行行号」溯源列，
   写入 `data/merge_published/`；`GET /published/{id}/verify` 可回溯核对这对输入。
6. `DataMergeService.merge_from_confirmed_worksheet()` 是服务层入口，发布同时写
   `call_audit` 审计，保留导出→归并→审计链路。

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API注册 | `api_registry` | 数据API | API编号、API名称、接口路径 |
| 查询路由 | `query_route` | 路由规则 | 规则编号、关联API、路由条件 |
| 订阅管理 | `subscription` | 订阅申请 | 申请编号、订阅API、申请方 |
| 频率限制 | `rate_limiter` | 限流策略 | 策略编号、目标API、调用方 |
| 用量计量 | `usage_billing` | 用量记录 | 记录编号、调用方、API名称 |
| 缓存分层 | `cache_layer` | 缓存策略 | 策略编号、关联API、缓存层级 |
| 数据过滤 | `data_filter` | 过滤规则 | 规则编号、关联API、过滤字段 |
| 字段脱敏 | `field_mask` | 脱敏规则 | 规则编号、目标字段、脱敏算法 |
| SLA监控 | `sla_monitor` | SLA指标 | 指标编号、关联API、可用率目标 |
| 结果格式化 | `result_format` | 格式模板 | 模板编号、关联API、输出格式 |
| 查询优化 | `query_optimize` | 优化规则 | 规则编号、关联API、索引建议 |
| 异常归类 | `error_classify` | 异常类型 | 类型编号、类型名称、错误码段 |
| 数据归并 | `data_merge` | 归并规则 | 规则编号、主API、从API |
| 导出任务 | `export_task` | 导出任务 | 任务编号、关联API、查询参数 |
| 调用审计 | `call_audit` | 调用记录 | 记录编号、调用方、目标API |
| 版本兼容 | `version_compat` | 版本映射 | 映射编号、关联API、旧版本 |
| 批量查询 | `batch_query` | 批量任务 | 任务编号、关联API、查询清单 |
| SDK配置 | `sdk_config` | SDK配置 | 配置编号、关联API、SDK语言 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
