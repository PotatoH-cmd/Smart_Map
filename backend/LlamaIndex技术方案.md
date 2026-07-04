# LlamaIndex 知识库技术方案

> 项目：map_assistant_v1（水利勘察智能助手）  
> 日期：2026-07-01  
> 背景：将知识库模块从 RagFlow（远程 HTTP API）迁移到 LlamaIndex（本地框架）

---

## 一、整体架构

```
┌─────────────────────────────────────────────────┐
│                  FastAPI 服务层                    │
│  main.py → 条件导入 → KnowledgeBaseTool           │
│  /api/knowledge  GET/POST/DELETE（6 端点不变）      │
├─────────────────────────────────────────────────┤
│              工具层 (Qwen Agent)                    │
│  LlamaIndexKnowledgeTool   ← 核心实现               │
│  KnowledgeQAAgent          ← 4 阶段管道             │
│  TaskExecutor              ← LangGraph 编排         │
├─────────────────────────────────────────────────┤
│            LlamaIndex 框架层                        │
│  ┌──────────────┐  ┌───────────────┐               │
│  │ VectorStore  │  │  Embedding    │               │
│  │ Index        │  │  DashScope    │               │
│  │ (检索/索引)   │  │  (向量化)      │               │
│  └──────────────┘  └───────────────┘               │
│  ┌──────────────┐  ┌───────────────┐               │
│  │ Document     │  │  Storage      │               │
│  │ Store        │  │  Context      │               │
│  │ (文档管理)    │  │  (JSON 持久化) │               │
│  └──────────────┘  └───────────────┘               │
├─────────────────────────────────────────────────┤
│              持久化层 (文件系统)                      │
│  llama_index_storage/                             │
│    ├── default__vector_store.json (11MB)          │
│    ├── docstore.json                (1.5MB)        │
│    └── index_store.json             (41KB)         │
└─────────────────────────────────────────────────┘
```

---

## 二、使用的 LlamaIndex 组件

### 2.1 核心框架

| 组件 | 所属包 | 用途 |
|------|--------|------|
| `VectorStoreIndex` | `llama_index.core` | 向量存储索引，封装文档的分块、向量化、存储和检索全流程 |
| `Settings` | `llama_index.core` | 全局配置单例，统一管理 embedding 模型、分块参数、batch size |
| `StorageContext` | `llama_index.core` | 存储上下文，负责索引的序列化（persist）和反序列化（load） |
| `Document` | `llama_index.core` | 文档数据结构，携带 `text`、`doc_id`、`metadata` 字段 |
| `SimpleDirectoryReader` | `llama_index.core` | 文件读取器，自动识别并加载 PDF/Word/TXT 文件 |
| `load_index_from_storage` | `llama_index.core` | 从持久化目录反序列化已有索引，实现启动即加载 |

### 2.2 Embedding 模型

| 组件 | 所属包 | 用途 |
|------|--------|------|
| `DashScopeEmbedding` | `llama_index.embeddings.dashscope` | 阿里云 DashScope `text-embedding-v3` 模型，复用现有 API Key |

### 2.3 向量存储（内置）

| 组件 | 说明 |
|------|------|
| `SimpleVectorStore` | LlamaIndex 内置向量存储，JSON 文件持久化，无需外部数据库，适合当前 84 份文档规模 |

### 2.4 检索器

| API | 说明 |
|-----|------|
| `index.as_retriever(similarity_top_k=n)` | 基于余弦相似度的 Top-K 向量检索，返回带相似度分数的 `NodeWithScore` 列表 |

### 2.5 文档管理 API

| API | 说明 |
|-----|------|
| `index.insert(doc)` | 向索引中增量添加文档（自动分块+向量化） |
| `index.delete_ref_doc(doc_id)` | 按 ID 删除文档及其所有分块 |
| `index.docstore` | 文档存储，支持 `docs`（遍历所有文档）和 `get_document(id)`（按 ID 获取） |
| `index.storage_context.persist(dir)` | 将当前索引状态持久化到磁盘 |

---

## 三、关键设计模式

### 3.1 延迟初始化（Lazy Initialization）

```python
def _ensure_initialized(self):
    if self._initialized:
        return True
    # 首次调用时才加载 DashScope 和索引
    # 避免服务启动时阻塞或因依赖缺失败
```

### 3.2 环境变量热切换（Strategy Pattern）

```python
# main.py / task_executor.py / knowledge_qa_agent.py 三处统一模式
_kb_backend = os.environ.get("KNOWLEDGE_BACKEND", "ragflow")
if _kb_backend == "llamaindex":
    from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
else:
    from tools.ragflow_knowledge_tool import KnowledgeBaseTool
```

同一工具类名 `KnowledgeBaseTool`、同一注册名 `@register_tool('knowledge_base_tool')`、同一参数 schema、同一返回格式 —— 确保上层调用方零修改。

### 3.3 降级策略（Fallback）

```
LlamaIndex 检索
    ↓ 失败/未初始化
RagFlow HTTP API（降级）
    ↓ 仍失败
返回错误信息给用户
```

