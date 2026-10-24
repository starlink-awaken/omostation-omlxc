"""上下文的唯一真源：**路由承诺**与**加载实参**必须用同一个数。

§5.70 实测（measured_from 2026-10-24 08:12 · MBP · omlxc 3.4.0）：

1. 声明了 ``placement.context_limit`` 的落点，加载时**真的**把 ``-c <n>``
   传给了 ``lms load``（实测 ``lms ps`` 显示 ``contextLength: 32768``）。
2. 但 33 条落点里**只有 8 条声明过**（全是 oMLX App 后端）；
   **21 条 LM Studio / Ollama 落点一条都没声明**
   ⇒ 「收紧」机制在 LM Studio 后端上**从未生效过**。
3. 两侧算的是**两个不同的数**：

   - 路由侧（catalog）：``model.context_limit`` 或 ``placement.context_limit``
     或 **32768 硬编码兜底**；
   - 加载侧：``min(该后端各落点的声明, default=None)``
     ⇒ **没人声明就不带 ``-c``** ⇒ LM Studio 回落到全局
     ``defaultContextLength``（实测 **262144**）。

   ⇒ **承诺 32768、实到 262144** —— 这正是 ``composition.py`` 里那段注释
   想防的事（「28GB+ 权重直接打穿统一内存」，2026-08-22 实测）。

所以这里给出**唯一**的算法：声明只能收紧，实物是硬上限，
谁都没说就回落到既有口径的保守天花板。
"""

from __future__ import annotations

#: 没人声明、也没有实物真值时的保守回落。
#:
#: ⚠ 这个数**不是新造的**：它是 catalog 侧既有的硬编码兜底
#: （``daemon/composition.py`` 里 ``else 32768``），沿用它 ⇒
#: 「没声明」这件事在两侧的含义完全一致 ⇒ 零行为变化。
DEFAULT_CONTEXT_LIMIT = 32768


def effective_context_limit(
    declared: int | None,
    native: int | None,
    *,
    ceiling: int = DEFAULT_CONTEXT_LIMIT,
) -> int:
    """这个落点**实际能服务**多少 token。

    :param declared: 配置里 ``placement.context_limit`` —— **人为的刻意收紧**
    :param native: 实物真值（LM Studio 原生面的 ``max_context_length``）—— **物理上限**
    :param ceiling: 谁都没说时的保守天花板

    规则：

    1. **声明了** ⇒ ``min(声明, 实物)``。
       声明只能**收紧**；声明超过物理上限时按实物算 ⇒ **fail-closed**。
    2. **没声明** ⇒ ``min(实物, 天花板)``。
       实物是事实；天花板是「没人说清楚时别放任失控」。
       ⚠ 取 ``min`` 而不是实物本身 ⇒ **不会因为实物大就自动放大承诺**
       ⇒ 路由行为与今天一致。
    3. **谁都不知道** ⇒ ``天花板``。
    """
    if declared is not None:
        return min(declared, native) if native is not None else declared
    if native is not None:
        return min(native, ceiling)
    return ceiling
