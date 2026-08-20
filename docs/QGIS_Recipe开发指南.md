# QGIS Recipe 开发指南

## 概述

`qgis_workflows.py` 是 QGIS 空间分析功能的统一配置中心。每个 GIS 操作（缓冲区、裁剪、中心点、面积计算等）都定义为一个 **Recipe**——一份纯数据声明，不含任何业务代码。

**新增一个 GIS 功能 = 在这个文件里加一个 Recipe dict，不需要改 `task_executor.py`。**

---

## Recipe 结构速览

```python
"操作名称": {
    "keywords":       [...],      # 触发关键词
    "extract":        [...],      # 参数提取规则
    "steps":          [...],      # MCP 调用序列
    "post_process":   {...},      # 后处理（可选）
    "result_message": "...",      # 返回给用户的文案
}
```

### 各字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `keywords` | `list[str]` | ✅ | 用户说了这些词就匹配到本 recipe，分数最高的胜出 |
| `extract` | `list[dict]` | ✅ | 从用户消息用正则提取参数，没匹配到就用默认值 |
| `steps` | `list[dict]` | ✅ | 按顺序执行的 MCP 调用，每步都可以引用前面捕获的变量 |
| `post_process` | `dict` | ❌ | 后处理，目前支持 `combine_geojson`（合并多个 GeoJSON） |
| `result_message` | `str` | ✅ | 返回文案，支持 `${变量名}` 引用 |

---

## 完整示例：添加"计算几何中心点"

### 需求

用户说"计算郝楼砂场的中心点"，系统调用 QGIS `native:centroids` 算法，输出中心点坐标并加载到地图。

### 对应 MCP 调用序列

```
Step 1: layer/add_vector    → 加载 SHP 文件
Step 2: layer/export        → 筛选目标要素、导出到 EPSG:4326
Step 3: layer/add_vector    → 加载筛选后的图层
Step 4: processing/execute  → 执行 native:centroids 算法
Step 5: layer/add_vector    → 加载中心点图层
Step 6: layer/export        → 导出中心点到 EPSG:4326
```

### 成品 Recipe

```python
"centroid": {
    "description": "计算指定要素的几何中心点",
    "keywords": ["中心点", "中心", "centroid", "几何中心", "重心"],
    "extract": [
        {"param": "shp_path",     "patterns": _PARAM_PATTERNS["shp_path"]},
        {"param": "feature_name", "patterns": _PARAM_PATTERNS["feature_name"]},
    ],
    "steps": [
        # 1. 加载源 SHP
        {"category": "layer", "action": "add_vector",
         "params": {"path": "$shp_path", "name": "source_data"},
         "capture": {"id": "source_layer_id"}},

        # 2. 筛选要素 → EPSG:4326
        {"category": "layer", "action": "export",
         "params": {
             "layer_id": "$source_layer_id",
             "output_path": "/output/${feature_name}_${uid}_4326.geojson",
             "filter_expression": "Name LIKE '%${feature_name}%'",
             "target_crs": "EPSG:4326",
         }},

        # 3. 加载筛选后图层
        {"category": "layer", "action": "add_vector",
         "params": {"path": "/output/${feature_name}_${uid}_4326.geojson",
                     "name": "${feature_name}_4326"},
         "capture": {"id": "filtered_layer_id"}},

        # 4. 执行 centroids
        {"category": "processing", "action": "execute",
         "params": {
             "algorithm": "native:centroids",
             "parameters": {
                 "INPUT": "$filtered_layer_id",
                 "OUTPUT": "/output/${feature_name}_centroid_${uid}.geojson",
             },
         }},

        # 5. 加载中心点图层
        {"category": "layer", "action": "add_vector",
         "params": {"path": "/output/${feature_name}_centroid_${uid}.geojson",
                     "name": "${feature_name}_centroid"},
         "capture": {"id": "centroid_layer_id"}},

        # 6. 导出中心点（可能不需要，但保证输出在 EPSG:4326）
        {"category": "layer", "action": "export",
         "params": {
             "layer_id": "$centroid_layer_id",
             "output_path": "/output/${feature_name}_centroid_${uid}_4326.geojson",
             "target_crs": "EPSG:4326",
         }},
    ],
    "result_message": "已计算${feature_name}的几何中心点，并标注在地图上。",
},
```

---

## 核心语法详解

### 1. 参数提取（extract）

每个参数由 `param`（变量名）和 `patterns`（提取规则）组成：

```python
"shp_path": {
    "regex": r"(/[\w./-]+\.shp)",          # 提取正则
    "default": "/gis_data/2026年采区.shp",  # 匹配不到时的默认值
}
"feature_name": {
    "regex": r"(?:对|给|为|的)?([\u4e00-\u9fa5]{2,10}(?:砂场|采区|河道|工程|水库|河流))",
    "default": "",
    "clean": r"^[对给为的]",                # 去掉提取结果开头的介词
}
"distance": {
    "regex": r"(\d+)\s*米",
    "default": 100,
}
```

**新增自定义参数**：在文件顶部 `_PARAM_PATTERNS` 里加一个即可，所有 recipe 都能复用。

### 2. 变量引用

在 `steps` 的任何字符串值里，可以用 `$变量名` 或 `${变量名}` 引用：