每个操作（search/list_topics/get_content/add_document/delete_document/upload_file）都配备对应的 `_ragflow_fallback_*` 方法。

### 3.4 迁移脚本的增量/断点续传

```bash
# 预览（只导出不导入）
python scripts/migrate_kb_to_llamaindex.py --dry-run

# 全量迁移
python scripts/migrate_kb_to_llamaindex.py

# 全量重建（清空旧索引）
python scripts/migrate_kb_to_llamaindex.py --rebuild
```

---

## 四、全局配置参数

```python
Settings.embed_model = DashScopeEmbedding(
    model_name="text-embedding-v3",  # 模型名称
    api_key=os.environ["DASHSCOPE_API_KEY"],
    embed_batch_size=10,             # ⚠️ DashScope API 硬限制，默认 25 会报错
)
Settings.embed_batch_size = 10       # 底层同步参数
Settings.chunk_size = 512            # 分块大小（字符数）
Settings.chunk_overlap = 64          # 相邻块重叠（字符数）
```

> **避坑**：`DashScopeEmbedding` 构造函数默认 `embed_batch_size=25`，但 DashScope API 限制每次最多 10 条，必须显式覆写为 `10`，否则大文档会报 `InvalidParameter: batch size is invalid`。

---

## 五、向量化与检索流程

```
用户查询 "采砂许可"
    ↓
index.as_retriever(similarity_top_k=8)
    ↓
DashScopeEmbedding 将查询转为 1024 维向量
    ↓
SimpleVectorStore 余弦相似度匹配
    ↓
返回 Top-8 NodeWithScore
    ↓
组装为 {id, document_id, title, content, relevance, metadata}
```

检索结果带 `method: "LlamaIndex Retrieval"` 标识与 RagFlow 区分。

---

## 六、文档生命周期

```
添加文档 (_add_document)
  ┌─ Document(text, doc_id, metadata)
  ├─ index.insert(doc)
  │   └─ SentenceSplitter(chunk_size=512, overlap=64) 自动分块
  │   └─ DashScopeEmbedding 批量向量化 (每批 ≤10 条)
  │   └─ SimpleVectorStore 写入向量 + docstore 写入原文
  └─ storage_context.persist() → JSON 文件落盘

删除文档 (_delete_document)
  ┌─ index.delete_ref_doc(doc_id, delete_from_docstore=True)
  │   └─ 从 vector store 删除所有 chunk 向量
  │   └─ 从 docstore 删除原文
  └─ storage_context.persist() → JSON 文件更新

文件上传 (_upload_file)
  ┌─ SimpleDirectoryReader(input_files=[path]).load_data()
  │   └─ 自动识别 PDF/Word/TXT，提取文本
  ├─ 逐段添加元数据 (title, document_id, source_file, created_at)
  └─ 逐个 index.insert() → persist()
```

---

## 七、依赖项

```txt
# requirements.txt
llama-index>=0.12.0
llama-index-embeddings-dashscope>=0.3.0
```

实际安装版本：
- `llama-index` 0.14.23
- `llama-index-embeddings-dashscope` 0.5.0

---

## 八、持久化文件说明

| 文件 | 大小 | 说明 |
|------|------|------|
| `default__vector_store.json` | 11MB | 所有 chunk 的向量数据（SimpleVectorStore） |
| `docstore.json` | 1.5MB | 原始文档及元数据（DocumentStore） |
| `index_store.json` | 41KB | 索引元信息（IndexStore） |
| `graph_store.json` | 18B | 知识图谱存储（当前为空） |
| `image__vector_store.json` | 72B | 图像向量存储（当前为空） |

迁移后共 84 份文档、1900+ 个分块（chunk）。

---

## 九、性能特征

| 指标 | LlamaIndex | RagFlow (对比) |
|------|-----------|----------------|
| 首次启动延迟 | 正常（延迟初始化） | 无额外开销 |
| 检索延迟 | ~200-500ms（本地向量匹配） | ~500-2000ms（HTTP 往返） |
| 内存占用 | 索引加载后 ~50MB | 无（纯 HTTP 客户端） |
| 磁盘空间 | ~13MB（持久化文件） | 0（云端存储） |
| 网络依赖 | Embedding 需访问 DashScope API | 全部操作依赖 RagFlow 服务 |
| 可用性 | DashScope 不可用时自动降级 | RagFlow 不可用时完全不可用 |

---

## 十、回归保障

| 保障项 | 说明 |
|--------|------|
| 6 个 API 端点不变 | `/api/knowledge` GET/POST/DELETE 路径、参数、返回格式完全兼容 |
| 工具注册名不变 | `@register_tool('knowledge_base_tool')` 两套实现一致 |
| 降级开关 | `KNOWLEDGE_BACKEND=ragflow` 秒级切回，零配置改动 |
| 迁移脚本 | 支持 dry-run / resume / rebuild，数据迁移可验证、可回滚 |
