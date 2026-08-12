"""DeepFilter 降噪处理 — 从参考项目移植，适配 Colab 环境。"""

from __future__ import annotations

import os
import sys
import re
import math
import shutil
import subprocess
import tempfile
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# DeepFilter 二进制路径 & 模型选择
# ═══════════════════════════════════════════════════════════

_DEEPFILTER_DIR = os.environ.get("DEEPFILTER_DIR", "/content/.deepfilter")
_DEEPFILTER_BIN = "deep-filter-0.5.6-x86_64-unknown-linux-musl"
DEEP_FILTER_PATH = os.path.join(_DEEPFILTER_DIR, _DEEPFILTER_BIN)

# subprocess 默认超时（秒）
_WGET_TIMEOUT = 120
_FFMPEG_TIMEOUT = 600
_DEEPFILTER_TIMEOUT = 1800

# GTCRN denoiser 缓存（避免每章节重新加载模型）
_gtcrn_denoiser = None

DEEPFILTER_DOWNLOAD_URL = (
    "https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/"
    "deep-filter-0.5.6-x86_64-unknown-linux-musl"
)

# 可选模型: DeepFilterNet2 (v2, RTF≈0.08, 推荐, 二进制内置默认),
#            DeepFilterNet3 (v3, 质量最高, RTF≈0.10, 需下载 tar.gz)
#            GTCRN (超轻量, RTF≈0.045, sherpa-onnx, 16kHz)
# 注: Rust 二进制的 -m 需要 tar.gz 文件路径, 不是模型名。
#     DeepFilterNet2 是二进制内置默认, 不需要 -m。
#     DeepFilterNet3 需下载 DeepFilterNet3_onnx.tar.gz 并传路径。
#     GTCRN 使用 sherpa-onnx Python API, 不需要 deep-filter 二进制。
DEFAULT_MODEL = "DeepFilterNet2"
VALID_MODELS = {"DeepFilterNet2", "DeepFilterNet3", "GTCRN"}

# DeepFilterNet3 模型 tar.gz 下载地址 (Rust 二进制用)
DF3_MODEL_URL = (
    "https://raw.githubusercontent.com/Rikorose/DeepFilterNet/main/"
    "models/DeepFilterNet3_onnx.tar.gz"
)
DF3_MODEL_PATH = os.path.join(_DEEPFILTER_DIR, "DeepFilterNet3_onnx.tar.gz")

# GTCRN 模型 (sherpa-onnx, 16kHz, 48K 参数)
GTCRN_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speech-enhancement-models/gtcrn_simple.onnx"
)
GTCRN_MODEL_PATH = os.path.join(_DEEPFILTER_DIR, "gtcrn_simple.onnx")


def setup_deep_filter():
    """确保 DeepFilter 二进制就绪（幂等）。"""
    os.makedirs(_DEEPFILTER_DIR, exist_ok=True)

    if os.path.exists(DEEP_FILTER_PATH) and os.path.getsize(DEEP_FILTER_PATH) > 0:
        if not os.access(DEEP_FILTER_PATH, os.X_OK):
            os.chmod(DEEP_FILTER_PATH, 0o755)
        return True

    logger.info(f"下载 DeepFilter 二进制...")
    try:
        subprocess.run(
            ["wget", "--tries=5", "--timeout=30", "--retry-connrefused",
             DEEPFILTER_DOWNLOAD_URL, "-O", DEEP_FILTER_PATH],
            check=True, timeout=_WGET_TIMEOUT,
        )
        os.chmod(DEEP_FILTER_PATH, 0o755)
        logger.info("DeepFilter 下载完成")
        return True
    except Exception as e:
        logger.error(f"DeepFilter 下载失败: {e}")
        return False


