import json
import logging
import os
from typing import Dict, Any, Optional, List, Union
import httpx
from qwen_agent.tools.base import BaseTool, register_tool

logger = logging.getLogger(__name__)


@register_tool("weather_tool")
class WeatherTool(BaseTool):
    description = """
天气查询工具，支持当前天气、3/7天预报与可选空气质量信息。
入参：city（必填）、forecast_days（1/3/7，默认1）、include_aqi（默认false）。
基于和风天气(QWeather) API v7 实现。
"""

    parameters = [
        {
            "name": "city",
            "type": "string",
            "description": "城市或地区名称，如 '信阳市'、'信阳市淮滨县'，也可以是 '116.41,39.92' 格式的经纬度",
            "required": True,
        },
        {
            "name": "forecast_days",
            "type": "integer",
            "description": "预报天数：1=今天，3=3天，7=7天",
            "required": False,
            "default": 1,
        },
        {
            "name": "include_aqi",
            "type": "boolean",
            "description": "是否包含空气质量信息",
            "required": False,
            "default": False,
        },
    ]

    QWEATHER_HOST = "https://devapi.qweather.com"
    GEO_HOST = "https://geoapi.qweather.com"

    def call(self, params: Union[str, Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        city = params.get("city", "").strip()
        forecast_days = int(params.get("forecast_days", 1) or 1)
        include_aqi = bool(params.get("include_aqi", False))

        if not city:
            return {"success": False, "error": "城市名称不能为空"}

        api_key = os.environ.get("WEATHER_API_KEY", "bb183d8bf04a4870be4efe78e7e6337b")

        try:
            result = self._call_qweather(city, forecast_days, include_aqi, api_key)
            if result.get("success"):
                return result
            logger.warning(f"QWeather unavailable for '{city}', falling back to web search")
        except Exception as e:
            logger.warning(f"Weather query failed: {e}")

        # 显式配置 WEATHER_MOCK=1 时才使用随机模拟数据（默认关闭，避免误导）
        if os.environ.get("WEATHER_MOCK", "0") == "1":
            return self._mock_weather(city, forecast_days, include_aqi)

        # 降级链：和风天气不可用 → 百炼联网搜索查实时天气（带真实来源）
        return self._fallback_web_search(city, forecast_days)

    # =============== QWeather v7 实现 ===============
    def _call_qweather(self, city: str, forecast_days: int, include_aqi: bool, api_key: str) -> Dict[str, Any]:
        location = self._resolve_location(city, api_key)
        if not location:
            logger.warning(f"Could not resolve location for '{city}', using coordinates directly")
            location = city

        now_data = self._qweather_now(location, api_key)
        aqi_data = self._qweather_air_now(location, api_key) if include_aqi else None

        if forecast_days <= 1:
            if not now_data:
                return self._weather_unavailable(city)
            return {
                "success": True,
                "provider": "qweather",
                "city": city,
                "location": location,
                "source": "api",
                "current": now_data,
                "aqi": aqi_data,
            }
        else:
            daily = self._qweather_daily(location, api_key, forecast_days)
            if not daily:
                return self._weather_unavailable(city)
            return {
                "success": True,
                "provider": "qweather",
                "city": city,
                "location": location,
                "source": "api",
                "current": now_data,
                "forecasts": daily,
                "aqi": aqi_data,
            }

    def _resolve_location(self, city: str, api_key: str) -> Optional[str]:
        if "," in city and city.replace(",", "").replace(".", "").replace("-", "").isdigit():
            return city
        loc = self._geo_lookup(city, api_key)
        if loc:
            return loc
        return None

    def _geo_lookup(self, city: str, api_key: str) -> Optional[str]:
        url = f"{self.GEO_HOST}/v2/city/lookup"
        params = {"location": city, "key": api_key}
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(url, params=params, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    if str(data.get("code")) == "200" and data.get("location"):
                        return data["location"][0].get("id")
        except Exception as e:
            logger.warning(f"Geo lookup failed: {e}")
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(url, params={"location": city, "key": api_key})
                if r.status_code == 200:
                    data = r.json()
                    if str(data.get("code")) == "200" and data.get("location"):
                        return data["location"][0].get("id")
        except Exception as e:
            logger.warning(f"Geo lookup (key auth) failed: {e}")
        return None

    def _qweather_now(self, location: str, api_key: str) -> Dict[str, Any]:
        url = f"{self.QWEATHER_HOST}/v7/weather/now"
        params = {"location": location, "key": api_key}
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(url, params=params)
                if r.status_code == 200:
                    data = r.json()
                    if str(data.get("code")) == "200":
                        now = data.get("now", {})
                        return {
                            "temp": self._to_number(now.get("temp")),
                            "feels_like": self._to_number(now.get("feelsLike")),
                            "humidity": self._to_number(now.get("humidity")),
                            "pressure": self._to_number(now.get("pressure")),
                            "wind_speed": self._to_number(now.get("windSpeed")),
                            "wind_direction": now.get("windDir"),
                            "wind_scale": now.get("windScale"),
                            "weather": now.get("text"),
                            "description": now.get("text"),
                            "visibility": self._to_number(now.get("vis")),
                            "cloudiness": self._to_number(now.get("cloud")),
                            "obs_time": now.get("obsTime"),
                        }
        except Exception as e:
            logger.warning(f"Weather now failed: {e}")
        return {}

    def _qweather_daily(self, location: str, api_key: str, days: int) -> List[Dict[str, Any]]:
        days_map = {3: "3d", 7: "7d", 10: "10d", 15: "15d", 30: "30d"}
        days_key = days_map.get(days, "7d") if days <= 30 else "7d"

        url = f"{self.QWEATHER_HOST}/v7/weather/{days_key}"
        params = {"location": location, "key": api_key}
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(url, params=params)
                if r.status_code == 200:
                    data = r.json()
                    if str(data.get("code")) == "200":
                        return self._parse_daily(data.get("daily", []))
        except Exception as e:
            logger.warning(f"Weather daily failed: {e}")
        return []

    def _parse_daily(self, daily_list: List[Dict]) -> List[Dict[str, Any]]:
        out = []
        for d in daily_list:
            out.append({
                "date": d.get("fxDate"),
                "temp_max": self._to_number(d.get("tempMax")),
                "temp_min": self._to_number(d.get("tempMin")),
                "text_day": d.get("textDay"),
                "text_night": d.get("textNight"),
                "wind_dir_day": d.get("windDirDay"),
                "wind_scale_day": d.get("windScaleDay"),
                "wind_speed_day": self._to_number(d.get("windSpeedDay")),
                "wind_dir_night": d.get("windDirNight"),
                "wind_scale_night": d.get("windScaleNight"),
                "wind_speed_night": self._to_number(d.get("windSpeedNight")),
                "precip": self._to_number(d.get("precip")),
                "humidity": self._to_number(d.get("humidity")),
                "pressure": self._to_number(d.get("pressure")),
                "visibility": self._to_number(d.get("vis")),
                "cloudiness": self._to_number(d.get("cloud")),
                "uv_index": self._to_number(d.get("uvIndex")),
                "sunrise": d.get("sunrise"),
                "sunset": d.get("sunset"),
                "moonrise": d.get("moonrise"),
                "moonset": d.get("moonset"),
                "moon_phase": d.get("moonPhase"),
            })
        return out

    def _qweather_air_now(self, location: str, api_key: str) -> Optional[Dict[str, Any]]:
        url = f"{self.QWEATHER_HOST}/v7/air/now"
        params = {"location": location, "key": api_key}
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(url, params=params)
                if r.status_code == 200:
                    data = r.json()
                    if str(data.get("code")) == "200":
                        now = data.get("now", {})
                        return {
                            "aqi": self._to_number(now.get("aqi")),
                            "category": now.get("category"),
                            "pm2p5": self._to_number(now.get("pm2p5")),
                            "pm10": self._to_number(now.get("pm10")),
                            "so2": self._to_number(now.get("so2")),
                            "no2": self._to_number(now.get("no2")),
                            "co": self._to_number(now.get("co")),
                            "o3": self._to_number(now.get("o3")),
                        }
        except Exception as e:
            logger.warning(f"Air now failed: {e}")
        return None

    # =============== 联网搜索降级 ===============
    def _fallback_web_search(self, city: str, forecast_days: int) -> Dict[str, Any]:
        """和风天气不可用时，通过百炼联网搜索查询实时天气（带真实来源）。"""
        try:
            from tools.web_search_tool import WebSearchTool
            question = f"{city}今天天气" if forecast_days <= 1 else f"{city}未来{forecast_days}天天气预报"
            r = WebSearchTool().call({"query": question})
            if r.get("success"):
                return {
                    "success": True,
                    "provider": "web-search",
                    "city": city,
                    "source": "web_search",
                    "answer": r.get("answer", ""),
                    "search_results": r.get("search_results", []),
                    "total_results": r.get("total_results", 0),
                }
            return {"success": False, "error": r.get("error", "联网搜索不可用"), "provider": "web-search"}
        except Exception as e:
            logger.warning(f"Weather web-search fallback failed: {e}")
            return {"success": False, "error": f"天气服务与联网搜索均不可用：{e}", "provider": "qweather"}

    # =============== 服务不可用兜底 ===============
    def _weather_unavailable(self, city: str) -> Dict[str, Any]:
        """真实天气服务不可用时返回明确失败，不编造数据。"""
        return {
            "success": False,
            "provider": "qweather",
            "city": city,
            "error": "天气服务暂时不可用，请稍后重试或通过联网搜索查询",
        }

    # =============== Fallback: 模拟数据（仅 WEATHER_MOCK=1 时启用） ===============
    def _mock_weather(self, city: str, forecast_days: int, include_aqi: bool) -> Dict[str, Any]:
        import random
        from datetime import datetime, timedelta

        conditions = ["晴", "多云", "阴", "小雨", "中雨", "雷阵雨", "雾", "霾"]
        cond = random.choice(conditions)
        base = random.randint(15, 32)

        current = {
            "temp": base,
            "feels_like": base + random.randint(-2, 2),
            "humidity": random.randint(40, 90),
            "pressure": random.randint(995, 1025),
            "wind_speed": random.randint(1, 5),
            "wind_direction": "N",
            "wind_scale": "3",
            "weather": cond,
            "description": cond,
            "visibility": random.randint(3000, 10000),
            "cloudiness": random.randint(0, 100),
            "obs_time": datetime.utcnow().isoformat(),
        }

        result: Dict[str, Any] = {
            "success": True,
            "provider": "mock",
            "city": city,
            "current": current,
            "source": "mock",
        }

        if include_aqi:
            result["aqi"] = {
                "aqi": random.randint(20, 150),
                "category": "良",
                "pm2p5": random.randint(10, 80),
                "pm10": random.randint(20, 120),
                "so2": random.randint(5, 50),
                "no2": random.randint(5, 60),
                "co": round(random.uniform(0.3, 1.5), 2),
                "o3": random.randint(50, 200),
            }

        if forecast_days > 1:
            days = 3 if forecast_days <= 3 else 7
            daily = []
            for i in range(days):
                dt = (datetime.now() + timedelta(days=i)).date().isoformat()
                daily.append({
                    "date": dt,
                    "temp_max": base + random.randint(0, 5),
                    "temp_min": base - random.randint(0, 5),
                    "text_day": random.choice(conditions),
                    "text_night": random.choice(conditions),
                    "wind_scale_day": str(random.randint(1, 4)),
                    "wind_scale_night": str(random.randint(1, 3)),
                    "precip": round(random.uniform(0, 10), 1),
                })
            result["forecasts"] = daily

        return result

    def _to_number(self, v: Optional[str]) -> Optional[float]:
        try:
            if v is None:
                return None
            return float(v)
        except Exception:
            return None
