# RagFlow 知识库配置信息

## 📋 基本信息

### RagFlow 服务
- **地址**: http://172.136.16.14:8080
- **API Key**: `ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0`

### 数据集信息
- **名称**: 信阳市智慧巡河报告
- **数据集 ID**: `538b0a5c36ff11f18e7d3d43671e73e4`
- **管理地址**: http://172.136.16.14:8080/dataset/dataset/538b0a5c36ff11f18e7d3d43671e73e4
- **文档数量**: 84 个
- **内容范围**: 第 1-3 章、第 5 章、第 6 章

## 🔧 环境变量配置

### 方式 1: 在 .env 文件中配置

创建或编辑 `/home/server/python/map_assistant_v1/backend/.env`：

```bash
# RagFlow 知识库配置
RAGFLOW_API_KEY=ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0
RAGFLOW_API_BASE=http://172.136.16.14:8080/api/v1
RAGFLOW_DATASET_ID=538b0a5c36ff11f18e7d3d43671e73e4
```

### 方式 2: 在系统环境中配置

```bash
export RAGFLOW_API_KEY="ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0"
export RAGFLOW_API_BASE="http://172.136.16.14:8080/api/v1"
export RAGFLOW_DATASET_ID="538b0a5c36ff11f18e7d3d43671e73e4"
```

### 方式 3: 使用默认配置（已内置）

如果不设置环境变量，工具将使用以下默认值（与上述配置相同）：

```python
# 在 ragflow_knowledge_tool.py 中
api_key = "ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0"
api_base = "http://172.136.16.14:8080/api/v1"
dataset_id = "538b0a5c36ff11f18e7d3d43671e73e4"
```

## 📊 数据集内容详情

### 章节分布

| 章节 | 文档数 | 说明 |
|------|--------|------|
| 第 1-3 章 | 17 个 | 项目概况、组织实施、项目汇总 |
| 第 5 章 | 65 个 | 各县区采砂场监测评估意见 |
| 第 6 章 | 2 个 | 总结与建议 |
| **总计** | **84 个** | - |

### 资源统计

- **总图片数**: 404 个
- **总表格数**: 7 个
- **文档类型**: text_section, table_chunk

## 🚀 快速验证

### 1. 测试 RagFlow 连接

```bash
cd /home/server/python/map_assistant_v1/backend
python3 test_ragflow_api.py
```

### 2. 触发文档解析（如果还未解析）

```bash
cd /home/server/python/大模型
python3 trigger_ragflow_parse.py
```

### 3. 重启后端服务

```bash
# 如果使用 PM2
pm2 restart map-assistant-backend

# 或直接运行
cd /home/server/python/map_assistant_v1/backend
python3 main.py
```

### 4. 测试知识库功能

访问前端知识库管理页面，或在 Agent 中测试：

```python
# Agent 工具调用示例
knowledge_base_tool(
    operation='search',
    query='超深度开采判定规则'
)
```

## 📝 API 端点

### RagFlow API

```
基础 URL: http://172.136.16.14:8080/api/v1

文档列表:
GET /api/v1/datasets/{dataset_id}/documents

知识检索:
POST /api/v1/retrieval

文档内容:
GET /api/v1/datasets/{dataset_id}/documents/{document_id}/chunks

触发解析:
POST /api/v1/datasets/{dataset_id}/chunks
```

### 地图助手 API（保持不变）

```
基础 URL: http://your-server:8006

知识库列表:
GET /api/knowledge

知识库内容:
GET /api/knowledge/{document_id}
```

## 🔍 常见问题

### Q: 搜索返回空内容？

**A**: 文档未解析。执行以下命令：
```bash
python3 /home/server/python/大模型/trigger_ragflow_parse.py
```

### Q: 如何在 RagFlow 控制台查看文档？

**A**: 访问 http://172.136.16.14:8080/dataset/dataset/538b0a5c36ff11f18e7d3d43671e73e4

### Q: 如何添加新文档？

**A**: 
1. 登录 RagFlow 控制台
2. 进入"信阳市智慧巡河报告"数据集
3. 点击"添加文档"
4. 上传文件或粘贴文本
5. 触发解析

### Q: 如何删除文档？

**A**: 
1. 登录 RagFlow 控制台
2. 进入数据集
3. 选择要删除的文档
4. 点击"删除"

## 📞 技术支持

如遇到问题，请检查：

1. ✅ RagFlow 服务是否运行
   ```bash
   curl http://172.136.16.14:8080/api/v1/datasets
   ```

2. ✅ API Key 是否正确
   - 当前: `ragflow-jZ-6x-X_PGr5ULHFSPqWhfbmd-0xlU_naoGg0hLc3K0`

3. ✅ 数据集 ID 是否正确
   - 当前: `538b0a5c36ff11f18e7d3d43671e73e4`

4. ✅ 查看后端日志
   ```bash
   tail -f /home/server/python/map_assistant_v1/backend/backend.log
   ```

## 📚 相关文档

- [迁移完成总结](./MIGRATION_COMPLETE.md)
- [详细迁移说明](./RAGFLOW_MIGRATION.md)
- [RagFlow 知识库工具](./backend/tools/ragflow_knowledge_tool.py)
