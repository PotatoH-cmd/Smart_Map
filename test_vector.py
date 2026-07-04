import requests

url = "http://172.136.16.14:8006/api/vector-data"

try:
    response = requests.get(url)
    print("Status Code:", response.status_code)
    if response.status_code == 200:
        data = response.json()
        print("GeoJSON type:", data.get("type"))
        print("Number of features:", len(data.get("features", [])))
        # 可选：验证是否符合 GeoJSON 规范
        assert data["type"] == "FeatureCollection"
        print("✅ 验证通过：返回了有效的 GeoJSON")
    else:
        print("❌ 请求失败:", response.text)
except Exception as e:
    print("💥 异常:", e)