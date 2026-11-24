# -*- coding: utf-8 -*-
"""corrsplit.billing —— 三表仓（payer/contract/charge）的本地版本化发布与账单交付。

仓表（仅三张）
--------------
- ``payer(id)``：付款方，``id`` 为不可变整数行身份；
- ``contract(id, code, payer_id)``：``code`` 非空且唯一；``payer_id`` 可空，
  非空时须指向存在的 payer；
- ``charge(id, contract_id, period, invoice_no, cents)``：``contract_id`` 可空，
  非空时须指向存在的 contract；``period`` 为有效的 ``YYYY-MM`` 月份；
  ``invoice_no`` 非空；``cents`` 为有符号整数分；后三者都不可为空。

更正语义与只读分析器一致：每行至多一条更正，新增 add / 删除 delete /
整行替换 replace（不得改变行身份），不自动级联、不补更正。整批更正先全部落库，
再对落库后的状态统一核对约束——因此批内合法换号（如两条合同互换 code）不会因
逐条写入的中间状态被拒绝。

账单
----
账单键 = (付款方行身份, 月份)。每条明细沿 contract → payer 归属，链上任一引用
为空即不计入。账单值 = (净额 net_cents, 不同 invoice_no 的数量 invoice_count)；
净额为零但仍有归属明细的账单依然存在。内部为每张账单维护票号多重集合
（invoice_no → 归属明细数），小批发布只需按增量套用，无需重算整张账单。

版本与交付
----------
- ``Repository.init`` 用完整三表数据建立工作库与版本 0（全量扫描底稿，允许），
  版本 0 的变更记录为全部初始账单的 upsert；
- ``publish(base_version, corrections)`` 校验并应用整批更正，成功则产生下一版本，
  交付该版本的账单变更：新增账单与值变化后仍存在的账单交付完整新值（upsert），
  不再有归属明细的旧账单只交付撤销键（revoke），值未变的账单不交付；
- 期望基准版本与当前版本不符、或整批应用后仓表不合法 → 拒绝并完整回滚；
- 仓表更正、当前账单、该版本变更在同一 SQLite 事务内生效：拒绝或进程意外退出
  都不会留下一半的新数据；``BEGIN IMMEDIATE`` 保证一个版本只被一批发布推进，
  后到者只会看到版本不符，无法覆盖别人已成功发布的结果；
- version_log / version_change 由触发器保护，成功发布的变更不可改写。

小批发布的读取范围（性能约束）
------------------------------
发布所读的业务数据限于：本批行（按行身份点查）、核对约束所需的关联行或占用
记录（code 占用、payer/contract 存在性、被删 payer/contract 的引用行，均走索引）、
前后归属可能改变的明细（本批直接更正的明细 + payer_id 发生变化的合同名下的明细，
走 contract_id 索引）、以及这些变化直接涉及的账单与票号状态。不整仓重读、不扫描
无关合同的明细、不重算整张受影响账单。读取整个当前账单视图（bills/verify）
不受此限制。每次发布的结果中带 ``reads`` 统计，可直接核对读取规模。
"""

import json
import os
import re
import sqlite3
from collections import defaultdict

from .core import InputError, InternalError

TABLES = ("payer", "contract", "charge")
COLUMNS = {
    "payer": ("id",),
    "contract": ("id", "code", "payer_id"),
    "charge": ("id", "contract_id", "period", "invoice_no", "cents"),
}

DB_NAME = "billing.db"
FORMAT = "corrsplit-billing/1"
MAX_PROBLEMS = 100
MAX_VIOLATIONS = 200

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_OPS = ("add", "delete", "replace")
_STATS_TABLES = ("payer", "contract", "charge", "bill", "bill_invoice")

# 仓表约束（唯一键、外键）由发布流程在整批落库后统一核对（见模块 docstring），
# 因此这里不声明 UNIQUE / FOREIGN KEY 硬约束——否则逐条写入的中间状态
# （如同批互换 code）会被数据库先行拒绝。索引仅用于定位占用/关联行。
_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE payer(id INTEGER PRIMARY KEY);
CREATE TABLE contract(id INTEGER PRIMARY KEY, code TEXT NOT NULL, payer_id INTEGER);
CREATE INDEX idx_contract_code ON contract(code);
CREATE INDEX idx_contract_payer ON contract(payer_id);
CREATE TABLE charge(id INTEGER PRIMARY KEY, contract_id INTEGER,
                    period TEXT NOT NULL, invoice_no TEXT NOT NULL,
                    cents INTEGER NOT NULL);
CREATE INDEX idx_charge_contract ON charge(contract_id);
CREATE TABLE bill(payer_id INTEGER NOT NULL, period TEXT NOT NULL,
                  net_cents INTEGER NOT NULL, charge_count INTEGER NOT NULL,
                  invoice_count INTEGER NOT NULL,
                  PRIMARY KEY(payer_id, period));
CREATE TABLE bill_invoice(payer_id INTEGER NOT NULL, period TEXT NOT NULL,
                          invoice_no TEXT NOT NULL, charge_count INTEGER NOT NULL,
                          PRIMARY KEY(payer_id, period, invoice_no));
CREATE TABLE version_log(version INTEGER PRIMARY KEY, batch_id TEXT,
                         corrections TEXT NOT NULL, change_count INTEGER NOT NULL);
CREATE TABLE version_change(version INTEGER NOT NULL, payer_id INTEGER NOT NULL,
                            period TEXT NOT NULL, kind TEXT NOT NULL,
                            net_cents INTEGER, invoice_count INTEGER,
                            PRIMARY KEY(version, payer_id, period));
