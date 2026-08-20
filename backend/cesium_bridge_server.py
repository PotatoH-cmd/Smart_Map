"""
Cesium WebSocket 中继服务
后端充当 WebSocket Server，前端 CesiumComponent 连接此服务以接收 Cesium 命令。
"""
import json
import logging
from typing import List
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# 已连接的 Cesium 前端客户端列表
cesium_clients: List[WebSocket] = []


async def cesium_ws_endpoint(websocket: WebSocket):
    """WebSocket 端点：接受前端 CesiumComponent 的连接"""
    await websocket.accept()
    cesium_clients.append(websocket)
    client_host = websocket.client.host if websocket.client else "unknown"
    logger.info(f"[CesiumBridge] 新连接: {client_host}，当前客户端数: {len(cesium_clients)}")
    try:
        while True:
            # 保持连接，也可接收前端上报消息（如截图结果、相机位置等）
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                msg_type = msg.get("type", "")
                if msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
                elif msg_type == "screenshot_result":
                    logger.info(f"[CesiumBridge] 收到截图结果，数据长度: {len(msg.get('data', ''))}")
                elif msg_type == "camera_info":
                    logger.debug(f"[CesiumBridge] 相机位置: {msg}")
                else:
                    logger.debug(f"[CesiumBridge] 前端消息: {msg_type}")
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        logger.info(f"[CesiumBridge] 连接断开: {client_host}")
    except Exception as e:
        logger.warning(f"[CesiumBridge] 连接异常: {e}")
    finally:
        if websocket in cesium_clients:
            cesium_clients.remove(websocket)
        logger.info(f"[CesiumBridge] 当前客户端数: {len(cesium_clients)}")


async def send_cesium_command(command: dict) -> bool:
    """
    向所有已连接的 Cesium 前端广播命令
    :param command: Cesium 命令字典（含 type 字段）
    :return: 是否有客户端接收
    """
    if not cesium_clients:
        logger.warning("[CesiumBridge] 无已连接的 Cesium 客户端，命令未发送")
        return False

    msg = json.dumps(command, ensure_ascii=False)
    failed = []
    for ws in list(cesium_clients):
        try:
            await ws.send_text(msg)
        except Exception as e:
            logger.warning(f"[CesiumBridge] 发送命令失败: {e}")
            failed.append(ws)

    for ws in failed:
        if ws in cesium_clients:
            cesium_clients.remove(ws)

    sent = len(cesium_clients) - len(failed)
    logger.info(f"[CesiumBridge] 命令已发送到 {sent} 个客户端: {command.get('type', '?')}")
    return sent > 0


def get_cesium_client_count() -> int:
    """返回当前已连接的 Cesium 客户端数量"""
    return len(cesium_clients)
