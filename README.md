<div align="center">
  <h1>🗺️ 智能一张图 · AI 地图助手</h1>
  <p>
    <strong>面向水利与自然资源领域的智能空间分析平台</strong>
  </p>
  <p>
    自然语言驱动 · 双地图引擎 · 知识库增强 · 自动化报告
  </p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.10+-blue" alt="Python">
    <img src="https://img.shields.io/badge/FastAPI-0.100+-green" alt="FastAPI">
    <img src="https://img.shields.io/badge/React-18-61dafb" alt="React">
    <img src="https://img.shields.io/badge/Leaflet-1.9-199900" alt="Leaflet">
    <img src="https://img.shields.io/badge/Cesium-3D-orange" alt="Cesium">
    <img src="https://img.shields.io/badge/PostGIS-3.0+-336791" alt="PostGIS">
    <img src="https://img.shields.io/badge/Qwen-Agent-purple" alt="Qwen Agent">
  </p>
</div>

---

## 📖 项目简介

**智能一张图**是一个将大语言模型（LLM）与地理空间技术深度融合的 AI 应用平台。你只需要用日常语言描述需求——"帮我看看固始县有哪些采砂场"、"分析这片河道近两年的变化"、"生成一份带地图的巡检报告"——系统就能自动调用数据库、地图、知识库、影像分析等工具，完成从数据查询到可视化展示再到报告输出的完整流程。

平台专为**水利工程管理、河道巡检、采砂监管、遥感变化检测**等场景设计，已在河南省多个水利项目中实际应用。

---

## ✨ 核心能力

<table>
  <tr>
    <td width="50%">
      <h3>🤖 AI 智能对话</h3>
      <p>基于 Qwen3 大模型 + Qwen Agent 框架，理解自然语言指令，自主编排多工具协作。支持流式输出（SSE），打字机效果实时响应。</p>
    </td>
    <td width="50%">
      <h3>🗺️ 双地图引擎</h3>
      <p><b>2D 地图</b>：Leaflet + GeoJSON/MVT 矢量瓦片，支持底图切换（Esri卫星/OSM/高分影像）<br><b>3D 地图</b>：Cesium + 3D Tiles，支持倾斜摄影、地形模型加载</p>
    </td>
  </tr>
  <tr>
    <td>
      <h3>🗄️ 空间数据库查询</h3>
      <p>集成 PostgreSQL + PostGIS，支持矢量数据的属性筛选、空间过滤、聚合统计，结果以上图+表格双模式呈现。</p>
    </td>
    <td>
      <h3>📚 知识库检索</h3>
      <p>支持 Dify / LlamaIndex / RagFlow 多种知识库引擎，可检索水利标准、政策法规、项目文档，提供精准引用回答。</p>
    </td>
  </tr>
  <tr>
    <td>
      <h3>📊 智能可视化</h3>
      <p>AI 自动将查询结果生成 ECharts 图表（折线图、柱状图、饼图等），配合数据摘要表格，一目了然。</p>
    </td>
    <td>
      <h3>📝 自动报告生成</h3>
      <p>一键生成包含地图截图、数据图表、文字分析的专业 Word 报告，支持模板定制和离线下载。</p>
    </td>
  </tr>
  <tr>
    <td>
      <h3>🛰️ GIS 影像处理</h3>
      <p>集成 GDAL 工具链，支持 GeoTIFF 批量预处理、坐标系修复、去黑边、影像镶嵌、重采样、水体提取等操作。</p>
    </td>
    <td>
      <h3>🔍 变化检测</h3>
      <p>集成 SAM + OmniOVCD 模型，支持多时相遥感影像的河道侵占、违章建筑自动识别与变化标注。</p>
    </td>
  </tr>
  <tr>
    <td>
      <h3>✈️ 无人机影像</h3>
      <p>支持无人机航飞栅格影像加载与集成，可在 2D/3D 地图中叠加显示高分辨率航拍成果。</p>
    </td>
    <td>
      <h3>🧩 切片管理</h3>
      <p>提供 3D Tiles、矢量瓦片等切片数据的上传、注册、预览与管理界面，支持 Cesium 联动加载。</p>
    </td>
  </tr>