CREATE TRIGGER version_log_no_update BEFORE UPDATE ON version_log
BEGIN SELECT RAISE(ABORT, 'version_log 不可改写'); END;
CREATE TRIGGER version_log_no_delete BEFORE DELETE ON version_log
BEGIN SELECT RAISE(ABORT, 'version_log 不可删除'); END;
CREATE TRIGGER version_change_no_update BEFORE UPDATE ON version_change
BEGIN SELECT RAISE(ABORT, 'version_change 不可改写'); END;
CREATE TRIGGER version_change_no_delete BEFORE DELETE ON version_change
BEGIN SELECT RAISE(ABORT, 'version_change 不可删除'); END;
"""


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class BillingError(Exception):
    """billing 模块业务异常的基类。"""


class NotARepository(BillingError):
    """目标路径不是已初始化的仓库。"""


class RepositoryExists(BillingError):
    """初始化目标位置已存在仓库或非空目录。"""


class VersionConflict(BillingError):
    """期望基准版本与当前版本不符，拒绝发布。"""

    def __init__(self, current_version):
        self.current_version = current_version
        super().__init__("期望基准版本与当前版本 %d 不符" % current_version)


class UnknownVersion(BillingError):
    """查询的版本号不存在。"""

    def __init__(self, version, current_version):
        self.version = version
        self.current_version = current_version
        super().__init__("未知版本 %r（当前版本 %d）" % (version, current_version))


class Refusal(BillingError):
    """仓表不合法：init 时为 invalid_initial_state，publish 时为 invalid_batch_state。"""

    def __init__(self, kind, violations):
        self.kind = kind
        self.violations = list(violations)
        super().__init__(kind)


# ---------------------------------------------------------------------------
# 行与清单校验
# ---------------------------------------------------------------------------

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str(v):
    return isinstance(v, str)


def _check_row(table, row, path, err):
    """校验一整行（含域规则）；通过则返回行身份，否则 None。"""
    if not isinstance(row, dict):
        err(path, "行必须是对象")
        return None
    cols = COLUMNS[table]
    ok = True
    for col in cols:
        if col not in row:
            err(path, "缺少字段: %s" % col)
            ok = False
    for col in row:
        if col not in cols:
            err("%s.%s" % (path, col), "未声明的字段")
            ok = False
    if not ok:
        return None
    if not _is_int(row["id"]):
        err("%s.id" % path, "行身份必须是整数")
        ok = False
    if table == "contract":
        if not _is_str(row["code"]) or not row["code"]:
            err("%s.code" % path, "code 必须是非空字符串")
            ok = False
        if row["payer_id"] is not None and not _is_int(row["payer_id"]):
            err("%s.payer_id" % path, "payer_id 必须是整数或空")
            ok = False
    elif table == "charge":
        if row["contract_id"] is not None and not _is_int(row["contract_id"]):
            err("%s.contract_id" % path, "contract_id 必须是整数或空")
            ok = False
        period = row["period"]
        if not _is_str(period) or not _PERIOD_RE.match(period):
            err("%s.period" % path, "period 必须是有效的 YYYY-MM 月份")
            ok = False
        if not _is_str(row["invoice_no"]) or not row["invoice_no"]:
            err("%s.invoice_no" % path, "invoice_no 必须是非空字符串")
            ok = False
        if not _is_int(row["cents"]):
            err("%s.cents" % path, "cents 必须是整数（分）")
            ok = False
    if not ok:
        return None
    return row["id"]


class _Corr(object):
    __slots__ = ("ext", "op", "table", "rid", "row")

    def __init__(self, ext, op, table, rid, row):
        self.ext = ext      # 外部更正 id（可省略，自动编号 c1..cn）
        self.op = op
        self.table = table
        self.rid = rid      # 行身份
        self.row = row      # add/replace 的整行；delete 为 None

    def to_json(self):
        d = {"id": self.ext, "op": self.op, "table": self.table}
        if self.op != "add":
            d["row_id"] = self.rid
        if self.op != "delete":
            d["row"] = self.row
        return d


def _validate_corrections(corrections):
    """更正单的纯结构校验（不读库）；返回规范化 _Corr 列表，失败抛 InputError。"""
    if not isinstance(corrections, list):
        raise InputError([{"path": "$.corrections",
                           "message": "corrections 必须是数组"}])
    problems = []

    def err(path, message):
        if len(problems) < MAX_PROBLEMS:
            problems.append({"path": path, "message": message})

    out = []
    used_ext = set()
    used_rows = set()
    for i, c in enumerate(corrections):
        path = "$.corrections[%d]" % i
        if not isinstance(c, dict):
            err(path, "更正必须是对象")
            continue
        ext = c.get("id")
        if ext is None:
            ext = "c%d" % (i + 1)
        elif isinstance(ext, bool) or not isinstance(ext, (str, int)):
            err(path + ".id", "id 必须是字符串或数字")
            continue
        else:
            ext = str(ext)
        if ext in used_ext:
            err(path + ".id", "更正 id 重复: %s" % ext)
            continue
        op = c.get("op")
        table = c.get("table")
        if op not in _OPS:
            err(path + ".op", "op 必须是 add/delete/replace")
            continue
        if table not in TABLES:
            err(path + ".table", "未声明的表: %r" % (table,))
            continue
        row = None
        if op == "add":
            row = c.get("row")
            rid = _check_row(table, row, path + ".row", err)
            if rid is None:
                continue
        else:
            rid = c.get("row_id")
            if not _is_int(rid):
                err(path + ".row_id", "row_id 缺失或类型应为 int")
                continue
            if op == "replace":
                row = c.get("row")
                rid2 = _check_row(table, row, path + ".row", err)
                if rid2 is None:
                    continue
                if rid2 != rid:
                    err(path + ".row", "整行替换不得改变行身份")
                    continue
        key = (table, rid)
        if key in used_rows:
            err(path, "同一行在清单中出现多条更正: %r" % (rid,))
            continue
        used_rows.add(key)
        used_ext.add(ext)
        out.append(_Corr(ext, op, table, rid, row))
    if problems:
        raise InputError(problems)
    return out


def _validate_dataset(rows_doc):
    """初始化数据的结构校验；返回 {表名: [行]}，失败抛 InputError。"""
    problems = []

    def err(path, message):
        if len(problems) < MAX_PROBLEMS:
            problems.append({"path": path, "message": message})

    if not isinstance(rows_doc, dict):
        raise InputError([{"path": "$.rows", "message": "rows 必须是对象"}])
    out = {}
    for tname in rows_doc:
        if tname not in TABLES:
            err("$.rows.%s" % tname, "未声明的表")
    for tname in TABLES:
        rows = rows_doc.get(tname, [])
        if not isinstance(rows, list):
            err("$.rows.%s" % tname, "表数据必须是行数组")
            continue
        seen = set()
        kept = []
        for i, row in enumerate(rows):
            rid = _check_row(tname, row, "$.rows.%s[%d]" % (tname, i), err)
            if rid is None:
                continue
            if rid in seen:
                err("$.rows.%s[%d]" % (tname, i), "行身份重复: %r" % (rid,))
                continue
            seen.add(rid)
            kept.append(dict(row))
        out[tname] = kept
    if problems:
        raise InputError(problems)
    return out


def _cap_violations(violations):
    if len(violations) > MAX_VIOLATIONS:
        return (violations[:MAX_VIOLATIONS]
                + [{"constraint": "truncated",
                    "message": "冲突过多，仅展示前 %d 条" % MAX_VIOLATIONS}])
    return violations


def _fk_violation(table, column, target, rid, ref_id, message, ext=None):
    row = {"table": table, "id": rid}
    if ext is not None:
        row["correction"] = ext
    return {"constraint": "foreign_key", "table": table, "column": column,
            "target_table": target, "referenced_id": ref_id,
            "rows": [row], "message": message}


def _find_dataset_violations(data):
    """对完整三表数据做全量约束核对（初始化/复核用，允许全表扫描）。"""
    violations = []
    by_code = {}
    for c in data["contract"]:
        code = c["code"]
        if code in by_code:
            violations.append({
                "constraint": "unique_key", "table": "contract",
                "key_columns": ["code"], "key_value": [code],
                "rows": [{"table": "contract", "id": by_code[code]["id"]},
                         {"table": "contract", "id": c["id"]}],
                "message": "唯一键 contract(code) 取值 %r 冲突" % code,
            })
        else:
            by_code[code] = c
    payer_ids = set(p["id"] for p in data["payer"])
    for c in data["contract"]:
        pid = c["payer_id"]
        if pid is not None and pid not in payer_ids:
            violations.append(_fk_violation(
                "contract", "payer_id", "payer", c["id"], pid,
                "外键 contract.payer_id → payer：行 %r 引用了不存在的行身份 %r"
                % (c["id"], pid)))
    contract_ids = set(c["id"] for c in data["contract"])
    for ch in data["charge"]:
        ctid = ch["contract_id"]
        if ctid is not None and ctid not in contract_ids:
            violations.append(_fk_violation(
                "charge", "contract_id", "contract", ch["id"], ctid,
                "外键 charge.contract_id → contract：行 %r 引用了不存在的行身份 %r"
                % (ch["id"], ctid)))
    return _cap_violations(violations)


def _compute_bills(data):
    """由完整三表数据重算全部账单（初始化/复核用）。

    返回 {(payer_id, period): [net_cents, {invoice_no: 明细数}, 明细数]}。
    """
    contract_by_id = {c["id"]: c for c in data["contract"]}
    bills = {}
    for ch in data["charge"]:
        ctid = ch["contract_id"]
        if ctid is None:
            continue
        contract = contract_by_id.get(ctid)
        if contract is None:
            raise InternalError("仓表不合法：明细 %r 的合同 %r 不存在"
                                % (ch["id"], ctid))
        pid = contract["payer_id"]
        if pid is None:
            continue
        key = (pid, ch["period"])
        acc = bills.get(key)
        if acc is None:
            acc = bills[key] = [0, {}, 0]
        acc[0] += ch["cents"]
        inv = ch["invoice_no"]
        acc[1][inv] = acc[1].get(inv, 0) + 1
        acc[2] += 1
    return bills


# ---------------------------------------------------------------------------
# 仓库
# ---------------------------------------------------------------------------

class Repository(object):
    """本地仓库：三表仓 + 当前账单 + 各版本变更，存于 目录/billing.db。

    每个 Repository 持有一个 SQLite 连接，不是线程安全的；
    并发发布请让每个线程/进程各自 Repository.open 同一目录，
    数据库写锁保证一个版本只被一批发布推进。
    """

    def __init__(self, path):
        if os.path.isdir(path):
            db = os.path.join(path, DB_NAME)
        else:
            db = path
            path = os.path.dirname(os.path.abspath(db))
        if not os.path.isfile(db):
            raise NotARepository("不是已初始化的仓库: %s" % path)
        self.path = path
        self.db_path = db
        conn = sqlite3.connect(db, timeout=10.0)
        conn.isolation_level = None  # 显式管理事务
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'format'").fetchone()
        except sqlite3.Error:
            conn.close()
            raise NotARepository("不是已初始化的仓库: %s" % path)
        if not row or row[0] != FORMAT:
            conn.close()
            raise NotARepository("仓库格式不符: %s" % path)
        self._conn = conn
        self._stats = defaultdict(int)

    # -- 生命周期 ----------------------------------------------------------

    @classmethod
    def open(cls, path):
        return cls(path)

    @classmethod
    def init(cls, path, rows_doc, batch_id=None):
        """用完整三表数据初始化工作库及版本 0 的账单（全量扫描底稿，允许）。"""
        data = _validate_dataset(rows_doc)
        violations = _find_dataset_violations(data)
        if violations:
            raise Refusal("invalid_initial_state", violations)
        if batch_id is not None:
            if isinstance(batch_id, bool) or not isinstance(batch_id, (str, int)):
                raise InputError([{"path": "$.batch_id",
                                   "message": "batch_id 必须是字符串或数字"}])
            batch_id = str(batch_id)
        if os.path.exists(path):
            if not os.path.isdir(path) or os.listdir(path):
                raise RepositoryExists("目标位置已存在仓库或非空目录: %s" % path)
        else:
            os.makedirs(path)
        db = os.path.join(path, DB_NAME)
        conn = sqlite3.connect(db, timeout=10.0)
        conn.isolation_level = None
        try:
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            for tname in TABLES:
                for row in data[tname]:
                    _insert_row(conn, tname, row)
            bills = _compute_bills(data)
            for (pid, period) in sorted(bills):
                net, invs, ccnt = bills[(pid, period)]
                conn.execute(
                    "INSERT INTO bill(payer_id, period, net_cents,"
                    " charge_count, invoice_count) VALUES (?,?,?,?,?)",
                    (pid, period, net, ccnt, len(invs)))
                for inv in sorted(invs):
                    conn.execute(
                        "INSERT INTO bill_invoice(payer_id, period,"
                        " invoice_no, charge_count) VALUES (?,?,?,?)",
                        (pid, period, inv, invs[inv]))
            for (pid, period) in sorted(bills):
                net, invs, _ = bills[(pid, period)]
                conn.execute(
                    "INSERT INTO version_change(version, payer_id, period,"
                    " kind, net_cents, invoice_count) VALUES (?,?,?,?,?,?)",
                    (0, pid, period, "upsert", net, len(invs)))
            conn.execute(
                "INSERT INTO version_log(version, batch_id, corrections,"
                " change_count) VALUES (?,?,?,?)",
                (0, batch_id if batch_id is not None else "init", "[]",
                 len(bills)))
            conn.execute("INSERT INTO meta(key, value) VALUES ('format', ?)",
                         (FORMAT,))
            conn.execute("INSERT INTO meta(key, value) VALUES ('version', '0')")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()
            try:
                os.unlink(db)
            except OSError:
                pass
            raise
        conn.close()
        return cls(path)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- 底层读取（统计读取行数，用于核对发布读取范围） ----------------------

    def _rows(self, table, sql, params=()):
        cur = self._conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
        self._stats[table] += len(out)
        return out

    def _row(self, table, rid):
        rows = self._rows(table, 'SELECT * FROM "%s" WHERE id = ?' % table,
                          (rid,))
        return rows[0] if rows else None

    def _contracts_with_code(self, code):
        return self._rows("contract",
                          "SELECT * FROM contract WHERE code = ?", (code,))

    def _contracts_of_payer(self, pid):
        return self._rows("contract",
                          "SELECT * FROM contract WHERE payer_id = ?", (pid,))

    def _charges_of_contract(self, cid):
        return self._rows("charge",
                          "SELECT * FROM charge WHERE contract_id = ?", (cid,))

    def _bill_row(self, pid, period):
        rows = self._rows(
            "bill",
            "SELECT net_cents, charge_count, invoice_count FROM bill"
            " WHERE payer_id = ? AND period = ?", (pid, period))
        if not rows:
            return None
        r = rows[0]
        return (r["net_cents"], r["charge_count"], r["invoice_count"])

    def _invoice_count(self, pid, period, inv):
        rows = self._rows(
            "bill_invoice",
            "SELECT charge_count FROM bill_invoice"
            " WHERE payer_id = ? AND period = ? AND invoice_no = ?",
            (pid, period, inv))
        return rows[0]["charge_count"] if rows else 0

    # -- 版本与视图 ----------------------------------------------------------

    @property
    def version(self):
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'version'").fetchone()
        return int(row[0])

    def current_bills(self):
        """带版本的当前账单视图（读取整个当前账单视图不受读取范围限制）。"""
        rows = self._rows(
            "bill",
            "SELECT payer_id, period, net_cents, invoice_count FROM bill"
            " ORDER BY payer_id, period")
        return {"status": "ok", "version": self.version,
                "bills": [{"payer_id": r["payer_id"], "period": r["period"],
                           "net_cents": r["net_cents"],
                           "invoice_count": r["invoice_count"]}
                          for r in rows]}

    def changes(self, version):
        """重新读取某次成功发布的账单变更；旧版本的变更不可改写。"""
        if isinstance(version, bool) or not isinstance(version, int):
            raise InputError([{"path": "$.version",
                               "message": "版本号必须是整数"}])
        current = self.version
        if version < 0 or version > current:
            raise UnknownVersion(version, current)
        log = self._conn.execute(
            "SELECT batch_id, corrections FROM version_log WHERE version = ?",
            (version,)).fetchone()
        rows = self._conn.execute(
            "SELECT payer_id, period, kind, net_cents, invoice_count"
            " FROM version_change WHERE version = ?"
            " ORDER BY payer_id, period", (version,)).fetchall()
        changes = []
        for pid, period, kind, net, icnt in rows:
            if kind == "upsert":
                changes.append({"kind": "upsert", "payer_id": pid,
                                "period": period, "net_cents": net,
                                "invoice_count": icnt})
            else:
                changes.append({"kind": "revoke", "payer_id": pid,
                                "period": period})
        return {"status": "ok", "version": version,
                "base_version": version - 1,
                "batch_id": log[0],
                "corrections": json.loads(log[1]),
                "changes": changes}

    def dump_warehouse(self):
        """导出当前仓表（核对用，全量读取）。"""
        return {"status": "ok", "version": self.version,
                "rows": {t: self._rows(t, 'SELECT * FROM "%s" ORDER BY id' % t)
                         for t in TABLES}}

    def verify(self):
        """复核仓库：仓表约束、账单重算、变更回放、版本链连续性。"""
        checks = []
        data = {t: self._rows(t, 'SELECT * FROM "%s"' % t) for t in TABLES}
        violations = _find_dataset_violations(data)
        checks.append({"name": "warehouse_constraints",
                       "ok": not violations,
                       "violations": violations[:20]})

        computed = _compute_bills(data)
        stored_bills = {}
        for r in self._rows("bill", "SELECT * FROM bill"):
            stored_bills[(r["payer_id"], r["period"])] = (
                r["net_cents"], r["charge_count"], r["invoice_count"])
        stored_inv = {}
        for r in self._rows("bill_invoice", "SELECT * FROM bill_invoice"):
            stored_inv[(r["payer_id"], r["period"], r["invoice_no"])] = \
                r["charge_count"]
        calc_bills = {}
        calc_inv = {}
        for key, (net, invs, ccnt) in computed.items():
            calc_bills[key] = (net, ccnt, len(invs))
            for inv, cnt in invs.items():
                calc_inv[key + (inv,)] = cnt
        common_bills = set(stored_bills) & set(calc_bills)
        common_invs = set(stored_inv) & set(calc_inv)
        mismatch = (sorted(set(stored_bills) ^ set(calc_bills))
                    + sorted(k for k in common_bills
                             if stored_bills[k] != calc_bills[k])
                    + sorted(set(stored_inv) ^ set(calc_inv))
                    + sorted(k for k in common_invs
                             if stored_inv[k] != calc_inv[k]))
        checks.append({"name": "bills_recomputed",
                       "ok": stored_bills == calc_bills
                       and stored_inv == calc_inv,
                       "bills": len(stored_bills),
                       "mismatch_keys": [list(k) for k in mismatch[:10]]})

        current = self.version
        replay = {}
        for v in range(current + 1):
            for ch in self.changes(v)["changes"]:
                key = (ch["payer_id"], ch["period"])
                if ch["kind"] == "upsert":
                    replay[key] = (ch["net_cents"], ch["invoice_count"])
                else:
                    replay.pop(key, None)
        view = {k: (v[0], v[2]) for k, v in stored_bills.items()}
        checks.append({"name": "changes_replay",
                       "ok": replay == view,
                       "versions_replayed": current + 1})

        logs = [r[0] for r in self._conn.execute(
            "SELECT version FROM version_log ORDER BY version")]
        checks.append({"name": "version_chain",
                       "ok": logs == list(range(current + 1)),
                       "versions": len(logs)})
        ok = all(c["ok"] for c in checks)
        return {"status": "ok" if ok else "error",
                "version": current, "checks": checks}

    # -- 发布 --------------------------------------------------------------

    def publish(self, base_version, corrections, batch_id=None,
                _crash_hook=None):
        """校验并发布一批更正；成功返回结果 dict，失败抛业务异常并完整回滚。

        _crash_hook 仅供测试：在整批落库后、提交前调用，用于模拟进程崩溃。
        """
        if isinstance(base_version, bool) or not isinstance(base_version, int):
            raise InputError([{"path": "$.base_version",
                               "message": "base_version 必须是整数"}])
        if batch_id is not None:
            if isinstance(batch_id, bool) or not isinstance(batch_id, (str, int)):
                raise InputError([{"path": "$.batch_id",
                                   "message": "batch_id 必须是字符串或数字"}])
            batch_id = str(batch_id)
        corr_objs = _validate_corrections(corrections)

        conn = self._conn
        self._stats = defaultdict(int)
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.version
            if base_version != current:
                raise VersionConflict(current)

            # 存在性核对（只读本批行），并缓存本批行的改前状态
            pre_rows = {}
            problems = []
            for c in corr_objs:
                if c.op == "add":
                    if self._row(c.table, c.rid) is not None:
                        problems.append({
                            "path": "$.corrections",
                            "message": "新增的行身份已存在: %s %r"
                                       % (c.table, c.rid)})
                else:
                    row = self._row(c.table, c.rid)
                    if row is None:
                        problems.append({
                            "path": "$.corrections",
                            "message": "更正的行不存在: %s %r" % (c.table, c.rid)})
                    else:
                        pre_rows[(c.table, c.rid)] = row
            if problems:
                raise InputError(problems)

            # 前后归属可能改变的明细：本批直接更正的明细 +
            # payer_id 发生变化的合同名下的全部明细（走 contract_id 索引，
            # 不扫描无关合同的明细）
            affected = {}
            charge_corr = {}
            contract_corr = {}
            for c in corr_objs:
                if c.table == "charge":
                    charge_corr[c.rid] = c
                    affected[c.rid] = pre_rows.get(("charge", c.rid))
                elif c.table == "contract":
                    contract_corr[c.rid] = c
            for c in corr_objs:
                if c.table == "contract" and c.op == "replace":
                    old = pre_rows[("contract", c.rid)]
                    if old["payer_id"] != c.row["payer_id"]:
                        for row in self._charges_of_contract(c.rid):
                            affected.setdefault(row["id"], row)

            # 归属链所需的合同：本批的合同 + 受影响明细改前/改后引用的合同
            needed = set(contract_corr)
            for cid, pre in affected.items():
                if pre is not None and pre["contract_id"] is not None:
                    needed.add(pre["contract_id"])
                cc = charge_corr.get(cid)
                if cc is not None and cc.op != "delete":
                    post_ct = cc.row["contract_id"]
                    if post_ct is not None:
                        needed.add(post_ct)
            contracts = {}
            for cid in needed:
                if ("contract", cid) in pre_rows:
                    contracts[cid] = pre_rows[("contract", cid)]
                else:
                    row = self._row("contract", cid)
                    if row is not None:
                        contracts[cid] = row

            # 整批落库（之后统一核对约束，合法换号不受写入顺序影响）
            for c in corr_objs:
                if c.op == "add":
                    _insert_row(conn, c.table, c.row)
                elif c.op == "delete":
                    conn.execute('DELETE FROM "%s" WHERE id = ?' % c.table,
                                 (c.rid,))
                else:
                    _update_row(conn, c.table, c.row)
            if _crash_hook is not None:
                _crash_hook()

            # 整批后的仓表约束核对（范围限于本批行及其关联行/占用记录）
            violations = self._batch_violations(corr_objs)
            if violations:
                raise Refusal("invalid_batch_state", violations)

            # 账单增量套用与变更生成交付
            changes, touched = self._apply_billing(
                affected, charge_corr, contract_corr, contracts)

            new_version = current + 1
            conn.execute(
                "INSERT INTO version_log(version, batch_id, corrections,"
                " change_count) VALUES (?,?,?,?)",
                (new_version, batch_id,
                 json.dumps([c.to_json() for c in corr_objs],
                            ensure_ascii=False), len(changes)))
            for ch in changes:
                conn.execute(
                    "INSERT INTO version_change(version, payer_id, period,"
                    " kind, net_cents, invoice_count) VALUES (?,?,?,?,?,?)",
                    (new_version, ch["payer_id"], ch["period"], ch["kind"],
                     ch.get("net_cents"), ch.get("invoice_count")))
            conn.execute("UPDATE meta SET value = ? WHERE key = 'version'",
                         (str(new_version),))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return {
            "status": "ok",
            "version": new_version,
            "base_version": current,
            "batch_id": batch_id,
            "changes": changes,
            "summary": {
                "corrections": len(corr_objs),
                "affected_charges": len(affected),
                "touched_bills": touched,
                "delivered_changes": len(changes),
            },
            # 本次发布读取的业务行数（按表），用于核对读取范围
            "reads": {t: self._stats.get(t, 0) for t in _STATS_TABLES},
        }

    def _batch_violations(self, corr_objs):
        """整批落库后的约束核对；只读取本批行及其关联行/占用记录。"""
        violations = []
        ext_of = {(c.table, c.rid): c.ext for c in corr_objs}

        def take(v):
            if len(violations) < MAX_VIOLATIONS:
                violations.append(v)

        # 唯一键 contract(code)：核对本批新增/替换合同的占用情况
        for c in corr_objs:
            if c.table == "contract" and c.op in ("add", "replace"):
                code = c.row["code"]
                for other in self._contracts_with_code(code):
                    if other["id"] == c.rid:
                        continue
                    take({
                        "constraint": "unique_key", "table": "contract",
                        "key_columns": ["code"], "key_value": [code],
                        "rows": [
                            {"table": "contract", "id": c.rid,
                             "correction": c.ext, "role": "takes_key"},
                            {"table": "contract", "id": other["id"],
                             "correction": ext_of.get(("contract",
                                                       other["id"])),
                             "role": "holds_key"},
                        ],
                        "message": "唯一键 contract(code) 取值 %r 冲突" % code,
                    })
        # 外键：本批新增/替换行的引用目标必须存在（整批后的状态）
        for c in corr_objs:
            if c.op not in ("add", "replace"):
                continue
            if c.table == "contract":
                pid = c.row["payer_id"]
                if pid is not None and self._row("payer", pid) is None:
                    take(_fk_violation(
                        "contract", "payer_id", "payer", c.rid, pid,
                        "外键 contract.payer_id → payer：更正 %s 使行 %r "
                        "引用了不存在的行身份 %r" % (c.ext, c.rid, pid),
                        ext=c.ext))
            elif c.table == "charge":
                ctid = c.row["contract_id"]
                if ctid is not None and self._row("contract", ctid) is None:
                    take(_fk_violation(
                        "charge", "contract_id", "contract", c.rid, ctid,
                        "外键 charge.contract_id → contract：更正 %s 使行 %r "
                        "引用了不存在的行身份 %r" % (c.ext, c.rid, ctid),
                        ext=c.ext))
        # 外键：本批删除的被引用行，整批后不得仍有引用
        for c in corr_objs:
            if c.op != "delete":
                continue
            if c.table == "payer":
                refs = self._contracts_of_payer(c.rid)
                for ref in refs:
                    take(_fk_violation(
                        "contract", "payer_id", "payer", ref["id"], c.rid,
                        "外键 contract.payer_id → payer：更正 %s 删除了行 %r，"
                        "但行 %r 仍引用它" % (c.ext, c.rid, ref["id"]),
                        ext=ext_of.get(("contract", ref["id"]))))
            elif c.table == "contract":
                refs = self._charges_of_contract(c.rid)
                for ref in refs:
                    take(_fk_violation(
                        "charge", "contract_id", "contract", ref["id"], c.rid,
                        "外键 charge.contract_id → contract：更正 %s 删除了行 "
                        "%r，但行 %r 仍引用它" % (c.ext, c.rid, ref["id"]),
                        ext=ext_of.get(("charge", ref["id"]))))
        return _cap_violations(violations)

    def _apply_billing(self, affected, charge_corr, contract_corr, contracts):
        """按增量套用账单，返回 (交付的变更列表, 触及的账单数)。

        每条受影响明细分别计算改前/改后贡献（payer_id, period, invoice_no,
        cents），在各自状态只计一次；按账单键聚合增量后套用到 bill /
        bill_invoice，不重算整张账单。
        """
        deltas = {}
        for cid, pre in affected.items():
            cc = charge_corr.get(cid)
            if cc is None:
                post = pre
            elif cc.op == "delete":
                post = None
            else:
                post = cc.row
            pre_c = _contribution(pre, contracts, contract_corr, True)
            post_c = _contribution(post, contracts, contract_corr, False)
            if pre_c == post_c:
                continue
            if pre_c is not None:
                _add_delta(deltas, pre_c, -1)
            if post_c is not None:
                _add_delta(deltas, post_c, +1)

        changes = []
        conn = self._conn
        for key in sorted(deltas):
            pid, period = key
            cents_d, count_d, inv_d = deltas[key]
            old = self._bill_row(pid, period)
            old_net, old_ccnt, old_icnt = old if old else (0, 0, 0)
            net = old_net + cents_d
            ccnt = old_ccnt + count_d
            icnt = old_icnt
            if ccnt < 0:
                raise InternalError("账单 %r 的明细数出现负值" % (key,))
            for inv in sorted(inv_d):
                d = inv_d[inv]
                if d == 0:
                    continue
                cur = self._invoice_count(pid, period, inv)
                new = cur + d
                if new < 0:
                    raise InternalError("账单 %r 票号 %r 的明细数出现负值"
                                        % (key, inv))
                if new == 0:
                    conn.execute(
                        "DELETE FROM bill_invoice WHERE payer_id = ?"
                        " AND period = ? AND invoice_no = ?",
                        (pid, period, inv))
                    icnt -= 1
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO bill_invoice(payer_id, period,"
                        " invoice_no, charge_count) VALUES (?,?,?,?)",
                        (pid, period, inv, new))
                    if cur == 0:
                        icnt += 1
            if ccnt == 0:
                # 不再有归属明细：账单撤销
                conn.execute(
                    "DELETE FROM bill WHERE payer_id = ? AND period = ?",
                    (pid, period))
                rest = conn.execute(
                    "SELECT COUNT(*) FROM bill_invoice WHERE payer_id = ?"
                    " AND period = ?", (pid, period)).fetchone()[0]
                if rest:
                    raise InternalError("账单 %r 撤销后仍残留票号状态" % (key,))
                if old is not None:
                    changes.append({"kind": "revoke", "payer_id": pid,
                                    "period": period})
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO bill(payer_id, period, net_cents,"
                    " charge_count, invoice_count) VALUES (?,?,?,?,?)",
                    (pid, period, net, ccnt, icnt))
                if old is None:
                    changes.append({"kind": "upsert", "payer_id": pid,
                                    "period": period, "net_cents": net,
                                    "invoice_count": icnt})
                elif (old_net, old_icnt) != (net, icnt):
                    changes.append({"kind": "upsert", "payer_id": pid,
                                    "period": period, "net_cents": net,
                                    "invoice_count": icnt})
                # 账单值未变：不交付
        return changes, len(deltas)


def _contribution(row, contracts, contract_corr, pre_state):
    """一条明细在指定状态下的账单贡献；链上任一引用为空则不计入（None）。"""
    if row is None:
        return None
    ctid = row["contract_id"]
    if ctid is None:
        return None
    if pre_state:
        contract = contracts.get(ctid)
    else:
        corr = contract_corr.get(ctid)
        if corr is not None:
            contract = None if corr.op == "delete" else corr.row
        else:
            contract = contracts.get(ctid)
    if contract is None:
        raise InternalError("归属链合同 %r 缺失（前置校验应已拒绝）" % (ctid,))
    pid = contract["payer_id"]
    if pid is None:
        return None
    return (pid, row["period"], row["invoice_no"], row["cents"])


def _add_delta(deltas, contribution, sign):
    pid, period, inv, cents = contribution
    key = (pid, period)
    acc = deltas.get(key)
    if acc is None:
        acc = deltas[key] = [0, 0, {}]
    acc[0] += sign * cents
    acc[1] += sign
    acc[2][inv] = acc[2].get(inv, 0) + sign


def _insert_row(conn, table, row):
    if table == "payer":
        conn.execute("INSERT INTO payer(id) VALUES (?)", (row["id"],))
    elif table == "contract":
        conn.execute(
            "INSERT INTO contract(id, code, payer_id) VALUES (?,?,?)",
            (row["id"], row["code"], row["payer_id"]))
    else:
        conn.execute(
            "INSERT INTO charge(id, contract_id, period, invoice_no, cents)"
            " VALUES (?,?,?,?,?)",
            (row["id"], row["contract_id"], row["period"],
             row["invoice_no"], row["cents"]))


def _update_row(conn, table, row):
    if table == "payer":
        pass  # payer 只有行身份，整行替换无可变字段
    elif table == "contract":
        conn.execute(
            "UPDATE contract SET code = ?, payer_id = ? WHERE id = ?",
            (row["code"], row["payer_id"], row["id"]))
    else:
        conn.execute(
            "UPDATE charge SET contract_id = ?, period = ?, invoice_no = ?,"
            " cents = ? WHERE id = ?",
            (row["contract_id"], row["period"], row["invoice_no"],
             row["cents"], row["id"]))


# ---------------------------------------------------------------------------
# 面向 JSON 的入口：返回 (结果dict, 退出码)，不抛业务异常
#
# 退出码：0 正常；2 输入不合法；3 仓表不合法被拒绝（未发布）；
# 4 版本不符（未发布）；1 其他错误。
# ---------------------------------------------------------------------------

def _input_error_result(e):
    return {"status": "error", "kind": "invalid_input",
            "message": "输入不合法", "problems": e.problems}, 2


def _refusal_result(e):
    msg = ("初始仓表不满足约束，拒绝初始化"
           if e.kind == "invalid_initial_state"
           else "应用整批后仓表不满足约束，已回滚，未产生新版本")
    return {"status": "error", "kind": e.kind,
            "message": msg, "violations": e.violations}, 3


def _locked_result(e):
    return {"status": "error", "kind": "locked",
            "message": "仓库正被其他发布占用，请稍后重试: %s" % e}, 1


def run_init(path, doc):
    try:
        if not isinstance(doc, dict):
            raise InputError([{"path": "$", "message": "输入必须是 JSON 对象"}])
        repo = Repository.init(path, doc.get("rows", {}),
                               batch_id=doc.get("batch_id"))
    except InputError as e:
        return _input_error_result(e)
    except Refusal as e:
        return _refusal_result(e)
    except RepositoryExists as e:
        return {"status": "error", "kind": "repository_exists",
                "message": str(e)}, 2
    except sqlite3.OperationalError as e:
        return _locked_result(e)
    try:
        ch = repo.changes(0)
        counts = {t: repo._conn.execute(
            'SELECT COUNT(*) FROM "%s"' % t).fetchone()[0] for t in TABLES}
        return {"status": "ok", "version": 0,
                "changes": ch["changes"],
                "summary": {"payers": counts["payer"],
                            "contracts": counts["contract"],
                            "charges": counts["charge"],
                            "bills": len(ch["changes"])}}, 0
    finally:
        repo.close()


def run_publish(path, doc):
    try:
        if not isinstance(doc, dict):
            raise InputError([{"path": "$", "message": "输入必须是 JSON 对象"}])
        if "corrections" not in doc:
            raise InputError([{"path": "$.corrections",
                               "message": "缺少 corrections 数组"}])
        repo = Repository.open(path)
    except InputError as e:
        return _input_error_result(e)
    except NotARepository as e:
        return {"status": "error", "kind": "not_a_repository",
                "message": str(e)}, 1
    try:
        return repo.publish(doc.get("base_version"), doc["corrections"],
                            batch_id=doc.get("batch_id")), 0
    except InputError as e:
        return _input_error_result(e)
    except VersionConflict as e:
        return {"status": "error", "kind": "version_conflict",
                "message": "期望基准版本与当前版本不符，未发布",
                "current_version": e.current_version}, 4
    except Refusal as e:
        return _refusal_result(e)
    except sqlite3.OperationalError as e:
        return _locked_result(e)
    finally:
        repo.close()


def run_bills(path):
    try:
        repo = Repository.open(path)
    except NotARepository as e:
        return {"status": "error", "kind": "not_a_repository",
                "message": str(e)}, 1
    try:
        return repo.current_bills(), 0
    finally:
        repo.close()


def run_changes(path, version):
    try:
        repo = Repository.open(path)
    except NotARepository as e:
        return {"status": "error", "kind": "not_a_repository",
                "message": str(e)}, 1
    try:
        return repo.changes(version), 0
    except InputError as e:
        return _input_error_result(e)
    except UnknownVersion as e:
        return {"status": "error", "kind": "unknown_version",
                "message": str(e),
                "current_version": e.current_version}, 2
    finally:
        repo.close()


def run_verify(path):
    try:
        repo = Repository.open(path)
    except NotARepository as e:
        return {"status": "error", "kind": "not_a_repository",
                "message": str(e)}, 1
    try:
        result = repo.verify()
        return result, 0 if result["status"] == "ok" else 1
    finally:
        repo.close()


def run_dump(path):
    try:
        repo = Repository.open(path)
    except NotARepository as e:
        return {"status": "error", "kind": "not_a_repository",
                "message": str(e)}, 1
    try:
        return repo.dump_warehouse(), 0
    finally:
        repo.close()