def _ensure_model(model: str) -> str:
    """返回传递给 deep-filter -m 的参数值, 或空字符串表示用默认模型。

    DeepFilterNet2 是二进制内置默认, 不需要 -m。
    DeepFilterNet3 需要下载 tar.gz 并返回其路径。
    """
    if model == "DeepFilterNet2":
        return ""  # 二进制默认就是 DeepFilterNet2

    if model == "DeepFilterNet3":
        if os.path.exists(DF3_MODEL_PATH) and os.path.getsize(DF3_MODEL_PATH) > 0:
            return DF3_MODEL_PATH
        logger.info("下载 DeepFilterNet3 模型...")
        try:
            subprocess.run(
                ["wget", "--tries=5", "--timeout=30", "--retry-connrefused",
                 DF3_MODEL_URL, "-O", DF3_MODEL_PATH],
                check=True, timeout=_WGET_TIMEOUT,
            )
            logger.info("DeepFilterNet3 模型下载完成")
            return DF3_MODEL_PATH
        except Exception as e:
            logger.error(f"DeepFilterNet3 模型下载失败, 回退到 DeepFilterNet2: {e}")
            return ""

    return ""


# ═══════════════════════════════════════════════════════════
# GTCRN 降噪 (sherpa-onnx, 16kHz, 无需 deep-filter 二进制)
# ═══════════════════════════════════════════════════════════

def _ensure_gtcrn():
    """确保 GTCRN 模型和 sherpa-onnx 就绪, 返回 denoiser 对象。模型缓存复用。"""
    global _gtcrn_denoiser
    if _gtcrn_denoiser is not None:
        return _gtcrn_denoiser

    if not os.path.exists(GTCRN_MODEL_PATH) or os.path.getsize(GTCRN_MODEL_PATH) == 0:
        os.makedirs(_DEEPFILTER_DIR, exist_ok=True)
        logger.info("下载 GTCRN 模型...")
        subprocess.run(
            ["wget", "--tries=5", "--timeout=30", "--retry-connrefused",
             GTCRN_MODEL_URL, "-O", GTCRN_MODEL_PATH],
            check=True, timeout=_WGET_TIMEOUT,
        )
        logger.info("GTCRN 模型下载完成")

    try:
        import sherpa_onnx
    except ImportError:
        logger.info("安装 sherpa-onnx + soundfile...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        "sherpa-onnx", "soundfile"], check=True)
        import sherpa_onnx

    config = sherpa_onnx.OfflineSpeechDenoiserConfig(
        model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
            gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(
                model=GTCRN_MODEL_PATH
            ),
            debug=False,
            num_threads=1,
            provider="cpu",
        )
    )
    if not config.validate():
        raise RuntimeError("GTCRN 配置无效")
    _gtcrn_denoiser = sherpa_onnx.OfflineSpeechDenoiser(config)
    return _gtcrn_denoiser


def _gtcrn_denoise(audio_path: str, output_wav: str) -> str:
    """用 GTCRN 降噪, 输出 16kHz WAV。"""
    import numpy as np
    import soundfile as sf

    # 转为 16kHz mono WAV 临时文件
    tmp_16k = output_wav.replace(".wav", "_16k_input.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1",
         "-sample_fmt", "s16", "-acodec", "pcm_s16le", tmp_16k],
        capture_output=True, check=True, timeout=_FFMPEG_TIMEOUT,
    )

    data, sr = sf.read(tmp_16k, always_2d=True, dtype="float32")
    samples = np.ascontiguousarray(data[:, 0])

    denoiser = _ensure_gtcrn()
    result = denoiser(samples, sr)

    sf.write(output_wav, result.samples, result.sample_rate)
    os.unlink(tmp_16k)
    logger.info(f"GTCRN 降噪完成: {output_wav}")
    return output_wav


# ═══════════════════════════════════════════════════════════
# 音频分片处理
# ═══════════════════════════════════════════════════════════

