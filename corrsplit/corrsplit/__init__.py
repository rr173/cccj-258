# -*- coding: utf-8 -*-
"""corrsplit —— 更正单拆批审批分析（只读）。

本地可调用入口：

    from corrsplit import Analyzer, analyze_document, check_stage, \
        minimal_closure, evaluate_stage

    analyzer = Analyzer(doc)          # doc 为输入 JSON 对应的 dict
    analyzer.analysis()               # 同批组 + 必要先后关系（含证据）
    analyzer.check(["c1", "c2"])      # 所选集合是否为可合法发布的阶段
    analyzer.closure(["c1"])          # 包含所选的最小合法集合（带证据链）
    analyzer.evaluate(["c1"])         # 对所选阶段直接做全约束求值

构造 Analyzer 时若输入不合法抛 InputError；若初始或全部更正后的仓表
不合法抛 Refusal（拒绝拆批并定位冲突）。
"""

from .core import (
    Analyzer,
    InputError,
    Refusal,
    analyze_document,
    check_stage,
    evaluate_stage,
    minimal_closure,
    run,
)

__all__ = [
    "Analyzer",
    "InputError",
    "Refusal",
    "analyze_document",
    "check_stage",
    "evaluate_stage",
    "minimal_closure",
    "run",
]

__version__ = "0.1.0"
