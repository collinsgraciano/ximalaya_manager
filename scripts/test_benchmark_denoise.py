"""Colab 降噪速度基准测试: DeepFilterNet1 vs DeepFilterNet2 vs DeepFilterNet3 vs GTCRN

用法:
    1. 上传音频文件到 Colab
    2. 修改 INPUT_AUDIO 路径
    3. 运行 !python scripts/test_benchmark_denoise.py
"""

import os
import sys
import time
import subprocess
import tempfile

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ═══════════════════════════════════════════════════
# 修改这里: 你的音频文件路径
# ═══════════════════════════════════════════════════
INPUT_AUDIO = "/content/0001_大奉打更人 第1集 牢狱之灾.m4a"

# ═══════════════════════════════════════════════════
# 路径常量
# ═══════════════════════════════════════════════════
DEEPFILTER_DIR = "/content/.deepfilter"
DF_BIN = os.path.join(DEEPFILTER_DIR, "deep-filter-0.5.6-x86_64-unknown-linux-musl")
DF_URL = (
    "https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/"
    "deep-filter-0.5.6-x86_64-unknown-linux-musl"
)
DF3_MODEL_URL = (
    "https://raw.githubusercontent.com/Rikorose/DeepFilterNet/main/"
    "models/DeepFilterNet3_onnx.tar.gz"
)
DF3_MODEL_PATH = os.path.join(DEEPFILTER_DIR, "DeepFilterNet3_onnx.tar.gz")
GTCRN_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speech-enhancement-models/gtcrn_simple.onnx"
)
GTCRN_MODEL_PATH = "/content/gtcrn_simple.onnx"

# 48kHz WAV (DeepFilter 要求), 16kHz WAV (GTCRN 用)
WAV_48K = "/tmp/bench_input_48k.wav"
WAV_16K = "/tmp/bench_input_16k.wav"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def prep_audio():
    """转换输入音频为 48kHz 和 16kHz WAV。"""
    # 48kHz mono (DeepFilter 要求)
    run(["ffmpeg", "-y", "-i", INPUT_AUDIO, "-ar", "48000", "-ac", "1",
         "-sample_fmt", "s16", "-acodec", "pcm_s16le", WAV_48K], check=True)
    # 16kHz mono (GTCRN 用)
    run(["ffmpeg", "-y", "-i", INPUT_AUDIO, "-ar", "16000", "-ac", "1",
         "-sample_fmt", "s16", "-acodec", "pcm_s16le", WAV_16K], check=True)
    # 获取时长
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", INPUT_AUDIO], check=True)
    duration = float(r.stdout.strip())
    print(f"输入: {INPUT_AUDIO}")
    print(f"  时长: {duration:.1f}s")
    print(f"  48kHz WAV: {WAV_48K}")
    print(f"  16kHz WAV: {WAV_16K}")
    return duration


def ensure_df_binary():
    if os.path.exists(DF_BIN) and os.path.getsize(DF_BIN) > 0:
        if not os.access(DF_BIN, os.X_OK):
            os.chmod(DF_BIN, 0o755)
        return
    os.makedirs(DEEPFILTER_DIR, exist_ok=True)
    print("下载 deep-filter 二进制...")
    run(["wget", "-q", DF_URL, "-O", DF_BIN], check=True)
    os.chmod(DF_BIN, 0o755)


def ensure_df3_model():
    if os.path.exists(DF3_MODEL_PATH) and os.path.getsize(DF3_MODEL_PATH) > 0:
        return
    print("下载 DeepFilterNet3 模型...")
    run(["wget", "-q", DF3_MODEL_URL, "-O", DF3_MODEL_PATH], check=True)


def ensure_gtcrn():
    if os.path.exists(GTCRN_MODEL_PATH) and os.path.getsize(GTCRN_MODEL_PATH) > 0:
        return
    print("下载 GTCRN 模型...")
    run(["wget", "-q", GTCRN_MODEL_URL, "-O", GTCRN_MODEL_PATH], check=True)


def bench_df_rust(name, extra_args):
    """测试 Rust 二进制 deep-filter (DeepFilterNet2/3)。"""
    out_dir = tempfile.mkdtemp(prefix=f"bench_{name}_")
    cmd = [DF_BIN] + extra_args + [WAV_48K, "-o", out_dir]
    t0 = time.time()
    r = run(cmd)
    elapsed = time.time() - t0
    if r.returncode != 0:
        return None, f"exit={r.returncode} {r.stderr[:200]}"
    files = os.listdir(out_dir) if os.path.isdir(out_dir) else []
    if not files:
        return None, "输出目录为空"
    return elapsed, None


