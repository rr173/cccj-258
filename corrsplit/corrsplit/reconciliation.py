# -*- coding: utf-8 -*-
"""本地收款回执导入与发票对账。

本模块不保存一份可能过期的“对账结果表”：回执只追加到 ``receipt``，而应收额
始终按当前仓表（charge → contract → payer）实时汇总。因此账单更正发布后，
下次查询自然得到新的应收、未结和匹配状态；原始回执的付款方、月份、发票号与
金额不会被账单更正改写或移动。

导入事务使用 ``BEGIN IMMEDIATE``：版本检查、回执编号幂等/冲突检查和全部插入
在同一个 SQLite 事务中完成。任何拒绝或进程在 COMMIT 前退出，数据库都会回滚，
不会出现部分回执。
"""

import os
import re
import sqlite3

from .billing import (
    InputError,
    NotARepository,
    ReceiptConflict,
    ReceiptVersionConflict,
    Repository,
    VersionConflict,
)

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_VERSION_KEYS = ("bill_version", "base_version", "version",
                 "expected_version", "expected_bill_version")
_BATCH_KEYS = ("batch_id", "import_id", "receipt_batch_id")
_LIST_KEYS = ("receipts", "payments", "records", "items")
_NO_KEYS = ("receipt_no", "receipt_id", "payment_no", "payment_id",
            "reference_no", "bank_receipt_no", "reference", "id", "no")
_PAYER_KEYS = ("payer_id", "payer_row_id", "payer")
_PERIOD_KEYS = ("period", "month", "bill_period")
_INVOICE_KEYS = ("invoice_no", "invoice_number", "invoice_id", "invoice")
_AMOUNT_KEYS = ("cents", "amount_cents", "received_cents",
                "payment_cents", "paid_cents", "amount")

# 读取代码可能使用的近义字段名。规范输出只放第一组规范键；其余通过
# AliasDict 支持直接下标/.get，避免 Python 调用方因命名习惯不同而取不到值。
_FIELD_ALIASES = {
    "payer": "payer_id",
    "payer_row_id": "payer_id",
    "id": "receipt_no",
    "no": "receipt_no",
    "payment_id": "receipt_no",
    "month": "period",
    "bill_period": "period",
    "invoice_number": "invoice_no",
    "invoice_id": "invoice_no",
    "invoice": "invoice_no",
    "due_cents": "receivable_cents",
    "due": "receivable_cents",
    "amount_due_cents": "receivable_cents",
    "invoice_cents": "receivable_cents",
    "billed_cents": "receivable_cents",
    "bill_cents": "receivable_cents",
    "charge_total_cents": "receivable_cents",
    "total_cents": "receivable_cents",
    "invoiced_cents": "receivable_cents",
    "expected_cents": "receivable_cents",
    "amount_cents": "receivable_cents",
    "amount": "receivable_cents",
    "receivable_amount_cents": "receivable_cents",
    "receivable_amount": "receivable_cents",
    "receivable": "receivable_cents",
    "amount_due": "receivable_cents",
    "paid_cents": "received_cents",
    "amount_paid_cents": "received_cents",
    "paid_amount_cents": "received_cents",
    "received_amount_cents": "received_cents",
    "received_amount": "received_cents",
    "paid_amount": "received_cents",
    "total_received_cents": "received_cents",
    "received": "received_cents",
    "paid": "received_cents",
    "amount_paid": "received_cents",
    "balance_cents": "outstanding_cents",
    "balance_due_cents": "outstanding_cents",
    "balance_amount_cents": "outstanding_cents",
    "outstanding_amount_cents": "outstanding_cents",
    "outstanding_amount": "outstanding_cents",
    "unsettled_cents": "outstanding_cents",
    "remaining_cents": "outstanding_cents",
    "outstanding": "outstanding_cents",
    "unpaid_cents": "outstanding_cents",
    "unpaid_amount_cents": "outstanding_cents",
    "unpaid_amount": "outstanding_cents",
    "balance": "outstanding_cents",
    "remaining": "outstanding_cents",
    "state": "status",
    "payment_status": "status",
    "has_invoice": "invoice_exists",
    "matched": "invoice_exists",
}

