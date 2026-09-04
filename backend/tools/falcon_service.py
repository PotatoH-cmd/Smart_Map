"""
Falcon-Perception 常驻推理服务
================================
将 TII Falcon-Perception（tiiuae/Falcon-Perception，0.6B 早融合多模态 Transformer）
以常驻 FastAPI 服务方式暴露，供 map_assistant_v1 后端 / falcon_detect.py 调用，
避免子进程每请求冷加载模型的巨大开销（原 SAM 子进程模式冷启动 ~13s）。

接口：
  GET  /health  → {status, model_id, device, vram_mb, ready}
  POST /detect  → {instances: [{mask_rle, bbox_norm}], processed_size, timing}
                  请求体 {image_path|image_b64, query, task=segmentation|detection,
                          max_new_tokens, min_dimension, max_dimension}

设计要点：
  · batch engine（无 CUDAGraph 依赖，共卡环境更稳），compile=False 避免长预热
  · dtype=bfloat16（0.6B 模型 ~2GB 显存，与 vLLM 共卡无压力）
  · flex_attn_safe kernel_options 默认开（Ada GPU 每-SM 共享内存限制，
    避免 FlexAttention Triton OOM，README 针对 3090/4090/L40 同代架构的说明）
  · 推理全程持锁串行（batch engine 非线程安全），但常驻后单瓦片亚秒级
  · masks_rle 为 COCO RLE（counts 为压缩字符串），尺寸为模型处理分辨率
    （16 的倍数、≤max_dimension），调用方需按原尺寸 NEAREST 缩放
"""
import os

# 显存碎片优化：与 vLLM 共卡时减少 OOM 概率（必须在 torch import 前设置）
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import base64
import time
import threading
from pathlib import Path

import torch
from fastapi import FastAPI
from pydantic import BaseModel, Field
from typing import Literal

from falcon_perception import (
    PERCEPTION_MODEL_ID,
    build_prompt_for_task,
    load_and_prepare_model,
    setup_torch_config,
)
from falcon_perception.aux_output import AuxOutput
from falcon_perception.batch_inference import (
    BatchInferenceEngine,
    process_batch_and_generate,
)
from falcon_perception.data import ImageProcessor, load_image
from falcon_perception.visualization_utils import pair_bbox_entries

setup_torch_config()

# ── 服务配置（环境变量） ──
FALCON_PORT = int(os.environ.get("FALCON_PORT", "8765"))
FALCON_HF_MODEL_ID = os.environ.get("FALCON_HF_MODEL_ID", PERCEPTION_MODEL_ID)
FALCON_HF_LOCAL_DIR = os.environ.get("FALCON_HF_LOCAL_DIR", "").strip()
FALCON_HF_REVISION = os.environ.get("FALCON_HF_REVISION", "main")
FALCON_DTYPE = os.environ.get("FALCON_DTYPE", "bfloat16")
FALCON_MAX_NEW_TOKENS = int(os.environ.get("FALCON_MAX_NEW_TOKENS", "2048"))
# Ada 级 GPU（RTX 5880 Ada / 3090 / 4090 / L40）FlexAttention Triton OOM 规避
FALCON_FLEX_ATTN_SAFE = os.environ.get("FALCON_FLEX_ATTN_SAFE", "1") == "1"

app = FastAPI(title="Falcon Perception Service", version="1.0.0")

_state = {
    "model": None,
    "tokenizer": None,
    "engine": None,
    "image_processor": None,
    "model_args": None,
    "loaded": False,
    "error": None,
    "started_at": time.time(),
}
_infer_lock = threading.Lock()  # batch engine 推理串行化


def _log(msg: str):
    print(f"[FALCON-SVC] {msg}", flush=True)


def _kernel_options():
    """flex_attn_safe=True 时使用小 block kernel，规避 Ada GPU Triton OOM。"""
    return {"BLOCK_M": 64, "BLOCK_N": 64, "num_stages": 1} if FALCON_FLEX_ATTN_SAFE else None


def get_engine():
    """懒加载模型与推理引擎（线程安全）。首次调用耗时 ~10-20s。"""
    with _infer_lock:
        if _state["loaded"] or _state["error"]:
            if _state["error"]:
                raise RuntimeError(f"模型加载失败: {_state['error']}")
            return _state
        try:
            _log(f"加载模型: id={FALCON_HF_MODEL_ID}, local_dir={FALCON_HF_LOCAL_DIR or '<hf hub>'}, "
                 f"dtype={FALCON_DTYPE}, compile=False, flex_attn_safe={FALCON_FLEX_ATTN_SAFE}")
            t0 = time.time()
            model, tokenizer, model_args = load_and_prepare_model(
                hf_model_id=FALCON_HF_MODEL_ID or None,
                hf_revision=FALCON_HF_REVISION,
                hf_local_dir=FALCON_HF_LOCAL_DIR or None,
                dtype=FALCON_DTYPE,
                compile=False,
            )
            engine = BatchInferenceEngine(model, tokenizer, kernel_options=_kernel_options())
            _state.update(
                model=model,
                tokenizer=tokenizer,
                engine=engine,
                model_args=model_args,
                image_processor=ImageProcessor(patch_size=16, merge_size=1),
                loaded=True,
            )
            _log(f"模型就绪，耗时 {time.time() - t0:.1f}s, device={model.device}, dtype={model.dtype}")
        except Exception as e:
            _state["error"] = str(e)
            _log(f"模型加载失败: {e}")
            raise
    return _state


