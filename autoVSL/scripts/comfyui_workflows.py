#!/usr/bin/env python3
"""ComfyUI workflow builders (API format) for the autoVSL local pipeline.

Two graphs, tuned for a 4 GB laptop GPU (RTX 3050 Ti):
  * build_txt2img       — SD 1.5 still image (rock solid on 4 GB)
  * build_animatediff   — SD 1.5 + AnimateDiff short clip -> mp4 (experimental)

Everything is emitted as the flat {node_id: {class_type, inputs}} dict that
ComfyUI's /prompt endpoint expects. Node class names were confirmed against
the live /object_info schema.
"""

from __future__ import annotations


def build_txt2img(
    *,
    checkpoint: str,
    positive: str,
    negative: str = "text, watermark, low quality, blurry, deformed",
    width: int = 512,
    height: int = 768,
    seed: int = 0,
    steps: int = 25,
    cfg: float = 7.0,
    sampler: str = "euler",
    scheduler: str = "normal",
    batch_size: int = 1,
    filename_prefix: str = "autovsl/still",
) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": batch_size}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": 1.0, "model": ["1", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 0], "filename_prefix": filename_prefix}},
    }


def build_upscale(
    *,
    image_name: str,
    upscale_model: str = "RealESRGAN_x4plus.pth",
    filename_prefix: str = "autovsl/upscaled",
) -> dict:
    """Upscale an already-uploaded image with an ESRGAN model (e.g. 4x)."""
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": "UpscaleModelLoader",
              "inputs": {"model_name": upscale_model}},
        "3": {"class_type": "ImageUpscaleWithModel",
              "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "SaveImage",
              "inputs": {"images": ["3", 0], "filename_prefix": filename_prefix}},
    }


def build_inpaint(
    *,
    checkpoint: str,
    image_name: str,
    mask_name: str | None,
    positive: str,
    negative: str = "text, watermark, low quality, blurry, deformed",
    seed: int = 0,
    steps: int = 25,
    cfg: float = 7.0,
    denoise: float = 1.0,
    grow_mask_by: int = 6,
    sampler: str = "euler",
    scheduler: str = "normal",
    filename_prefix: str = "autovsl/inpaint",
) -> dict:
    """Inpaint the masked region of an uploaded image with a new prompt.
    If mask_name is None, the mask is taken from the image's own alpha channel
    (LoadImage output 1). Uses the base checkpoint (no dedicated inpaint model).
    """
    mask_src = ["1", 1] if mask_name is None else ["8", 0]
    wf = {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["5", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["5", 1]}},
        "6": {"class_type": "VAEEncodeForInpaint",
              "inputs": {"pixels": ["1", 0], "vae": ["5", 2],
                         "mask": mask_src, "grow_mask_by": grow_mask_by}},
        "7": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": denoise, "model": ["5", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["6", 0]}},
        "9": {"class_type": "VAEDecode",
              "inputs": {"samples": ["7", 0], "vae": ["5", 2]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["9", 0], "filename_prefix": filename_prefix}},
    }
    if mask_name is not None:
        # a separate mask image → convert its red channel to a MASK
        wf["8"] = {"class_type": "ImageToMask",
                   "inputs": {"image": ["11", 0], "channel": "red"}}
        wf["11"] = {"class_type": "LoadImage", "inputs": {"image": mask_name}}
    return wf


