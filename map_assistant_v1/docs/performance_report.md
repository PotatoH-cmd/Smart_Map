# 地图缩放性能对比报告（模板）

- 测试目标：采区边界与红线底图在 Leaflet 中缩放交互达到稳定 60 FPS
- 测试环境：Chrome 最新版，性能录制 30s，网络禁用缓存

## 优化前
- Timeline：Scripting/Rendering 峰值与均值
- 平均帧率：XX FPS
- 内存峰值：XX MB
- Lighthouse Performance：XX 分

## 优化后
- Timeline：Scripting/Rendering 峰值与均值
- 平均帧率：XX FPS
- 内存峰值：XX MB
- Lighthouse Performance：XX 分

## 结论
- 主要瓶颈与对应改进
- 后续可继续优化项（MVT、端侧简化、Worker 管道等）
