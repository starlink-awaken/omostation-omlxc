#!/usr/bin/env python3
"""LM Studio 本地/集群模型自动化基准测评套件 (Benchmark & Evaluation Suite).

四大基准评测维度:
  1. 吞吐与首字延迟 (TTFT & TPS Baseline)
  2. 代码生成与类型系统自洽度 (Coding & Algorithms)
  3. 复杂多步逻辑推演与思维链深度 (Reasoning & Trap Logic)
  4. 结构化 Schema 遵循度 (Strict JSON Compliance)

用法:
  python3 bin/benchmark-lmstudio.py --list
  python3 bin/benchmark-lmstudio.py --model <model_id>
  python3 bin/benchmark-lmstudio.py --auto-load <model_id>
  python3 bin/benchmark-lmstudio.py --endpoint http://100.99.210.78:1234/v1 --list
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Any

TEST_CASES = [
    {
        "name": "1. 吞吐与生成速率 (Speed Baseline)",
        "prompt": "Output the integers from 1 through 60 separated only by commas, nothing else.",
        "max_tokens": 200,
        "temperature": 0.0,
    },
    {
        "name": "2. 代码生成与类型设计 (Coding & Algorithms)",
        "prompt": "Write a Python class for a Thread-Safe In-Memory Cache with TTL expiration and LRU eviction. Include type hints and clean docstrings.",
        "max_tokens": 600,
        "temperature": 0.2,
    },
    {
        "name": "3. 复杂推理与陷阱判断 (Reasoning & Logic)",
        "prompt": "A farmer has 17 sheep. All but 9 die. How many sheep does the farmer have left? Explain your thought process step by step before giving the final answer.",
        "max_tokens": 400,
        "temperature": 0.1,
    },
    {
        "name": "4. 结构化 JSON 遵循 (Strict JSON Compliance)",
        "prompt": 'Extract entity from this text into strict JSON: "Alice Johnson, age 29, CTO at TechCorp based in Berlin." Format: {"name": str, "age": int, "role": str, "company": str, "city": str}. Output ONLY valid raw JSON, no markdown codeblocks.',
        "max_tokens": 150,
        "temperature": 0.0,
    },
]


def check_lms_status() -> dict[str, Any]:
    """查询本地 lms 状态与已加载模型。"""
    try:
        res = subprocess.run(["lms", "ps"], capture_output=True, text=True, timeout=5)
        lines = [line.strip() for line in res.stdout.strip().split("\n") if line.strip()]
        loaded = []
        if len(lines) > 1:
            for l in lines[1:]:
                parts = l.split()
                if parts:
                    loaded.append(parts[0])
        return {"available": True, "loaded": loaded, "raw": res.stdout}
    except Exception as e:
        return {"available": False, "loaded": [], "error": str(e)}


def list_models(base_url: str) -> list[str]:
    """通过 OpenAI 兼容接口拉取模型列表。"""
    url = f"{base_url}/models"
    try:
        req = urllib.request.urlopen(url, timeout=5)
        data = json.loads(req.read().decode("utf-8"))
        return [m.get("id") for m in data.get("data", [])]
    except Exception as e:
        print(f"无法连接到 {url}: {e}")
        return []


def run_benchmark(
    base_url: str,
    model: str,
    *,
    timeout: int = 120,
) -> dict[str, Any]:
    """对特定模型运行标准四维评测。"""
    url = f"{base_url}/chat/completions"
    print(f"\n{'=' * 65}")
    print(f" 开始对 LM Studio 模型 [{model}] 执行四维基准测评")
    print(f" 端点: {url}")
    print(f"{'=' * 65}\n")

    results = []

    for t in TEST_CASES:
        print(f"▶ 正在测试: {t['name']} ...")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": t["prompt"]}],
            "temperature": t["temperature"],
            "max_tokens": t["max_tokens"],
            "stream": True,
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        t0 = time.time()
        ttft = None
        reasoning_tokens = 0
        content_tokens = 0
        reasoning_text = ""
        content_text = ""

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                for line in resp:
                    line_str = line.decode("utf-8").strip()
                    if line_str.startswith("data: "):
                        chunk_data = line_str[6:]
                        if chunk_data == "[DONE]":
                            break
                        try:
                            parsed = json.loads(chunk_data)
                            delta = parsed["choices"][0]["delta"]
                            r_part = delta.get("reasoning_content", "")
                            c_part = delta.get("content", "")

                            if r_part:
                                if ttft is None:
                                    ttft = time.time() - t0
                                reasoning_tokens += 1
                                reasoning_text += r_part
                            if c_part:
                                if ttft is None:
                                    ttft = time.time() - t0
                                content_tokens += 1
                                content_text += c_part
                        except Exception:
                            pass
        except Exception as e:
            print(f"   ✗ 请求失败: {e}")
            continue

        total_time = max(0.001, time.time() - t0)
        total_tokens = reasoning_tokens + content_tokens
        tps = total_tokens / total_time

        entry = {
            "name": t["name"],
            "ttft_ms": (ttft * 1000) if ttft else 0,
            "reasoning_tokens": reasoning_tokens,
            "content_tokens": content_tokens,
            "total_tokens": total_tokens,
            "total_time_s": total_time,
            "tps": tps,
            "reasoning_preview": reasoning_text[:120].replace("\n", " ")
            if reasoning_text
            else "(无思考链输出)",
            "content_preview": content_text[:180].replace("\n", " "),
        }
        results.append(entry)

        print(
            f"   ✓ 耗时: {total_time:.2f}s | TTFT: {entry['ttft_ms']:.1f}ms | 生成: {total_tokens} tokens | 速率: {tps:.2f} tok/s"
        )
        if reasoning_tokens > 0:
            print(f"     思考链 ({reasoning_tokens} tok): {entry['reasoning_preview']}...")
        print(f"     输出预览: {entry['content_preview']}...\n")

    if not results:
        return {"model": model, "success": False, "results": []}

    avg_tps = sum(r["tps"] for r in results) / len(results)
    avg_ttft = sum(r["ttft_ms"] for r in results) / len(results)

    summary = {
        "model": model,
        "success": True,
        "avg_ttft_ms": avg_ttft,
        "avg_tps": avg_tps,
        "has_reasoning": any(r["reasoning_tokens"] > 0 for r in results),
        "results": results,
    }

    print(f"{'=' * 65}")
    print(f" 测评总结与量化指标 ({model})")
    print(f"{'=' * 65}")
    print(f"平均首字延迟 (Avg TTFT): {avg_ttft:.2f} ms")
    print(f"平均吞吐速率 (Avg TPS):  {avg_tps:.2f} tok/s")
    print(f"思维链支持度: {'全面支持 (含 reasoning_content 独立流)' if summary['has_reasoning'] else '无显式 CoT'}")

    return summary


def main():
    parser = argparse.ArgumentParser(description="LM Studio 本地模型基准测评套件")
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:1234/v1",
        help="LM Studio OpenAI 兼容端点 (默认: http://127.0.0.1:1234/v1)",
    )
    parser.add_argument("--list", action="store_true", help="列出端点发现的模型")
    parser.add_argument("--model", help="指定要评测的模型 ID")
    parser.add_argument("--auto-load", help="通过 lms load 自动载入模型并评测，测试后卸载")
    parser.add_argument("--json", help="导出 JSON 评测报告路径")

    args = parser.parse_args()

    if args.list:
        models = list_models(args.endpoint)
        print(f"\n端点 [{args.endpoint}] 可见模型 ({len(models)} 个):")
        status = check_lms_status()
        loaded = set(status.get("loaded", []))
        for m in models:
            tag = "● 已加载" if m in loaded else "○ 未加载"
            print(f"  {tag:<10} {m}")
        return

    target_model = args.model
    need_unload = False

    if args.auto_load:
        target_model = args.auto_load
        print(f"▶ 正在通过 lms load 载入模型: {target_model} ...")
        res = subprocess.run(["lms", "load", target_model], capture_output=True, text=True)
        if res.returncode != 0:
            print(f"加载失败: {res.stderr}")
            sys.exit(1)
        need_unload = True
        time.sleep(2)

    if not target_model:
        # 默认选取当前已加载的模型
        status = check_lms_status()
        if status.get("loaded"):
            target_model = status["loaded"][0]
            print(f"未指定模型，自动选取当前已加载模型: {target_model}")
        else:
            models = list_models(args.endpoint)
            if models:
                target_model = models[0]
                print(f"自动选取可见模型: {target_model}")
            else:
                print("未发现可用模型，请通过 --model 指定或使用 --list 查看")
                sys.exit(1)

    try:
        report = run_benchmark(args.endpoint, target_model)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            print(f"\n✅ 评测报告已保存至: {args.json}")
    finally:
        if need_unload:
            print(f"\n▶ 正在卸载模型释放显存: {target_model} ...")
            subprocess.run(["lms", "unload", target_model], capture_output=True)


if __name__ == "__main__":
    main()
