# 单编排内核（C 档）设计方案 —— 图驱动流式与 RunEngine 收敛

> 定位：P1 方案（`ReAct子图与Critic评审设计方案.md`）§8.4-C 档的详细展开，独立里程碑。
> 前置条件：P1 全部完成且稳定运行（react_loop / critic / observe_node / critic_node 已就位）。
> 终态：`run_engine.py` 1207 行 → ≤400 行「Run 宿主」；编排语义（工具执行/回退/观察/报告/汇总）全部收敛到同一张 LangGraph 图；两条执行路径共用唯一编排内核。

---

## 1. 目标与判定

### 1.1 要解决什么

| 问题 | 现状 | C 档终态 |
|---|---|---|
| 双编排并存 | 图（task_executor + nodes）与 RunEngine 手工编排各一份，工具执行/回退/观察/报告/汇总语义重复 | 唯一编排内核 = 图；RunEngine 降级为「Run 宿主」只做生命周期 |
| 最大文件 | `run_engine.py` 1207 行，7 种职责混居 | ≤400 行（生命周期 + 规则 + 事件契约） |
| 行为漂移 | 历史上 kb_sources 透传遗漏、KB→Web 回退补全都源于双路径同步 | 编排单点修改、单点测试 |
| 新增能力成本 | 每个编排级能力（如 observe/critic）要在两条路径各实现一次 | 只改图一处，流式自动获得 |

### 1.2 为什么现在可以合（三个已核实的关键事实）

1. **SSE 早已解耦**：`/chat/stream` 的 run 跑在独立 asyncio task，chat.py 只是 `bus.subscribe` 的订阅者（L789-830 转发事件 + final 时做 map/charts 优化、持久化、事实抽取）。**只要执行侧继续向 event_bus 发同样的事件，路由层与前端零感知**——这是 C 档最大的可行性红利。
2. **图已具备基础设施**：`graph.compile(checkpointer=self._memory)`（MemorySaver，thread_id 级），非流式已跑通 checkpointer 语义；P1 完成后图已含 observe_node / critic_node，编排能力与非流式对齐。
3. **执行语义本就同构**：两边工具执行都是「普通步骤 asyncio.gather 并行 + 报告步骤串行」；两边汇总都是 `_extract_node → _summarize_node`。不存在"流式要串行"的隐性差异。

### 1.3 与 P1/A/B 档的关系

```
P1（进行中）：react_loop / critic —— 消除"观察与评审"的双份实现
A 档（并入 P1）：_invoke_step 双份收敛 —— 消除"工具执行内核"的双份实现
B 档（P1.5）：ToolCapability / 声明式 Agent —— 降低节点内散度
C 档（本方案）：编排单内核 —— 消除"编排流程"的最后一份重复
```

---

## 2. 目标架构

### 2.1 数据流对比

```mermaid
flowchart TB
    subgraph NOW[现状：两条路径两套编排]
        A1[chat.py 非流式] --> A2[TaskExecutor.execute → 图 ainvoke]
        A2 --> A3[图编排：intent/tool/extract/summarize]
        B1[chat.py 流式] --> B2[RunEngine.start → _run_plan 手工编排]
        B2 --> B3[手工：意图/步骤/React/回退/报告/汇总]
        B3 -. emit .-> B4[bus] --> B5[SSE 转发]
    end
    subgraph NEXT[C 档：单内核]
        C1[chat.py 非流式] --> C2[TaskExecutor.execute → 图 ainvoke]
        C3[chat.py 流式] --> C4[RunHost.start/resume → 图 ainvoke]
        C4 -. state._run_id .-> C5[图节点内 emit → bus] --> C6[SSE 转发]
        C2 --> C7[同一张图]
        C4 --> C7
    end
```

**本质**：把"流式"从一种**编排方式**降级为一种**观察与输出方式**——run 生命周期（宿主）与编排流程（图）正交。

### 2.2 RunEngine 现状职责三清单