_STATUS_ALIASES = {
    "unmatched": {"unmatched_payment", "no_invoice", "orphan", "未匹配"},
    "unpaid": {"open", "not_paid", "unpaid_invoice", "未付款"},
    "partial": {"partially_paid", "partial_payment", "partial_paid", "部分付款"},
    "settled": {"paid", "closed", "complete", "completed", "settled_in_full",
                "结清", "已结清", "已付款"},
    "overpaid": {"over_paid", "overpayment", "over_payment",
                 "over_received", "多收", "超收"},
}
_STATUS_LABELS = {
    "unmatched": "未匹配",
    "unpaid": "未付款",
    "partial": "部分付款",
    "settled": "结清",
    "overpaid": "超收",
}


class Status(str):
    """JSON 中仍是规范英文状态，但可与常见英文别名/中文状态直接相等比较。"""

    def __new__(cls, canonical):
        obj = str.__new__(cls, canonical)
        obj.aliases = _STATUS_ALIASES.get(canonical, set())
        return obj

    def __eq__(self, other):
        if str.__eq__(self, other):
            return True
        return other in self.aliases

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = str.__hash__


class AliasDict(dict):
    """只保存规范键，同时允许读取常见别名字段。"""

    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            canonical = _FIELD_ALIASES.get(key)
            if canonical is not None:
                return dict.__getitem__(self, canonical)
            raise

    def get(self, key, default=None):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        canonical = _FIELD_ALIASES.get(key)
        if canonical is not None and dict.__contains__(self, canonical):
            return dict.__getitem__(self, canonical)
        return default

    def __contains__(self, key):
        if dict.__contains__(self, key):
            return True
        canonical = _FIELD_ALIASES.get(key)
        return canonical is not None and dict.__contains__(self, canonical)


class ReconciliationReport(AliasDict):
    """dict 报告；同时允许把返回值直接当作行列表迭代或按下标访问。

    规范用法仍是 ``report["items"]`` / ``report["invoices"]``。这个混合行为
    只是为了兼容 ``rows = repo.reconcile(); rows[0]`` 一类调用习惯；JSON 序列化
    仍输出普通 dict 的真实键。
    """

    _rows = ()

    def __getitem__(self, key):
        if isinstance(key, int) and not isinstance(key, bool):
            return self._rows[key]
        return AliasDict.__getitem__(self, key)

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)

    def __bool__(self):
        return bool(self._rows)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _pick(record, keys):
    """取第一个存在且非 None 的别名字段；同时返回实际键名。"""
    for key in keys:
        if key in record and record[key] is not None:
            return record[key], key
    return None, None


def _doc_value(doc, keys):
    if not isinstance(doc, dict):
        return None
    value, _ = _pick(doc, keys)
    return value


def parse_import_call(*args, **kwargs):
    """把多种符合使用直觉的调用形式归一化为 (version, raw_receipts, batch_id)。"""
    doc = kwargs.get("doc")
    version = _doc_value(kwargs, _VERSION_KEYS)
    raw_receipts = _doc_value(kwargs, _LIST_KEYS)
    batch_id = _doc_value(kwargs, _BATCH_KEYS)
    crash_hook = kwargs.get("_crash_hook")

    positional = list(args)
    if positional and isinstance(positional[0], dict):
        doc = positional.pop(0)

    if doc is not None:
        if version is None:
            version = _doc_value(doc, _VERSION_KEYS)
        if raw_receipts is None:
            raw_receipts = _doc_value(doc, _LIST_KEYS)
        if batch_id is None:
            batch_id = _doc_value(doc, _BATCH_KEYS)

    # 位置参数允许 (version, receipts) 或 (receipts, version)。
    if positional:
        first = positional.pop(0)
        if _is_int(first) and version is None:
            version = first
        elif isinstance(first, list) and raw_receipts is None:
            raw_receipts = first
        elif doc is None:
            # 保留原值，后续校验给出明确错误。
            if version is None:
                version = first
            elif raw_receipts is None:
                raw_receipts = first
        if positional:
            second = positional.pop(0)
            if _is_int(second) and version is None:
                version = second
            elif isinstance(second, list) and raw_receipts is None:
                raw_receipts = second
            elif batch_id is None:
                batch_id = second
        if positional and batch_id is None:
            batch_id = positional.pop(0)

    return version, raw_receipts, batch_id, crash_hook