class DetectRequest(BaseModel):
    image_path: str | None = None
    image_b64: str | None = None
    query: str = Field(..., min_length=1)
    task: Literal["segmentation", "detection"] = "segmentation"
    max_new_tokens: int = FALCON_MAX_NEW_TOKENS
    min_dimension: int = 256
    max_dimension: int = 1024


class InstanceOut(BaseModel):
    bbox_norm: dict | None = None      # {x, y, h, w} 归一化 [0,1]
    mask_rle: dict | None = None       # COCO RLE {counts: str, size: [H, W]}


class DetectResponse(BaseModel):
    instances: list[InstanceOut]
    processed_size: list[int]          # RLE 所处分辨率 [H, W]（16 的倍数）
    query: str
    task: str
    timing_ms: int


def _load_request_image(req: DetectRequest):
    if req.image_b64:
        import io
        from PIL import Image as PILImage
        raw = base64.b64decode(req.image_b64)
        return PILImage.open(io.BytesIO(raw)).convert("RGB")
    if req.image_path:
        if not os.path.exists(req.image_path):
            raise FileNotFoundError(f"image_path 不存在: {req.image_path}")
        return load_image(req.image_path).convert("RGB")
    raise ValueError("image_path 与 image_b64 至少提供一个")


@app.post("/detect", response_model=DetectResponse)
def detect(req: DetectRequest):
    """单图单查询推理：分割 → RLE masks，检测 → 归一化 bbox。"""
    st = get_engine()
    pil_image = _load_request_image(req)
    w, h = pil_image.size

    prompt = build_prompt_for_task(req.query, req.task)
    stop_token_ids = [st["tokenizer"].eos_token_id, st["tokenizer"].end_of_query_token_id]

    t0 = time.time()
    with _infer_lock:
        batch_inputs = process_batch_and_generate(
            st["tokenizer"],
            [(pil_image, prompt)],
            max_length=4096,
            min_dimension=req.min_dimension,
            max_dimension=req.max_dimension,
        )
        batch_inputs = {
            k: (v.to(st["model"].device) if torch.is_tensor(v) else v)
            for k, v in batch_inputs.items()
        }
        _, aux_out = st["engine"].generate(
            **batch_inputs,
            max_new_tokens=req.max_new_tokens,
            temperature=0.0,
            stop_token_ids=stop_token_ids,
            seed=42,
            task=req.task,
        )
    elapsed_ms = int((time.time() - t0) * 1000)

    aux = aux_out[0] if isinstance(aux_out, (list, tuple)) and aux_out else aux_out
    instances: list[InstanceOut] = []

    if isinstance(aux, AuxOutput):
        if req.task == "segmentation":
            for rle in aux.masks_rle:
                if isinstance(rle, dict) and rle.get("size"):
                    instances.append(InstanceOut(mask_rle=rle))
        else:
            for bb in pair_bbox_entries(aux.bboxes_raw):
                instances.append(InstanceOut(bbox_norm=bb))
    elif isinstance(aux, (list, tuple)):
        # 兜底：裸 token 流（理论上 batch engine 已 finalize 为 AuxOutput）
        n = len(aux) // (3 if req.task == "segmentation" else 2)
        _log(f"警告: aux_out 为裸列表，长度 {len(aux)}，约 {n} 个实例（无 mask 细节）")

    _log(f"detect: query={req.query!r} task={req.task} img={w}x{h} → "
         f"{len(instances)} instances, {elapsed_ms}ms")
    return DetectResponse(
        instances=instances,
        processed_size=(instances[0].mask_rle["size"] if instances and instances[0].mask_rle else [h, w]),
        query=req.query,
        task=req.task,
        timing_ms=elapsed_ms,
    )


@app.get("/health")
def health():
    st = _state
    vram_mb = None
    try:
        if torch.cuda.is_available():
            vram_mb = round(torch.cuda.memory_allocated() / 1024 / 1024)
    except Exception:
        pass
    return {
        "status": "ready" if st["loaded"] else ("error" if st["error"] else "loading"),
        "model_id": FALCON_HF_MODEL_ID,
        "local_dir": FALCON_HF_LOCAL_DIR or None,
        "device": str(st["model"].device) if st["model"] else ("cuda" if torch.cuda.is_available() else "cpu"),
        "vram_mb": vram_mb,
        "uptime_s": int(time.time() - st["started_at"]),
        "error": st["error"],
    }


if __name__ == "__main__":
    import uvicorn
    _log(f"启动 Falcon Perception Service: port={FALCON_PORT}, model={FALCON_HF_MODEL_ID}")
    uvicorn.run(app, host="0.0.0.0", port=FALCON_PORT, log_level="info")
