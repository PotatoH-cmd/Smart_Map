#!/bin/bash
# QGIS MCP 容器启动脚本
# 1. 验证插件已就位（Dockerfile 中预先安装）
# 2. 启动 Xvfb 虚拟显示器
# 3. 启动 QGIS（插件自动启动 socket 服务 :9876）
# 4. 等待 socket 就绪后启动 MCP Server（streamable-http :8000）
set -e

PLUGIN_SRC="$HOME/.local/share/QGIS/QGIS3/profiles/default/python/plugins/qgis_mcp_plugin"

echo "=== Step 1: 验证插件 ==="
if [ ! -f "$PLUGIN_SRC/metadata.txt" ]; then
    echo "错误: QGIS MCP 插件未找到！请检查 Dockerfile"
    exit 1
fi
echo "插件就绪: $PLUGIN_SRC"

echo "=== Step 2: 启动虚拟显示器 Xvfb ==="
# 清理上次残留下的 lock 文件（容器重启场景）
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
Xvfb :99 -screen 0 1280x1024x24 -ac +extension GLX +render &
export DISPLAY=:99
sleep 1
echo "DISPLAY=$DISPLAY 就绪"

echo "=== Step 3: 启动 QGIS ==="
# 确保插件在 PythonPlugins 中启用（QgsSettings 需要显式声明）
# 使用 strict=False 容忍 QGIS INI 文件中的重复 key
python3 -c "
import configparser, os
ini_path = os.path.expanduser('~/.local/share/QGIS/QGIS3/profiles/default/QGIS/QGIS3.ini')
ini = configparser.ConfigParser(strict=False)
ini.read(ini_path)
if not ini.has_section('PythonPlugins'):
    ini.add_section('PythonPlugins')
ini.set('PythonPlugins', 'qgis_mcp_plugin', 'true')
with open(ini_path, 'w') as f:
    ini.write(f)
print('PythonPlugins updated')
"
qgis --nologo --noversioncheck --skipbadlayers &
QGIS_PID=$!
echo "QGIS PID=$QGIS_PID"

# 等待插件 socket 服务就绪（轮询端口 9876）
echo "=== 等待 QGIS MCP 插件 socket 服务就绪 ==="
for i in $(seq 1 90); do
    if nc -z localhost 9876 2>/dev/null; then
        echo "QGIS MCP socket 服务已启动 (localhost:9876) —— 耗时 ${i}s"
        break
    fi
    if [ $i -eq 90 ]; then
        echo "错误: QGIS MCP socket 服务未能在 3 分钟内启动"
        echo "--- 诊断信息 ---"
        ps aux | grep qgis | grep -v grep || echo "无 QGIS 进程"
        netstat -tlnp 2>/dev/null | grep 9876 || echo "端口未监听"
        exit 1
    fi
    sleep 2
done

echo "=== Step 4: 启动 MCP Server (streamable-http) ==="
# 修补 server.py：将 streamable-http 绑定地址从 127.0.0.1 改为 0.0.0.0（Docker 端口转发需要）
sed -i 's/mcp.run(transport="streamable-http")/mcp.run(transport="streamable-http", host="0.0.0.0")/' \
    $HOME/src/qgis_mcp/server.py
export QGIS_MCP_HOST=localhost
export QGIS_MCP_PORT=9876
export QGIS_MCP_TRANSPORT=streamable-http
export QGIS_MCP_TOOL_MODE=compound
export QGIS_MCP_LOG_LEVEL=WARNING

cd $HOME
exec uv run --no-sync src/qgis_mcp/server.py
