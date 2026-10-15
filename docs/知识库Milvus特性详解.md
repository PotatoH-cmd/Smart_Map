# 知识库 Milvus 特性详解

> 适用对象：map_assistant_v1 后端维护者。
> 本文记录知识库向量层迁移到 **Milvus 2.4.15（standalone @127.0.0.1:19530）** 后实际用到的每一项特性：**为什么用 → 怎么用 → 代码在哪 → 实测有什么坑**。
> 代码位置：`backend/kb_milvus/`（store.py / sparse.py / adapter.py / sync.py）；迁移脚本 `backend/scripts/migrate_kb_to_milvus.py`。

---

## 1. 总体架构：docstore 真相来源 + Milvus 向量副本

### 1.1 为什么这么设计

LlamaIndex 的 `VectorStoreIndex` 有两个存储角色：

| 存储 | 职责 | 本项目的选择 |
|---|---|---|
| `docstore`（docstore.json） | 节点文本 + metadata，**真相来源** | 保留在本地 JSON（LlamaIndex 管理） |
| `vector_store` | 向量索引（检索用） | **Milvus**（自研适配器） |

**关键设计决策：适配器声明 `stores_text = False`。**

LlamaIndex 写入节点时的内部逻辑等价于：

```python
if not vector_store.stores_text or store_nodes_override:
    docstore.add_documents(nodes)      # 只在「向量库不负责文本」时才写 docstore
vector_store.add(nodes)                # 向量写入 Milvus
```

官方 `MilvusVectorStore` 的 `stores_text=True`（它把文本塞进 `_node_content` JSON 由 Milvus 统一管理）会导致 **docstore 被静默跳过**——这在历史上造成过「写入报成功、但文档列表/删除查不到」的坑。自研适配器取 `stores_text=False`，docstore 照常写入，Milvus 只做向量副本（含正文快照用于检索返回）。

### 1.2 数据流总览

```
【写入】kb_pipeline 增量管线 / 手动上传 / ingest 脚本
    │
    ├─▶ LlamaIndex 切分（512 字/块，重叠 64）
    ├─▶ DashScope text-embedding-v3 → 1024 维稠密向量
    ├─▶ docstore.json 写入（文本 + metadata，真相来源）
    └─▶ LlamaIndexMilvusVectorStore.add(nodes)
            └─▶ build_row() 组装行 → Milvus upsert(flush=True)

【检索】KB 工具 _search → _retrieve_hybrid → 适配器 query()
    │
    ├─▶ 查询稠密向量（DashScope query embedding）
    ├─▶ 查询稀疏向量（jieba 分词 + BM25 权重）
    ├─▶ Milvus hybrid_search：
    │       route1: vector  AnnSearchRequest(COSINE, HNSW, expr=project过滤)
    │       route2: sparse  AnnSearchRequest(IP, 倒排索引,  expr=project过滤)
    │       ranker: RRFRanker(60) 服务端融合
    │       consistency: Strong（读己之写）
    ├─▶ 行 → TextNode 还原（meta_json + 标量列 → metadata）
    └─▶ DashScope gte-rerank-v2 精排 → top_k
```

### 1.3 Collection Schema（`store.py::MilvusKBStore._schema`）

| 字段 | 类型 | 角色 | 说明 |
|---|---|---|---|
| `pk` | VARCHAR(512) | 主键 | 节点 id（node_id） |
| `vector` | FLOAT_VECTOR(1024) | 稠密向量 | HNSW / COSINE |
| `sparse` | SPARSE_FLOAT_VECTOR | 稀疏向量 | jieba+BM25 权重，SPARSE_INVERTED_INDEX / IP |
| `project` | VARCHAR(64) | **partition key** | 项目文件夹（固始/商城/…），分区剪枝 |
| `project_name` `document_id` `title` `chunk_type` `breadcrumb` `section_no` `chunk_fp` `source_file` `created_at` | VARCHAR | 标量列 | 高频过滤/展示字段（document_id / chunk_fp / chunk_type / created_at 建 INVERTED 索引） |
| `text` | VARCHAR(60000) | 正文快照 | 检索直接返回，免二次读 docstore |
| `meta_json` | JSON | 全量 metadata | 完整备份，还原 node.metadata 时优先使用 |

