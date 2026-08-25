"""RemoteResidentConfig.role 字段单测 (2026-08-25 #6 embedding 迁移定案).

背景: bge-m3 等 embedding 模型常驻 mac-mini ollama 时, maintain 脚本必须打
/api/embed 而非 /api/generate — embedding 模型不支持 generate(2026-08-22
warm-keep 已实测踩坑: embedding 角色打 chat 端点 400 "not an LLM/chat model")。
role 是 remote_resident 条目的真实属性, 进 daemon schema 走正规军。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from omlxc.config.schema import RemoteResidentConfig
from omlxc.domain.models import BackendKind


def test_role_defaults_to_chat() -> None:
    entry = RemoteResidentConfig(
        node_id="mac-mini-m4-24g",
        kind=BackendKind.OLLAMA,
        backend_model_id="qwen3.5:9b",
        port=11434,
    )
    assert entry.role == "chat"


def test_role_embedding_accepted() -> None:
    entry = RemoteResidentConfig(
        node_id="mac-mini-m4-24g",
        kind=BackendKind.OLLAMA,
        backend_model_id="bge-m3:latest",
        port=11434,
        role="embedding",
    )
    assert entry.role == "embedding"


def test_role_invalid_value_rejected() -> None:
    with pytest.raises(ValidationError):
        RemoteResidentConfig(
            node_id="mac-mini-m4-24g",
            kind=BackendKind.OLLAMA,
            backend_model_id="x",
            port=11434,
            role="tts",  # pyright: ignore[reportArgumentType] — 故意传非法值测拒绝
        )
