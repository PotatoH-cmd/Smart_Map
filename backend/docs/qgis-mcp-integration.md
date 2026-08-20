# 地图助手 v1 集成 QGIS MCP —— 时空处理能力拓展方案

## 一、背景

当前地图助手已经具备基础的时空能力：

- **坐标转换**：CGCS2000 ↔ WGS84 投影变换、带号去留、XY 自动交换
- **矢量生成**：将用户输入的坐标生成点/线/面 GeoJSON 并加载到地图
- **空间参考查询**：查询河道红线、采区边界并作为空间约束参与后续分析

但这些都属于"坐标搬运"层面，缺少真正的**空间分析能力**——比如给一个采区范围，自动计算面积；给两个图层，自动判断它们有没有重叠；给一组坐标点，自动生成缓冲区做影响范围分析。这些是日常水利管理中最常见的需求。

## 二、方案目标

通过 Docker 部署 [QGIS + QGIS MCP 插件](https://github.com/nkarasiak/qgis-mcp)，让后端的 AI 智能体直接调用 QGIS 的 **100+ 种空间处理算法**，把地图助手从"看图工具"升级为"时空分析引擎"。

## 三、QGIS MCP 是什么

简单讲，QGIS MCP 就是一座"桥"——一头连着大模型（你的后端智能体），另一头连着专业的 GIS 桌面软件 QGIS。桥的两端通过标准协议（MCP / Model Context Protocol）对话，大模型可以用自然语言指挥 QGIS 完成各种专业的地理信息处理任务。

具体来说，这个桥分成两截：

```
后端智能体  ←→  MCP 服务器（HTTP）  ←→  QGIS 插件（socket）  ←→  QGIS 引擎
```

- **QGIS 插件**：装在 QGIS 里，开一个本地 socket 服务（默认端口 9876），接收外部指令，然后在 QGIS 内部执行操作
- **MCP 服务器**：一个独立的 Python 服务，把 socket 上的指令包装成标准的 MCP 工具，通过 HTTP 暴露给大模型

选用的 [nkarasiak/qgis-mcp](https://github.com/nkarasiak/qgis-mcp) 是目前最活跃的 QGIS MCP 项目（版本 0.9.3，2.7 万+下载），已稳定运行近半年，兼容 QGIS 3.28~4.x。

## 四、能力概览——能给地图助手带来什么

这个插件提供了 **117 个工具**，按功能分成 27 组。以下按水利场景列出最有价值的部分：

### 4.1 空间分析（此前完全缺失的能力）

| 工具 | 通俗解释 | 水利场景举例 |
|---|---|---|
| `buffer` | 沿着一个范围向外扩展一圈 | "以河道红线为中心，画 50 米管理范围" |
| `clip` | 用一个范围去"裁"另一个图层 | "统计某采区内的 RTK 测点" |
| `intersection` | 取两个范围的重合部分 | "找出同时位于红线和采区内的区域" |
| `difference` | 从一个范围里挖掉另一个 | "去除已开采区域，看还剩多少可采面积" |
| `spatial_join` | 将两个图层的属性按位置关联 | "给每个测点自动附上其所在采区的名称" |
| `zonal_statistics` | 统计每个区域内栅格的数值 | "计算每个采区内的平均高程" |

### 4.2 数据处理

| 工具 | 用途 |
|---|---|
| `add_vector_layer` / `add_raster_layer` | 加载矢量或栅格数据（shp、geojson、tif 等） |
| `export_layer` | 导出为其他格式 |
| `field_calculator` | 像 Excel 公式一样修改属性表的字段 |
| `raster_calculator` | 对栅格做"加减乘除"运算（如两个时期的影像相减找变化区域） |
| `transform_coordinates` | 任意坐标系之间互转 |
| `execute_processing` | 调用 QGIS 工具箱里的几百种处理算法 |

### 4.3 地图渲染与出图

| 工具 | 用途 |
|---|---|
| `render_map` | 渲染地图画面 |
| `export_layout` | 将地图布局导出为图片或 PDF |
| `add_layout_map` / `add_layout_legend` | 创建专业打印布局（加图例、比例尺等） |

### 4.4 其他能力

- **SQL 查询**：对数据库图层直接写 SQL (`execute_sql`)
- **样式管理**：自动配色、分级渲染 (`set_layer_style`)
- **代码执行**：运行自定义 PyQGIS 脚本 (`execute_code`)
- **批量命令**：一次发送多个操作 (`batch_commands`)

> **模式选择**：推荐启用 `compound` 模式（27 个分组工具，每个带 `action` 参数），而非默认的 granular 模式（117 个平铺工具）。因为 117 个工具的 schema 太大，塞进大模型的上下文窗口会占用大量 token，影响理解质量。compound 模式把功能按类别收拢，模型先选大类再选具体操作，更符合人的思维习惯。

## 五、整体部署架构

```
┌──────────────────────────────────────────────────────────┐
│  Docker 容器: qgis-mcp                                   │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  Xvfb（虚拟显示器，让 QGIS 以为有屏幕）              │ │
│  │  ↓                                                  │ │
│  │  QGIS 桌面版（无头模式运行）                         │ │
│  │  ├── 已安装 qgis_mcp_plugin（自动启动 socket 服务） │ │
│  │  └── 内置 PyQGIS 引擎 + 全部处理算法                │ │
│  │       ↓ 本地 socket :9876                           │ │
│  │  MCP 服务器（FastMCP）                              │ │
│  │  ├── QGIS_MCP_TRANSPORT=streamable-http             │ │
│  │  └── 监听 0.0.0.0:8000                              │ │
│  └─────────────────────────────────────────────────────┘ │
│                     ↓ 映射宿主机 :8036 → 容器 :8000      │
└──────────────────────────────────────────────────────────┘
                         ↓ HTTPS 内部网络
┌──────────────────────────────────────────────────────────┐
│  后端服务（map_assistant_v1）                             │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  qgis_mcp_tool（新增）                               │ │
│  │  ├── 通过 HTTP 调用容器 MCP Server                   │ │
│  │  ├── 标准 MCP streamable-http 协议                  │ │
│  │  └── 仿照 mcp_postgres_tool 实现                    │ │
│  ├─────────────────────────────────────────────────────┤ │
│  │  ToolRegistry 注册 → Agent Harness 分派              │ │
│  │  ├── 新增 IntentType: SPATIAL_ANALYSIS              │ │
│  │  └── map_agent 工具列表加入 qgis_mcp_tool            │ │
│  └─────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

## 六、Docker 部署详细方案

### 6.1 容器配置

```dockerfile
# 建议文件名: docker/qgis-mcp/Dockerfile
FROM ubuntu:22.04

# 避免安装时的交互提示
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

# 安装系统基础 + QGIS 3.28 LTS + Xvfb（虚拟显示器）+ 额外依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gnupg software-properties-common \
    xvfb x11-utils \
    qgis qgis-plugin-grass \
    python3-pip python3-venv python3-pyqt5 \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv 包管理器（qgis-mcp server 的推荐启动器）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 创建运行用户（避免 root 跑 QGIS）
RUN useradd -m -s /bin/bash qgis
WORKDIR /home/qgis

# 预配置 QGIS 插件自动启动（写入 QgsSettings 配置文件）
RUN mkdir -p /home/qgis/.local/share/QGIS/QGIS3/profiles/default/QGIS \
    && python3 -c "
import configparser, os
ini = configparser.ConfigParser()
ini['qgis_mcp'] = {
    'autostart': 'true',
    'first_run': 'false',
    'port': '9876'
}
os.makedirs('/home/qgis/.local/share/QGIS/QGIS3/profiles/default/QGIS/', exist_ok=True)
with open('/home/qgis/.local/share/QGIS/QGIS3/profiles/default/QGIS/QGIS3.ini', 'w') as f:
    ini.write(f)
"
# 也可以通过环境变量方式：
# QGIS_CUSTOM_CONFIG_PATH 指向预置的 QGIS3.ini

# 启动脚本
COPY entrypoint.sh /home/qgis/entrypoint.sh
RUN chmod +x /home/qgis/entrypoint.sh

USER qgis
EXPOSE 8000 9876

ENTRYPOINT ["/home/qgis/entrypoint.sh"]
```

### 6.2 启动脚本

```bash
#!/bin/bash
# entrypoint.sh
set -e

# 检查 MCP 插件是否已安装（首次需要手动装，后续挂载卷持久化）
QGIS_PLUGIN_DIR="$HOME/.local/share/QGIS/QGIS3/profiles/default/python/plugins/qgis_mcp_plugin"
if [ ! -d "$QGIS_PLUGIN_DIR" ]; then
    echo "=== 安装 QGIS MCP 插件 ==="
    # 从 GitHub 下载插件到 QGIS 插件目录
    uvx --from https://github.com/nkarasiak/qgis-mcp/archive/refs/heads/main.zip \
        python -c "
import zipfile, io, sys, os, shutil
import urllib.request
# 下载 zip
resp = urllib.request.urlopen('https://github.com/nkarasiak/qgis-mcp/archive/refs/heads/main.zip')
z = zipfile.ZipFile(io.BytesIO(resp.read()))
extract_dir = '$HOME/.local/share/QGIS/QGIS3/profiles/default/python/plugins/qgis_mcp_plugin'
# 创建临时目录
tmp = '$HOME/.tmp_qgis_plugin'
if os.path.exists(tmp):
    shutil.rmtree(tmp)
z.extractall(tmp)
# 插件在 qgis-mcp-main/qgis_mcp_plugin/
src = os.path.join(tmp, 'qgis-mcp-main', 'qgis_mcp_plugin')
if os.path.exists(extract_dir):
    shutil.rmtree(extract_dir)
shutil.copytree(src, extract_dir)
shutil.rmtree(tmp)
print('Plugin installed to', extract_dir)
"
fi

echo "=== 启动虚拟显示器 ==="
# Xvfb 创建一个虚拟屏幕（1024x768，24位色）
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99

echo "=== 启动 QGIS ==="
# 后台启动 QGIS（进程名 qgis 或 qgis.bin）
qgis --nologo &
QGIS_PID=$!

# 等待 QGIS 初始化完毕（插件自动启动 socket 服务）
echo "等待 QGIS 初始化..."
for i in $(seq 1 60); do
    if nc -z localhost 9876 2>/dev/null; then
        echo "QGIS MCP socket 服务已启动 (端口 9876)"
        break
    fi
    sleep 2
    if [ $i -eq 60 ]; then
        echo "错误: QGIS MCP 未能在 2 分钟内启动"
        exit 1
    fi
done

echo "=== 启动 MCP 服务器 ==="
# 使用 streamable-http 传输模式，让后端可以通过 HTTP 调用
export QGIS_MCP_HOST=localhost
export QGIS_MCP_PORT=9876
export QGIS_MCP_TRANSPORT=streamable-http
export QGIS_MCP_TOOL_MODE=compound   # 27 个分组工具，减少 schema 大小
export QGIS_MCP_LOG_LEVEL=WARNING

cd $HOME
exec uvx --from https://github.com/nkarasiak/qgis-mcp/archive/refs/heads/main.zip \
    qgis-mcp-server
```

### 6.3 docker-compose 集成

```yaml
# 添加到现有 docker-compose.yml 或独立文件
services:
  qgis-mcp:
    build:
      context: ./docker/qgis-mcp
      dockerfile: Dockerfile
    container_name: qgis-mcp
    restart: unless-stopped
    ports:
      - "8036:8000"      # MCP HTTP 服务（对后端暴露）
      # 9876 不对外暴露（仅容器内通信）
    environment:
      - QGIS_MCP_TRANSPORT=streamable-http
      - QGIS_MCP_PORT=9876
      - QGIS_MCP_TOOL_MODE=compound
      - QGIS_MCP_TOKEN=${QGIS_MCP_TOKEN:-}  # 可选认证密钥
    volumes:
      # 持久化 QGIS 设置和插件（容器重建后不需重新安装）
      - qgis_data:/home/qgis/.local/share/QGIS
      # 挂载数据目录（让 QGIS 能访问项目的 GIS 数据）
      - /home/server/python/map_assistant_v1/data:/data:ro
      - /home/server/python/GIS:/gis_data:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  qgis_data:
```

### 6.4 容器资源预估

| 资源 | 最低 | 推荐 |
|---|---|---|
| 内存 | 2 GB | 4 GB |
| CPU | 2 核 | 4 核 |
| 磁盘 | 5 GB（镜像 + 插件） | 10 GB（含缓存） |

> QGIS 在处理大栅格时内存占用较高（与 GDAL 类似），具体取决于数据量。日常矢量分析和轻量渲染通常 2GB 够用。

## 七、后端接入方案

### 7.1 MCP 协议适配

项目已有的 `mcp_postgres_tool` 用的是简化版 JSON-RPC（直接 POST `tools/call`），而 QGIS MCP 基于标准的 **MCP streamable-http** 协议。区别在于：

- **标准 MCP 要求先"握手"**：客户端发送 `initialize` 请求 → 服务器返回会话 ID → 客户端发送 `initialized` 通知 → 之后才能调用工具
- **响应是 SSE 流格式**：`Content-Type: text/event-stream`

所以新工具需要实现一个轻量的 MCP streamable-http 客户端，核心流程：

```
1. POST /mcp  →  {jsonrpc:"2.0", method:"initialize", ...}
2. 解析响应头中的 Mcp-Session-Id
3. POST /mcp  →  {jsonrpc:"2.0", method:"notifications/initialized"}
4. POST /mcp  →  {jsonrpc:"2.0", method:"tools/call", params:{name:"buffer", arguments:{...}}}
   后续每次请求都带上 Mcp-Session-Id 头
```

### 7.2 工具实现伪代码

```python
# tools/qgis_mcp_tool.py
@register_tool('qgis_mcp_tool')
class QGISMcpTool(BaseTool):
    """
    QGIS 空间处理工具，通过 MCP 协议连接 QGIS。
    支持：空间分析（缓冲区、裁剪、相交）、数据格式转换、
          坐标系变换、栅格运算、地图渲染出图、SQL 查询等。
    """

    description = '''QGIS 空间分析工具，使用 MCP 协议连接 QGIS 引擎。
    支持操作：buffer（缓冲区）, clip（裁剪）, intersection（相交）,
    spatial_join（空间关联）, zonal_statistics（分区统计）,
    execute_processing（运行处理算法）, render_map（渲染地图）,
    export_layer（导出图层）, transform_coordinates（坐标转换）...'''

    parameters = [
        {
            'name': 'category',  # compound 模式的 action 对应
            'type': 'string',
            'description': '工具类别',
            'enum': ['layer', 'features', 'processing', 'analysis',
                     'render', 'transform', 'query', 'system'],
            'required': True
        },
        {
            'name': 'action',
            'type': 'string',
            'description': '具体操作（如 buffer, clip, execute_processing 等）',
            'required': True
        },
        {
            'name': 'params',
            'type': 'object',
            'description': '操作的参数，JSON 对象格式',
            'required': False
        },
        {
            'name': 'mcpServer',
            'type': 'string',
            'description': 'QGIS MCP Server 地址（默认环境变量 QGIS_MCP_SERVER_URI）',
            'required': False
        }
    ]

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self._session_id = None      # MCP 会话 ID
        self._initialized = False

    def _ensure_session(self, server_uri):
        """确保 MCP 会话已建立（initialize → initialized）"""
        if self._initialized:
            return
        # 1. 发送 initialize 请求
        resp = httpx.post(f"{server_uri}/mcp", json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "map-assistant", "version": "1.0"}
            },
            "id": 1
        })
        self._session_id = resp.headers.get("Mcp-Session-Id")
        # 2. 发送 initialized 通知
        httpx.post(f"{server_uri}/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Mcp-Session-Id": self._session_id}
        )
        self._initialized = True

    def call(self, params, **kwargs):
        """qwen_agent 标准调用入口"""
        server_uri = params.get('mcpServer') or os.environ.get(
            'QGIS_MCP_SERVER_URI', 'http://localhost:8036'
        )
        self._ensure_session(server_uri)

        # 调用 MCP 工具
        result = self._call_mcp(server_uri, {
            "name": params['category'],          # compound 模式用类别名
            "arguments": {
                "action": params['action'],
                "params": params.get('params', {})
            }
        })
        return result
