# NL-to-SQL 管线优化文档

> 版本：v1.0 | 日期：2026-04-29 | 作者：AI 协助生成

---

## 1. 优化背景

系统原有的自然语言→数据库查询→图表可视化管线存在以下效率瓶颈：

| 阶段 | 原有调用 | 问题 |
|------|---------|------|
| 意图分析 | IntentAgent LLM ×1 | — |
| SQL 生成 | DataVisualizerTool LLM ×1 | 与意图分析重复理解用户需求 |
| SQL 修复 | LLM ×0-2 | 必要开销，保留 |
| 图表决策 | LLM ×1 | 简单场景可用规则推断 |
| 数据分析摘要 | LLM ×1 | 与图表决策可合并 |
| 最终回复生成 | TaskExecutor LLM ×1 | 工具已有高质量摘要时属冗余 |
| **总计** | **5-7 次 LLM 调用** | |

单次可视化请求平均耗时约 15-25 秒，其中 LLM 调用占 80% 以上。

---

## 2. 优化方案总览

实施了 4 项互相独立、均带兜底的优化措施：

```
优化前（5-7 次 LLM 调用）：
  IntentAgent → [SQL 生成] → [SQL 修复 ×0-2] → [图表决策] → [数据分析] → [回复生成]

优化后（1-3 次 LLM 调用）：
  IntentAgent(含 SQL + chart_type) → [SQL 修复 ×0-1] → [规则推断 or 合并决策+分析] → [透传回复]
```

---

## 3. 详细实现

### 3.1 Schema 缓存集中管理

**文件**：`backend/tools/schema_manager.py`（新建）

**设计**：
- 进程级单例（`SchemaManager.instance()`），线程安全
- 首次访问时从 PostgreSQL `information_schema` 加载全部公共表结构
- 提供统一接口：`get_formatted_schema()`、`get_columns()`、`get_lower_map()`、`get_column_cache()`
- 支持手动刷新：`refresh()`
- DB 不可用时回退到本地 `config/db_schema.json` 文件缓存

**影响的模块**：
- `PostgreSQLTool._load_schema_cache()` → 委托给 SchemaManager
- `PostgreSQLTool._get_db_schema()` → 委托给 SchemaManager
- `DataVisualizerTool._async_call()` → 从 SchemaManager 获取 schema 文本
- `IntentAgent._inject_schema_once()` → 将 schema 注入系统提示词

**关键代码**：

```python
class SchemaManager:
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls, db_cfg=None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db_cfg or {})
        return cls._instance
```

### 3.2 合并 SQL 生成路径

**文件**：`backend/agents/intent_agent.py`、`backend/tools/data_visualizer_tool.py`

**原流程**：
1. IntentAgent 分析意图，输出 `{demand: "..."}`
2. DataVisualizerTool 内部再调 LLM 生成 SQL

**优化后**：
1. IntentAgent 的系统提示词注入完整 DB schema（首次调用时从 SchemaManager 获取）
2. IntentAgent 在 execution_plan 的 params 中同时输出 `sql` 和 `chart_type`
3. DataVisualizerTool 接收到 `sql` 参数后直接执行，跳过 SQL 生成 LLM 调用

**参数约束变更**：

```
# 原有
data_visualizer_tool: {"demand": "完整制图需求"}

# 优化后
data_visualizer_tool: {
    "demand": "完整制图需求",       # 必填
    "sql": "SELECT ...",           # 可选，强烈推荐
    "chart_type": "bar"            # 可选
}
```

**兜底机制**：若 IntentAgent 未输出 `sql`，DataVisualizerTool 回退到原有 LLM 生成流程。

**节省**：1 次 LLM 调用（`_generate_sql`）。

### 3.3 合并图表决策与数据分析

**文件**：`backend/tools/data_visualizer_tool.py`

**新增方法**：

| 方法 | 用途 |
|------|------|
| `_infer_chart_type_from_data(data, demand)` | 规则推断图表类型，零 LLM |
| `_rule_based_chart_data(data, chart_type)` | 规则推断数据映射，零 LLM |
| `_decide_chart_and_analyze(demand, data)` | 合并图表决策 + 数据分析为 1 次 LLM |

**规则推断逻辑**：

| 条件 | 推断结果 |
|------|---------|
| 用户需求含"折线/折线图" | `line` |
| 用户需求含"柱状/柱状图" | `bar` |
| 用户需求含"饼图/饼状图" | `pie` |
| 用户需求含"占比/比例/构成" | `pie` |
| 用户需求含"趋势/变化/时间" | `line` |
| 多行数据 + 有分类字段 + 有数值字段 | `bar` |
| IntentAgent 传入 `chart_type` | 直接采用 |

**数据映射规则**：
- `bar`/`line`：第一个字符串字段 → category，第一个数值字段 → value
- `pie`：第一个字符串字段 → name，第一个数值字段 → value

