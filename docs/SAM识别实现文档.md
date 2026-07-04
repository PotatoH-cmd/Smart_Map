# SAM 目标识别实现文档

## 一、概述

本项目的 SAM（Segment Anything Model）目标识别功能基于 **SegEarth-OV-3** 项目实现，采用文本提示的语义分割方式，支持在地图上绘制区域后识别建筑物、车辆、道路、植被、水体等多种目标。

---

## 二、系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                   前端 (SAMPanel.jsx)                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ 地图绘制区域 │  │ 输入提示词   │  │ 进度显示/结果展示   │ │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘ │
└─────────┼─────────────────┼───────────────────┼─────────────┘
          │ POST /api/sam-detect                 │
          ▼                                      ▼
┌─────────────────────────────────────────────────────────────┐
│                 后端 API (main.py FastAPI)                   │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ POST /api/sam-detect                                   ││
│  │ 1. 生成 task_id                                        ││
│  │ 2. 创建进度文件                                        ││
│  │ 3. subprocess 调用 sam_predict.py                        ││
│  └─────────────────────────┬───────────────────────────────┘│
│                            │                                 │
│  ┌─────────────────────────┴───────────────────────────────┐│
│  │ GET /api/sam-progress/{task_id} (轮询进度)               ││
│  └───────────────────────────────────────────────────────────┘│
└─────────────────────────────┬───────────────────────────────┘
                              │ subprocess.run()
                              ▼
┌─────────────────────────────────────────────────────────────┐
│              SAM 推理脚本 (sam_predict.py)                    │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ 1. crop_image_from_local_tif() - 从 GeoTIFF 裁剪影像    │ │
│  │ 2. run_tile_based_inference() - 瓦片 + 放大推理        │ │
│  │    ├─ SegEarthOV3Segmentation (SAM3 模型)               │ │
│  │    └─ 9 类语义分割: 建筑物/车辆/道路/植被/水体等        │ │
│  │ 3. mask_to_polygons() - 轮廓提取                        │ │
│  │ 4. pixel_to_geo() - 坐标转换                           │ │
│  │ 5. 返回 GeoJSON FeatureCollection                       │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

---

## 三、核心文件说明

| 文件路径 | 功能描述 |
|---------|---------|
| `backend/tools/sam_predict.py` | SAM 核心推理脚本 |
| `backend/main.py` | FastAPI 后端接口（含 SAM 检测端点） |
| `frontend/src/components/SAMPanel.jsx` | 前端 SAM 交互面板 |

---

## 四、API 接口

### 4.1 SAM 检测接口

**路径**: `POST /api/sam-detect`

**请求参数**:
```python
class SAMDetectRequest(BaseModel):
    geometry: dict      # GeoJSON Polygon geometry
    prompt: str         # 识别目标提示词，如"建筑物"、"车辆"
    mode: str = "rectangle"  # rectangle | polygon
```

**处理流程**:
1. 生成唯一任务 ID (`task_id`)
2. 创建进度文件用于实时追踪 (`/tmp/sam_progress/{task_id}.json`)
3. 通过子进程调用 `sam_predict.py` 脚本
4. 解析脚本输出（JSON 格式的 GeoJSON 结果）
5. 返回带 `_task_id` 的 GeoJSON FeatureCollection

### 4.2 进度查询接口

**路径**: `GET /api/sam-progress/{task_id}`

返回实时推理进度，状态包括: `init` → `cropped` → `inference` → `inference_done` → `done`

### 4.3 SHP 下载接口

**路径**: `POST /api/sam-download`

将 GeoJSON 结果打包为 SHP + ZIP 格式下载。

---

## 五、SAM 推理核心逻辑

### 5.1 模型加载

推理脚本位于 `backend/tools/sam_predict.py`，依赖外部 SegEarth-OV-3 项目：

```python
SEG_DIR = Path(__file__).parent.parent.parent.parent / "seg" / "SegEarth-OV-3-main"
sys.path.insert(0, str(SEG_DIR))
os.chdir(str(SEG_DIR))  # SAM3 模型使用相对路径加载资源

from segearthov3_segmentor import SegEarthOV3Segmentation
```

### 5.2 支持的类别

模型支持 9 类语义分割：

| 索引 | 类别名称 |
|------|---------|
| 0 | background |
| 1 | bareland,barren |
| 2 | grass |
| 3 | road |
| 4 | car |
| 5 | tree,forest |
| 6 | water,river |
| 7 | cropland |
| 8 | building,roof,house |

### 5.3 瓦片分割 + 放大推理策略

为提高高分辨率遥感影像的小目标检测能力，系统采用**瓦片分割 + 放大推理**策略：

```python
def run_tile_based_inference(image_path, text_prompt, output_dir,
                              tile_size=512, zoom_factor=3, overlap=64):
```