---

## 2. 特性一：HNSW 稠密索引（COSINE）

### 原理

HNSW（Hierarchical Navigable Small World）是一种**多层图**近似最近邻索引：上层稀疏图负责快速"跳远"，下层稠密图负责精细搜索。查询从顶层入口点出发，逐层贪婪下降，在每层维护一个大小为 `ef` 的候选队列。

**三个核心参数**（我们的取值）：

| 参数 | 取值 | 作用 | 调大后果 |
|---|---|---|---|
| `M` | 16 | 每个节点的邻居上限 | 召回↑ 内存↑ 建图慢 |
| `efConstruction` | 200 | 建图时候选队列大小 | 图质量↑ 构建慢（只影响建索引） |
| `ef`（查询期） | 64 | 搜索时候选队列大小 | 召回↑ 延迟↑ |

`ef` 是**查询期**参数，不用重建索引——我们通过环境变量 `KB_MILVUS_EF`（默认 64）传入每路的 `param.params.ef`，可按需在线调整。

### 为什么用 COSINE

- 与迁移前 `SimpleVectorStore` 的默认相似度（cosine）语义一致，保证迁移前后排序口径不变；
- 文本语义检索的标准度量；余弦对向量模长不敏感，只比较方向。

### 代码位置

- 建索引：`store.py::ensure_collection()` → `add_index("vector", "HNSW", metric_type="COSINE", params={"M":16, "efConstruction":200})`
- 查询：`store.py::hybrid_search()` → `AnnSearchRequest(param={"metric_type": "COSINE", "params": {"ef": self.ef}})`

---

## 3. 特性二：稀疏向量 + SPARSE_INVERTED_INDEX（jieba + BM25）

### 为什么需要自己编码

Milvus **2.4 没有内置 BM25 Function**（2.5 才引入），且内置分析器对中文分词支持有限。因此采用「客户端 jieba 分词 + BM25 权重 → 稀疏向量」方案，检索语义与原先 LlamaIndex 的 `BM25Retriever`（同为 jieba + BM25）对齐，且从「客户端算分」升级为「服务端倒排索引检索」。

### BM25 如何拆进两侧向量（`sparse.py` 的核心数学）

标准 BM25 的 query-document 得分为：

```
score = Σ_t  IDF(t) · f(t,d)·(k1+1) / (f(t,d) + k1·(1 - b + b·|d|/avgdl))
```

要点：**内积只能做乘法求和**，所以把每一项拆成「文档侧 × 查询侧」两个因子，使内积正好还原 BM25：

```
文档侧权重 = [f(t,d)·(k1+1) / (f(t,d) + k1·(1-b+b·|d|/avgdl))] · sqrt(IDF(t))   ← tf 饱和项 × √IDF
查询侧权重 = sqrt(IDF(t))                                                      ← √IDF（去重后每词一次）
IP(doc, query) = Σ_t 文档侧 · 查询侧 = Σ_t IDF(t) · tf饱和项  = BM25 ✓
```

- IDF 用 `sqrt` **对称地分给两侧**，避免大语料下 IDF 平方后数值过大、单侧权重失衡；
- `IDF(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))`，标准 BM25 形式；
- `k1=1.2`、`b=0.75` 为标准默认参数；
- **全语料统计**（N / df / avgdl）通过 `fit()` 扫描一次后缓存为 JSON（`data/bm25_stats.json`），增量文档复用缓存统计（近似），`--refit-sparse` 可强制重建。当前语料：979 篇、词表 14147、平均长度 253.8。

### token → 稀疏向量下标

Milvus 稀疏向量的下标必须是 uint32。取 `crc32(token) & 0xFFFFFF`（2²⁴ ≈ 1677 万桶）：

- 词表 1.4 万，按生日悖论估算期望碰撞对数 ≈ n²/2m ≈ 14147²/(2×2²⁴) ≈ **6 对**——碰撞时权重合并、影响可忽略；
- 用 crc32 而非递增 id，是为了**增量文档无需重映射**（同一词永远映射到同一桶）。