def bench_df1_python():
    """测试 DeepFilterNet1 via pip install deepfilternet (Python + PyTorch)。"""
    # 安装
    run([sys.executable, "-m", "pip", "install", "-q", "deepfilternet"], check=True)
    # 用 CLI 运行
    out_dir = tempfile.mkdtemp(prefix="bench_df1_")
    t0 = time.time()
    r = run([sys.executable, "-m", "df.enhance", "-m", "DeepFilterNet",
              "-o", out_dir, WAV_48K])
    elapsed = time.time() - t0
    if r.returncode != 0:
        return None, f"exit={r.returncode} {r.stderr[:200]}"
    files = os.listdir(out_dir) if os.path.isdir(out_dir) else []
    if not files:
        return None, "输出目录为空"
    return elapsed, None


def bench_gtcrn():
    """测试 GTCRN via sherpa-onnx。"""
    run([sys.executable, "-m", "pip", "install", "-q", "sherpa-onnx", "soundfile"], check=True)
    import numpy as np
    import sherpa_onnx
    import soundfile as sf

    ensure_gtcrn()
    data, sr = sf.read(WAV_16K, always_2d=True, dtype="float32")
    samples = np.ascontiguousarray(data[:, 0])

    config = sherpa_onnx.OfflineSpeechDenoiserConfig(
        model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
            gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(
                model=GTCRN_MODEL_PATH
            ),
            debug=False, num_threads=1, provider="cpu",
        )
    )
    if not config.validate():
        return None, "配置无效"
    denoiser = sherpa_onnx.OfflineSpeechDenoiser(config)

    t0 = time.time()
    denoiser(samples, sr)
    elapsed = time.time() - t0
    return elapsed, None


def main():
    if not os.path.exists(INPUT_AUDIO):
        print(f"ERROR: 文件不存在: {INPUT_AUDIO}")
        sys.exit(1)

    duration = prep_audio()
    ensure_df_binary()

    results = []  # [(name, elapsed, error)]

    # 1. DeepFilterNet1 (Python/PyTorch)
    print("\n[1/4] DeepFilterNet1 (Python)...")
    try:
        t, err = bench_df1_python()
        results.append(("DeepFilterNet1", t, err))
        print(f"  {'OK' if t else 'FAIL'}: {t:.3f}s" if t else f"  FAIL: {err}")
    except Exception as e:
        results.append(("DeepFilterNet1", None, str(e)))
        print(f"  FAIL: {e}")

    # 2. DeepFilterNet2 (Rust, 默认)
    print("\n[2/4] DeepFilterNet2 (Rust)...")
    try:
        t, err = bench_df_rust("df2", [])
        results.append(("DeepFilterNet2", t, err))
        print(f"  {'OK' if t else 'FAIL'}: {t:.3f}s" if t else f"  FAIL: {err}")
    except Exception as e:
        results.append(("DeepFilterNet2", None, str(e)))
        print(f"  FAIL: {e}")

    # 3. DeepFilterNet3 (Rust, -m tar.gz)
    print("\n[3/4] DeepFilterNet3 (Rust)...")
    try:
        ensure_df3_model()
        t, err = bench_df_rust("df3", ["-m", DF3_MODEL_PATH])
        results.append(("DeepFilterNet3", t, err))
        print(f"  {'OK' if t else 'FAIL'}: {t:.3f}s" if t else f"  FAIL: {err}")
    except Exception as e:
        results.append(("DeepFilterNet3", None, str(e)))
        print(f"  FAIL: {e}")

    # 4. GTCRN (sherpa-onnx)
    print("\n[4/4] GTCRN (sherpa-onnx)...")
    try:
        t, err = bench_gtcrn()
        results.append(("GTCRN", t, err))
        print(f"  {'OK' if t else 'FAIL'}: {t:.3f}s" if t else f"  FAIL: {err}")
    except Exception as e:
        results.append(("GTCRN", None, str(e)))
        print(f"  FAIL: {e}")

    # 汇总
    print(f"\n{'='*60}")
    print(f"降噪速度基准测试结果 (音频时长: {duration:.1f}s)")
    print(f"{'='*60}")
    print(f"{'模型':<20} {'耗时(s)':<12} {'RTF':<10} {'状态'}")
    print(f"{'-'*60}")
    for name, t, err in results:
        if t is not None:
            rtf = t / duration
            print(f"{name:<20} {t:<12.3f} {rtf:<10.4f} PASS")
        else:
            print(f"{name:<20} {'-':<12} {'-':<10} FAIL ({err[:40]})")

    # 排名
    ok = [(n, t) for n, t, e in results if t is not None]
    if ok:
        ok.sort(key=lambda x: x[1])
        print(f"\n速度排名 (快→慢):")
        for i, (n, t) in enumerate(ok, 1):
            rtf = t / duration
            print(f"  {i}. {n}: {t:.3f}s (RTF {rtf:.4f})")


if __name__ == "__main__":
    main()