| 职责类别 | 内容 | C 档归属 |
|---|---|---|
| **① 生命周期（宿主保留）** | start / resume / _guarded（timeout / PendingRequired / CancelledError / 异常 / finally） / _check_cancel / checkpoint 序列化（_checkpoint_state / _checkpoint_to_state / _merge_supplied） / pending 落库（_persist_pending） / obs（_obs_t0 / _obs_finish / _emit_final 内的 trace_update） | → 新 `RunHost`（run_engine.py 重写） |
| **② 规则函数（宿主保留）** | is_confirm_message / normalize_basemap_choice / parse_supplied_from_message / _detect_missing_params / _remaining_or_full | → 新 `run_rules.py`（独立模块，宿主与 gate 节点共用） |
| **③ 编排流程（迁移入图）** | 意图调用与 workspace 注入 / 路由（低置信、直答、工具）/ 工具步骤执行（并行 + 逐完成事件）/ ReAct 循环 / DB→KB→Web 回退链 / 报告串行（充分性检查 + 跳过事件 + 变量构建）/ 汇总（extract + summarize）/ **全部过程中的事件发射** | → 图与节点（P1/A/B 后大部分已就位） |
| **④ 被删除** | _run_plan 手工编排主体、_invoke_step / _invoke_step_bare（A 档已收敛）、_decide_web_search_fallback（图内已有）、_run_preflight / _run_postflight 包装（tool_node 已有） | → 删除 |

### 2.3 文件级变化

| 文件 | 动作 | 说明 |
|---|---|---|
| `agents/run_engine.py` | 重写 | RunEngine → RunHost：生命周期 + 事件契约 + final 组装（≤400 行） |
| `agents/run_rules.py` | 新增 | 确认词 / 底图归一化 / 补参解析 / 缺参检测 / 剩余步骤（从 run_engine 搬移，逻辑不变） |
| `agents/nodes/emitter.py` | 新增 | `EventEmitter` 协议 + `bus_emitter` / `noop_emitter` 实现 |
| `agents/nodes/gate_node.py` | 新增 | 流式中断点：确认 / 缺参检测（产出 PendingRequired） |
| `agents/nodes/tool_node.py` | 改造 | 接收 emitter；_remaining_steps/_done_steps 语义；报告串行编排（搬移自 RunEngine L431-467） |
| `agents/nodes/intent_node.py` | 改造 | 发射 intent/plan 事件；workspace 摘要从 state 读取 |
| `agents/nodes/observe_node.py` | 改造 | ReAct 轮事件改由 emitter 发射（对齐现状 react_round payload） |
| `agents/nodes/summarize_node.py` | 改造 | 发射 status/response、status/done 事件（对齐现状） |
| `agents/task_executor.py` | 改造 | 图新增 gate 节点 + 重入入口路由；execute() 注入 noop emitter（非流式零开销） |
| `routers/chat.py` | 微调 | import 更新（run_rules）；SSE 循环不变 |

---

## 3. 核心机制设计

### 3.1 事件发射注入（Emit Decoupling）

编排语义归图后，事件怎么发？——**节点内发射，发射器由 state 携带**：

```python
# agents/nodes/emitter.py
class EventEmitter:
    """节点内事件发射协议。非流式注入 NoopEmitter，流式注入 BusEmitter。"""
    async def emit(self, type_: str, stage: str, **fields) -> None: ...

class NoopEmitter(EventEmitter):
    async def emit(self, type_, stage, **fields): return  # 零开销

class BusEmitter(EventEmitter):
    def __init__(self, bus, run_id): ...
    async def emit(self, type_, stage, **fields):
        await self.bus.publish(self.run_id, RunEvent(type=type_, stage=stage, **fields))
```

- 宿主启动时把 `BusEmitter` 放入 `state["_emitter"]`（图重入后仍在 state 中，checkpoint 序列化时剔除）；
- 节点通过 `state.get("_emitter") or NoopEmitter()` 获取——**节点代码只有一份**，非流式自动静默；
- 发射点与现状逐一对应（见 §4 契约表），payload 字段完全保留。

### 3.2 pending / 确认 / 缺参的图化