### SPARSE_INVERTED_INDEX 索引

- 稀疏倒排索引：为每个非零维度维护 posting list（哪些行、权重多少），检索时只扫查询词命中的 posting list；
- 度量 `IP`（内积）：权重乘积求和 → 正好是 BM25 打分；
- `drop_ratio_build=0.0`：建索引时**不丢弃**任何小权重项（精度优先；索引更大，当前规模无压力）。

### 退化路径（保证永不阻塞）

| 场景 | 行为 |
|---|---|
| 未 `fit`/无缓存 | 权重退化为纯 tf（仍可用，精度下降） |
| jieba 不可用 | 字符级兜底切分（连续字母数字 / 单汉字） |
| 查询分词结果为空 | 稀疏向量为 `{}` → 自动退化为**纯稠密检索** |

### 代码位置

- 编码器：`sparse.py::BM25SparseEncoder`（fit/load/save/encode_document/encode_query）
- 文档侧编码：`store.py::build_row()` → `encoder.encode_document(text)`
- 查询侧编码：`adapter.py::query()` → `encoder.encode_query(query.query_str)`

---

## 4. 特性三：hybrid_search + RRFRanker(60) 服务端混合检索

### 原理

Milvus 的 `hybrid_search` 接受**多个 `AnnSearchRequest`**（各自独立检索、各自 `limit`），由服务端 `Ranker` 融合后返回最终 `limit` 条。

我们启用两路：

```python
reqs = [
    AnnSearchRequest(data=[dense],  anns_field="vector", param={"metric_type":"COSINE", ...}, limit=top_k, expr=expr),
    AnnSearchRequest(data=[sparse], anns_field="sparse", param={"metric_type":"IP"},          limit=top_k, expr=expr),
]
client.hybrid_search(collection, reqs=reqs, ranker=RRFRanker(60), limit=top_k, ...)
```

### RRF 融合：只看排名，不看分数

```
RRF_score(d) = Σ_route  1 / (k + rank_route(d))     （k = 60）
```

- **为什么用 RRF 而不是加权求和**：稠密分（COSINE，0~1）与稀疏分（BM25，无界）量纲不可比，直接加权需要调参归一化；RRF 只用「第几名」，天然规避量纲问题；
- **k=60** 与迁移前客户端融合（LlamaIndex `QueryFusionRetriever` SIMPLE 模式）的默认值一致——保持迁移前后排序口径一致；
- 服务端融合的优势：**一次网络往返**，且只回传最终 top_k（不是各路完整结果再在客户端合并）。

### 与迁移前客户端融合的对比

| | 迁移前（simple 后端） | 迁移后（Milvus） |
|---|---|---|
| 稀疏路 | 客户端 `BM25Retriever`（jieba） | 服务端稀疏向量倒排索引 |
| 融合 | 客户端 `_rrf_fuse`（k=60） | 服务端 `RRFRanker(60)` |
| 网络 | 向量检索 1 次 + BM25 本地 | **1 次** |
| 过滤 | 检索后客户端过滤 | **expr 前置过滤（分区剪枝）** |

### 实测坑（必须记住）

> **`hybrid_search` 的外层 `filter` / `filter_params` 参数不生效**——实测发现传外层参数时会静默返回**未过滤的全量数据**。过滤表达式必须挂在**每个 `AnnSearchRequest` 的 `expr` 上**。

对应代码：`store.py::hybrid_search()` 中两路 `AnnSearchRequest(..., expr=expr)`；纯稠密退化路径走 `client.search(filter=expr)`（search 的 filter 正常生效）。

---

## 5. 特性四：partition key（project 分区键）

### 原理

`partition key` 是 Milvus 2.4 提供的**数据自动分区**能力：指定一个字段为分区键后，写入时按该字段值**自动路由**到对应物理分区；查询时若过滤条件携带分区键，Milvus 只扫描对应分区（**分区剪枝**），无需扫全量。

### 项目中的应用

