# tools/map_tool.py
import json
import re
import logging
import httpx
from typing import Dict, Any, Union
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)


@register_tool('map_tool')
class MapTool(BaseTool):
    """
    地图操作工具，支持通过自然语言控制地图显示、标记、视图、图层切换及矢量数据加载。
    """

    description = '地图操作工具，可以添加标记、设置视图、清除标记、切换底图、加载矢量数据等'

    parameters = [
        {
            'name': 'action',
            'type': 'string',
            'description': '地图操作类型',
            'enum': [
                'add_marker',
                'set_view',
                'clear_markers',
                'switch_layer',
                'add_circle',
                'add_circles',
                'add_polygon',
                'add_polyline',
                'fit_markers',
                'load_vector_layer'  # ✅ 新增：加载矢量图层
            ],
            'required': True
        },
        {
            'name': 'lat',
            'type': 'number',
            'description': '纬度（add_marker, set_view, add_circle 等需要）',
            'required': False
        },
        {
            'name': 'lng',
            'type': 'number',
            'description': '经度（add_marker, set_view, add_circle 等需要）',
            'required': False
        },
        {
            'name': 'zoom',
            'type': 'integer',
            'description': '缩放级别 (1-18)',
            'required': False
        },
        {
            'name': 'title',
            'type': 'string',
            'description': '标记标题',
            'required': False
        },
        {
            'name': 'popup',
            'type': 'string',
            'description': '弹窗内容',
            'required': False
        },
        {
            'name': 'layer',
            'type': 'string',
            'description': '图层类型',
            'enum': ['arcgis', 'jcdt', 'gf2024', 'gf2025'],
            'required': False
        },
        {
            'name': 'radius',
            'type': 'number',
            'description': '圆形半径（米）',
            'required': False
        },
        {
            'name': 'color',
            'type': 'string',
            'description': '颜色（如 "red", "#ff0000"）',
            'required': False
        },
        {
            'name': 'coordinates',
            'type': 'array',
            'description': '坐标数组，格式: [[lat1, lng1], [lat2, lng2], ...]',
            'required': False
        },
        {
            'name': 'weight',
            'type': 'number',
            'description': '线条粗细',
            'required': False
        },
        {
            'name': 'color_expression',
            'type': 'string',
            'description': 'SQL 颜色表达式（load_vector_layer 使用）。若涉及超深判定，必须使用规则："CASE WHEN (Control_Elevation - Measured_Depth) > 2 THEN \'red\' ELSE \'green\' END"。即：只有实测比控制低 2m 以上才显示红色。',
            'required': False
        },
        {
            'name': 'circles',
            'type': 'array',
            'description': '多个圆形数据，每个为 {lat, lng, radius, color}',
            'required': False
        },
        {
            'name': 'layer_name',
            'type': 'string',
            'description': '矢量图层展示名称',
            'required': False
        },
        {
            'name': 'table_name',
            'type': 'string',
            'description': '数据库表名（load_vector_layer 时必需）',
            'required': False
        },
        {
            'name': 'geom_col',
            'type': 'string',
            'description': '几何列名，默认为 geom',
            'required': False
        },
        {
            'name': 'properties',
            'type': 'string',
            'description': '属性字段名，逗号分隔，不传则包含所有字段',
            'required': False
        },
        {
            'name': 'filter',
            'type': 'string',
            'description': 'SQL 过滤条件，必须使用双引号包裹字段名，例如 "\\"Mineable_Area_Name\\"=\'种子场可采区\'" 或 "\\"Mineable_Area_Name\\" LIKE \'%种子场%\'"。',
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
                        'map_command': None
                    }

            action = params.get('action')

            # =============== 优化：动态加载矢量图层 ===============
            if action == 'load_vector_layer':
                table_name = params.get('table_name')
                layer_name = params.get('layer_name', table_name)
                geom_col = params.get('geom_col', 'geom')
                properties = params.get('properties', '')
                filter_query = params.get('filter', '')
                color_expression = params.get('color_expression', '')
                show_popup = params.get('show_popup', True)

                if not table_name:
                    return {
                        'success': False,
                        'error': 'table_name 不能为空',
                        'map_command': None
                    }

                # 构建 API URL，包含 filter 参数
                import urllib.parse
                api_url = f"/api/vector-data?table_name={urllib.parse.quote(table_name)}&geom_col={urllib.parse.quote(geom_col)}&debug=true"
                if properties:
                    api_url += f"&properties={urllib.parse.quote(properties)}"
                if filter_query:
                    api_url += f"&filter={urllib.parse.quote(filter_query)}"
                if color_expression:
                    api_url += f"&color_expression={urllib.parse.quote(color_expression)}"

                if filter_query and (not layer_name or layer_name == table_name):
                    name_match = re.search(r'"Mineable_Area_Name"\s*=\s*\'([^\']+)\'', filter_query)
                    if name_match:
                        layer_name = name_match.group(1).strip()

                return {
                    'success': True,
                    'map_command': {
                        'type': 'load_vector_layer',
                        'url': api_url,
                        'name': layer_name,
                        'show_popup': show_popup
                    }
                }

            # =============== 原有操作 ===============
            elif action == 'add_marker':
                lat = params.get('lat')
                lng = params.get('lng')
                if lat is None or lng is None:
                    return {'success': False, 'error': '缺少 lat 或 lng', 'map_command': None}
                return {
                    'success': True,
                    'message': f'添加标记: ({lat}, {lng})',
                    'map_command': {
                        'type': 'add_marker',
                        'lat': lat,
                        'lng': lng,
                        'title': params.get('title', ''),
                        'popup': params.get('popup', '')
                    }
                }

            elif action == 'set_view':
                lat = params.get('lat')
                lng = params.get('lng')
                zoom = params.get('zoom', 13)
                if lat is None or lng is None:
                    return {'success': False, 'error': '缺少 lat 或 lng', 'map_command': None}
                return {
                    'success': True,
                    'message': f'设置视图到 ({lat}, {lng})',
                    'map_command': {
                        'type': 'set_view',
                        'lat': lat,
                        'lng': lng,
                        'zoom': zoom
                    }
                }

            elif action == 'clear_markers':
                return {
                    'success': True,
                    'message': '已清除所有标记',
                    'map_command': {'type': 'clear_markers'}
                }

            elif action == 'switch_layer':
                layer = params.get('layer', 'arcgis')
                if layer not in ['arcgis', 'jcdt', 'gf2024', 'gf2025', 'gf2026']:
                    return {'success': False, 'error': '当前仅支持 arcgis/jcdt/gf2024/gf2025/gf2026', 'map_command': None}
                layer_names = {'arcgis': '2023年高分影像', 'jcdt': '基础底图', 'gf2024': '2024年高分影像', 'gf2025': '2025年高分影像', 'gf2026': '2026年高分影像'}
                return {
                    'success': True,
                    'message': f'切换到底图: {layer_names.get(layer, layer)}',
                    'map_command': {'type': 'switch_layer', 'layer': layer}
                }

            elif action == 'add_circle':
                lat = params.get('lat')
                lng = params.get('lng')
                radius = params.get('radius', 1000)
                if lat is None or lng is None:
                    return {'success': False, 'error': '缺少 lat 或 lng', 'map_command': None}
                return {
                    'success': True,
                    'message': f'添加圆形: ({lat}, {lng}), 半径={radius}m',
                    'map_command': {
                        'type': 'add_circle',
                        'lat': lat,
                        'lng': lng,
                        'radius': radius,
                        'color': params.get('color', 'blue')
                    }
                }

            elif action == 'add_circles':
                circles = params.get('circles', [])
                if not isinstance(circles, list):
                    return {'success': False, 'error': 'circles 必须是数组', 'map_command': None}
                return {
                    'success': True,
                    'message': f'添加 {len(circles)} 个圆形',
                    'map_command': {'type': 'add_circles', 'circles': circles}
                }

            elif action == 'add_polygon':
                coords = params.get('coordinates')
                if not coords or len(coords) < 3:
                    return {'success': False, 'error': '多边形至少需要3个点', 'map_command': None}
                return {
                        'coordinates': coords,
                        'color': params.get('color', 'red')
                    }

            elif action == 'add_polyline':
                coords = params.get('coordinates')
                if not coords or len(coords) < 2:
                    return {'success': False, 'error': '折线至少需要2个点', 'map_command': None}
                return {
                    'success': True,
                    'message': '添加折线',
                    'map_command': {
                        'type': 'add_polyline',
                        'coordinates': coords,
                        'color': params.get('color', 'red'),
                        'weight': params.get('weight', 3)
                    }
                }

            elif action == 'fit_markers':
                return {
                    'success': True,
                    'message': '自动缩放至所有标记',
                    'map_command': {'type': 'fit_markers'}
                }

            else:
                return {
                    'success': False,
                    'error': f'不支持的操作: {action}',
                    'map_command': None
                }

        except Exception as e:
            return {
                'success': False,
                'error': f'地图工具异常: {str(e)}',
                'map_command': None
            }

# 地理位置数据库，用于地名查询
LOCATION_DATABASE = {
    # 中国主要城市
    '北京': {'lat': 39.9042, 'lng': 116.4074, 'name': '北京'},
    '天安门': {'lat': 39.9043, 'lng': 116.4074, 'name': '天安门广场'},
    '天安门广场': {'lat': 39.9043, 'lng': 116.4074, 'name': '天安门广场'},
    '上海': {'lat': 31.2304, 'lng': 121.4737, 'name': '上海'},
    '东方明珠': {'lat': 31.2397, 'lng': 121.4994, 'name': '东方明珠塔'},
    '东方明珠塔': {'lat': 31.2397, 'lng': 121.4994, 'name': '东方明珠塔'},
    '广州': {'lat': 23.1291, 'lng': 113.2644, 'name': '广州'},
    '深圳': {'lat': 22.5431, 'lng': 114.0579, 'name': '深圳'},
    '杭州': {'lat': 30.2741, 'lng': 120.1551, 'name': '杭州'},
    '西湖': {'lat': 30.2369, 'lng': 120.1451, 'name': '西湖'},
    '南京': {'lat': 32.0603, 'lng': 118.7969, 'name': '南京'},
    '成都': {'lat': 30.5728, 'lng': 104.0668, 'name': '成都'},
    '重庆': {'lat': 29.5630, 'lng': 106.5516, 'name': '重庆'},
    '武汉': {'lat': 30.5928, 'lng': 114.3055, 'name': '武汉'},
    '西安': {'lat': 34.3416, 'lng': 108.9398, 'name': '西安'},
    # 国际城市
    '纽约': {'lat': 40.7128, 'lng': -74.0060, 'name': '纽约'},
    '伦敦': {'lat': 51.5074, 'lng': -0.1278, 'name': '伦敦'},
    '巴黎': {'lat': 48.8566, 'lng': 2.3522, 'name': '巴黎'},
    '东京': {'lat': 35.6762, 'lng': 139.6503, 'name': '东京'},
    '悉尼': {'lat': -33.8688, 'lng': 151.2093, 'name': '悉尼'},
    '无锡': {'lat': 31.5912, 'lng': 120.3110, 'name': '无锡'},
}

@register_tool('location_search')
class LocationSearchTool(BaseTool):
    """
    地理位置查询工具，根据地名查找坐标
    """
    
    description = '根据地名查找地理坐标，支持中国主要城市和世界知名城市'
    
    parameters = [{
        'name': 'location_name',
        'type': 'string',
        'description': '地名或地标名称',
        'required': True
    }]

    def call(self, params: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """
        根据地名查找坐标
        """
        try:
            # 处理参数类型：如果是字符串，尝试解析为JSON；如果已经是字典，直接使用
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except json.JSONDecodeError:
                    # 如果JSON解析失败，假设字符串本身就是location_name
                    params = {'location_name': params}
            elif not isinstance(params, dict):
                raise ValueError(f"Invalid params type: {type(params)}")
            
            location_name = params.get('location_name', '').strip()
            
            if not location_name:
                return {
                    'success': False,
                    'error': '请提供地名',
                    'location': None
                }
            
            # 在数据库中查找
            for key, value in LOCATION_DATABASE.items():
                if location_name in key or key in location_name:
                    return {
                        'success': True,
                        'location': value,
                        'message': f'找到 {value["name"]} 的坐标: ({value["lat"]}, {value["lng"]})',
                        'map_command': {
                            'type': 'set_view',
                            'lat': value['lat'],
                            'lng': value['lng'],
                            'zoom': 13
                        }
                    }
            
            # 如果本地数据库没找到，集成天地图 API (如果配置了 TIANDITU_TOKEN)
            import os
            tk = os.environ.get("TIANDITU_TOKEN", "e644a451f326492790e39a09efe8e2de") # 默认使用一个可用的测试 token
            
            try:
                logger.info(f"Calling Tianditu Geocoder for: {location_name}")
                # 天地图 API 要求 ds 参数为 JSON 字符串
                ds = json.dumps({"keyWord": location_name})
                url = f"http://api.tianditu.gov.cn/geocoder?ds={ds}&tk={tk}"
                
                with httpx.Client(timeout=5.0) as client:
                    resp = client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") == "0" and data.get("location"):
                            loc = data["location"]
                            # 天地图返回的是 lon, lat
                            lat = float(loc.get("lat", 0))
                            lng = float(loc.get("lon", 0))
                            if lat != 0 and lng != 0:
                                return {
                                    'success': True,
                                    'location': {'lat': lat, 'lng': lng, 'name': location_name},
                                    'message': f'通过天地图找到 {location_name} 的坐标: ({lat}, {lng})',
                                    'map_command': {
                                        'type': 'set_view',
                                        'lat': lat,
                                        'lng': lng,
                                        'zoom': 14
                                    }
                                }
                        else:
                            logger.warning(f"Tianditu returned error: {data}")
                            return {
                                'success': False,
                                'error': f"天地图查询失败: {data.get('msg', '未知错误')}",
                                'location': None
                            }
                    elif resp.status_code == 403:
                        logger.error(f"Tianditu Geocoder failed with 403 Forbidden. Key might be invalid or browser-type: {tk}")
                        return {
                            'success': False,
                            'error': "天地图 API 权限不足 (403 Forbidden)。请确认是否使用的是“服务端”类型的 Key。",
                            'location': None
                        }
                    else:
                        logger.warning(f"Tianditu Geocoder returned status {resp.status_code}")
            except Exception as e:
                logger.warning(f"Tianditu Geocoder failed: {e}")
            
            return {
                'success': False,
                'error': f'未找到 "{location_name}" 的坐标信息，请尝试其他地名或直接提供坐标',
                'location': None,
                'suggestions': list(LOCATION_DATABASE.keys())[:10]
            }
        
        except Exception as e:
            return {
                'success': False,
                'error': f'位置搜索失败: {str(e)}',
                'map_command': None
            }

@register_tool('coordinate_marker')
class CoordinateMarkerTool(BaseTool):
    """
    在地图上根据经纬度标记点的工具，会在标记处显示该位置的经纬度。
    """
    description = '在地图上根据给定的经纬度标记一个点，并在弹出窗口中显示该位置的经纬度。'
    parameters = [
        {
            'name': 'lat',
            'type': 'number',
            'description': '该点的纬度',
            'required': True
        },
        {
            'name': 'lng',
            'type': 'number',
            'description': '该点的经度',
            'required': True
        },
        {
            'name': 'title',
            'type': 'string',
            'description': '标记的标题（可选）',
            'required': False
        }
    ]

    def call(self, params: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """
        根据经纬度在地图上标记点并显示经纬度
        """
        try:
            # 处理参数类型：如果是字符串，尝试解析为JSON；如果已经是字典，直接使用
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except json.JSONDecodeError:
                    # 如果解析失败，这里可能需要报错
                    return {'success': False, 'error': '无效的参数格式'}
            elif not isinstance(params, dict):
                raise ValueError(f"Invalid params type: {type(params)}")
            
            lat = params.get('lat')
            lng = params.get('lng')
            title = params.get('title', '坐标点').strip()
            
            if lat is None or lng is None:
                return {
                    'success': False,
                    'error': '请提供经纬度坐标',
                    'location': None
                }
            
            # 构建显示经纬度的弹出内容
            popup_content = f"<b>{title}</b><br>纬度: {lat}<br>经度: {lng}"
            
            return {
                'success': True,
                'message': f'已在坐标 ({lat}, {lng}) 标记该点',
                'map_command': {
                    'type': 'add_marker',
                    'lat': lat,
                    'lng': lng,
                    'title': title,
                    'popup': popup_content
                }
            }
        
        except Exception as e:
            return {
                'success': False,
                'error': f'坐标标记失败: {str(e)}',
                'map_command': None
            }

