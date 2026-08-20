# QGIS MCP Docker 镜像
# 在容器内运行 QGIS 桌面版（无头）+ qgis_mcp_plugin + MCP Server
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai
ENV LANG=C.UTF-8

# === 阶段 1：系统依赖（合并源以减少 apt update 次数） ===
RUN apt-get update && apt-get install -y --no-install-recommends \
    gnupg \
    software-properties-common \
    wget \
    curl \
    netcat-openbsd \
    xvfb \
    x11-utils \
    && rm -rf /var/lib/apt/lists/*

# 添加 deadsnakes PPA（提供 Python 3.12）和 QGIS LTR 源
RUN add-apt-repository -y ppa:deadsnakes/ppa \
    && echo "deb [trusted=yes] https://qgis.org/ubuntu-ltr jammy main" \
       > /etc/apt/sources.list.d/qgis.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       qgis \
       qgis-plugin-grass \
       python3-qgis \
       python3.12 \
       python3.12-venv \
       python3.12-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv 包管理器（qgis-mcp server 的启动器）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 创建运行用户（QGIS 不推荐 root 运行）
RUN useradd -m -s /bin/bash qgis
WORKDIR /home/qgis

# === 阶段 2：复制源码和配置（root 身份） ===
COPY setup_qgis_config.py /home/qgis/setup_qgis_config.py
COPY qgis_mcp_plugin /home/qgis/.local/share/QGIS/QGIS3/profiles/default/python/plugins/qgis_mcp_plugin
COPY pyproject.toml /home/qgis/pyproject.toml
COPY README.md /home/qgis/README.md
COPY qgis_mcp_server /home/qgis/src/qgis_mcp
COPY entrypoint.sh /home/qgis/entrypoint.sh
RUN chmod +x /home/qgis/entrypoint.sh

# 安装 MCP Server 依赖（以 root 运行，uv 使用系统 Python 3.12）
# 注意：chown 在 uv sync 之后，因为 COPY 的文件属于 root
RUN cd /home/qgis && uv sync --no-dev
# 确保 .venv 所有人都可读/执行（Python 指向 /usr/bin/python3.12，qgis 用户可访问）
RUN chmod -R 755 /home/qgis/.venv

# 预配置 QGIS
RUN mkdir -p /home/qgis/.local/share/QGIS/QGIS3/profiles/default/QGIS \
    && python3 /home/qgis/setup_qgis_config.py

# 将所有文件所有权转给 qgis 用户
RUN chown -R qgis:qgis /home/qgis
USER qgis

EXPOSE 8000 9876

ENTRYPOINT ["/home/qgis/entrypoint.sh"]