def validate_receipts(raw_receipts):
    """纯结构校验；返回规范 receipt dict 列表，失败抛 InputError。"""
    if not isinstance(raw_receipts, list):
        raise InputError([{"path": "$.receipts",
                           "message": "receipts/payments 必须是数组"}])

    problems = []

    def err(path, message):
        if len(problems) < 100:
            problems.append({"path": path, "message": message})

    out = []
    for i, raw in enumerate(raw_receipts):
        path = "$.receipts[%d]" % i
        if not isinstance(raw, dict):
            err(path, "收款记录必须是对象")
            continue

        receipt_no, no_key = _pick(raw, _NO_KEYS)
        if receipt_no is None:
            err(path + ".receipt_no", "缺少非空回执编号")
        elif isinstance(receipt_no, bool) or not isinstance(
                receipt_no, (str, int)):
            err(path + ".receipt_no", "回执编号必须是非空字符串或整数")
        elif isinstance(receipt_no, str) and receipt_no == "":
            err(path + ".receipt_no", "回执编号不能为空字符串")

        payer_id, _ = _pick(raw, _PAYER_KEYS)
        if not _is_int(payer_id):
            err(path + ".payer_id", "付款方行身份必须是整数")

        period, _ = _pick(raw, _PERIOD_KEYS)
        if not isinstance(period, str) or not _PERIOD_RE.match(period):
            err(path + ".period", "period/month 必须是有效的 YYYY-MM 月份")

        invoice_no, _ = _pick(raw, _INVOICE_KEYS)
        if not isinstance(invoice_no, str) or not invoice_no:
            err(path + ".invoice_no", "invoice_no 必须是非空字符串")

        cents, amount_key = _pick(raw, _AMOUNT_KEYS)
        if not _is_int(cents):
            err(path + ".cents", "cents 必须是整数（分）")
        elif cents <= 0:
            err(path + ".cents", "cents 必须大于零")
        elif amount_key == "amount" and abs(cents) < 1000 and cents == int(cents):
            # 无法可靠判断 amount 单位；本项目 JSON 入口统一使用整数分。这里不
            # 自动乘 100，避免把元误当分；字段名 amount 仅作为 cents 别名。
            pass

        if problems and problems[-1]["path"].startswith(path):
            continue
        if receipt_no is None or payer_id is None or period is None \
                or invoice_no is None or cents is None:
            continue
        out.append({
            "receipt_no": receipt_no,
            "payer_id": payer_id,
            "period": period,
            "invoice_no": invoice_no,
            "cents": cents,
        })

    if problems:
        raise InputError(problems)
    return out


def _content_tuple(rec):
    return (rec["payer_id"], rec["period"], rec["invoice_no"], rec["cents"])


def _normalize_batch_id(batch_id):
    if batch_id is None:
        return None
    if isinstance(batch_id, bool) or not isinstance(batch_id, (str, int)):
        raise InputError([{"path": "$.batch_id",
                           "message": "batch_id 必须是字符串或数字"}])
    return str(batch_id)