**核心原理**:
1. **瓦片分割**: 将高分辨率图像切成 512×512 像素的重叠小瓦片
2. **放大推理**: 每个瓦片放大 3 倍后进行 SAM 推理（小目标在放大后更清晰可检测）
3. **加权合并**: 使用线性权重（中心区域权重更高，边缘渐变）合并所有瓦片的预测 mask
4. **阈值化**: 超过 35% 置信度即认为存在目标

**参数说明**:

| 参数 | 默认值 | 说明 |
|------|--------|------|
| tile_size | 512 | 瓦片像素大小 |
| zoom_factor | 3 | 瓦片放大倍数 |
| overlap | 64 | 瓦片间重叠像素（避免边缘伪影） |
| merge_threshold | 0.35 | 合并阈值(35%) |

### 5.4 影像裁剪

```python
def crop_image_from_local_tif(bounds, output_path, target_size=1024):
```

从本地 GeoTIFF 裁剪指定范围的影像：
- 支持坐标系转换（EPSG:4326 ↔ TIF 坐标系）
- 线性拉伸: 2%-98% → uint8
- 高分辨率模式：保留原始分辨率用于瓦片分割

本地影像路径: `/home/server/python/GIS/output/merged_output123.tif`

---

## 六、推理结果后处理

### 6.1 Mask 转多边形

```python
def mask_to_polygons(mask, min_area=100):
    """使用 OpenCV 轮廓检测将二值 mask 转换为多边形"""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
```

### 6.2 像素坐标转地理坐标

```python
def pixel_to_geo(poly_pixels, img_bounds, img_shape):
    """将像素坐标转换为 EPSG:4326 地理坐标"""
```

### 6.3 输出格式

返回 GeoJSON FeatureCollection，每个 Feature 包含:
- `geometry`: 多边形几何
- `properties`:
  - `id`: 序号
  - `prompt`: 识别提示词
  - `area_m2`: 面积（平方米）
  - `area_mu`: 面积（亩）

---

## 七、前端交互流程

1. **绘制区域**: 用户在地图上绘制矩形或多边形
2. **输入提示词**: 输入识别目标，如"建筑物"、"车辆"
3. **发起识别**: 点击"开始识别"按钮
4. **进度显示**: 实时显示推理进度百分比
5. **结果展示**: 识别结果以红色边框多边形叠加到地图上
6. **下载结果**: 可将结果下载为 SHP 格式

---

## 八、技术特点

| 特点 | 说明 |
|------|------|
| 文本提示 | 支持中文提示词匹配类别 |
| 瓦片策略 | 解决高分辨率影像小目标检测问题 |
| 放大推理 | 3 倍放大使小目标更清晰 |
| 加权融合 | 线性权重消除拼接痕迹 |
| 实时进度 | 通过进度文件实现异步进度跟踪 |
| 多边形简化 | 使用 Douglas-Peucker 算法减少顶点数 |

---

## 九、依赖环境

- Python 环境: `/home/server/miniconda3/envs/sam/bin/python`
- CUDA 设备: 通过环境变量 `CUDA_VISIBLE_DEVICES` 配置
- 外部模型: SegEarth-OV-3 项目位于 `/home/server/python/seg/SegEarth-OV-3-main`

---

## 十、SegEarth-OV-3 放大推理策略（已集成）

SegEarth-OV-3 项目已集成放大推理策略，修改了 `segearthov3_segmentor.py`：

### 10.1 新增参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `zoom_factor` | 1 | 瓦片放大倍数（>1时启用放大推理） |
| `zoom_overlap` | 64 | 瓦片重叠像素数 |
| `zoom_tile_size` | 512 | 瓦片像素大小 |
| `zoom_max_size` | 2048 | 放大后最大尺寸限制 |

### 10.2 核心方法

**`slide_with_zoom_inference`** - 放大推理核心方法
- 将图像切成 512×512 重叠瓦片
- 每片放大 zoom_factor 倍后推理
- 放大后小目标更清晰
- 使用线性权重图加权合并消除边缘伪影

**`_compute_tile_weight_map`** - 权重图计算
- 中心区域权重高（=1）
- 边缘区域权重低（渐变至0）
- 消除瓦片拼接痕迹

### 10.3 使用方式

```python
# 初始化时设置 zoom_factor > 1 即可启用放大推理
model = SegEarthOV3Segmentation(
    type='SegEarthOV3Segmentation',
    model_type='SAM3',
    classname_path='./configs/my_name.txt',
    prob_thd=0.08,
    confidence_threshold=0.05,
    zoom_factor=3,        # 启用3倍放大推理
    zoom_overlap=64,     # 64像素重叠
    zoom_tile_size=512,  # 512像素瓦片
    zoom_max_size=2048,  # 最大放大尺寸
)
```

### 10.4 推理流程选择

`predict()` 方法根据参数自动选择推理模式：

1. **`zoom_factor > 1`**: 放大推理模式（瓦片分割 + 放大）
2. **`slide_crop > 0`**: 普通滑动窗口模式
3. **其他**: 单次整体推理
