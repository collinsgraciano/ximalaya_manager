"""Colab 测试脚本: 验证 DeepFilter 模型选择功能。"""

from __future__ import annotations

import os
import sys
import time
import tempfile
import subprocess

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


def generate_test_wav(path: str, duration: float = 5.0, sr: int = 48000):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"anoisesrc=color=white:amplitude=0.3:duration={duration}:sample_rate={sr}",
         "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={duration}:sample_rate={sr}",
         "-filter_complex", "[0:a][1:a]amix=inputs=2:weights=0.5 0.5",
         "-ar", str(sr), "-ac", "1", "-sample_fmt", "s16",
         "-acodec", "pcm_s16le", path],
        capture_output=True, check=True,
    )
    print(f"测试音频: {path} ({duration}s, {sr}Hz)")


def run_test(name: str, extra_args: list[str], test_wav: str) -> bool:
    out_dir = tempfile.mkdtemp(prefix=f"df_{name}_")
    cmd = [DEEPFILTER_PATH] + extra_args + [test_wav, "-o", out_dir]
    print(f"\n{'='*50}")
    print(f"[{name}]")
    print(f"  CMD: {' '.join(cmd)}")
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    # 打印所有输出用于调试
    if r.stdout.strip():
        print(f"  stdout: {r.stdout.strip()[:500]}")
    if r.stderr.strip():
        print(f"  stderr: {r.stderr.strip()[:500]}")

    if r.returncode != 0:
        print(f"  FAIL (exit={r.returncode}, {elapsed:.1f}s)")
        return False

    # 列出输出目录所有文件
    out_files = []
    if os.path.isdir(out_dir):
        out_files = os.listdir(out_dir)
    print(f"  输出目录 {out_dir}: {out_files}")

    if out_files:
        print(f"  PASS ({elapsed:.1f}s)")
        return True
    print(f"  FAIL: 输出目录为空 ({elapsed:.1f}s)")
    return False


def main():
    ensure_binary()

    # 先看 --help 确认参数
    print("\n--- deep-filter --help ---")
    r = subprocess.run([DEEPFILTER_PATH, "--help"], capture_output=True, text=True)
    print(r.stdout or r.stderr)

    test_wav = tempfile.mktemp(suffix=".wav", prefix="df_input_")
    generate_test_wav(test_wav)

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