- `project` 字段（项目文件夹：固始 / 商城 / 平桥 / 淮滨…）是知识库的天然分区维度；
- Schema：`FieldSchema("project", DataType.VARCHAR, max_length=64, is_partition_key=True)`；
- 过滤链路：工具层 `MetadataFilters(project=...)` → `adapter._filters_to_expr` → expr 挂到每个 `AnnSearchRequest` → Milvus 分区剪枝；
- 无 project 的节点：写入空串（实测可行，空串进入独立分区）；
- 实测验证：带 project 检索 10 条结果 **0 条越界**。

### 收益与代价

| | 说明 |
|---|---|
| 收益 | 项目过滤只扫单分区；随项目/文档规模增长，剪枝收益线性放大 |
| 代价 | 分区键**所有行必填**（schema 约束）；分区数过多会增加元数据开销（当前 < 10 个项目，远低于阈值） |
| 限制 | 一个 collection **只能有一个** partition key 字段 |

### 双保险

工具层检索后仍保留一次 `project` 后置过滤（`_search_deterministic` / `_search_agent` 中的列表推导），防止 `expr` 转义漏网——过滤下推是性能优化，后置过滤是正确性兜底。

---

## 6. 特性五：INVERTED 标量索引

Milvus 2.4 引入的标量倒排索引，建立后等值/范围过滤与按条件删除不再全表扫描。

| 索引列 | 用途 |
|---|---|
| `document_id` | 整篇删除（`delete_by_document`）、溯源聚合 |
| `chunk_fp` | 一致性校验（docstore ↔ Milvus 指纹差分导出） |
| `chunk_type` | 按切片类型过滤（章节/表格/引言等） |
| `created_at` | 时间类巡检查询 |

`project` **不建** INVERTED——partition key 已承担等值过滤职责（且两者叠加没有必要）。

```python
for col in ("document_id", "chunk_fp", "chunk_type", "created_at"):
    index_params.add_index(col, index_type="INVERTED")
```

---

## 7. 特性六：JSON 字段（meta_json）

- `meta_json` 存**全量 metadata**（JSON 对象），标量列是它的「性能投影」；
- 还原节点时（`adapter._fields_to_metadata`）：**meta_json 优先**，再用标量列**覆盖**关键键——保证即使标量列被截断（如 `breadcrumb` 4096 截断），JSON 侧仍是完整值；
- 为什么冗余存储：标量列为了**过滤与索引性能**，JSON 为了**完整性**——metadata 将来加新字段（如 `source_url`）无需改 schema 也能取回；
- 注意：2.4 的 JSON 字段本身不建路径索引，我们的过滤键全部落在标量列上（JSON 只存不查）。

---

## 8. 特性七：upsert 幂等写入

- Milvus `upsert`：主键存在则覆盖（内部 delete+insert），不存在则插入——**幂等**；
- 实测：同 pk 多次 upsert 后查询仅 1 条（无重复行）；
- 带来的运维安全：
  - 迁移脚本可**任意重跑**（`migrate_from_local` 按 pk 覆盖增量补齐）；
  - 增量更新 = 重切片的节点重新 upsert，无需先删；
- 批量写入用 `upsert_batched`（默认 200/批），最后统一 `flush`（见第 10 节）。

---

## 9. 特性八：query_iterator（流式全量导出）

- `client.query()` 有 `limit` 上限（默认 16384），全量导出会被截断；
- `query_iterator` 按 `batch_size`（默认 500）**流式分批**遍历，绕过 limit；
- 用途：`export_id_fp()` 导出全量 `{pk: chunk_fp}`，供一致性校验（迁移复核 / `--check` 巡检 / 启动自愈 diff）做集合差分；
- 已知无害告警：`failed to get mvccTs from milvus server, use client-side ts instead`——iterator 拿不到服务端时间戳时退回客户端时钟，不影响结果。

---

## 10. 特性九：consistency_level（一致性级别）

### 原理

Milvus 提供四档一致性：`Strong / Bounded / Session / Eventually`。写入是「streaming + 不可变段（segment）」模型：写入先进入 streaming，flush 后封存为段，查询节点需要等待时间戳推进才能看到新数据。

