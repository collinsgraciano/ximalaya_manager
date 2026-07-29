"""Colab 测试脚本: GTCRN 语音降噪 (sherpa-onnx)。

用法:
    1. 上传你的音频文件到 Colab (左侧文件栏或 google.colab.files.upload())
    2. 修改 INPUT_AUDIO 变量指向你的文件
    3. 运行 !python scripts/test_gtcrn.py
"""

import os
import sys
import time
import subprocess

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speech-enhancement-models/gtcrn_simple.onnx"
)
MODEL_PATH = "/content/gtcrn_simple.onnx"

# ═══ 修改这里: 你的音频文件路径 ═══
INPUT_AUDIO = "/content/0001_大奉打更人 第1集 牢狱之灾.m4a"
OUTPUT_AUDIO = "/content/gtcrn_enhanced.wav"


def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                     "sherpa-onnx", "soundfile"], check=True)
    print("依赖安装完成: sherpa-onnx, soundfile")


def download_model():
    if os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 0:
        print(f"模型已存在: {MODEL_PATH}")
        return
    print("下载 GTCRN 模型...")
    subprocess.run(["wget", "-q", MODEL_URL, "-O", MODEL_PATH], check=True)
    print(f"模型下载完成: {MODEL_PATH} ({os.path.getsize(MODEL_PATH) // 1024} KB)")


def main():
    install_deps()
    import numpy as np
    import sherpa_onnx
    import soundfile as sf

    download_model()

    if not os.path.exists(INPUT_AUDIO):
        print(f"ERROR: 音频文件不存在: {INPUT_AUDIO}")
        print("请上传音频文件后修改 INPUT_AUDIO 变量")
        sys.exit(1)

    # m4a/mp3 等格式先转 wav (soundfile 只支持 wav/flac/ogg)
    audio_path = INPUT_AUDIO
    if not audio_path.lower().endswith(".wav"):
        audio_path = "/tmp/gtcrn_input.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", INPUT_AUDIO, "-ar", "16000", "-ac", "1",
             "-sample_fmt", "s16", "-acodec", "pcm_s16le", audio_path],
            capture_output=True, check=True,
        )
        print(f"已转换为 16kHz WAV: {audio_path}")

    # 加载音频
    data, sr = sf.read(audio_path, always_2d=True, dtype="float32")
    samples = np.ascontiguousarray(data[:, 0])  # 取第一声道
    duration = len(samples) / sr
    print(f"输入: {INPUT_AUDIO}")
    print(f"  采样率: {sr}Hz, 时长: {duration:.1f}s, 样本数: {len(samples)}")

    # 创建降噪器
    config = sherpa_onnx.OfflineSpeechDenoiserConfig(
        model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
            gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(
                model=MODEL_PATH
            ),
            debug=False,
            num_threads=1,
            provider="cpu",
        )
    )
    if not config.validate():
        print("ERROR: 配置无效")
        sys.exit(1)

    denoiser = sherpa_onnx.OfflineSpeechDenoiser(config)

    # 降噪
    print("降噪中...")
    t0 = time.time()
    result = denoiser(samples, sr)
    elapsed = time.time() - t0
    rtf = elapsed / duration

    # 保存
    sf.write(OUTPUT_AUDIO, result.samples, result.sample_rate)
    print(f"\n输出: {OUTPUT_AUDIO}")
    print(f"  采样率: {result.sample_rate}Hz, 样本数: {len(result.samples)}")
    print(f"  耗时: {elapsed:.3f}s / 音频 {duration:.1f}s = RTF {rtf:.4f}")
    if rtf < 1.0:
        print(f"  => 比 DeepFilterNet2 (RTF≈0.08) {'快' if rtf < 0.08 else '慢'}")
    else:
        print("  => 慢于实时，不适合实时使用")


if __name__ == "__main__":
    main()