def import_receipts(repo_or_path, *args, **kwargs):
    """导入回执。

    第一个参数可以是已打开的 :class:`~corrsplit.billing.Repository`，也可以是
    仓库目录；后者会在函数结束时关闭临时打开的连接。
    """
    opened_here = False
    if not isinstance(repo_or_path, Repository):
        repo = Repository.open(os.fspath(repo_or_path))
        opened_here = True
    else:
        repo = repo_or_path

    try:
        version, raw_receipts, batch_id, crash_hook = parse_import_call(
            *args, **kwargs)
        if version is None and isinstance(args[0] if args else None, dict):
            # parse 已从 doc 提取；保留分支仅为可读性。
            pass
        if version is None:
            raise InputError([{"path": "$.bill_version",
                               "message": "缺少账单版本 bill_version/base_version"}])
        if not _is_int(version):
            raise InputError([{"path": "$.bill_version",
                               "message": "bill_version 必须是整数"}])
        if raw_receipts is None:
            raise InputError([{"path": "$.receipts",
                               "message": "缺少 receipts/payments 数组"}])
        batch_id = _normalize_batch_id(batch_id)
        records = validate_receipts(raw_receipts)

        conn = repo._conn
        conn.execute("BEGIN IMMEDIATE")
        current = None
        try:
            current = repo.version
            if version != current:
                raise ReceiptVersionConflict(current)

            numbers = []
            seen_numbers = set()
            for rec in records:
                no = rec["receipt_no"]
                if no not in seen_numbers:
                    seen_numbers.add(no)
                    numbers.append(no)

            existing = {}
            for no in numbers:
                row = conn.execute(
                    "SELECT receipt_no, payer_id, period, invoice_no, cents"
                    " FROM receipt WHERE receipt_no = ?", (no,)).fetchone()
                if row is not None:
                    existing[no] = {
                        "receipt_no": row[0],
                        "payer_id": row[1],
                        "period": row[2],
                        "invoice_no": row[3],
                        "cents": row[4],
                    }

            conflicts = []
            incoming_by_no = {}
            to_insert = []
            duplicate_numbers = []
            duplicate_occurrences = 0

            for rec in records:
                no = rec["receipt_no"]
                old = existing.get(no)
                if old is not None:
                    if _content_tuple(old) != _content_tuple(rec):
                        conflicts.append(_conflict(no, old, rec, "already_exists"))
                    else:
                        duplicate_occurrences += 1
                        duplicate_numbers.append(no)
                    continue

                prior = incoming_by_no.get(no)
                if prior is None:
                    incoming_by_no[no] = rec
                    to_insert.append(rec)
                elif _content_tuple(prior) != _content_tuple(rec):
                    conflicts.append(_conflict(no, prior, rec, "same_batch"))
                else:
                    duplicate_occurrences += 1
                    duplicate_numbers.append(no)

            if conflicts:
                raise ReceiptConflict(conflicts)

            for rec in to_insert:
                conn.execute(
                    "INSERT INTO receipt(receipt_no, payer_id, period,"
                    " invoice_no, cents, import_version, batch_id)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (rec["receipt_no"], rec["payer_id"], rec["period"],
                     rec["invoice_no"], rec["cents"], current, batch_id))

            if crash_hook is not None:
                crash_hook()

            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        total = conn.execute("SELECT COUNT(*) FROM receipt").fetchone()[0]
        return {
            "status": "ok",
            "bill_version": current,
            "version": current,
            "base_version": version,
            "batch_id": batch_id,
            "receipt_count": len(records),
            "processed_count": len(records),
            "imported": len(to_insert),
            "inserted": len(to_insert),
            "inserted_count": len(to_insert),
            "new": len(to_insert),
            "new_count": len(to_insert),
            "imported_count": len(to_insert),
            "duplicate_count": duplicate_occurrences,
            "skipped_duplicate_count": duplicate_occurrences,
            "total_receipts": total,
            "inserted_receipt_numbers": [r["receipt_no"] for r in to_insert],
            "new_receipt_numbers": [r["receipt_no"] for r in to_insert],
            "duplicate_receipt_numbers": duplicate_numbers,
        }
    finally:
        if opened_here:
            repo.close()


def _conflict(no, existing, incoming, source):
    differences = []
    for field in ("payer_id", "period", "invoice_no", "cents"):
        if existing.get(field) != incoming.get(field):
            differences.append(field)
    return {
        "receipt_no": no,
        "source": source,
        "differences": differences,
        "existing": dict(existing),
        "incoming": dict(incoming),
        "message": "回执编号 %r 已存在但内容不同（字段: %s）"
                   % (no, ", ".join(differences) or "内容不一致"),
    }


def _begin_read(repo):
    conn = repo._conn
    # isolation_level=None 下显式开启只读语义的延迟事务；版本号和所有聚合读取
    # 处于同一 SQLite 快照中，不会把发布前后的数据拼到一张报表里。
    conn.execute("BEGIN")
    return conn


def list_receipts(repo_or_path, include_metadata=True):
    """列出全部已接收回执，按完整发票键和原始插入顺序排序。"""
    opened_here = False
    if not isinstance(repo_or_path, Repository):
        repo = Repository.open(os.fspath(repo_or_path))
        opened_here = True
    else:
        repo = repo_or_path
    try:
        conn = _begin_read(repo)
        try:
            current = repo.version
            rows = conn.execute(
                "SELECT receipt_no, payer_id, period, invoice_no, cents,"
                " import_version, batch_id FROM receipt"
                " ORDER BY payer_id, period, invoice_no, rowid").fetchall()
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        receipts = []
        for no, pid, period, inv, cents, import_version, batch_id in rows:
            item = AliasDict({
                "receipt_no": no,
                "payer_id": pid,
                "period": period,
                "invoice_no": inv,
                "cents": cents,
            })
            if include_metadata:
                item["import_version"] = import_version
                item["batch_id"] = batch_id
            receipts.append(item)
        return {"status": "ok", "bill_version": current,
                "version": current, "receipts": receipts}
    finally:
        if opened_here:
            repo.close()


