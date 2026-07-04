# 知识库模块迁移至 RagFlow 说明

## 📋 概述

地图助手项目 (`map_assistant_v1`) 的知识库模块已从 **Dify** 迁移至 **RagFlow** 框架。

## 🔄 变更内容

### 1. 后端工具替换

- **旧文件**: `backend/tools/knowledge_base_tool.py` (Dify 实现)
- **新文件**: `backend/tools/ragflow_knowledge_tool.py` (RagFlow 实现)
- **导入更新**: `main.py` 中的导入语句已更新

### 2. 配置变更

#### 环境变量配置

```bash
# RagFlow API 配置
export RAGFLOW_API_KEY="ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0"
export RAGFLOW_API_BASE="http://172.136.16.14:8080/api/v1"
export RAGFLOW_DATASET_ID="538b0a5c36ff11f18e7d3d43671e73e4"

# 旧的 Dify 配置（已弃用）
# export DIFY_KNOWLEDGE_API_KEY="..."
# export DIFY_API_BASE="..."
# export DIFY_DATASET_ID="..."
```

#### 默认配置

如果未设置环境变量，工具将使用以下默认值：

```python
api_key = "ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0"
api_base = "http://172.136.16.14:8080/api/v1"
dataset_id = "538b0a5c36ff11f18e7d3d43671e73e4"  # 信阳市智慧巡河报告
```

### 3. API 接口变化

#### RagFlow 检索 API

```python
# 请求端点
POST /api/v1/retrieval

# 请求体
{
    "question": "搜索关键词",
    "dataset_ids": ["数据集ID"],
    "top_k": 5,
    "similarity_threshold": 0.2,
    "vector_similarity_weight": 0.3
}

# 响应格式
{
    "code": 0,
    "data": {
        "chunks": [
            {
                "chunk_id": "...",
                "document_id": "...",
                "docnm_kwd": "文档名称",
                "content_with_weight": "内容片段",
                "similarity": 0.95,
                "meta_fields": {}
            }
        ]
    }
}
```

#### RagFlow 文档列表 API

```python
# 请求端点
GET /api/v1/datasets/{dataset_id}/documents

# 查询参数
{
    "page": 1,
    "page_size": 100,
    "orderby": "create_time",
    "desc": "true"
}
```

#### RagFlow 文档内容 API

```python
# 请求端点
GET /api/v1/datasets/{dataset_id}/chunks

# 查询参数
{
    "document_id": "文档ID",
    "page": 1,
    "page_size": 100
}
```

## 📊 功能对比

| 功能 | Dify | RagFlow | 状态 |
|------|------|---------|------|
| 知识检索 | ✅ | ✅ | 已迁移 |
| 文档列表 | ✅ | ✅ | 已迁移 |
| 获取文档内容 | ✅ | ✅ | 已迁移 |
| 添加文档 | ✅ | ❌ | 暂不支持（需通过 RagFlow 控制台） |
| 删除文档 | ✅ | ❌ | 暂不支持（需通过 RagFlow 控制台） |

## 🚀 使用方式

### Agent 工具调用

知识库工具在 Agent 中的使用方式保持不变：

```python
# 搜索知识
knowledge_base_tool(
    operation='search',
    query='超深度开采判定规则'
)

# 列出文档
knowledge_base_tool(
    operation='list_topics'
)

# 获取文档内容
knowledge_base_tool(
    operation='get_content',
    document_id='文档ID'
)
```

### 前端访问

前端知识库管理页面的 API 调用方式不变：

- `GET /api/knowledge` - 获取文档列表
- `GET /api/knowledge/{document_id}` - 获取文档内容
- `POST /api/knowledge` - 添加文档（暂不支持）
- `DELETE /api/knowledge/{kb_id}` - 删除文档（暂不支持）

## 📝 已上传的知识文档

当前 RagFlow 数据集 (`信阳市智慧巡河报告`) 包含：

- **总文档数**: 84 个
- **总图片数**: 404 个
- **总表格数**: 7 个
- **章节范围**: 第 1-3 章、第 5 章、第 6 章

文档列表可通过以下方式查看：

1. **RagFlow 控制台**: http://172.136.16.14:8080
2. **API 调用**: `GET /api/knowledge`
3. **前端界面**: 地图助手 -> 知识库管理

## ⚠️ 注意事项

1. **文档管理**: 目前添加和删除文档需要通过 RagFlow 控制台操作，暂不支持通过 API 自动完成
2. **检索参数**: RagFlow 的检索算法与 Dify 不同，可能需要调整 `similarity_threshold` 和 `vector_similarity_weight` 参数以获得最佳效果
3. **向后兼容**: 旧的 `knowledge_base_tool.py` 文件已保留但不再使用，可在确认新工具稳定后删除

## 🔧 回滚方案

如需回滚到 Dify 版本：

1. 修改 `main.py` 第 77 行：
   ```python
   # 改回
   from tools.knowledge_base_tool import KnowledgeBaseTool
   ```

2. 恢复环境变量：
   ```bash
   export DIFY_KNOWLEDGE_API_KEY="..."
   export DIFY_API_BASE="http://172.136.16.52:83/v1"
   export DIFY_DATASET_ID="5ec0a57c-21be-43b4-8cd3-c34f657c3efe"
   ```

3. 重启后端服务

## 📞 技术支持

如遇到问题，请检查：

1. RagFlow 服务是否正常运行: `http://172.136.16.14:8080`
2. API Key 是否正确
3. 数据集 ID 是否正确
4. 后端日志: `/home/server/python/map_assistant_v1/backend/backend.log`