| 变量来源 | 示例 |
|----------|------|
| extract 提取的参数 | `$shp_path`、`$feature_name`、`$distance` |
| capture 捕获的值 | `$source_layer_id`、`$filtered_layer_id` |
| 系统自动生成 | `$uid`（8 位随机 ID，防止文件名冲突） |

### 3. capture 机制

`capture` 告诉引擎从 MCP 返回值中提取数据，供后续步骤使用：

```python
"capture": {"id": "source_layer_id"}
# 含义：从 MCP 返回 JSON 中取 "id" 字段，存到变量 source_layer_id
```

目前支持的 capture key：
- `"id"`：提取图层 ID（用于 link/add_vector 的返回值）

### 4. 后处理（post_process）

#### combine_geojson — 合并多个 GeoJSON

```python
"post_process": {
    "combine_geojson": {
        "inputs": [
            "/output/${feature_name}_original_${uid}_4326.geojson",
            "/output/${feature_name}_buffer_${uid}_4326.geojson",
        ],
        "tags": [
            {"_type": "original", "_name": "${feature_name}范围"},
            {"_type": "buffer",  "_name": "${feature_name}${distance}米缓冲区"},
        ],
        "output": "/output/${feature_name}_combined_${uid}.geojson",
    }
}
```

- `inputs`：要合并的 GeoJSON 文件列表
- `tags`：给每个文件的 features 打上属性标签（用于前端区分）
- `output`：合并后的输出路径

### 5. 返回文案

```python
"result_message": "已为${feature_name}范围创建${distance}米缓冲区，共 2 个图层。"
```

支持所有变量的替换。`map_command` 由引擎自动生成，指向最终 GeoJSON。

---

## 如何发现 QGIS 可用算法

通过 MCP 查询 QGIS 引擎提供的所有处理算法：

```bash
curl -s -X POST http://localhost:8036/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "processing",
      "arguments": {
        "action": "list_algorithms",
        "params": {}
      }
    }
  }' | python3 -c "
import json,sys
d = json.load(sys.stdin)
# 打印算法名
for item in d.get('result',{}).get('content',[]):
    text = item.get('text','')
    if text:
        data = json.loads(text) if isinstance(text,str) else text
        if isinstance(data, list):
            for alg in data:
                print(alg.get('name',''))
"
```

常用算法速查：

| 算法 | 用途 | 典型步骤数 |
|------|------|-----------|
| `native:buffer` | 缓冲区 | 5-7（需 CRS 转换） |
| `native:centroids` | 几何中心点 | 3-4 |
| `native:clip` | 裁剪 | 2-3 |
| `native:intersection` | 相交 | 2-3 |
| `native:difference` | 差集 | 2-3 |
| `native:dissolve` | 融合 | 2 |
| `native:simplify` | 简化 | 2 |
| `native:multiparttosingleparts` | 多部件→单部件 | 2 |

---

## 调试方法

### 看匹配日志

```
[qgis_workflow] Matched: buffer
[qgis_workflow] Vars: {"feature_name":"郝楼砂场","distance":"200",...}
```

如果没看到 `Matched`，说明关键词没命中。检查 `keywords` 里有没有用户输入中的词。

### 看步骤执行日志

```
[qgis_workflow] Captured source_layer_id=source_data_fa7f952b...
```

如果某步失败，对应 `success=False` 会在日志体现。

### 直接测试 MCP

绕开后端，直接 curl MCP 验证每步是否正常：

```bash
# 测试加载 SHP
curl -s -X POST http://localhost:8036/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"layer","arguments":{"action":"add_vector",
       "params":{"path":"/gis_data/2026年采区.shp","name":"test"}}}}'
```

---

## 最小 Recipe 模板

复制下面这段，改三个地方就能用：

```python
"你的操作名": {
    "keywords": ["触发词A", "trigger_word_b"],
    "extract": [
        {"param": "shp_path",     "patterns": _PARAM_PATTERNS["shp_path"]},
        {"param": "feature_name", "patterns": _PARAM_PATTERNS["feature_name"]},
    ],
    "steps": [
        # 加载
        {"category": "layer", "action": "add_vector",
         "params": {"path": "$shp_path", "name": "src"},
         "capture": {"id": "src_id"}},
        # 你的核心算法
        {"category": "processing", "action": "execute",
         "params": {"algorithm": "native:你的算法名",
                    "parameters": {"INPUT": "$src_id",
                                   "OUTPUT": "/output/${feature_name}_${uid}.geojson"}}},
    ],
    "result_message": "已完成${feature_name}的某某操作。",
},
```

---

## 常见问题

### Q: recipe 怎么和已有的冲突？

A: 引擎按关键词分数自动匹配，分数最高的胜出。如果两个 recipe 分数相同，取 `RECIPES` 字典中先定义的那个。

### Q: 步骤之间怎么传数据？

A: 用 `capture` 捕获上一步的返回值。capture 的 `id` 会存成变量，后续步骤用 `$变量名` 引用。

### Q: 怎么处理 CRS 坐标系问题？

A: 缓冲区这种需要精确距离的操作，中间步骤用 `EPSG:3857`（米制），最后导出用 `EPSG:4326`（经纬度）。直接在 recipe 的 `export` 步骤里设 `target_crs`。

### Q: 不改 `task_executor.py`，post_process 能扩展吗？

A: 目前支持 `combine_geojson`。需要新的后处理类型时，在 `_execute_qgis_workflow` 方法（`task_executor.py`）的"后处理"段落里加一个分支即可，只需改那 10 行。