### 本项目的双档策略

| 方向 | 级别 | 原因 |
|---|---|---|
| 写入（collection 默认） | `Bounded` | 高吞吐（`KB_MILVUS_CONSISTENCY`） |
| 查询/检索 | `Strong` | **读己之写**：上传/更新文档后立即可搜到（`KB_MILVUS_SEARCH_CONSISTENCY`，默认 Strong） |

### 实测坑

> **upsert 后立即 query 可能读到旧值**（Bounded 下 streaming 数据尚未对查询可见）。实测：+0.5s 后读到新值；显式 `consistency_level="Strong"` 立即可读；`hybrid_search + Strong + expr` 组合也能立即读新值。

组合拳：**写入 `flush()` + 查询 `Strong`** —— `upsert_rows(flush=True)`、`delete_*` 后显式 `flush()`；所有读路径（`count / get_rows / iter_rows / hybrid_search / search`）统一带 `consistency_level=self.query_consistency`。

业务含义：知识库的「上传文档后立刻提问能搜到」是用户核心预期，Strong 的小延迟代价（数据量小可忽略）是值得的；若未来库很大且高并发，可调 `KB_MILVUS_SEARCH_CONSISTENCY=Bounded` 换取吞吐。

---

## 11. LlamaIndex 适配层（与官方适配器的差异）

`adapter.py::LlamaIndexMilvusVectorStore(BasePydanticVectorStore)` 需要实现四件事：

| 接口 | 实现 | 要点 |
|---|---|---|
| `client` property | 返回 pymilvus 客户端 | 基类要求 |
| `add(nodes)` | 逐节点 `build_row()` → `upsert_rows(flush=True)` | **强制要求 embedding**（缺失直接 raise——宁可报错不可静默丢向量） |
| `delete` / `delete_nodes` | 按主键删除 + 按 document_id 删除 | `delete(ref_doc_id)` 双语义（可能是节点 id 或文档 id）；删除后 flush |
| `query(VectorStoreQuery)` | `hybrid_search` + `MetadataFilters → expr` | `query_str` 用于稀疏编码；`query_embedding` 为稠密路 |

### filters → expr 转换（`_filters_to_expr`）

- **白名单键**（8 个）：`project / document_id / chunk_type / chunk_fp / section_no / title / source_file / created_at`，其余键忽略并告警（防 expr 注入）；
- 支持嵌套 `MetadataFilters` 与 `AND / OR`，操作符支持 `== / != / in`；
- 值统一走 `escape_expr_value`（转义 `\` 与 `"`）。

### 一个「意外的好处」：persist 是 no-op

`BasePydanticVectorStore.persist()` 基类实现是 `return None`。这意味着 `storage_context.persist()` 时：

- `docstore.json / index_store.json / graph_store.json` 正常落盘（真相来源持续持久化）✓
- `default__vector_store.json` **不被覆盖**——迁移前的 Simple 向量快照原样保留，天然成为 `KB_VECTOR_BACKEND=simple` 的回退保险 ✓

---

## 12. 一致性与自愈体系（chunk_fp 指纹）

以 `chunk_fp`（kb_pipeline 增量管线的切片指纹）为差异判据，对比 docstore 与 Milvus 的 `(node_id, fp)` 集合：

| 场景 | 入口 | 行为 |
|---|---|---|
| 巡检（只读） | `verify_consistency`（`migrate_kb_to_milvus.py --check`） | 报告 missing / extra / fp_mismatch |
| 启动自愈 | `repair_from_docstore`（工具初始化时按 `KB_MILVUS_AUTOSYNC` 触发） | 缺失/指纹变化 → 重新 upsert；多余 → 删除 |
| 一次性迁移 | `migrate_from_local` | 全量重建/补齐 + 复核 |

**增量写入路径无需额外代码**：`kb_pipeline` 走 `VectorStoreIndex.insert_nodes / delete_nodes`，适配器的 `add / delete_nodes` 自动同步 Milvus（已实测：insert → Milvus +1 行；delete → 归零，双端一致）。

