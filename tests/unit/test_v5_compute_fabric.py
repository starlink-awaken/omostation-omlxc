"""
Unit tests for omlxc V5.0 Sovereign Compute Fabric (ADR-0435).

Covers:
1. Thunderbolt 5 P2P Zero-Copy DMA & Fallback (ThunderboltDMABus)
2. Symbiotic Draft Head Online Distillation (SymbioticDraftDistiller)
3. Vision-Language Patch Feature Level Streaming (ViTPatchStreamer & Receiver)
4. Continuous Edge LoRA Hot-Swapping & Distillation (LoRAAdapterManager & Distiller)
"""

from __future__ import annotations

import pytest

from omlxc.dataplane.thunderbolt_dma import (
    ThunderboltDMABus,
    ThunderboltTransportMode,
)
from omlxc.dataplane.symbiotic_distiller import (
    SymbioticDraftDistiller,
)
from omlxc.dataplane.vit_patch_streamer import (
    CrossAttentionStreamingReceiver,
    ViTPatchStreamer,
)
from omlxc.dataplane.lora_adapter_manager import (
    LoRAAdapterManager,
    LoRAAdapterMetadata,
    SignatureDiffDistiller,
)


class TestThunderboltDMABus:
    def test_link_probe_and_dma_transfer(self) -> None:
        bus = ThunderboltDMABus(
            prefer_mode=ThunderboltTransportMode.THUNDERBOLT_5_DMA,
            link_speed_gbps=120.0,
        )
        status = bus.probe_link()
        assert status.is_connected is True
        assert status.link_speed_gbps == 120.0
        assert status.numa_pool_size_gb == 152.0

        receipt = bus.transfer_kv_block(
            block_id="block-kv-001",
            size_mb=2.0,  # Standard 2MB KV Page (2048 INT4 tokens)
            source_node="MBP-M5Max",
            target_node="MacMini-M4",
        )
        assert receipt.transport_mode == ThunderboltTransportMode.THUNDERBOLT_5_DMA
        assert receipt.transfer_latency_ms < 0.25  # Under 250 microseconds (0.15ms typical)
        assert receipt.checksum_verified is True

    def test_fallback_to_10gbe_when_disconnected(self) -> None:
        bus = ThunderboltDMABus()
        bus.is_connected = False
        status = bus.probe_link()
        assert status.active_transport == ThunderboltTransportMode.HIGH_SPEED_10GBE_FALLBACK

        receipt = bus.transfer_kv_block(
            block_id="block-kv-fallback",
            size_mb=32.0,
        )
        assert receipt.transport_mode == ThunderboltTransportMode.HIGH_SPEED_10GBE_FALLBACK
        assert receipt.transfer_latency_ms > 1.0


class TestSymbioticDraftDistiller:
    def test_kl_divergence_computation(self) -> None:
        distiller = SymbioticDraftDistiller()
        p = [0.8, 0.1, 0.05, 0.05]
        q = [0.75, 0.15, 0.05, 0.05]
        kl = distiller.compute_kl_divergence(p, q)
        assert kl >= 0.0
        assert kl < 0.1  # Very close distributions

    def test_online_adaptation_and_metrics(self) -> None:
        distiller = SymbioticDraftDistiller(baseline_acceptance_rate=0.75)
        for i in range(10):
            res = distiller.record_step_and_adapt(
                target_token_id=1024,
                draft_token_ids=[1024, 1024, 2048],  # 2 accepted
            )
            assert res.accepted_tokens == 2

        metrics = distiller.get_metrics()
        assert metrics.total_steps_trained == 10
        assert metrics.current_acceptance_rate > 0.75
        assert metrics.estimated_speedup_ratio > 3.0


class TestViTPatchStreamer:
    def test_streaming_patches_and_receiver(self) -> None:
        streamer = ViTPatchStreamer(
            patch_size=16,
            embedding_dim=768,
            chunk_size_patches=64,
        )
        # 512x512 image -> 32x32 = 1024 patches -> 16 chunks of 64
        stream = streamer.stream_image_features(
            image_id="arch-diagram-01",
            image_resolution=(512, 512),
        )

        receiver = CrossAttentionStreamingReceiver()
        receipt = receiver.consume_stream(
            image_id="arch-diagram-01",
            stream=stream,
            simulated_transfer_delay_per_chunk_ms=4.0,
        )

        assert receipt.total_patches == 1024
        assert receipt.total_chunks == 16
        assert receipt.ttft_reduction_ratio > 0.50
        assert receipt.cross_attention_ready is True


class TestLoRAAdapterManager:
    def test_default_registration_and_hot_mount(self) -> None:
        mgr = LoRAAdapterManager()
        assert "lora-dfsq-architecture" in mgr.adapters
        assert "lora-gac-governance" in mgr.adapters

        receipt = mgr.mount_adapter("lora-dfsq-architecture")
        assert receipt.is_active is True
        assert receipt.mount_latency_ms < 1.0
        assert mgr.adapters["lora-dfsq-architecture"].is_active is True

    def test_auto_route_adapter(self) -> None:
        mgr = LoRAAdapterManager()
        receipt_gov = mgr.auto_route_adapter("Please audit our GaC governance rules")
        assert receipt_gov is not None
        assert receipt_gov.adapter_id == "lora-gac-governance"

        receipt_arch = mgr.auto_route_adapter("Refactor this DFSQ slot pattern")
        assert receipt_arch is not None
        assert receipt_arch.adapter_id == "lora-dfsq-architecture"

    def test_signature_diff_distillation(self) -> None:
        distiller = SignatureDiffDistiller()
        pair = distiller.record_signature_diff(
            context_instruction="Fix import ordering in mesh.py",
            original_draft="import os\nfrom rich import console",
            user_signed_diff="+from __future__ import annotations\n import os",
            domain="signature-style",
        )
        assert pair.sample_id.startswith("diff-")

        job = distiller.trigger_idle_distillation("signature-style", epochs=1)
        assert job.status == "COMPLETED"
        assert job.samples_processed >= 1
