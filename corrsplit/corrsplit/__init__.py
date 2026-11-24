# -*- coding: utf-8 -*-
"""corrsplit —— 更正单拆批审批分析（只读）+ 三表仓的版本化发布与账单交付。

只读分析入口：

    from corrsplit import Analyzer, analyze_document, check_stage, \
        minimal_closure, evaluate_stage

    analyzer = Analyzer(doc)          # doc 为输入 JSON 对应的 dict
    analyzer.analysis()               # 同批组 + 必要先后关系（含证据）
    analyzer.check(["c1", "c2"])      # 所选集合是否为可合法发布的阶段
    analyzer.closure(["c1"])          # 包含所选的最小合法集合（带证据链）
    analyzer.evaluate(["c1"])         # 对所选阶段直接做全约束求值

构造 Analyzer 时若输入不合法抛 InputError；若初始或全部更正后的仓表
不合法抛 Refusal（拒绝拆批并定位冲突）。

发布流程入口（corrsplit.billing）：

    from corrsplit import Repository

    repo = Repository.init("仓库目录", rows)        # 初始化工作库与版本 0
    repo = Repository.open("仓库目录")              # 打开已有仓库
    repo.publish(base_version, corrections)         # 发布一批更正，交付账单变更
    repo.current_bills()                            # 带版本的当前账单
    repo.changes(version)                           # 重读某次成功发布的变更
    repo.verify()                                   # 复核：约束/重算/回放/版本链

发布时版本不符抛 VersionConflict，整批应用后仓表不合法抛 BillingRefusal
（均已完整回滚）；输入不合法抛 InputError。
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
from . import billing, reconciliation
from .billing import (
    BillingError,
    NotARepository,
    ReceiptConflict,
    ReceiptVersionConflict,
    Repository,
    RepositoryExists,
    UnknownVersion,
    VersionConflict,
)
from .billing import Refusal as BillingRefusal
from .reconciliation import (
    ReconciliationReport,
    import_receipts,
    list_receipts,
    reconcile,
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
    "billing",
    "reconciliation",
    "import_receipts",
    "reconcile",
    "list_receipts",
    "ReconciliationReport",
    "ReceiptConflict",
    "ReceiptVersionConflict",
    "Repository",
    "BillingError",
    "BillingRefusal",
    "NotARepository",
    "RepositoryExists",
    "UnknownVersion",
    "VersionConflict",
]

__version__ = "0.2.0"
