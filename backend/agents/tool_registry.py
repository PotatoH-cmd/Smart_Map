"""
工具注册表 — 替代 TaskExecutor._create_tool() 中的 if-elif 硬编码。
每个工具名对应一个零参数工厂函数，自含配置。
"""
import os
import logging
from typing import Dict, Callable, Optional

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册表：管理工具名 → 工厂函数的映射。"""

    def __init__(self):
        self._factories: Dict[str, Callable] = {}
        self._init_defaults()

    def _init_defaults(self):
        """注册所有默认工具。每个工厂是零参数可调用对象，返回工具实例。"""
        self.register("map_tool", self._create_map_tool)
        self.register("location_search", self._create_location_search)
        self.register("coordinate_marker", self._create_coordinate_marker)
        self.register("cesium_tool", self._create_cesium_tool)
        self.register("postgresql_tool", self._create_postgresql_tool)
        self.register("mcp_postgres_tool", self._create_mcp_postgresql_tool)
        self.register("knowledge_base_tool", self._create_knowledge_base_tool)
        self.register("data_visualizer_tool", self._create_data_visualizer_tool)
        self.register("report_generator_tool", self._create_report_generator_tool)
        self.register("caisha_report_tool", self._create_caisha_report_tool)
        self.register("weather_tool", self._create_weather_tool)
        self.register("spatial_processing_tool", self._create_spatial_processing_tool)
        self.register("spatial_reference_tool", self._create_spatial_reference_tool)
        self.register("tile_publish_tool", self._create_tile_publish_tool)
        self.register("qgis_mcp_tool", self._create_qgis_mcp_tool)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def register(self, name: str, factory: Callable):
        """注册一个工具工厂。"""
        self._factories[name] = factory

    def create(self, name: str) -> Optional[object]:
        """创建工具实例；未注册返回 None。"""
        factory = self._factories.get(name)
        if factory is None:
            logger.warning(f"Tool not registered: {name}")
            return None
        try:
            return factory()
        except Exception as e:
            logger.error(f"Failed to create tool {name}: {e}")
            return None

    def names(self):
        """返回所有已注册工具名。"""
        return list(self._factories.keys())

    # ------------------------------------------------------------------
    # 工具工厂（每个自含 import 和配置）
    # ------------------------------------------------------------------

    @staticmethod
    def _create_map_tool():
        from tools.map_tool import MapTool
        return MapTool()

    @staticmethod
    def _create_location_search():
        from tools.map_tool import LocationSearchTool
        return LocationSearchTool()

    @staticmethod
    def _create_coordinate_marker():
        from tools.map_tool import CoordinateMarkerTool
        return CoordinateMarkerTool()

    @staticmethod
    def _create_cesium_tool():
        from tools.cesium_tool import CesiumTool
        return CesiumTool()

    @staticmethod
    def _create_postgresql_tool():
        from tools.postgresql_tool import PostgreSQLTool
        return PostgreSQLTool(cfg={
            "host": "172.136.16.52",
            "port": 5432,
            "database": "postgres",
            "user": "postgres",
        })

    @staticmethod
    def _create_mcp_postgresql_tool():
        from tools.mcp_postgres_tool import MCPPostgreSQLTool
        return MCPPostgreSQLTool(cfg={"readonly": True})

    @staticmethod
    def _create_knowledge_base_tool():
        _kb = os.environ.get("KNOWLEDGE_BACKEND", "ragflow")
        if _kb == "llamaindex":
            from tools.llamaindex_knowledge_tool import KnowledgeBaseTool
        else:
            from tools.ragflow_knowledge_tool import KnowledgeBaseTool
        return KnowledgeBaseTool()

    @staticmethod
    def _create_data_visualizer_tool():
        from tools.data_visualizer_tool import DataVisualizerTool
        return DataVisualizerTool()

    @staticmethod
    def _create_report_generator_tool():
        from tools.report_generator_tool import ReportGeneratorTool
        return ReportGeneratorTool()

    @staticmethod
    def _create_caisha_report_tool():
        from tools.caisha_report_tool import CaishaReportTool
        return CaishaReportTool()

    @staticmethod
    def _create_weather_tool():
        from tools.weather_tool import WeatherTool
        return WeatherTool()

    @staticmethod
    def _create_spatial_processing_tool():
        from tools.spatial_processing_tool import SpatialProcessingTool
        return SpatialProcessingTool()

    @staticmethod
    def _create_spatial_reference_tool():
        from tools.spatial_reference_tool import SpatialReferenceTool
        return SpatialReferenceTool()

    @staticmethod
    def _create_tile_publish_tool():
        from tools.tile_publish_tool import TilePublishTool
        return TilePublishTool()

    @staticmethod
    def _create_qgis_mcp_tool():
        from tools.qgis_mcp_tool import QGISMcpTool
        return QGISMcpTool()