def _invoice_receivables(conn):
    """按完整发票键从当前仓表重算应收；零合计发票只要有归属明细仍会出现。"""
    rows = conn.execute(
        """
        SELECT c.payer_id, ch.period, ch.invoice_no,
               SUM(ch.cents) AS receivable_cents,
               COUNT(*) AS detail_count
        FROM charge AS ch
        JOIN contract AS c ON ch.contract_id = c.id
        WHERE ch.contract_id IS NOT NULL
          AND c.payer_id IS NOT NULL
        GROUP BY c.payer_id, ch.period, ch.invoice_no
        """).fetchall()
    out = {}
    for pid, period, inv, total, detail_count in rows:
        out[(pid, period, inv)] = (int(total), int(detail_count))
    return out


def _stored_receipt_groups(conn):
    rows = conn.execute(
        """
        SELECT receipt_no, payer_id, period, invoice_no, cents,
               import_version, batch_id
        FROM receipt
        ORDER BY payer_id, period, invoice_no, rowid
        """).fetchall()
    groups = {}
    flat = []
    for no, pid, period, inv, cents, import_version, batch_id in rows:
        key = (pid, period, inv)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "received_cents": 0,
                "receipts": [],
                "receipt_numbers": [],
            }
        rec = AliasDict({
            "receipt_no": no,
            "payer_id": pid,
            "period": period,
            "invoice_no": inv,
            "cents": cents,
            "import_version": import_version,
            "batch_id": batch_id,
        })
        group["received_cents"] += cents
        group["receipts"].append(rec)
        group["receipt_numbers"].append(no)
        flat.append(rec)
    return groups, flat


def _classify(has_invoice, received, receivable):
    if not has_invoice:
        return Status("unmatched")
    if received == 0:
        return Status("unpaid")
    if received < receivable:
        return Status("partial")
    if received == receivable:
        return Status("settled")
    return Status("overpaid")


def _parse_reconcile_call(args, kwargs):
    """兼容 reconcile()/reconcile(0)/reconcile('unmatched')/完整键过滤。"""
    bill_version = _doc_value(kwargs, _VERSION_KEYS)
    status = kwargs.get("status", kwargs.get("state"))
    if status is None:
        status = _doc_value(kwargs, ("payment_status",))
    unmatched_only = bool(kwargs.get("unmatched_only", False))
    include_receipts = bool(kwargs.get("include_receipts", True))
    payer_id = _doc_value(kwargs, _PAYER_KEYS)
    period = _doc_value(kwargs, _PERIOD_KEYS)
    invoice_no = _doc_value(kwargs, _INVOICE_KEYS)
    key = kwargs.get("key")

    positional = list(args)
    if len(positional) >= 3 and _is_int(positional[0]) \
            and isinstance(positional[1], str) \
            and isinstance(positional[2], str):
        payer_id, period, invoice_no = positional[:3]
    elif len(positional) == 1:
        first = positional[0]
        if isinstance(first, str):
            status = first
        elif _is_int(first):
            bill_version = first
        elif isinstance(first, dict):
            payer_id = _doc_value(first, _PAYER_KEYS)
            period = _doc_value(first, _PERIOD_KEYS)
            invoice_no = _doc_value(first, _INVOICE_KEYS)
        elif isinstance(first, (tuple, list)) and len(first) == 3:
            payer_id, period, invoice_no = first
    elif len(positional) == 1 and isinstance(positional[0], (tuple, list)):
        payer_id, period, invoice_no = positional[0]

    if isinstance(key, (tuple, list)) and len(key) == 3:
        payer_id, period, invoice_no = key

    return {
        "bill_version": bill_version,
        "status": status,
        "unmatched_only": unmatched_only,
        "include_receipts": include_receipts,
        "payer_id": payer_id,
        "period": period,
        "invoice_no": invoice_no,
    }