</table>

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                      用户界面 (React)                        │
│  ┌──────────┬──────────┬──────────┬──────────┬───────────┐  │
│  │ AI 对话  │ 2D 地图  │ 3D 地图  │ 知识库   │ 切片管理  │  │
│  │  面板    │ (Leaflet)│ (Cesium) │ 管理面板 │   面板    │  │
│  └──────────┴──────────┴──────────┴──────────┴───────────┘  │
└──────────────────────┬──────────────────────────────────────┘
                       │  REST API / SSE
┌──────────────────────▼──────────────────────────────────────┐
│                    FastAPI 后端服务                           │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              Qwen Agent 智能编排层                     │  │
│  │   意图识别 → 任务拆分 → 工具选择 → 结果整合            │  │
│  └──────────────────────────────────────────────────────┘  │
│  ┌──────────┬──────────┬──────────┬────────────────────┐  │
│  │ MapTool  │ PostgreSQL│Knowledge │ DataVisualizer     │  │
│  │ 地图操作  │   Tool   │ BaseTool │    Tool 图表生成    │  │
│  │          │ 数据库查询│ 知识检索  │                    │  │
│  ├──────────┼──────────┼──────────┼────────────────────┤  │
│  │ Report   │ Cesium   │  GIS     │ ChangeDetection    │  │
│  │Generator │  Tool    │ Pipeline │    Tool 变化检测    │  │
│  │ 报告生成  │ 3D 控制  │ 影像处理  │                    │  │
│  └──────────┴──────────┴──────────┴────────────────────┘  │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│                    数据与模型层                               │
│  ┌──────────┬──────────┬──────────┬──────────────────────┐ │
│  │PostgreSQL│ Dify /   │  GDAL    │  SAM / OmniOVCD      │ │
│  │ +PostGIS │LlamaIndex│ 影像引擎 │  变化检测模型         │ │
│  └──────────┴──────────┴──────────┴──────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## 🎬 典型使用场景

### 场景一：河道采砂监管
> "帮我查一下固始县范围内有多少个采砂场，在地图上标出来，用红色标注可采区、蓝色标注禁采区。"

系统自动：查询 PostGIS 数据库 → 获取 GeoJSON 矢量数据 → 按类型着色 → 在地图上叠加显示 → 附上游标信息弹窗。

### 场景二：违章建筑检测
> "对比 2023 年和 2024 年这段河道的卫星影像，看看有没有新增的建筑物。"

系统自动：调用 SAM 模型分割影像 → OmniOVCD 进行变化检测 → 标注变化区域 → 生成对比报告。

### 场景三：GIS 数据预处理
> "把这三个县的 GeoTIFF 影像拼到一起，去掉黑边，统一转成 CGCS2000 坐标系。"

系统自动：GDAL 批量处理 → 坐标系检测与修复 → 影像镶嵌 → 去黑边 → 输出标准化成果。

### 场景四：巡检报告生成
> "根据今天的巡检数据，生成一份包含地图截图、统计图表和文字总结的周报。"

系统自动：查询数据库 → 生成统计图表 → 截取地图 → 组装 Word 报告 → 提供下载链接。

---

## 🚀 快速开始

### 环境要求
- Python 3.10+
- Node.js 18+
- PostgreSQL 14+ (需安装 PostGIS 扩展)
- GDAL 3.0+

### 后端启动

```bash
# 安装依赖
cd backend
pip install -r requirements.txt

# 配置环境变量（参考 .env.template）
cp .env.template .env
# 编辑 .env 填入 API Key、数据库连接等信息

# 启动服务
python main.py
# 服务默认运行在 http://localhost:8006
```