**优化后流程**：
```
1. 尝试规则推断 chart_type + data 映射
   ├── 成功 → 零 LLM 调用，直接构建 ECharts 配置
   └── 失败 → 调用 _decide_chart_and_analyze()（1 次 LLM，替代原来的 2 次）
```

**节省**：1-2 次 LLM 调用（`_decide_chart_config` + `_generate_data_analysis`）。

### 3.4 减少 response 冗余 LLM 调用

**文件**：`backend/agents/task_executor.py`

**新增方法**：`_try_extract_rich_response(tool_results)`

**判定逻辑**：
- `data_visualizer_tool` 返回 `content` 字段且长度 > 50 字 → 直接采用
- `report_generator_tool` 成功 → 拼接消息 + 下载链接
- 当 `data_visualizer_tool` 已提供完整分析时，`postgresql_tool` 结果也视为已覆盖
- 仅当所有工具结果均被覆盖时才跳过 LLM，否则回退到原有汇总流程

**节省**：1 次 LLM 调用（`_generate_response`）。

---

## 4. 优化效果

### 4.1 LLM 调用次数对比

| 场景 | 优化前 | 优化后 | 节省 |
|------|--------|--------|------|
| 数据可视化（最佳） | 5 次 | **1 次** | 80% |
| 数据可视化（规则推断失败） | 5 次 | **2 次** | 60% |
| 数据可视化（SQL 需修复） | 6-7 次 | **2-3 次** | 57-67% |
| 纯数据查询 | 2 次 | 2 次 | 不变 |
| 报告生成 | 4+ 次 | 3+ 次 | 25% |

### 4.2 实测日志验证

```
# Schema 集中加载
[SchemaManager] Loaded schema from DB: 8 tables, 45 columns

# SQL 由 IntentAgent 预生成
DataVisualizerTool called with demand: ..., pre_sql=True, pre_chart_type=bar
[DataVisualizerTool] Using pre-generated SQL from IntentAgent: SELECT ...

# 规则推断成功，零 LLM 图表调用
[DataVisualizerTool] Rule-based chart inference: type=pie, data_points=1

# 跳过 response LLM
[response_node] Skipping LLM response generation — using rich tool content directly
```

---

## 5. 兜底与兼容性

所有优化均设计了完整的回退路径：

| 优化 | 失败场景 | 兜底行为 |
|------|---------|---------|
| Schema 集中缓存 | DB 不可达 | 回退到本地 JSON 文件 |
| SQL 预生成 | IntentAgent 未输出 sql | DataVisualizerTool 内部 LLM 生成 |
| 规则推断图表 | 数据结构复杂 | 合并 LLM 调用决策 |
| Response 透传 | 有未覆盖工具结果 | 回退到 LLM 汇总 |

**接口兼容性**：
- 前端零改动，所有返回格式不变
- `postgresql_tool.call()` 接口完全不变
- `data_visualizer_tool` 新增的 `sql` 和 `chart_type` 参数为可选

---

## 6. 文件变更清单

| 文件 | 操作 | 变更摘要 |
|------|------|---------|
| `tools/schema_manager.py` | **新建** | 进程级 Schema 单例缓存 |
| `tools/postgresql_tool.py` | 修改 | `_load_schema_cache` / `_get_db_schema` 委托给 SchemaManager |
| `tools/data_visualizer_tool.py` | 修改 | 接受预生成 SQL/chart_type；规则推断；合并 LLM 调用 |
| `agents/intent_agent.py` | 修改 | 注入 DB schema；参数约束更新 |
| `agents/task_executor.py` | 修改 | `_try_extract_rich_response` 透传逻辑 |

---

## 7. 架构图

```
                          ┌─────────────────────┐
                          │    SchemaManager     │  ← 进程级单例
                          │  (schema_manager.py) │
                          └──┬──────┬──────┬─────┘
                             │      │      │
              ┌──────────────┘      │      └──────────────┐
              ▼                     ▼                      ▼
     ┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐
     │  IntentAgent     │  │ PostgreSQLTool   │  │DataVisualizerTool│
     │  (intent_agent)  │  │ (postgresql_tool) │  │(data_visualizer) │
     └────────┬─────────┘  └──────────────────┘  └────────┬─────────┘
              │                                            │
              │  sql + chart_type                          │
              └──────────────►─────────────────────────────┘
                                                           │
                                                           ▼
                                               ┌─────────────────────┐
                                               │ 规则推断 chart_type  │
                                               │ + data 映射          │
                                               ├─────────────────────┤
                                               │ 成功 → 零 LLM       │
                                               │ 失败 → 合并 1 次 LLM│
                                               └─────────┬───────────┘
                                                         │
                                                         ▼
                                               ┌─────────────────────┐
                                               │  TaskExecutor       │
                                               │  _response_node     │
                                               ├─────────────────────┤
                                               │ 有 rich content     │
                                               │  → 直接透传         │
                                               │ 否则 → LLM 汇总    │
                                               └─────────────────────┘
```
