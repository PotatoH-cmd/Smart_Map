# tools/cesium_tool.py
"""
Cesium 3D 地图操作工具
参考 cesium-mcp 项目（https://github.com/gaopengbin/cesium-mcp）的工具设计
支持飞行、标注、图层、底图、截图、矢量数据加载等操作
"""
import json
import logging
import urllib.parse
import re
from typing import Dict, Any, Union
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)


@register_tool('cesium_tool')
class CesiumTool(BaseTool):
    """
    Cesium 3D 地图操作工具，通过 WebSocket 向前端 CesiumJS 发送控制命令。
    当用户切换到 3D 视图时优先使用此工具进行地图操作。
    """

    description = (
        'Cesium 3D 地图操作工具，支持飞行到位置、添加3D标注、加载GeoJSON图层、'
        '切换底图、清除实体、截图等3D地球操作。当用户要求3D展示或当前为3D视图时调用。'
    )

    parameters = [
        {
            'name': 'action',
            'type': 'string',
            'description': (
                'Cesium 操作类型:\n'
                '  flyTo - 飞行到指定坐标或地名（需要lat/lng，可选height相机高度）\n'
                '  addMarker - 在3D地图上添加标注点（需要lat/lng，可选title/color）\n'
                '  addGeoJsonLayer - 从后端API加载GeoJSON矢量图层（需要table_name）\n'
                '  removeLayer - 移除指定图层（需要name）\n'
                '  clearAll - 清除3D地图上所有实体和图层\n'
                '  setBasemap - 切换底图（satellite/osm/tianditu）\n'
                '  setView - 直接设置相机视角（需要lat/lng/height）\n'
                '  screenshot - 截取当前3D地图截图\n'
                '  addPolygon - 在3D地图上绘制多边形（需要coordinates）\n'
                '  addPolyline - 在3D地图上绘制折线（需要coordinates）\n'
                '  addHeatmap - 添加热力图（需要points数据）\n'
                '  load3dTiles - 加载3D Tiles倾斜摄影/建筑（需要url）\n'
                '  loadTerrain - 加载地形服务（需要url）'
            ),
            'enum': [
                'flyTo', 'addMarker', 'addGeoJsonLayer', 'removeLayer',
                'clearAll', 'setBasemap', 'setView', 'screenshot',
                'addPolygon', 'addPolyline', 'addHeatmap',
                'load3dTiles', 'loadTerrain'
            ],
            'required': True
        },
        {
            'name': 'lat',
            'type': 'number',
            'description': '纬度（flyTo, addMarker, setView 需要）',
            'required': False
        },
        {
            'name': 'lng',
            'type': 'number',
            'description': '经度（flyTo, addMarker, setView 需要）',
            'required': False
        },
        {
            'name': 'height',
            'type': 'number',
            'description': '相机距地面高度（米），flyTo/setView 时使用，默认 50000（约5万米）',
            'required': False
        },
        {
            'name': 'title',
            'type': 'string',
            'description': '标注标题或图层名称',
            'required': False
        },
        {
            'name': 'popup',
            'type': 'string',
            'description': '点击标注时显示的弹窗内容',
            'required': False
        },
        {
            'name': 'color',
            'type': 'string',
            'description': '颜色（如 "red", "#ff0000", "rgba(255,0,0,0.8)"），用于标注/图层样式',
            'required': False
        },
        {
            'name': 'table_name',
            'type': 'string',
            'description': '数据库表名（addGeoJsonLayer 时必需，从PostGIS加载矢量数据）',
            'required': False
        },
        {
            'name': 'geom_col',
            'type': 'string',
            'description': '几何列名，默认为 geom',
            'required': False
        },
        {
            'name': 'filter',
            'type': 'string',
            'description': 'SQL 过滤条件，需使用双引号包裹字段名，例如 "\\"Mineable_Area_Name\\"=\'种子场可采区\'"',
            'required': False
        },
        {
            'name': 'name',
            'type': 'string',
            'description': '图层名称（removeLayer 时必需，addGeoJsonLayer 可用于展示名称）',
            'required': False
        },
        {
            'name': 'url',
            'type': 'string',
            'description': 'GeoJSON/3DTiles/地形服务 URL（load3dTiles, loadTerrain 需要）',
            'required': False
        },
        {
            'name': 'basemap',
            'type': 'string',
            'description': '底图类型：satellite（Esri卫星）、osm（OpenStreetMap）、tianditu（天地图）',
            'enum': ['satellite', 'osm', 'tianditu'],
            'required': False
        },
        {
            'name': 'coordinates',
            'type': 'array',
            'description': '坐标数组，格式: [[lng1, lat1], [lng2, lat2], ...] (注意是经度在前)',
            'required': False
        },
        {
            'name': 'color_expression',
            'type': 'string',
            'description': 'SQL 颜色表达式（addGeoJsonLayer 使用），例如超深判定规则',
            'required': False
        },
        {
            'name': 'duration',
            'type': 'number',
            'description': 'flyTo 飞行动画时长（秒），默认 3',
            'required': False
        }
    ]

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        try:
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except json.JSONDecodeError:
                    return {
                        'success': False,
                        'error': '参数不是有效的 JSON',
                        'cesium_command': None
                    }

            action = params.get('action')

            # ===== flyTo：飞行到指定位置 =====
            if action == 'flyTo':
                lat = params.get('lat')
                lng = params.get('lng')
                if lat is None or lng is None:
                    return {'success': False, 'error': '缺少 lat 或 lng', 'cesium_command': None}
                return {
                    'success': True,
                    'message': f'飞行到 ({lat}, {lng})',
                    'cesium_command': {
                        'type': 'flyTo',
                        'lat': lat,
                        'lng': lng,
                        'height': params.get('height', 50000),
                        'duration': params.get('duration', 3)
                    }
                }

            # ===== setView：直接设置相机视角 =====
            elif action == 'setView':
                lat = params.get('lat')
                lng = params.get('lng')
                if lat is None or lng is None:
                    return {'success': False, 'error': '缺少 lat 或 lng', 'cesium_command': None}
                return {
                    'success': True,
                    'message': f'设置3D视角到 ({lat}, {lng})',
                    'cesium_command': {
                        'type': 'setView',
                        'lat': lat,
                        'lng': lng,
                        'height': params.get('height', 50000)
                    }
                }

            # ===== addMarker：添加3D标注 =====
            elif action == 'addMarker':
                lat = params.get('lat')
                lng = params.get('lng')
                if lat is None or lng is None:
                    return {'success': False, 'error': '缺少 lat 或 lng', 'cesium_command': None}
                return {
                    'success': True,
                    'message': f'在3D地图添加标注: ({lat}, {lng})',
                    'cesium_command': {
                        'type': 'addMarker',
                        'lat': lat,
                        'lng': lng,
                        'title': params.get('title', '标注点'),
                        'popup': params.get('popup', ''),
                        'color': params.get('color', '#ff4444')
                    }
                }

            # ===== addGeoJsonLayer：从PostGIS API加载矢量图层 =====
            elif action == 'addGeoJsonLayer':
                table_name = params.get('table_name')
                layer_name = params.get('name') or params.get('title') or table_name
                geom_col = params.get('geom_col', 'geom')
                filter_query = params.get('filter', '')
                color_expression = params.get('color_expression', '')

                if not table_name:
                    return {
                        'success': False,
                        'error': 'table_name 不能为空',
                        'cesium_command': None
                    }

                api_url = (
                    f"/api/vector-data?table_name={urllib.parse.quote(table_name)}"
                    f"&geom_col={urllib.parse.quote(geom_col)}&debug=true"
                )
                if filter_query:
                    api_url += f"&filter={urllib.parse.quote(filter_query)}"
                if color_expression:
                    api_url += f"&color_expression={urllib.parse.quote(color_expression)}"

                # 从过滤条件中提取图层名
                if filter_query and (not layer_name or layer_name == table_name):
                    name_match = re.search(
                        r'"Mineable_Area_Name"\s*=\s*\'([^\']+)\'', filter_query
                    )
                    if name_match:
                        layer_name = name_match.group(1).strip()

                return {
                    'success': True,
                    'message': f'在3D地图加载图层: {layer_name}',
                    'cesium_command': {
                        'type': 'addGeoJsonLayer',
                        'url': api_url,
                        'name': layer_name,
                        'color': params.get('color', '#2773d7')
                    }
                }

            # ===== removeLayer：移除图层 =====
            elif action == 'removeLayer':
                name = params.get('name')
                if not name:
                    return {'success': False, 'error': '缺少 name 参数', 'cesium_command': None}
                return {
                    'success': True,
                    'message': f'移除3D图层: {name}',
                    'cesium_command': {'type': 'removeLayer', 'name': name}
                }

            # ===== clearAll：清除所有实体 =====
            elif action == 'clearAll':
                return {
                    'success': True,
                    'message': '已清除3D地图所有实体和图层',
                    'cesium_command': {'type': 'clearAll'}
                }

            # ===== setBasemap：切换底图 =====
            elif action == 'setBasemap':
                basemap = params.get('basemap', 'satellite')
                if basemap not in ['satellite', 'osm', 'tianditu']:
                    return {
                        'success': False,
                        'error': 'basemap 必须是 satellite/osm/tianditu',
                        'cesium_command': None
                    }
                return {
                    'success': True,
                    'message': f'切换3D底图为: {basemap}',
                    'cesium_command': {'type': 'setBasemap', 'basemap': basemap}
                }

            # ===== screenshot：截图 =====
            elif action == 'screenshot':
                return {
                    'success': True,
                    'message': '请求3D地图截图',
                    'cesium_command': {'type': 'screenshot'}
                }

            # ===== addPolygon：添加3D多边形 =====
            elif action == 'addPolygon':
                coords = params.get('coordinates')
                if not coords or len(coords) < 3:
                    return {'success': False, 'error': '多边形至少需要3个点', 'cesium_command': None}
                return {
                    'success': True,
                    'message': f'在3D地图添加多边形，{len(coords)}个顶点',
                    'cesium_command': {
                        'type': 'addPolygon',
                        'coordinates': coords,
                        'color': params.get('color', '#2773d7'),
                        'name': params.get('name', '多边形')
                    }
                }

            # ===== addPolyline：添加3D折线 =====
            elif action == 'addPolyline':
                coords = params.get('coordinates')
                if not coords or len(coords) < 2:
                    return {'success': False, 'error': '折线至少需要2个点', 'cesium_command': None}
                return {
                    'success': True,
                    'message': f'在3D地图添加折线，{len(coords)}个顶点',
                    'cesium_command': {
                        'type': 'addPolyline',
                        'coordinates': coords,
                        'color': params.get('color', '#ff4444'),
                        'name': params.get('name', '折线')
                    }
                }

            # ===== addHeatmap：添加热力图 =====
            elif action == 'addHeatmap':
                return {
                    'success': True,
                    'message': '请求添加热力图',
                    'cesium_command': {
                        'type': 'addHeatmap',
                        'name': params.get('name', '热力图')
                    }
                }

            # ===== load3dTiles：加载3D Tiles =====
            elif action == 'load3dTiles':
                url = params.get('url')
                if not url:
                    return {'success': False, 'error': '缺少 url 参数', 'cesium_command': None}
                return {
                    'success': True,
                    'message': f'加载3D Tiles: {url}',
                    'cesium_command': {
                        'type': 'load3dTiles',
                        'url': url,
                        'name': params.get('name', '3D Tiles')
                    }
                }

            # ===== loadTerrain：加载地形 =====
            elif action == 'loadTerrain':
                url = params.get('url')
                if not url:
                    return {'success': False, 'error': '缺少 url 参数', 'cesium_command': None}
                return {
                    'success': True,
                    'message': f'加载地形服务: {url}',
                    'cesium_command': {
                        'type': 'loadTerrain',
                        'url': url
                    }
                }

            else:
                return {
                    'success': False,
                    'error': f'不支持的 Cesium 操作: {action}',
                    'cesium_command': None
                }

        except Exception as e:
            logger.error(f"CesiumTool 异常: {e}")
            return {
                'success': False,
                'error': f'Cesium 工具异常: {str(e)}',
                'cesium_command': None
            }