### 前端启动

```bash
cd frontend
npm install
npm start
# 前端默认运行在 http://localhost:3003
```

### Docker 部署

```bash
# 使用 docker-compose 一键启动
docker-compose up -d
```

---

## 📂 项目结构

```
.
├── backend/                     # FastAPI 后端
│   ├── main.py                  # 服务入口，API 路由
│   ├── prompts.py               # 系统提示词配置
│   ├── agents/                  # AI 智能体（意图识别、任务执行）
│   ├── tools/                   # 工具集（20+ 个功能模块）
│   │   ├── map_tool.py          # 2D 地图操作工具
│   │   ├── cesium_tool.py       # 3D Cesium 控制工具
│   │   ├── postgresql_tool.py   # 数据库查询工具
│   │   ├── data_visualizer_tool.py  # 图表生成工具
│   │   ├── report_generator_tool.py # 报告生成工具
│   │   ├── knowledge_base_tool.py   # 知识库检索工具
│   │   ├── spatial_processing_tool.py # GIS 空间处理工具
│   │   ├── sam_predict.py       # SAM 分割推理
│   │   ├── omniovcd_inference.py # 变化检测推理
│   │   └── ...
│   ├── config/                  # 配置文件
│   └── templates/               # 报告模板
├── frontend/                    # React 前端
│   ├── src/
│   │   ├── App.jsx              # 主应用组件
│   │   └── components/
│   │       ├── MapComponent.jsx      # 2D Leaflet 地图
│   │       ├── CesiumComponent.jsx   # 3D Cesium 地图
│   │       ├── GisPipeline.jsx       # GIS 处理流水线
│   │       ├── SAMPanel.jsx          # SAM 检测面板
│   │       ├── TileManager.jsx       # 切片管理面板
│   │       └── KnowledgeBaseManager.jsx # 知识库管理
│   └── public/
├── deploy/                      # 部署配置
│   ├── docker-compose.geoserver.yml
│   └── nginx.conf
├── docs/                        # 技术文档
│   ├── 技术架构说明.md
│   ├── 变化检测技术方案.md
│   └── ...
├── .gitignore
└── README.md
```

---

## 🛠️ 技术栈

| 层级 | 技术 |
|------|------|
| **AI 引擎** | Qwen3 / Qwen Agent、DashScope |
| **后端框架** | FastAPI、Uvicorn、PM2 |
| **前端框架** | React 18、Create React App |
| **2D 地图** | Leaflet、Leaflet.VectorGrid、Esri Leaflet |
| **3D 地图** | CesiumJS、3D Tiles |
| **空间数据库** | PostgreSQL 14、PostGIS 3.x |
| **影像处理** | GDAL、Rasterio、NumPy |
| **AI 模型** | SAM、OmniOVCD、YOLOv11 |
| **知识库** | Dify、LlamaIndex、RagFlow |
| **可视化** | ECharts |
| **部署** | Docker、Nginx、PM2 |

---

## 🔧 配置说明

主要环境变量（`backend/.env`）：

| 变量名 | 说明 |
|--------|------|
| `DASHSCOPE_API_KEY` | 阿里云 DashScope API 密钥（LLM 推理） |
| `DIFY_API_BASE` | Dify 知识库 API 地址 |
| `DIFY_KNOWLEDGE_API_KEY` | Dify 知识库访问密钥 |
| `PG_HOST` / `PG_PORT` / `PG_USER` / `PG_PASSWORD` / `PG_DATABASE` | PostgreSQL 数据库连接 |
| `CUDA_VISIBLE_DEVICES` | GPU 设备配置（SAM/变化检测用） |

> ⚠️ 请勿将 `.env` 文件提交到 Git，仓库中只保留 `.env.template` 作为参考模板。

---

## 📄 License

本项目仅限内部使用和学习参考。

---

<div align="center">
  <p>Made with ❤️ for Water Resources Digitalization</p>
</div>