现状流程（宿主手工）：意图 → 路由检查低置信 → `_detect_missing_params` → confirm → 工具执行。
图化后的流程：

```
entry 条件路由
 ├─ state["_resumed"] 为真 → gate_node（跳过意图，直接用已有 intent_result）
 └─ 否则 → intent_node → route_after_intent
               ├─ confidence < 0.3 → END（宿主 final）
               ├─ requires_confirmation 且非流式 → confirmation_node（保持现状语义）
               └─ 有工具 → gate_node → tool_node / observe_node → ...
gate_node（仅流式：state["_stream_mode"]=True）
 1. missing = detect_missing_params(...)（规则，见 run_rules.py）→ 有则 raise PendingRequired("input", …)
 2. requires_confirmation 且未 resume → raise PendingRequired("confirm", …)
 3. 非流式 → 直接透传（行为与现状一致）
```

- `PendingRequired` 仍由宿主 `_guarded` 捕获 → `_persist_pending` → checkpoint + pending 事件 + final（**与现状逐字同构**，包括 resume 死循环规避：`_resumed=True` 时 gate 只做参数合并后的透传）；
- `_remaining_steps` / `_done_steps` 进入图 state：`tool_node` 支持"只执行剩余、跳过已完成"，resume 语义完整保留；
- **技术验证项**（C2 首日实测）：`ainvoke` 传入 state 与 MemorySaver 旧 checkpoint 的合并语义；若内部字段（_done_steps 等）受旧值干扰 → 备选：流式路径使用"独立编译的图实例（无 checkpointer）"或 `thread_id=run_id` 隔离。

### 3.3 取消与超时

| 机制 | 现状 | C 档 |
|---|---|---|
| 超时 | 宿主 `asyncio.wait_for(_run_plan, 600s)` | 宿主 `wait_for(graph.ainvoke, 600s)` 不变 |
| 取消检测 | `_check_cancel`：工具步间 / 各阶段间（读 store.is_cancelled） | 宿主把检测函数放入 `state["_check_cancel"]`；节点边界（intent/tool 入口、每步完成后、observe 每轮前、summarize 前）调用 |
| 取消粒度变化 | 工具步骤之间可中断 | **节点边界 + 工具步间（报告的串行步间保留）可中断；并行 gather 段内不可中断**——见 §6 行为差异 D1 |

### 3.4 报告串行编排（搬移）

现状 RunEngine L431-467 的报告编排（充分性检查 → 跳过事件 → 变量构建 → 串行执行）与 tool_node 现有"pre/report 分离"逻辑合并为唯一实现：

```
tool_node：
  1. steps = _remaining_steps or plan-with-tool；过滤 _done_steps
  2. 普通步骤 gather 并行，逐完成 emit tool_start/tool_result
  3. 回退链：DB 空 → KB（现 tool_node 已有）→ KB 空 → Web（发射 status/web_fallback）
  4. 报告步骤串行：充分性检查 →（不足）emit tool_result/跳过 →（足）build_report_variables → invoke → emit
  5. 结果写回 tool_results（部分更新，遵守 P1 的 reducer 约定）
```

### 3.5 checkpoint 双轨边界

| 场景 | 机制 | 说明 |
|---|---|---|
| 非流式图内状态 | MemorySaver（thread_id=会话） | 现状保留，`_prune_checkpoints` 不变 |
| 流式 pending/resume | 自研 `run_checkpoints`（SQLite，宿主） | 载体不变——存的是"宿主视角 state 快照"，与图 checkpointer 正交 |

明确原则：**宿主 checkpoint 是 resume 的权威来源**；图的 MemorySaver 只服务于非流式会话隔离。

---

## 4. 流式事件契约冻结表（前端兼容基线）

迁移后事件时序与 payload **逐条对齐现状**（前端零改动）。发射位置标注变更：