`KB_MILVUS_AUTOSYNC` 三档：`0` 关闭 / `1`（默认）count 不一致时才全量 diff / `full` 每次启动都 diff。

---

## 13. 实测坑与规避清单（汇总）

| # | 坑 | 现象 | 规避 |
|---|---|---|---|
| 1 | `hybrid_search` 外层 `filter` 不生效 | 返回未过滤的全量数据（静默！） | expr 挂到**每个** `AnnSearchRequest` 上 |
| 2 | upsert 后立即读旧值 | 新上传文档检索不到 | 写入 `flush()` + 查询 `consistency_level="Strong"` |
| 3 | 官方适配器 `stores_text=True` | docstore 被跳过 →「报成功但查不到」 | 自研适配器 `stores_text=False` |
| 4 | 稀疏向量为负权重 | 索引报错 | BM25 权重恒正（√IDF ≥ 0），且仅保留 `w > 0` 的项 |
| 5 | VARCHAR 超长 | 写入报错 | `_truncate` 按列限长截断（text 60000 / breadcrumb 4096 / …） |
| 6 | metadata 含不可序列化值 | JSON 字段写入报错 | `_sanitize_json` 兜底转字符串 |
| 7 | query 默认 limit | 全量导出被截断（16384） | `query_iterator` 流式导出 |
| 8 | iterator `mvccTs` 告警 | 日志 WARNING | 无害（客户端时钟兜底），可忽略 |

---

## 14. 环境变量与运维命令

### 环境变量（backend/.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_VECTOR_BACKEND` | `simple` | `milvus` 启用 Milvus 后端；回退改回 `simple` |
| `MILVUS_URI` | `http://127.0.0.1:19530` | Milvus 服务地址 |
| `KB_MILVUS_COLLECTION` | `map_kb` | collection 名 |
| `KB_MILVUS_AUTOSYNC` | `1` | 启动自愈：`0` 关 / `1` 按需 / `full` 总是 |
| `KB_SPARSE_ENABLE` | `1` | jieba+BM25 稀疏向量开关（关=纯稠密） |
| `KB_MILVUS_EF` | `64` | HNSW 查询期 ef |
| `KB_MILVUS_CONSISTENCY` | `Bounded` | collection 写入一致性 |
| `KB_MILVUS_SEARCH_CONSISTENCY` | `Strong` | 查询/检索一致性 |
| `KB_EMBED_DIM` | `1024` | 稠密向量维度 |
| `KB_BM25_STATS_PATH` | `data/bm25_stats.json` | BM25 统计缓存路径 |

### 运维命令（backend 目录下执行）

```bash
# 一致性巡检（只读，exit 0 = 一致）
python scripts/migrate_kb_to_milvus.py --check

# 增量补齐迁移（幂等，可重复执行）
python scripts/migrate_kb_to_milvus.py

# 重建 collection 全量重写（危险：清空后重灌）
python scripts/migrate_kb_to_milvus.py --drop

# 重建 BM25 统计缓存（语料大幅变化时）
python scripts/migrate_kb_to_milvus.py --refit-sparse

# 缺失向量时调用 DashScope 现算（默认关闭）
python scripts/migrate_kb_to_milvus.py --embed-missing
```

### 迁移前后能力对照

| 能力 | 迁移前（SimpleVectorStore） | 迁移后（Milvus） |
|---|---|---|
| 向量存储 | 本地 JSON（全量加载进内存） | Milvus 服务端（HNSW 索引） |
| 关键词检索 | 客户端 BM25Retriever（全量内存索引） | 服务端稀疏倒排索引 |
| RRF 融合 | 客户端 | 服务端 RRFRanker(60) |
| 项目过滤 | 检索后客户端过滤 | expr 下推 + **分区剪枝**（+后置兜底） |
| 写入可见性 | 立即（内存） | flush + Strong 立即 |
| 一致性校验 | 无 | chunk_fp 指纹巡检 / 自愈 |
| 扩展性 | 单机内存瓶颈 | 服务端横向扩展（standalone→cluster） |