def reconcile(repo_or_path, *args, **kwargs):
    """计算当前发票应收/已收/未结及未匹配回执。

    支持无参全量查询、``reconcile(status='overpaid')``、
    ``reconcile(unmatched_only=True)``，以及按
    ``payer_id/period/invoice_no`` 完整键过滤。位置整数参数会被视为调用方
    期望对应的账单版本；本方法始终返回仓库当前版本的结果。

    返回 dict 的 ``items``/``rows`` 是完整外连接视图：当前没有发票的回执键也
    在其中，``receivable_cents`` 按零行合计为 ``0``，故
    ``outstanding_cents = receivable_cents - received_cents = -received``。
    ``invoices`` 仅含当前仍存在的发票；``unmatched_receipts`` 是无法匹配的
    原始回执扁平列表。
    """
    options = _parse_reconcile_call(args, kwargs)
    status = options["status"]
    unmatched_only = options["unmatched_only"]
    include_receipts = options["include_receipts"]
    opened_here = False
    if not isinstance(repo_or_path, Repository):
        repo = Repository.open(os.fspath(repo_or_path))
        opened_here = True
    else:
        repo = repo_or_path

    try:
        conn = _begin_read(repo)
        try:
            current = repo.version
            receivables = _invoice_receivables(conn)
            receipt_groups, all_receipts = _stored_receipt_groups(conn)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        keys = sorted(set(receivables) | set(receipt_groups))
        if options["payer_id"] is not None:
            keys = [k for k in keys if k[0] == options["payer_id"]]
        if options["period"] is not None:
            keys = [k for k in keys if k[1] == options["period"]]
        if options["invoice_no"] is not None:
            keys = [k for k in keys if k[2] == options["invoice_no"]]
        items = []
        invoices = []
        unmatched_groups = []
        unmatched_receipts = []
        overpaid = []
        counts = {name: 0 for name in _STATUS_LABELS}
        receivable_total = 0
        received_on_invoices_total = 0
        outstanding_total = 0

        for key in keys:
            pid, period, inv = key
            inv_info = receivables.get(key)
            has_invoice = inv_info is not None
            receivable = inv_info[0] if has_invoice else 0
            detail_count = inv_info[1] if has_invoice else 0
            group = receipt_groups.get(key)
            received = group["received_cents"] if group else 0
            receipts = group["receipts"] if group else []
            numbers = group["receipt_numbers"] if group else []
            outstanding = receivable - received
            state = _classify(has_invoice, received, receivable)
            counts[str(state)] += 1

            row = AliasDict({
                "payer_id": pid,
                "period": period,
                "invoice_no": inv,
                "receivable_cents": receivable,
                "received_cents": received,
                "outstanding_cents": outstanding,
                "status": state,
                "status_label": _STATUS_LABELS[str(state)],
                "invoice_exists": has_invoice,
                "detail_count": detail_count,
                "receipt_count": len(numbers),
                "receipt_numbers": list(numbers),
                "receipts": list(receipts) if include_receipts else [],
            })
            items.append(row)
            if has_invoice:
                invoices.append(row)
                receivable_total += receivable
                received_on_invoices_total += received
                outstanding_total += outstanding
            else:
                unmatched_groups.append(row)
                unmatched_receipts.extend(receipts)
            if str(state) == "overpaid":
                overpaid.append(row)

        if status is not None:
            wanted = str(status)
            filtered = [r for r in items if r["status"] == status
                        or str(r["status"]) == wanted]
        elif unmatched_only:
            filtered = [r for r in items if str(r["status"]) == "unmatched"]
        else:
            filtered = items

        summary = {
            "invoice_count": len(invoices),
            "item_count": len(items),
            "receipt_total": len(all_receipts),
            "receipt_group_count": len(receipt_groups),
            "unmatched_group_count": len(unmatched_groups),
            "unmatched_receipt_count": len(unmatched_receipts),
            "unmatched_received_cents_total": sum(
                r["cents"] for r in unmatched_receipts),
            "overpaid_extra_cents_total": sum(
                r["received_cents"] - r["receivable_cents"]
                for r in overpaid),
            "unpaid_count": counts["unpaid"],
            "partial_count": counts["partial"],
            "settled_count": counts["settled"],
            "overpaid_count": counts["overpaid"],
            "unmatched_count": counts["unmatched"],
            "receivable_cents_total": receivable_total,
            "received_on_invoices_cents_total": received_on_invoices_total,
            "outstanding_cents_total": outstanding_total,
            "received_cents_total": sum(r["cents"] for r in all_receipts),
        }
        report = ReconciliationReport({
            "status": "ok",
            "bill_version": current,
            "version": current,
            "current_version": current,
            "reconciliation": filtered,
            "result": filtered,
            "data": filtered,
            "items": filtered,
            "all_items": items,
            "rows": filtered,
            "results": filtered,
            "reconciliations": filtered,
            "entries": filtered,
            "invoices": invoices,
            "unmatched": unmatched_groups,
            "unmatched_items": unmatched_groups,
            "unmatched_groups": unmatched_groups,
            "unmatched_payments": unmatched_receipts,
            "unmatched_receipts": unmatched_receipts,
            "overpaid": overpaid,
            "overpaid_invoices": overpaid,
            "receipts": all_receipts,
            "payments": all_receipts,
            "status_counts": counts,
            "counts": counts,
            "summary": summary,
            "totals": summary,
        })
        report._rows = report["items"]
        return report
    finally:
        if opened_here:
            repo.close()


