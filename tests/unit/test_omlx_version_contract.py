"""oMLX App 版本契约测试 (2026-08-26 第六层终修).

铁证: oMLX App 0.6.2 被 [0.5.0, 0.6.0) 开区间上界拒绝 → compatible=False
→ 全 placement available=False → 409。上界应放行同次版本 (0,7,0)。
"""

from omlxc.adapters.omlx_app import DEFAULT_MAXIMUM_VERSION, DEFAULT_MINIMUM_VERSION


def test_omlx_062_in_contract() -> None:
    actual = (0, 6, 2)
    assert DEFAULT_MINIMUM_VERSION <= actual < DEFAULT_MAXIMUM_VERSION, (
        f"oMLX 0.6.2 被契约 [{DEFAULT_MINIMUM_VERSION}, {DEFAULT_MAXIMUM_VERSION}) 拒绝"
    )
