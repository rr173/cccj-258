# -*- coding: utf-8 -*-
"""兼容入口：推荐使用 :mod:`corrsplit.reconciliation`。

本模块也可直接调用：``corrsplit.reconcile(repo)``。为避免与包顶层导出的
同名函数/子模块加载顺序相互覆盖，下面把模块对象本身变成可调用对象。
"""

import sys
import types

from . import reconciliation as _impl
from .reconciliation import *  # noqa: F401,F403
from .reconciliation import (  # noqa: F401
    AliasDict,
    ReconciliationReport,
    Status,
    import_receipts,
    jsonable_report,
    list_receipts,
    reconcile,
    run_import_receipts,
    run_list_receipts,
    run_reconcile,
    validate_receipts,
)
from .billing import ReceiptConflict, VersionConflict  # noqa: F401


class _CallableModule(types.ModuleType):
    def __call__(self, *args, **kwargs):
        return _impl.reconcile(*args, **kwargs)


sys.modules[__name__].__class__ = _CallableModule