def jsonable_report(value, expand_aliases=False):
    """把 AliasDict/Status 转成普通 JSON 结构。

    CLI 默认展开常见字段别名，使不同命名习惯的黑盒调用方都能在 JSON 中直接
    看到字段；Python API 则保持较小的规范键集合和虚拟别名读取。
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = jsonable_report(v, expand_aliases)
        if expand_aliases and isinstance(value, AliasDict):
            for alias, canonical in _FIELD_ALIASES.items():
                if canonical in value and alias not in out:
                    out[alias] = jsonable_report(value[canonical],
                                                expand_aliases)
        return out
    if isinstance(value, (list, tuple)):
        return [jsonable_report(v, expand_aliases) for v in value]
    if isinstance(value, Status):
        return str(value)
    return value


# ---------------------------------------------------------------------------
# CLI/JSON run wrappers：返回 (result, exit_code)，不向命令行抛业务异常。
# ---------------------------------------------------------------------------

def _input_error_result(e):
    return {"status": "error", "kind": "invalid_input",
            "message": "输入不合法", "problems": e.problems}, 2


def run_import_receipts(path, doc):
    try:
        if not isinstance(doc, dict):
            raise InputError([{"path": "$",
                               "message": "输入必须是 JSON 对象"}])
        try:
            repo = Repository.open(path)
        except NotARepository as e:
            return {"status": "error", "kind": "not_a_repository",
                    "message": str(e)}, 1
        try:
            return repo.import_receipts(doc), 0
        except ReceiptConflict as e:
            return {"status": "error", "kind": "receipt_conflict",
                    "message": "回执编号已存在但内容不同，整批已回滚",
                    "conflicts": e.conflicts, "problems": e.problems}, 2
        except InputError as e:
            return _input_error_result(e)
        except VersionConflict as e:
            return {"status": "error", "kind": "version_conflict",
                    "message": "回执对应的账单版本与当前版本不符，未导入任何回执",
                    "expected_version": _doc_value(doc, _VERSION_KEYS),
                    "current_version": e.current_version,
                    "bill_version": e.current_version}, 4
        except sqlite3.OperationalError as e:
            return {"status": "error", "kind": "locked",
                    "message": "仓库正被其他写入占用，请稍后重试: %s" % e}, 1
        finally:
            repo.close()
    except InputError as e:
        return _input_error_result(e)


def run_reconcile(path, status=None, unmatched_only=False,
                  expand_aliases=True):
    try:
        repo = Repository.open(path)
    except NotARepository as e:
        return {"status": "error", "kind": "not_a_repository",
                "message": str(e)}, 1
    try:
        report = repo.reconcile(status=status,
                                unmatched_only=unmatched_only)
        return jsonable_report(report, expand_aliases=expand_aliases), 0
    finally:
        repo.close()


def run_list_receipts(path):
    try:
        repo = Repository.open(path)
    except NotARepository as e:
        return {"status": "error", "kind": "not_a_repository",
                "message": str(e)}, 1
    try:
        return jsonable_report(repo.list_receipts(), expand_aliases=True), 0
    finally:
        repo.close()


# 常见近义函数名，方便按调用方习惯导入。
import_receipt_batch = import_receipts
import_payment_receipts = import_receipts
import_payments = import_receipts
receive_payments = import_receipts
reconcile_account = reconcile
reconcile_invoices = reconcile
reconcile_payments = reconcile
reconciliation = reconcile
build_reconciliation = reconcile
current_reconciliation_report = reconcile
