"""whisper_http.py — y7000p 语音转写 HTTP 服务 (2026-08-25).

POST /asr  body: JSON {"audio_b64": "<base64 of wav/mp3>", "language": "zh"}
       ->  JSON {"text": "...", "elapsed_s": 1.2}
GET  /health -> {"status": "ok", "model": "tiny", "device": "cpu"}

设计: 标准库 http.server 零依赖; CPU int8(稳定, GPU 差 cudnn 符号一债);
模型常驻内存(进程不退), 一次加载多请求复用。绑 0.0.0.0 供 tailscale 网调用。
"""
import base64
import json
import os
import sys
import sysconfig
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ctranslate2.dll 的 CUDA12 运行库注入(同 asr_test.py, 必须在 import 前注入)
_sp = sysconfig.get_paths()["purelib"]
for _sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
    _p = os.path.join(_sp, _sub)
    if os.path.isdir(_p):
        os.add_dll_directory(_p)
        os.environ["PATH"] = _p + os.pathsep + os.environ["PATH"]

from faster_whisper import WhisperModel

MODEL = None


def load_model() -> None:
    global MODEL
    device = os.environ.get("ASR_DEVICE", "cpu")
    t0 = time.time()
    MODEL = WhisperModel("tiny", device=device, compute_type="int8")
    print(f"[whisper-http] model ready device={device} {time.time() - t0:.1f}s", flush=True)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "model": "tiny"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/asr":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode())
            audio = base64.b64decode(payload.get("audio_b64", ""))
            if not audio:
                self._json(400, {"error": "audio_b64 empty"})
                return
            language = payload.get("language") or None
            suffix = ".wav" if audio[:4] == b"RIFF" else ".bin"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                f.write(audio)
                tmp = f.name
            try:
                t0 = time.time()
                segments, _ = MODEL.transcribe(tmp, language=language)
                text = " ".join(s.text.strip() for s in segments).strip()
                self._json(200, {"text": text, "elapsed_s": round(time.time() - t0, 1)})
            finally:
                os.unlink(tmp)
        except Exception as e:  # 如实回传, 不吞
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[whisper-http] {self.address_string()} {fmt % args}", flush=True)


def main() -> int:
    port = int(os.environ.get("ASR_PORT", "8390"))
    load_model()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104
    return 0


if __name__ == "__main__":
    sys.exit(main())
