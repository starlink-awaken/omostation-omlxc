"""OpenAIChatBody OpenAI 兼容字段集测试 (2026-08-25 UDS 422 修复).

背景: /openai/v1/chat/completions 是 OpenAI SDK 的直连端点(UDS transport),
但 ApiModel(extra=forbid) 下 schema 缺 stop 等 OpenAI 标准字段 → SDK 完整
请求体 422 E100(实测: temperature 通过、stop 炸)。兼容端点必须吃下
OpenAI 标准字段集(未实现的显式接受并忽略)。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omlxc.api.app import OpenAIChatBody


def _minimal() -> dict:
    return {"model": "coding", "messages": [{"role": "user", "content": "hi"}]}


def test_stop_accepted() -> None:
    body = OpenAIChatBody(**_minimal(), stop=["\n\n"])
    assert body.stop == ("\n\n",)


def test_stop_none_default() -> None:
    assert OpenAIChatBody(**_minimal()).stop is None


def test_top_p_and_penalties_accepted() -> None:
    body = OpenAIChatBody(**_minimal(), top_p=0.9, presence_penalty=0.5, frequency_penalty=0.2)
    assert body.top_p == 0.9


def test_user_and_seed_accepted() -> None:
    body = OpenAIChatBody(**_minimal(), user="mail-daemon", seed=42)
    assert body.seed == 42


def test_unknown_field_still_rejected() -> None:
    # 严格性保留: 非 OpenAI 标准的未知字段仍拒绝(防注入)
    with pytest.raises(ValidationError):
        OpenAIChatBody(**_minimal(), totally_unknown_field="x")


def test_stop_tuple_or_str() -> None:
    assert OpenAIChatBody(**_minimal(), stop="END").stop == ("END",)


def testmlx_passthrough_fields_accepted() -> None:
    # gateway 从 SSOT request_defaults 转发的 MLX 原生参数(2026-08-26 消费者链422)
    body = OpenAIChatBody(
        **_minimal(),
        enable_thinking=False,
        kv_bits=8,
        chat_template_kwargs={"enable_thinking": False},
        thinking_budget=0,
    )
    assert body.kv_bits == 8
    assert body.thinking_budget == 0


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high"])
def test_reasoning_effort_accepted(effort: str) -> None:
    # 2026-09-27: 门面 no_think_param 与 cockpit 分诊带 reasoning_effort="none" → 422 → 门面兜底且熔断 oMLX
    assert OpenAIChatBody(**_minimal(), reasoning_effort=effort).reasoning_effort == effort


def test_reasoning_effort_rejects_non_standard_value() -> None:
    with pytest.raises(ValidationError):
        OpenAIChatBody(**_minimal(), reasoning_effort="turbo")