def build_controlnet_keyframe(
    *,
    checkpoint: str,
    controlnet: str,
    control_image: str,
    positive: str,
    negative: str = "text, watermark, low quality, blurry, deformed",
    width: int = 512,
    height: int = 768,
    seed: int = 0,
    steps: int = 25,
    cfg: float = 7.0,
    strength: float = 0.8,
    sampler: str = "euler",
    scheduler: str = "normal",
    filename_prefix: str = "autovsl/keyframe",
) -> dict:
    """SD1.5 + ControlNet: generate an image guided by a control image
    (depth/pose/canny/lineart map) so composition stays consistent across a set.
    `control_image` is an already-uploaded image name."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": control_image}},
        "5": {"class_type": "ControlNetLoader",
              "inputs": {"control_net_name": controlnet}},
        "6": {"class_type": "ControlNetApplyAdvanced",
              "inputs": {"positive": ["2", 0], "negative": ["3", 0],
                         "control_net": ["5", 0], "image": ["4", 0],
                         "strength": strength, "start_percent": 0.0,
                         "end_percent": 1.0}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "8": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": 1.0, "model": ["1", 0],
                         "positive": ["6", 0], "negative": ["6", 1],
                         "latent_image": ["7", 0]}},
        "9": {"class_type": "VAEDecode",
              "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["9", 0], "filename_prefix": filename_prefix}},
    }


def build_ltx2_i2v(
    *,
    checkpoint: str,
    text_encoder: str,
    upscale_model: str,
    image_name: str,
    positive: str,
    negative: str = "cartoon, childish, ugly, low quality, watermark, text",
    width: int = 576,
    height: int = 1024,
    length: int = 97,
    fps: int = 25,
    seed: int = 1,
    lora: str | None = None,
    lora_strength: float = 0.5,
    img_strength_pass1: float = 0.7,
    tile_size: int = 512,
    filename_prefix: str = "broll/ltx",
) -> dict:
    """LTX-2.3 image→video, transcribed from ComfyUI's own blueprint
    ("Image to Video (LTX-2.3).json", subgraph of 45 nodes) with the UI plumbing
    (Primitive*/ComfyMathExpression/Reroute) inlined.

    Two passes: sample at half resolution, LTXVLatentUpsampler doubles the video
    latent (this is where the spatial-upscaler-x2 model runs), then a short
    3-step refine at full res, tiled VAE decode, silent h264 mp4 via VHS.
    The model is audio-video joint, so an empty audio latent rides along through
    both passes exactly as in the blueprint — we simply never decode it.

    Constraints: width/height divisible by 64 (pass 1 runs at half size and
    latents are /32); length must be 8k+1 at 25 fps.

    Sampling regime depends on the distilled LoRA. The blueprint's few-step
    ManualSigmas (8 + 3) at cfg 1.0 are DISTILLATION settings — the blueprint
    always loads the LoRA, so they're only valid with it. Running them on the
    bare 22B dev model is unguided under-denoising: output ranges from soft
    (strong image guidance rescues it) to a contentless gradient. Without the
    LoRA this builder now switches to base-model sampling: LTXVScheduler
    24 steps + cfg 4.0 for pass 1, a 7-step refine + cfg 4.0 for pass 2.
    Slower (~2.5x) but correct; installing the LoRA restores the fast path.
    """
    if width % 64 or height % 64:
        raise ValueError(f"LTX dims must be divisible by 64, got {width}x{height}")
    if (length - 1) % 8:
        raise ValueError(f"LTX length must be 8k+1 frames, got {length}")

    model_src = ["30", 0] if lora else ["1", 0]
    if lora:
        cfg = 1.0
        sampler1, sigmas1 = "euler_ancestral_cfg_pp", ["17", 0]
        sampler2, sigmas2 = "euler_cfg_pp", ["28", 0]
    else:
        cfg = 4.0
        sampler1, sigmas1 = "euler_ancestral", ["34", 0]
        sampler2, sigmas2 = "euler", ["35", 0]
    wf = {
        # loaders -----------------------------------------------------------
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "2": {"class_type": "LTXAVTextEncoderLoader",
              "inputs": {"text_encoder": text_encoder, "ckpt_name": checkpoint,
                         "device": "default"}},
        "3": {"class_type": "LTXVAudioVAELoader",
              "inputs": {"ckpt_name": checkpoint}},
        "4": {"class_type": "LatentUpscaleModelLoader",
              "inputs": {"model_name": upscale_model}},
        # conditioning ------------------------------------------------------
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["2", 0]}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["2", 0]}},
        "7": {"class_type": "LTXVConditioning",
              "inputs": {"positive": ["5", 0], "negative": ["6", 0],
                         "frame_rate": float(fps)}},
        # source image ------------------------------------------------------
        "8": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "9": {"class_type": "ImageScale",
              "inputs": {"image": ["8", 0], "upscale_method": "lanczos",
                         "width": width, "height": height, "crop": "center"}},
        "10": {"class_type": "LTXVPreprocess",
               "inputs": {"image": ["9", 0], "img_compression": 18}},
        # pass 1: half resolution ------------------------------------------
        "11": {"class_type": "EmptyLTXVLatentVideo",
               "inputs": {"width": width // 2, "height": height // 2,
                          "length": length, "batch_size": 1}},
        "12": {"class_type": "LTXVImgToVideoInplace",
               "inputs": {"vae": ["1", 2], "image": ["10", 0], "latent": ["11", 0],
                          "strength": img_strength_pass1, "bypass": False}},
        "13": {"class_type": "LTXVEmptyLatentAudio",
               "inputs": {"frames_number": length, "frame_rate": fps,
                          "batch_size": 1, "audio_vae": ["3", 0]}},
        "14": {"class_type": "LTXVConcatAVLatent",
               "inputs": {"video_latent": ["12", 0], "audio_latent": ["13", 0]}},
        "15": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "16": {"class_type": "KSamplerSelect",
               "inputs": {"sampler_name": sampler1}},
        "18": {"class_type": "CFGGuider",
               "inputs": {"model": model_src, "positive": ["7", 0],
                          "negative": ["7", 1], "cfg": cfg}},
        "19": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["15", 0], "guider": ["18", 0],
                          "sampler": ["16", 0], "sigmas": sigmas1,
                          "latent_image": ["14", 0]}},
        "20": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["19", 0]}},
        # pass 2: latent-upscale x2 then a short refine ---------------------
        "21": {"class_type": "LTXVLatentUpsampler",
               "inputs": {"samples": ["20", 0], "upscale_model": ["4", 0],
                          "vae": ["1", 2]}},
        "22": {"class_type": "LTXVImgToVideoInplace",
               "inputs": {"vae": ["1", 2], "image": ["10", 0], "latent": ["21", 0],
                          "strength": 1.0, "bypass": False}},
        "23": {"class_type": "LTXVCropGuides",
               "inputs": {"positive": ["7", 0], "negative": ["7", 1],
                          "latent": ["20", 0]}},
        "24": {"class_type": "CFGGuider",
               "inputs": {"model": model_src, "positive": ["23", 0],
                          "negative": ["23", 1], "cfg": cfg}},
        "25": {"class_type": "LTXVConcatAVLatent",
               "inputs": {"video_latent": ["22", 0], "audio_latent": ["20", 1]}},
        "26": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed + 1}},
        "27": {"class_type": "KSamplerSelect",
               "inputs": {"sampler_name": sampler2}},
        "29": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["26", 0], "guider": ["24", 0],
                          "sampler": ["27", 0], "sigmas": sigmas2,
                          "latent_image": ["25", 0]}},
        "31": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["29", 0]}},
        # decode + save -----------------------------------------------------
        "32": {"class_type": "VAEDecodeTiled",
               "inputs": {"samples": ["31", 0], "vae": ["1", 2],
                          "tile_size": tile_size, "overlap": 64,
                          "temporal_size": 4096, "temporal_overlap": 4}},
        "33": {"class_type": "VHS_VideoCombine",
               "inputs": {"images": ["32", 0], "frame_rate": float(fps),
                          "loop_count": 0, "filename_prefix": filename_prefix,
                          "format": "video/h264-mp4", "pingpong": False,
                          "save_output": True}},
    }
    if lora:
        wf["30"] = {"class_type": "LoraLoaderModelOnly",
                    "inputs": {"model": ["1", 0], "lora_name": lora,
                               "strength_model": lora_strength}}
        wf["17"] = {"class_type": "ManualSigmas",
                    "inputs": {"sigmas": "1.0, 0.99375, 0.9875, 0.98125, 0.975, "
                                         "0.909375, 0.725, 0.421875, 0.0"}}
        wf["28"] = {"class_type": "ManualSigmas",
                    "inputs": {"sigmas": "0.85, 0.7250, 0.4219, 0.0"}}
    else:
        # base-model schedules: LTXVScheduler shifts by latent size (fed the
        # pass-1 video latent), the refine keeps the blueprint's 0.85 re-noise
        # entry point but descends in real steps instead of three.
        wf["34"] = {"class_type": "LTXVScheduler",
                    "inputs": {"steps": 24, "max_shift": 2.05, "base_shift": 0.95,
                               "stretch": True, "terminal": 0.1,
                               "latent": ["12", 0]}}
        wf["35"] = {"class_type": "ManualSigmas",
                    "inputs": {"sigmas": "0.85, 0.72, 0.58, 0.44, 0.30, 0.17, "
                                         "0.08, 0.0"}}
    return wf


def build_animatediff(
    *,
    checkpoint: str,
    motion_module: str,
    positive: str,
    negative: str = "text, watermark, low quality, blurry, deformed, jpeg artifacts",
    width: int = 384,
    height: int = 672,
    num_frames: int = 16,
    fps: int = 8,
    seed: int = 0,
    steps: int = 20,
    cfg: float = 7.5,
    sampler: str = "euler",
    scheduler: str = "normal",
    beta_schedule: str = "sqrt_linear (AnimateDiff)",
    context_length: int = 16,
    filename_prefix: str = "autovsl/clip",
    video_format: str = "video/h264-mp4",
) -> dict:
    """SD1.5 + AnimateDiff Gen1 -> VHS_VideoCombine mp4.

    Defaults are deliberately small (384x672, 16 frames) so a 4 GB card has a
    fighting chance. Scale down further if it OOMs; up if you have headroom.
    """
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "10": {"class_type": "ADE_AnimateDiffUniformContextOptions",
               "inputs": {"context_length": context_length, "context_stride": 1,
                          "context_overlap": 4,
                          "context_schedule": "uniform", "closed_loop": False,
                          "fuse_method": "flat", "use_on_equal_length": False,
                          "start_percent": 0.0, "guarantee_steps": 1}},
        "11": {"class_type": "ADE_AnimateDiffLoaderGen1",
               "inputs": {"model": ["1", 0], "model_name": motion_module,
                          "beta_schedule": beta_schedule,
                          "context_options": ["10", 0]}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": num_frames}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": sampler, "scheduler": scheduler,
                         "denoise": 1.0, "model": ["11", 0],
                         "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode",
              "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "VHS_VideoCombine",
              "inputs": {"images": ["6", 0], "frame_rate": float(fps),
                         "loop_count": 0, "filename_prefix": filename_prefix,
                         "format": video_format, "pingpong": False,
                         "save_output": True}},
    }
    return wf