| # | type / stage | 现状发射点 | C 档发射位置 |
|---|---|---|---|
| 1 | status / start | 宿主（意图前） | 宿主（ainvoke 前）· 不变 |
| 2 | status / intent_start | 宿主（意图前） | 宿主（ainvoke 前）· 不变 |
| 3 | intent / intent | 宿主（意图后） | **intent_node 内**（payload 同现状：plan_steps/intent_info/details） |
| 4 | plan / plan | 宿主（计划后） | **intent_node 内** |
| 5 | status / tool_plan | 宿主（执行前） | **tool_node 入口** |
| 6 | tool_start / tool_start | 宿主（每步前） | **tool_node 每步前** |
| 7 | tool_result / tool_result | 宿主（每完成/跳过/回退） | **tool_node 每完成/跳过/回退**（含 KB 回退、报告跳过） |
| 8 | status / react_round | 宿主（ReAct 轮） | **observe_node 每轮** |
| 9 | status / web_fallback | 宿主（联网回退） | **tool_node 回退段** |
| 10 | verification / verification | 宿主（校验未过） | **tool_node 内 postflight 段** |
| 11 | status / response | 宿主（直答或汇总前） | **summarize_node 前**（或 gate 直答分支） |
| 12 | status / done | 宿主（汇总后） | **summarize_node 后 / critic 后** |
| 13 | status / critic\*（P1 新增） | RunEngine | **critic_node** |
| 14 | pending / pending | 宿主（PendingRequired） | 宿主 · 不变 |
| 15 | cancelled / cancelled | 宿主（_check_cancel） | 宿主 · 不变 |
| 16 | error / error | 宿主（超时/异常） | 宿主 · 不变 |
| 17 | final / final | 宿主（_emit_final） | 宿主 · 不变（result 结构 = `_build_stream_result`） |
| 18 | status / queued、run_started | chat.py 路由层 | chat.py · 不变 |

**验收手段**：C3 灰度阶段做"事件快照对比"——同一组测试请求分别在旧/新内核下执行，逐事件比对（type/stage/title/message/details/payload 键集合），除时序微差外必须一致。

---

## 5. 迁移步骤（预计 3~5 天）

| 步骤 | 内容 | 验收 |
|---|---|---|
| **C1（0.5~1 天）** | 新增 `emitter.py` + `run_rules.py`（纯搬移）；节点 emit 点接入（先在 **非流式图**验证 NoopEmitter 零影响） | 现有测试全绿；图节点行为不变 |
| **C2（1~1.5 天）** | gate_node + 图重入入口路由 + tool_node 剩余步骤/报告串行/取消检查点；技术验证项（§3.2）实测 | 非流式全量回归；gate 单元测试（confirm/input/透传） |
| **C3（1~1.5 天）** | `RunHost` 重写（start/resume/guarded/checkpoint/pending 保留；_run_plan → `graph.ainvoke`）；`STREAM_VIA_GRAPH=1` 开关灰度；事件快照对比 | 事件契约对比通过；E2E 场景全过；可即时回退开关 |
| **C4（0.5~1 天）** | 删除手工编排副本（_invoke_step_bare / _decide_web_search / _run_plan 等）；文件行数达标；文档更新 | run_engine.py ≤400 行；全量 pytest + 生产灰度 |

---

## 6. 行为差异清单（明确会变的，逐条评审）

| # | 差异 | 影响评估 | 处置 |
|---|---|---|---|
| D1 | 取消粒度：由"工具步间"细化点后移为"节点边界 + 报告步间" | 并行 gather 段内不可中断（单步工具时长有限） | 接受；如需更强，observe 每轮是自然检查点 |
| D2 | 事件时序微差：tool_result 之间顺序本就不定（as_completed）；intent/plan 由节点发射，与 status/response 的相对位置不变 | 前端按到达顺序渲染，无影响 | 快照对比确认 |
| D3 | 直答路径（无工具）从宿主分支变为图内 summarize 分支 | response/done 事件时序一致 | 对比确认 |
| D4 | 流式图重入时 MemorySaver 旧快照的合并语义 | 未知（§3.2 验证项） | C2 实测；备选隔离方案已列 |
| D5 | `intent_result` 外部传入参数（start 支持）路径 | 无变化，宿主仍接收 | 保留 |

---

## 7. 测试与验收

### 7.1 单元

