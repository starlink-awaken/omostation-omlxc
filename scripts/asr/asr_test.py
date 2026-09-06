"""asr_test.py — y7000p 语音入口本地验证 (2026-08-25).

管线: SAPI 合成语音 wav → faster-whisper tiny(int8, GPU) 转写 → 比对原文。
通过 = 语音入口(会议/语音邮件)算力层就绪。失败信息原样打印, 不吞异常。
"""

import os
import sys
import sysconfig
import time
from pathlib import Path

# ctranslate2.dll 依赖 CUDA12 运行库(pip 的 nvidia-*-cu12 包自带),
# 必须显式注入 DLL 搜索路径, 否则 FileNotFoundError: ctranslate2.dll
_sp = sysconfig.get_paths()["purelib"]
for _sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
    _p = os.path.join(_sp, _sub)
    if os.path.isdir(_p):
        os.add_dll_directory(_p)
        os.environ["PATH"] = _p + os.pathsep + os.environ["PATH"]

from faster_whisper import WhisperModel

WAV = Path(r"C:\Users\xia\asr_input.wav")
EXPECTED = "hello world this is a whisper test on the y seven thousand p"


def main() -> int:
    if not WAV.exists():
        print(f"NO_INPUT: {WAV}")
        return 1
    device = os.environ.get("ASR_DEVICE", "cuda")
    print(f"[1/3] 加载 faster-whisper tiny (int8, {device}) ...")
    t0 = time.time()
    model = WhisperModel("tiny", device=device, compute_type="int8")
    print(f"    模型就绪 {time.time() - t0:.1f}s")
    print("[2/3] GPU 转写 ...")
    t0 = time.time()
    segments, info = model.transcribe(str(WAV), language="en")
    text = " ".join(s.text.strip() for s in segments).strip()
    elapsed = time.time() - t0
    print(f"    转写耗时 {elapsed:.1f}s | 检测语言={info.language}")
    print(f"[3/3] RESULT: {text!r}")
    overlap = len(set(text.lower().split()) & set(EXPECTED.split()))
    total = len(set(EXPECTED.split()))
    print(f"词汇命中: {overlap}/{total} -> {'PASS' if overlap >= total * 0.7 else 'CHECK'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
