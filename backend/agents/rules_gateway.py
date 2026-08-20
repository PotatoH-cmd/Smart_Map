"""
rules_gateway.py — RulesGateway：可插拔的 preflight / postflight 规则链（阶段2：验证层重构）。

把散落在 task_executor 的兜底判断（_is_empty_aggregate / 空结果 / 文件存在性等）
收敛为按工具注册的校验规则：

  - preflight(tool)：执行前校验——参数 schema、CRS 是否支持米制运算、输出路径冲突
  - postflight(tool)：执行后校验——空结果、聚合全零、GeoJSON 要素数、报告文件存在、图表 series 非空

校验产出 VerificationReport{passed, checks[], risk: none|warning|critical}；
critical 触发 fail-fast（run_engine 在 _invoke_step 读取 risk，跳过依赖步骤并发出
verification 事件，复用现有"数据不足跳过报告 / DB 空回退知识库"语义）。

设计取舍：网关只负责"验证"，"回退编排"（_fallback_knowledge_search）仍留在
TaskExecutor，两者职责分离。
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

RISK_NONE = "none"
RISK_WARNING = "warning"
RISK_CRITICAL = "critical"


@dataclass
class Check:
    """单条校验结论。"""
    code: str
    name: str
    passed: bool
    detail: str = ""
    severity: str = RISK_WARNING  # warning | critical

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "severity": self.severity,
        }


@dataclass
class VerificationReport:
    """一次 preflight / postflight 的完整校验报告。"""
    tool: str
    phase: str  # preflight | postflight
    checks: List[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def risk(self) -> str:
        if any(not c.passed and c.severity == RISK_CRITICAL for c in self.checks):
            return RISK_CRITICAL
        if any(not c.passed for c in self.checks):
            return RISK_WARNING
        return RISK_NONE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "phase": self.phase,
            "passed": self.passed,
            "risk": self.risk,
            "checks": [c.to_dict() for c in self.checks],
            "errors": [c.detail for c in self.checks if not c.passed and c.severity == RISK_CRITICAL],
            "details": [c.detail for c in self.checks if not c.passed],
        }


class RulesGateway:
    """规则网关：按工具名注册 preflight / postflight 校验函数并执行。

    校验函数签名：fn(arg: Dict, state: Dict) -> List[Check]
      - preflight 的 arg 为工具参数 params
      - postflight 的 arg 为工具结果 result
    """

    def __init__(self):
        self._preflights: Dict[str, List[Callable]] = defaultdict(list)
        self._postflights: Dict[str, List[Callable]] = defaultdict(list)
        self._register_builtin_rules()

    # ------------------------------------------------------------------
    # 装饰器注册 API（@gateway.preflight("tool") / @gateway.postflight("tool")）
    # ------------------------------------------------------------------

    def preflight(self, tool_name: str):
        def deco(fn):
            self._preflights[tool_name].append(fn)
            return fn
        return deco

    def postflight(self, tool_name: str):
        def deco(fn):
            self._postflights[tool_name].append(fn)
            return fn
        return deco

    # ------------------------------------------------------------------
    # 执行入口（run_engine 调用；无注册规则返回 None）
    # ------------------------------------------------------------------

    def run_preflight(self, tool_name: str, params: Dict, state: Dict) -> Optional[Dict]:
        fns = self._preflights.get(tool_name)
        if not fns:
            return None
        return self._run(tool_name, "preflight", fns, params, state)

    def run_postflight(self, tool_name: str, result: Dict, state: Dict) -> Optional[Dict]:
        fns = self._postflights.get(tool_name)
        if not fns:
            return None
        return self._run(tool_name, "postflight", fns, result, state)

    def _run(self, tool_name: str, phase: str, fns: List[Callable],
             arg: Dict, state: Dict) -> Dict:
        report = VerificationReport(tool=tool_name, phase=phase)
        for fn in fns:
            try:
                checks = fn(arg, state) or []
                report.checks.extend(checks)
            except Exception as e:
                logger.warning(
                    f"[RulesGateway] {phase} rule {getattr(fn, '__name__', fn)} "
                    f"error for {tool_name}: {e}"
                )
                report.checks.append(Check(
                    code="rule_error", name="规则执行异常",
                    passed=False, detail=str(e), severity=RISK_WARNING,
                ))
        return report.to_dict()

    # ------------------------------------------------------------------
    # 内置规则注册（延迟导入 validators，避免循环导入）
    # ------------------------------------------------------------------

    def _register_builtin_rules(self):
        from .validators import db_validators, gis_validators, report_validators

        # postgresql_tool：空结果（critical → 知识库回退）/ 聚合全零（warning）
        self._postflights["postgresql_tool"].extend([
            db_validators.check_empty_result,
            db_validators.check_aggregate_zero,
        ])
        # knowledge_base_tool：检索内容为空（critical）
        self._postflights["knowledge_base_tool"].extend([
            db_validators.check_kb_content_empty,
        ])
        # qgis_mcp_tool：CRS 米制（preflight）/ 步骤失败与 GeoJSON 要素数（postflight）
        self._preflights["qgis_mcp_tool"].extend([
            gis_validators.check_crs_metric,
        ])
        self._postflights["qgis_mcp_tool"].extend([
            gis_validators.check_workflow_steps_failed,
            gis_validators.check_geojson_features,
        ])
        # report_generator_tool：输出路径冲突（preflight）/ 文件存在（postflight）
        self._preflights["report_generator_tool"].extend([
            report_validators.check_output_conflict,
        ])
        self._postflights["report_generator_tool"].extend([
            report_validators.check_file_exists,
        ])
        # data_visualizer_tool：图表 series 非空（postflight）
        self._postflights["data_visualizer_tool"].extend([
            report_validators.check_chart_series_nonempty,
        ])


# 进程级单例
_gateway_singleton: Optional[RulesGateway] = None


def get_rules_gateway() -> RulesGateway:
    global _gateway_singleton
    if _gateway_singleton is None:
        _gateway_singleton = RulesGateway()
    return _gateway_singleton
