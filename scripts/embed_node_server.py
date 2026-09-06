#!/usr/bin/env python3
"""embed_node_server.py — Mac mini mesh 嵌入节点服务端点 (BET-Y1Q4-T3-02 P3).

bos://compute/omlxc/embed 节点的最小 HTTP 面 (纯标准库, 零额外依赖):

    POST /embed  {"texts": ["..."]} → {"vectors": [[...]], "elapsed_ms": n}
    GET  /health → {"ok": true, "model": "...", "device": "..."}

部署 (Mac mini):
    ~/omlx-embed/.venv/bin/python embed_node_server.py --port 18700
MBP 侧调用:
    curl http://100.99.210.78:18700/embed -d '{"texts": ["测试"]}'
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 100% 本地红线

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_engine = None  # lazy singleton


def get_engine():
    global _engine
    if _engine is None:
        from sentence_transformers import SentenceTransformer

        device = "mps" if _mps_available() else "cpu"
        _engine = SentenceTransformer(MODEL_NAME, device=device)
        _engine.encode(["warmup 预热"])  # MPS graph compile at boot
    return _engine


def _mps_available() -> bool:
    try:
        import torch

        return torch.backends.mps.is_available()
    except ImportError:
        return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *log_args):  # quiet access log
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            eng = get_engine()
            self._json(200, {"ok": True, "model": MODEL_NAME, "device": eng.device.type})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/embed":
            self._json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            texts = [str(t) for t in req["texts"]][:256]
        except (json.JSONDecodeError, KeyError, ValueError):
            self._json(400, {"error": "bad_request"})
            return
        t0 = time.monotonic()
        vectors = get_engine().encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        self._json(
            200,
            {
                "schema": "omlxc.embed-node.v1",
                "vectors": vectors.astype("float32").tolist(),
                "dim": int(vectors.shape[1]),
                "count": len(texts),
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # 默认 0.0.0.0 是算力池设计要求: 该节点服务需被其他节点经 Tailscale 访问
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--port", type=int, default=18700)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"embed-node listening on {args.host}:{args.port} (model={MODEL_NAME})")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
