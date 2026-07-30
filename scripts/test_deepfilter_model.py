"""Colab 测试脚本: 验证 DeepFilter 模型选择功能。

用法:
    !python scripts/test_deepfilter_model.py /content/你的音频.m4a
    或不传参数则列出 /content 下的音频文件供选择
"""

from __future__ import annotations

import os
import sys
import time
import tempfile
import subprocess
import glob

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DEEPFILTER_DIR = os.environ.get("DEEPFILTER_DIR", "/content/.deepfilter")
DEEPFILTER_BIN = "deep-filter-0.5.6-x86_64-unknown-linux-musl"
DEEPFILTER_PATH = os.path.join(DEEPFILTER_DIR, DEEPFILTER_BIN)
DEEPFILTER_URL = (
    "https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/"
    "deep-filter-0.5.6-x86_64-unknown-linux-musl"
)
DF3_MODEL_URL = (
    "https://raw.githubusercontent.com/Rikorose/DeepFilterNet/main/"
    "models/DeepFilterNet3_onnx.tar.gz"
)
DF3_MODEL_PATH = os.path.join(DEEPFILTER_DIR, "DeepFilterNet3_onnx.tar.gz")


def ensure_binary():
    if os.path.exists(DEEPFILTER_PATH) and os.path.getsize(DEEPFILTER_PATH) > 0:
        if not os.access(DEEPFILTER_PATH, os.X_OK):
            os.chmod(DEEPFILTER_PATH, 0o755)
        return True
    os.makedirs(DEEPFILTER_DIR, exist_ok=True)
    print("下载 deep-filter 二进制...")
    subprocess.run(
        ["wget", "--tries=5", "--timeout=30", DEEPFILTER_URL, "-O", DEEPFILTER_PATH],
        check=True,
    )
    os.chmod(DEEPFILTER_PATH, 0o755)
    print("下载完成")
    return True


def ensure_df3_model():
    if os.path.exists(DF3_MODEL_PATH) and os.path.getsize(DF3_MODEL_PATH) > 0:
        return DF3_MODEL_PATH
    print("下载 DeepFilterNet3 模型...")
    subprocess.run(
        ["wget", "--tries=5", "--timeout=30", DF3_MODEL_URL, "-O", DF3_MODEL_PATH],
        check=True,
    )
    print("下载完成")
    return DF3_MODEL_PATH


def select_audio():
    """选择音频文件: 命令行参数 > 列出 /content 下的音频文件。"""
    if len(sys.argv) >= 2:
        path = sys.argv[1]
        if os.path.exists(path):
            return path
        print(f"ERROR: 文件不存在: {path}")
        sys.exit(1)

    # 搜索 /content 下的音频文件
    exts = ["*.m4a", "*.mp3", "*.wav", "*.aac", "*.flac", "*.ogg"]
    found = []
    for ext in exts:
        found.extend(glob.glob(f"/content/{ext}"))
        found.extend(glob.glob(f"/content/**/{ext}", recursive=True))
    found = sorted(set(found))

    if not found:
        print("未找到音频文件。请上传音频到 /content 或指定路径:")
        print("  !python scripts/test_deepfilter_model.py /path/to/audio.m4a")
        sys.exit(1)

    print("找到以下音频文件:")
    for i, f in enumerate(found, 1):
        size = os.path.getsize(f) // 1024
        print(f"  {i}. {f} ({size}KB)")

    while True:
        choice = input("\n选择文件序号 (或输入路径): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            return found[int(choice) - 1]
        if os.path.exists(choice):
            return choice
        print("无效选择，请重试")


def to_48k_wav(input_path: str) -> str:
    """转为 48kHz mono WAV (DeepFilter 要求)。"""
    if input_path.lower().endswith(".wav"):
        # 检查采样率
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of",
             "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, text=True,
        )
        if r.stdout.strip() == "48000":
            return input_path

    out = "/tmp/df_test_input_48k.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-ar", "48000", "-ac", "1",
         "-sample_fmt", "s16", "-acodec", "pcm_s16le", out],
        capture_output=True, check=True,
    )
    print(f"已转为 48kHz WAV: {out}")
    return out


def run_test(name: str, extra_args: list[str], test_wav: str) -> bool:
    out_dir = tempfile.mkdtemp(prefix=f"df_{name}_")
    cmd = [DEEPFILTER_PATH] + extra_args + [test_wav, "-o", out_dir]
    print(f"\n{'='*50}")
    print(f"[{name}]")
    print(f"  CMD: {' '.join(cmd)}")
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    if r.stdout.strip():
        print(f"  stdout: {r.stdout.strip()[:500]}")
    if r.stderr.strip():
        print(f"  stderr: {r.stderr.strip()[:500]}")

    if r.returncode != 0:
        print(f"  FAIL (exit={r.returncode}, {elapsed:.1f}s)")
        return False

    out_files = os.listdir(out_dir) if os.path.isdir(out_dir) else []
    print(f"  输出目录: {out_files}")

    if out_files:
        print(f"  PASS ({elapsed:.1f}s)")
        return True
    print(f"  FAIL: 输出目录为空 ({elapsed:.1f}s)")
    return False


def main():
    audio_path = select_audio()
    print(f"\n使用音频: {audio_path}")
    print(f"  大小: {os.path.getsize(audio_path) // 1024}KB")

    test_wav = to_48k_wav(audio_path)
    ensure_binary()

    results = {}

    # 1. DeepFilterNet2 (默认, 不传 -m)
    results["DeepFilterNet2"] = run_test(
        "DeepFilterNet2 (默认)", [], test_wav,
    )

    # 2. DeepFilterNet3 (传 tar.gz 路径)
    try:
        df3_path = ensure_df3_model()
        results["DeepFilterNet3"] = run_test(
            "DeepFilterNet3 (-m tar.gz)", ["-m", df3_path], test_wav,
        )
    except Exception as e:
        print(f"\nDeepFilterNet3 模型下载失败: {e}")
        results["DeepFilterNet3"] = False

    print(f"\n{'='*50}")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")

    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
