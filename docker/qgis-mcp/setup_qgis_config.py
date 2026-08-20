#!/usr/bin/env python3
"""预配置 QGIS 插件自动启动设置。
将 autostart=true 写入 QGIS 用户配置，使 QGIS 打开时自动启动 MCP socket 服务。
"""
import configparser
import os

ini = configparser.ConfigParser()
ini["qgis_mcp"] = {
    "autostart": "true",
    "first_run": "false",
    "port": "9876",
}

qgis_dir = "/home/qgis/.local/share/QGIS/QGIS3/profiles/default/QGIS"
os.makedirs(qgis_dir, exist_ok=True)

ini_path = os.path.join(qgis_dir, "QGIS3.ini")
with open(ini_path, "w") as f:
    ini.write(f)
print(f"QGIS3.ini written to {ini_path}")