```

### 7.3 注册到 ToolRegistry

在 `agents/tool_registry.py` 中新增一个工厂方法：

```python
@staticmethod
def _create_qgis_mcp_tool():
    from tools.qgis_mcp_tool import QGISMcpTool
    return QGISMcpTool()
```

在 `names()` 返回列表中加入 `"qgis_mcp_tool"`。

## 八、Agent 提示词集成

### 8.1 新增意图类型

在 `agents/intent_types.py` 中：

```python
SPATIAL_ANALYSIS = "spatial_analysis"  # 空间分析（缓冲区、裁剪、叠加等）
```

### 8.2 工具-意图映射

在 `TOOL_INTENT_MAPPING` 中：

```python
"qgis_mcp_tool": [IntentType.SPATIAL_ANALYSIS, IntentType.SPATIAL_PROCESSING]
```

### 8.3 快速路由关键词

在 `agent_harness.py` 的 `FAST_ROUTE_KEYWORDS` 中追加：

```python
("缓冲区", IntentType.SPATIAL_ANALYSIS),
("裁剪", IntentType.SPATIAL_ANALYSIS),
("叠加分析", IntentType.SPATIAL_ANALYSIS),
("面积计算", IntentType.SPATIAL_ANALYSIS),
("空间分析", IntentType.SPATIAL_ANALYSIS),
("相交", IntentType.SPATIAL_ANALYSIS),
("包含", IntentType.SPATIAL_ANALYSIS),
```

### 8.4 map_agent 提示词补充

在 `map_agent.py` 的 system prompt 中追加：

```
qgis_mcp_tool 通过 MCP 协议连接 QGIS 引擎，处理空间分析任务。
支持：缓冲区(buffer)、裁剪(clip)、叠加分析(intersection/difference)、
空间关联(spatial_join)、分区统计(zonal_statistics)、处理算法(execute_processing)、
地图渲染(render_map) 等。
注意：需要 QGIS MCP 服务已经在运行（Docker 容器）。
```

## 九、部署步骤清单

| 步骤 | 操作 | 负责人 | 预计耗时 |
|---|---|---|---|
| 1 | 编写 Dockerfile + entrypoint.sh + docker-compose.yml | 开发 | 2h |
| 2 | `docker compose build qgis-mcp` 构建镜像（首次约 15 分钟） | 运维 | 15min |
| 3 | `docker compose up -d qgis-mcp` 启动容器，验证端口 8036 | 运维 | 5min |
| 4 | 用 curl 测试 MCP 握手：`initialize` → `ping` → 验证返回 `pong` | 开发 | 10min |
| 5 | 编写 `qgis_mcp_tool.py`（MCP 客户端 + BaseTool 适配） | 开发 | 4h |
| 6 | 注册到 `ToolRegistry`，新增 `SPATIAL_ANALYSIS` 意图 | 开发 | 30min |
| 7 | 更新 `map_agent` 系统提示词、快速路由关键词 | 开发 | 30min |
| 8 | 编写测试用例（ping、buffer、clip、render 等） | 开发 | 2h |
| 9 | 联调测试（用户输入 → 意图识别 → qgis_mcp_tool → QGIS → 地图反馈） | 开发 | 2h |
| 10 | PM2 重启后端服务，观察日志无异常 | 运维 | 5min |

**总估时：约 1.5 个工作日**（含调试 buffer）

## 十、风险与注意事项

### 10.1 已知风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| **大栅格处理导致 OOM** | 容器崩溃，服务不可用 | 限制容器内存上限（`mem_limit: 4g`），大文件走 GDAL 预处理后只需矢量分析 |
| **QGIS 无头环境下渲染异常** | render_map 出黑图/白图 | Xvfb 配置足够分辨率，渲染前先验证 extent 有效 |
| **首次启动下载 GitHub zip 超时** | 容器启动失败 | 国内网络可用镜像 / 提前将 zip 打包进镜像 |
| **MCP 会话过期** | 后端调用返回 4xx | 实现自动重连机制（检测到 session 过期时重新 initialize） |
| **117 个工具 schema 太大** | LLM 上下文窗口被占满 | 强制使用 `QGIS_MCP_TOOL_MODE=compound`（27 个分组工具） |

### 10.2 安全建议

- **`execute_code` 工具**：允许在 QGIS 内执行任意 Python 代码。生产环境建议通过 `QGIS_MCP_TOKEN` 设置共享密钥认证
- **网络隔离**：`QGIS_MCP_PORT=9876`（socket）不要对外暴露，只绑 `localhost`
- **数据挂载用只读**：避免模型误操作删除原始数据（`docker-compose` 中已设置 `:ro`）

### 10.3 后续可拓展方向

- **多个 QGIS 实例**：通过 `QGIS_MCP_INSTANCES` 环境变量，一个 MCP Server 可以连多个 QGIS 窗口，实现任务并行
- **自定义处理脚本**：将项目已有的 GDAL 处理逻辑（影像镶嵌、匀色、MVT 切片）封装为 QGIS 处理模型，通过 `execute_processing` 统一调用
- **自动出报告插图**：采砂监测报告的插图可以走 QGIS 的 `export_layout` 生成规范化的地图截图

---

> **文档版本**：v1.0  
> **编写日期**：2026-08-03  
> **依赖项目**：[nkarasiak/qgis-mcp](https://github.com/nkarasiak/qgis-mcp) v0.9.3  
> **关联工具**：`spatial_processing_tool`、`spatial_reference_tool`、`mcp_postgres_tool`