def _split_audio_to_wav(input_file: str, output_dir: str, seg_minutes: int = 60, sr: int = 48000):
    """将长音频分片为 WAV 段。deep-filter 二进制要求 48kHz。"""
    # 获取音频时长（m4a 可能含视频流，ffprobe 可能返回非零，所以不用 check=True）
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_file],
        capture_output=True, text=True, timeout=60,
    )
    try:
        total = float(r.stdout.strip())
    except (ValueError, AttributeError):
        fallback = f"ffprobe 失败 (exit={r.returncode}): {r.stderr[:200]}"
        logger.warning(f"获取时长失败: {fallback}，尝试用 ffmpeg 转码整段")
        # 回退：不切分，直接整段转 WAV
        os.makedirs(output_dir, exist_ok=True)
        out = os.path.join(output_dir, "segment_001.wav")
        r = subprocess.run(
            ["ffmpeg", "-i", input_file, "-map", "0:a:0", "-vn", "-ar", str(sr), "-ac", "2",
             "-sample_fmt", "s16", "-acodec", "pcm_s16le", "-y", out],
            capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT,
        )
        if r.returncode != 0:
            raise RuntimeError(f"音频转码失败 (整段): {r.stderr[-500:]}")
        return
    seg_sec = seg_minutes * 60
    n = max(1, math.ceil(total / seg_sec))
    os.makedirs(output_dir, exist_ok=True)
    for i in range(n):
        start = i * seg_sec
        dur = min(seg_sec, total - start)
        out = os.path.join(output_dir, f"segment_{i + 1:03d}.wav")
        r = subprocess.run(
            ["ffmpeg", "-ss", str(start), "-t", str(dur), "-i", input_file,
             "-map", "0:a:0", "-vn", "-ar", str(sr), "-ac", "2", "-sample_fmt", "s16",
             "-acodec", "pcm_s16le", "-y", out],
            capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT,
        )
        if r.returncode != 0:
            raise RuntimeError(f"音频转码失败 (segment_{i+1:03d}): {r.stderr[-500:]}")