- `emitter`：NoopEmitter 无副作用；BusEmitter 字段透传；
- `run_rules`：搬移函数逐字对照测试（与搬移前输出一致）；
- `gate_node`：缺参 → PendingRequired(input)；确认 → (confirm)；resume → 透传；非流式 → no-op；
- `tool_node` 剩余步骤：_done_steps 跳过 + _remaining_steps 生效。

### 7.2 集成与端到端

| 场景 | 断言 |
|---|---|
| 报告流式（多步骤 + 报告串行） | 事件序列含 tool_plan/tool_start×N/tool_result×N/final；报告跳过路径事件保留 |
| pending 确认全链路 | awaiting_confirmation → resume → 继续执行 → final；事件与现状一致 |
| pending 补参（switch_layer/cesium setBasemap） | 补参合并、工具纠正（cesium 视图）保留 |
| 取消 | 用户取消 → cancelled 事件 + CANCELLED 状态；节点边界生效 |
| 超时 | 600s 兜底路径不变 |
| 知识空 → 联网回退 | web_fallback 事件 + 回答正常 |
| 非流式全量 | 行为与现状零差异（NoopEmitter 证明零开销） |

### 7.3 验收指标

| 指标 | 目标 |
|---|---|
| `run_engine.py` 行数 | ≤400（现状 1207） |
| 编排双份实现 | 0（工具执行/回退/观察/报告/汇总全部单点） |
| 事件契约对比 | 逐条一致（§4 表 18 项） |
| 现有测试 | 全绿 |

---

## 8. 风险与回滚

| # | 风险 | 缓解 |
|---|---|---|
| 1 | 图内状态语义与宿主 checkpoint 错位（D4） | C2 首日技术验证；备选隔离方案（独立编译/无 checkpointer） |
| 2 | 事件顺序/字段微差影响前端 | 事件契约冻结表 + 快照对比测试；灰度开关 |
| 3 | pending 死循环（确认/缺参重复触发） | 复用现状同构逻辑（_resumed 跳过检测）；集成测试覆盖 |
| 4 | 迁移期间双轨并存引入新漂移 | C1/C2 只增强图（非流式验证），C3 才切换流式；每步独立 commit |
| 5 | 取消粒度变粗引发体验问题（D1） | observe 轮/报告步为自然检查点；必要时在 gather 段内增补检查 |

**回滚**：`STREAM_VIA_GRAPH=0` → 流式回退 `_run_plan`（C3/C4 完成前保留旧路径于 git 历史，C4 后回滚依赖 revert）；非流式无开关（NoopEmitter 即现状）。

---

## 9. 附录

### 9.1 现状关键锚点（C 档改动前基线）

| 目标 | 位置 |
|---|---|
| 宿主生命周期 | `run_engine.py` start / resume / _guarded / _persist_pending |
| 手工编排主体 | `run_engine.py` _run_plan（意图→路由→步骤→React→回退→报告→汇总） |
| 事件发射 helper | `run_engine.py` _emit / _emit_tool_start / _emit_tool_result / _emit_verification / _emit_final |
| pending 检测与规则 | `run_engine.py` _detect_missing_params / _merge_supplied / is_confirm_message / parse_supplied_from_message / normalize_basemap_choice |
| SSE 转发与 final 后处理 | `routers/chat.py` L789-830 |
| 图入口与编译 | `task_executor.py` _build_graph（MemorySaver + thread_id） |
| 图节点 | `nodes/`（P1 后含 observe/critic） |

### 9.2 技术验证清单（C2 首日完成）

1. `ainvoke` 传入 state 与 MemorySaver 同 thread_id 旧快照的合并语义（对 `_done_steps/_remaining_steps/tool_results` 的影响）；
2. 图 state 中 `_emitter`（含 asyncio 对象）是否参与 checkpointer 序列化——如是则必然从 checkpoint 序列化中剔除；
3. `graph.ainvoke` 对 `asyncio.CancelledError` 的传播行为（宿主 cancel task 时的清理路径）；
4. 报告串行段搬移后与 tool_node 现有充分性判定的一致性回归。