def _df_process_wav(wav_file: str, output_dir: str, model: str = DEFAULT_MODEL) -> str:
    """用 DeepFilter 处理单个 WAV 段。"""
    model_path = _ensure_model(model)
    cmd = [DEEP_FILTER_PATH]
    if model_path:
        cmd += ["-m", model_path]
    cmd += [wav_file, "--output-dir", output_dir]
    subprocess.run(cmd, check=True, timeout=_DEEPFILTER_TIMEOUT,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.join(output_dir, os.path.basename(wav_file))


def _df_and_merge(input_dir: str, output_dir: str, final_output: str,
                  max_workers: int = 1, model: str = DEFAULT_MODEL):
    """处理所有 WAV 段并合并。"""
    from pydub import AudioSegment

    os.makedirs(output_dir, exist_ok=True)
    wavs = sorted(
        [os.path.join(input_dir, f) for f in os.listdir(input_dir) if f.endswith(".wav")],
        key=os.path.getmtime,
    )
    # 重命名
    renamed = []
    for idx, f in enumerate(wavs, 1):
        np_ = os.path.join(input_dir, f"{idx}.wav")
        os.rename(f, np_)
        renamed.append(np_)

    worker_count = max(1, min(max_workers, len(renamed)))
    with ThreadPoolExecutor(max_workers=worker_count) as ex:
        processed = list(ex.map(lambda f: _df_process_wav(f, output_dir, model), renamed))

    processed.sort(key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    combined = AudioSegment.empty()
    for f in processed:
        combined += AudioSegment.from_wav(f)
    combined.export(final_output, format="wav")
    logger.info(f"降噪合并完成: {final_output}")


# ═══════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════

def denoise_audio(audio_path: str, segment_minutes: int = 60,
                   model: str = DEFAULT_MODEL) -> tuple[str, str]:
    """降噪单个音频文件，返回 (denoised_wav_path, job_dir)。"""
    model = model if model in VALID_MODELS else DEFAULT_MODEL
    source = Path(audio_path)
    job_dir = Path(tempfile.mkdtemp(prefix="deepfilter_job_"))
    split_dir = job_dir / "segments"
    df_dir = job_dir / "df"
    safe_stem = re.sub(r'[^\w\-]', '_', source.stem) if source.stem else "audio"
    denoised = job_dir / f"denoised_{safe_stem}.wav"

    logger.info(f"开始降噪 (模型={model}): {source.name}")
    _split_audio_to_wav(audio_path, str(split_dir), segment_minutes)
    _df_and_merge(str(split_dir), str(df_dir), str(denoised),
                  max_workers=1, model=model)
    logger.info(f"降噪完成: {source.name}")
    return str(denoised), str(job_dir)


def denoise_audio_keep_format(audio_path: str, output_path: str = "",
                              segment_minutes: int = 60,
                              model: str = DEFAULT_MODEL) -> str:
    """降噪并保持原始音频格式，返回输出路径。"""
    model = model if model in VALID_MODELS else DEFAULT_MODEL

    source = Path(audio_path)
    suffix = source.suffix.lower() or ".wav"
    target = Path(output_path) if output_path else source.with_name(f"{source.stem}_denoised{suffix}")

    # 已存在则跳过
    if target.exists() and target.stat().st_size > 0:
        logger.info(f"复用已降噪音频: {target.name}")
        return str(target)

    # GTCRN 走 sherpa-onnx, 不需要 deep-filter 二进制
    if model == "GTCRN":
        job_dir = Path(tempfile.mkdtemp(prefix="gtcrn_job_"))
        safe_stem = re.sub(r'[^\w\-]', '_', source.stem) if source.stem else "audio"
        temp_wav = str(job_dir / f"denoised_{safe_stem}.wav")
        try:
            logger.info(f"开始 GTCRN 降噪: {source.name}")
            _gtcrn_denoise(audio_path, temp_wav)
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

        os.makedirs(target.parent, exist_ok=True)
        try:
            if target.suffix.lower() == ".wav":
                if target.exists():
                    target.unlink()
                shutil.move(temp_wav, str(target))
            else:
                cmd = ["ffmpeg", "-y", "-i", temp_wav]
                if target.suffix.lower() == ".mp3":
                    cmd += ["-codec:a", "libmp3lame", "-b:a", "192k"]
                elif target.suffix.lower() in {".m4a", ".aac"}:
                    cmd += ["-codec:a", "aac", "-b:a", "192k"]
                else:
                    cmd += ["-codec:a", "libmp3lame", "-b:a", "192k"]
                cmd.append(str(target))
                subprocess.run(cmd, capture_output=True, check=True, timeout=_FFMPEG_TIMEOUT)
            logger.info(f"降噪音频已写回: {target.name}")
            return str(target)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    # DeepFilterNet2/3 走 Rust 二进制
    if not os.path.exists(DEEP_FILTER_PATH):
        if not setup_deep_filter():
            raise RuntimeError("DeepFilter 二进制不可用且下载失败")

    temp_wav, job_dir = denoise_audio(audio_path, segment_minutes, model=model)
    os.makedirs(target.parent, exist_ok=True)

    try:
        if target.suffix.lower() == ".wav":
            if target.exists():
                target.unlink()
            shutil.move(temp_wav, str(target))
        else:
            cmd = ["ffmpeg", "-y", "-i", temp_wav]
            if target.suffix.lower() == ".mp3":
                cmd += ["-codec:a", "libmp3lame", "-b:a", "192k"]
            elif target.suffix.lower() in {".m4a", ".aac"}:
                cmd += ["-codec:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-codec:a", "libmp3lame", "-b:a", "192k"]
            cmd.append(str(target))
            subprocess.run(cmd, capture_output=True, check=True)
        logger.info(f"降噪音频已写回: {target.name}")
        return str(target)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
