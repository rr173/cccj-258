# -*- coding: utf-8 -*-
"""corrsplit.billing 测试套件。运行：python3 tests/test_billing.py"""
import json
import multiprocessing
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from corrsplit import billing  # noqa: E402
from corrsplit.billing import Repository  # noqa: E402
from corrsplit.core import InputError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def init_rows():
    """与 examples/billing_init.json 相同的样例数据。"""
    return {
        "payer": [{"id": 1}, {"id": 2}],
        "contract": [
            {"id": 10, "code": "C-A", "payer_id": 1},
            {"id": 11, "code": "C-B", "payer_id": 1},
            {"id": 12, "code": "C-C", "payer_id": 2},
            {"id": 13, "code": "C-D", "payer_id": None},
        ],
        "charge": [
            {"id": 100, "contract_id": 10, "period": "2026-01",
             "invoice_no": "INV-1", "cents": 1000},
            {"id": 101, "contract_id": 11, "period": "2026-01",
             "invoice_no": "INV-1", "cents": 500},
            {"id": 102, "contract_id": 11, "period": "2026-01",
             "invoice_no": "INV-2", "cents": -200},
            {"id": 103, "contract_id": 12, "period": "2026-01",
             "invoice_no": "INV-9", "cents": 800},
            {"id": 104, "contract_id": 10, "period": "2026-02",
             "invoice_no": "INV-3", "cents": 300},
            {"id": 105, "contract_id": None, "period": "2026-01",
             "invoice_no": "INV-X", "cents": 999},
            {"id": 106, "contract_id": 13, "period": "2026-01",
             "invoice_no": "INV-Y", "cents": 777},
        ],
    }


def repl(cid, table, rid, row):
    return {"id": cid, "op": "replace", "table": table, "row_id": rid,
            "row": row}


def add(cid, table, row):
    return {"id": cid, "op": "add", "table": table, "row": row}


def delete(cid, table, rid):
    return {"id": cid, "op": "delete", "table": table, "row_id": rid}


def bills_view(repo):
    return {(b["payer_id"], b["period"]): (b["net_cents"], b["invoice_count"])
            for b in repo.current_bills()["bills"]}


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="corrsplit-billing-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "repo")

    def init_repo(self, rows=None):
        return Repository.init(self.path, rows if rows is not None
                               else init_rows())


class TestInit(RepoTestCase):
    def test_init_version0_bills_and_changes(self):
        repo = self.init_repo()
        self.assertEqual(repo.version, 0)
        # 同号明细跨合同只计一次票号；负数保留；空链不计入
        self.assertEqual(bills_view(repo), {
            (1, "2026-01"): (1300, 2),
            (1, "2026-02"): (300, 1),
            (2, "2026-01"): (800, 1),
        })
        ch = repo.changes(0)
        self.assertEqual(ch["base_version"], -1)
        self.assertEqual(
            {(c["payer_id"], c["period"]): (c["net_cents"], c["invoice_count"])
             for c in ch["changes"]},
            {(1, "2026-01"): (1300, 2), (1, "2026-02"): (300, 1),
             (2, "2026-01"): (800, 1)})
        self.assertTrue(all(c["kind"] == "upsert" for c in ch["changes"]))
        self.assertEqual(repo.verify()["status"], "ok")
        repo.close()

    def test_init_rejects_constraint_violations(self):
        rows = init_rows()
        rows["contract"].append({"id": 14, "code": "C-A", "payer_id": 1})
        with self.assertRaises(billing.Refusal) as cm:
            self.init_repo(rows)
        self.assertEqual(cm.exception.kind, "invalid_initial_state")
        self.assertEqual(cm.exception.violations[0]["constraint"],
                         "unique_key")
        rows = init_rows()
        rows["charge"].append({"id": 199, "contract_id": 99,
                               "period": "2026-01", "invoice_no": "I",
                               "cents": 1})
        with self.assertRaises(billing.Refusal) as cm:
            self.init_repo(rows)
        self.assertEqual(cm.exception.violations[0]["constraint"],
                         "foreign_key")
        # 失败后不留仓库
        self.assertFalse(os.path.exists(os.path.join(self.path, "billing.db")))

    def test_init_rejects_malformed_rows(self):
        rows = init_rows()
        rows["charge"][0]["period"] = "2026-13"
        with self.assertRaises(InputError):
            self.init_repo(rows)
        rows = init_rows()
        rows["contract"][0]["code"] = ""
        with self.assertRaises(InputError):
            self.init_repo(rows)
        rows = init_rows()
        rows["charge"][0]["cents"] = True
        with self.assertRaises(InputError):
            self.init_repo(rows)
        rows = init_rows()
        rows["charge"][0]["invoice_no"] = ""
        with self.assertRaises(InputError):
            self.init_repo(rows)

    def test_init_existing_repo_rejected(self):
        repo = self.init_repo()
        repo.close()
        with self.assertRaises(billing.RepositoryExists):
            self.init_repo()

    def test_open_not_a_repository(self):
        with self.assertRaises(billing.NotARepository):
            Repository.open(self.path)


class TestPublish(RepoTestCase):
    def test_publish_delivers_changes_and_reread(self):
        repo = self.init_repo()
        with open(os.path.join(ROOT, "examples", "billing_batch1.json"),
                  encoding="utf-8") as f:
            batch = json.load(f)
        result = repo.publish(batch["base_version"], batch["corrections"],
                              batch_id=batch["batch_id"])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["base_version"], 0)
        expect = [
            {"kind": "upsert", "payer_id": 1, "period": "2026-01",
             "net_cents": 2200, "invoice_count": 3},
            {"kind": "upsert", "payer_id": 1, "period": "2026-02",
             "net_cents": 1000, "invoice_count": 1},
            {"kind": "revoke", "payer_id": 2, "period": "2026-01"},
        ]
        self.assertEqual(result["changes"], expect)
        # 当前账单同步生效
        self.assertEqual(bills_view(repo), {
            (1, "2026-01"): (2200, 3), (1, "2026-02"): (1000, 1)})
        # 按版本重读该次变更，内容一致
        again = repo.changes(1)
        self.assertEqual(again["changes"], expect)
        self.assertEqual(again["batch_id"], "batch-001")
        self.assertEqual(len(again["corrections"]), 3)
        # 仓表也已更正
        dump = repo.dump_warehouse()["rows"]
        c12 = [c for c in dump["contract"] if c["id"] == 12][0]
        self.assertEqual(c12["payer_id"], 1)
        c102 = [c for c in dump["charge"] if c["id"] == 102][0]
        self.assertEqual(c102["cents"], -100)
        self.assertEqual(repo.verify()["status"], "ok")
        repo.close()

    def test_version_conflict_rejected(self):
        repo = self.init_repo()
        with self.assertRaises(billing.VersionConflict) as cm:
            repo.publish(3, [delete("d", "charge", 100)])
        self.assertEqual(cm.exception.current_version, 0)
        self.assertEqual(repo.version, 0)
        repo.close()

    def test_invalid_batch_rolls_back_everything(self):
        repo = self.init_repo()
        before_bills = repo.current_bills()["bills"]
        before_rows = repo.dump_warehouse()["rows"]
        with self.assertRaises(billing.Refusal) as cm:
            repo.publish(0, [delete("del_c10", "contract", 10)])
        self.assertEqual(cm.exception.kind, "invalid_batch_state")
        v = cm.exception.violations[0]
        self.assertEqual(v["constraint"], "foreign_key")
        self.assertEqual(v["referenced_id"], 10)
        # 版本、账单、仓表均无变化
        self.assertEqual(repo.version, 0)
        self.assertEqual(repo.current_bills()["bills"], before_bills)
        self.assertEqual(repo.dump_warehouse()["rows"], before_rows)
        with self.assertRaises(billing.UnknownVersion):
            repo.changes(1)
        repo.close()

    def test_code_swap_same_batch_not_rejected(self):
        # 批内合法换号：整批落库后再核对，不受逐条写入中间状态影响
        repo = self.init_repo()
        result = repo.publish(0, [
            repl("swap_a", "contract", 10,
                 {"id": 10, "code": "C-B", "payer_id": 1}),
            repl("swap_b", "contract", 11,
                 {"id": 11, "code": "C-A", "payer_id": 1}),
        ])
        self.assertEqual(result["version"], 1)
        # 换号不影响账单：无变更交付的成功版本
        self.assertEqual(result["changes"], [])
        self.assertEqual(bills_view(repo), {
            (1, "2026-01"): (1300, 2), (1, "2026-02"): (300, 1),
            (2, "2026-01"): (800, 1)})
        dump = repo.dump_warehouse()["rows"]
        codes = {c["id"]: c["code"] for c in dump["contract"]}
        self.assertEqual(codes[10], "C-B")
        self.assertEqual(codes[11], "C-A")
        repo.close()

    def test_batch_internal_dependencies(self):
        # 同批新增 payer → contract → charge，整批后核对，链内引用合法
        repo = self.init_repo()
        result = repo.publish(0, [
            add("ap", "payer", {"id": 3}),
            add("ac", "contract", {"id": 20, "code": "C-E", "payer_id": 3}),
            add("ah", "charge", {"id": 200, "contract_id": 20,
                                 "period": "2026-01", "invoice_no": "INV-7",
                                 "cents": 50}),
        ])
        self.assertEqual(result["changes"], [
            {"kind": "upsert", "payer_id": 3, "period": "2026-01",
             "net_cents": 50, "invoice_count": 1}])
        repo.close()

    def test_repoint_migrates_uncorrected_charges(self):
        repo = self.init_repo()
        # 只改合同归属，未直接更正的明细 103 随之迁移
        result = repo.publish(0, [
            repl("mv", "contract", 12,
                 {"id": 12, "code": "C-C", "payer_id": 1}),
        ])
        self.assertEqual(result["summary"]["affected_charges"], 1)
        self.assertEqual(result["changes"], [
            {"kind": "upsert", "payer_id": 1, "period": "2026-01",
             "net_cents": 2100, "invoice_count": 3},
            {"kind": "revoke", "payer_id": 2, "period": "2026-01"},
        ])
        repo.close()

    def test_repoint_and_edit_same_charge_once_per_state(self):
        repo = self.init_repo()
        # 同批既改合同归属又改该明细金额：改前在旧付款方计一次（1000），
        # 改后在新付款方计一次（1500），不重复、不遗漏；
        # 合同 10 上未直接更正的明细 104 也一并迁出
        result = repo.publish(0, [
            repl("mv", "contract", 10,
                 {"id": 10, "code": "C-A", "payer_id": 2}),
            repl("fix", "charge", 100,
                 {"id": 100, "contract_id": 10, "period": "2026-01",
                  "invoice_no": "INV-1", "cents": 1500}),
        ])
        self.assertEqual(result["changes"], [
            {"kind": "upsert", "payer_id": 1, "period": "2026-01",
             "net_cents": 300, "invoice_count": 2},
            {"kind": "revoke", "payer_id": 1, "period": "2026-02"},
            {"kind": "upsert", "payer_id": 2, "period": "2026-01",
             "net_cents": 2300, "invoice_count": 2},
            {"kind": "upsert", "payer_id": 2, "period": "2026-02",
             "net_cents": 300, "invoice_count": 1},
        ])
        repo.close()

    def test_invoice_dedup_across_contracts(self):
        repo = self.init_repo()
        # (1,2026-01) 的 INV-1 来自合同 10、11 两条明细：票号只计一次
        self.assertEqual(bills_view(repo)[(1, "2026-01")], (1300, 2))
        # 删掉其中一条，票号仍在（另一条还引用），金额减少
        result = repo.publish(0, [delete("d", "charge", 100)])
        self.assertEqual(result["changes"], [
            {"kind": "upsert", "payer_id": 1, "period": "2026-01",
             "net_cents": 300, "invoice_count": 2}])
        # 再删掉另一条，INV-1 消失，票数减一
        result = repo.publish(1, [delete("d", "charge", 101)])
        self.assertEqual(result["changes"], [
            {"kind": "upsert", "payer_id": 1, "period": "2026-01",
             "net_cents": -200, "invoice_count": 1}])
        repo.close()

    def test_zero_net_bill_still_exists(self):
        repo = self.init_repo()
        result = repo.publish(0, [
            add("a", "charge", {"id": 200, "contract_id": 10,
                                "period": "2026-03", "invoice_no": "I1",
                                "cents": 500}),
            add("b", "charge", {"id": 201, "contract_id": 10,
                                "period": "2026-03", "invoice_no": "I2",
                                "cents": -500}),
        ])
        # 净额为零但仍有归属明细：账单存在并交付
        self.assertEqual(result["changes"], [
            {"kind": "upsert", "payer_id": 1, "period": "2026-03",
             "net_cents": 0, "invoice_count": 2}])
        self.assertIn((1, "2026-03"), bills_view(repo))
        # 全部删去后才撤销
        result = repo.publish(1, [delete("a", "charge", 200),
                                  delete("b", "charge", 201)])
        self.assertEqual(result["changes"], [
            {"kind": "revoke", "payer_id": 1, "period": "2026-03"}])
        self.assertNotIn((1, "2026-03"), bills_view(repo))
        repo.close()

    def test_revoke_delivers_only_key(self):
        repo = self.init_repo()
        result = repo.publish(0, [delete("d", "charge", 103)])
        revoke = [c for c in result["changes"] if c["kind"] == "revoke"]
        self.assertEqual(revoke, [{"kind": "revoke", "payer_id": 2,
                                   "period": "2026-01"}])
        self.assertNotIn("net_cents", revoke[0])
        self.assertNotIn("invoice_count", revoke[0])
        repo.close()

    def test_null_chain_not_billed(self):
        repo = self.init_repo()
        # 空链明细（105 无合同、106 的合同无付款方）的更正不产生账单变更
        result = repo.publish(0, [
            repl("r", "charge", 105,
                 {"id": 105, "contract_id": None, "period": "2026-01",
                  "invoice_no": "INV-X", "cents": 1}),
            delete("d", "charge", 106),
        ])
        self.assertEqual(result["changes"], [])
        # 空链 → 挂到付款方：进入账单
        result = repo.publish(1, [
            repl("r", "charge", 105,
                 {"id": 105, "contract_id": 10, "period": "2026-01",
                  "invoice_no": "INV-X", "cents": 1}),
        ])
        self.assertEqual(result["changes"], [
            {"kind": "upsert", "payer_id": 1, "period": "2026-01",
             "net_cents": 1301, "invoice_count": 3}])
        repo.close()

    def test_unchanged_bill_not_delivered(self):
        repo = self.init_repo()
        # 两条明细同账单同票号对调金额：净额与票数均未变
        result = repo.publish(0, [
            repl("r1", "charge", 100,
                 {"id": 100, "contract_id": 10, "period": "2026-01",
                  "invoice_no": "INV-1", "cents": 500}),
            repl("r2", "charge", 101,
                 {"id": 101, "contract_id": 11, "period": "2026-01",
                  "invoice_no": "INV-1", "cents": 1000}),
        ])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["changes"], [])
        self.assertEqual(bills_view(repo)[(1, "2026-01")], (1300, 2))
        repo.close()

    def test_empty_batch_allowed(self):
        repo = self.init_repo()
        result = repo.publish(0, [])
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["changes"], [])
        self.assertEqual(repo.changes(1)["changes"], [])
        repo.close()

    def test_payer_delete_requires_contract_handling(self):
        repo = self.init_repo()
        # 删付款方但合同仍引用：拒绝
        with self.assertRaises(billing.Refusal):
            repo.publish(0, [delete("dp", "payer", 2)])
        # 同批把合同改挂到付款方 1 后即可删除
        result = repo.publish(0, [
            repl("mv", "contract", 12,
                 {"id": 12, "code": "C-C", "payer_id": 1}),
            delete("dp", "payer", 2),
        ])
        self.assertEqual(result["version"], 1)
        dump = repo.dump_warehouse()["rows"]
        self.assertEqual([p["id"] for p in dump["payer"]], [1])
        repo.close()

    def test_old_changes_immutable(self):
        repo = self.init_repo()
        repo.publish(0, [delete("d", "charge", 103)])
        before = repo.changes(1)
        repo.publish(1, [delete("d2", "charge", 104)])
        self.assertEqual(repo.changes(1), before)
        self.assertEqual(repo.version, 2)
        repo.close()
        # 数据库层面也不允许改写历史变更
        conn = sqlite3.connect(os.path.join(self.path, "billing.db"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE version_change SET net_cents = 0")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM version_log")
        conn.close()

    def test_unknown_version(self):
        repo = self.init_repo()
        with self.assertRaises(billing.UnknownVersion) as cm:
            repo.changes(1)
        self.assertEqual(cm.exception.current_version, 0)
        with self.assertRaises(billing.UnknownVersion):
            repo.changes(-1)
        repo.close()


class TestInputValidation(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.init_repo()
        self.addCleanup(self.repo.close)

    def assert_input_error(self, corrections, needle=None):
        version = self.repo.version
        with self.assertRaises(InputError) as cm:
            self.repo.publish(version, corrections)
        if needle is not None:
            self.assertIn(needle, json.dumps(cm.exception.problems,
                                             ensure_ascii=False))
        self.assertEqual(self.repo.version, version)

    def test_bad_op(self):
        self.assert_input_error([{"id": "x", "op": "upsert", "table": "payer",
                                  "row_id": 1}], "op")

    def test_unknown_table(self):
        self.assert_input_error([{"id": "x", "op": "delete", "table": "ghost",
                                  "row_id": 1}], "未声明的表")

    def test_duplicate_correction_same_row(self):
        self.assert_input_error([delete("a", "charge", 100),
                                 delete("b", "charge", 100)], "多条更正")

    def test_add_existing_identity(self):
        self.assert_input_error([add("a", "payer", {"id": 1})], "已存在")

    def test_delete_missing_row(self):
        self.assert_input_error([delete("d", "charge", 999)], "不存在")

    def test_replace_changes_identity(self):
        self.assert_input_error([
            repl("r", "charge", 100,
                 {"id": 101, "contract_id": 10, "period": "2026-01",
                  "invoice_no": "I", "cents": 1})], "行身份")

    def test_bad_period(self):
        self.assert_input_error([
            repl("r", "charge", 100,
                 {"id": 100, "contract_id": 10, "period": "2026-1",
                  "invoice_no": "I", "cents": 1})], "YYYY-MM")

    def test_null_period_rejected(self):
        self.assert_input_error([
            repl("r", "charge", 100,
                 {"id": 100, "contract_id": 10, "period": None,
                  "invoice_no": "I", "cents": 1})], "period")

    def test_null_cents_rejected(self):
        self.assert_input_error([
            repl("r", "charge", 100,
                 {"id": 100, "contract_id": 10, "period": "2026-01",
                  "invoice_no": "I", "cents": None})], "cents")

    def test_empty_code_rejected(self):
        self.assert_input_error([
            repl("r", "contract", 10,
                 {"id": 10, "code": "", "payer_id": 1})], "code")

    def test_missing_field(self):
        self.assert_input_error([
            repl("r", "charge", 100,
                 {"id": 100, "contract_id": 10, "period": "2026-01",
                  "cents": 1})], "缺少字段")

    def test_unknown_field(self):
        self.assert_input_error([
            repl("r", "charge", 100,
                 {"id": 100, "contract_id": 10, "period": "2026-01",
                  "invoice_no": "I", "cents": 1, "zz": 2})], "未声明的字段")

    def test_base_version_type(self):
        with self.assertRaises(InputError):
            self.repo.publish("0", [])


class TestScopedReads(RepoTestCase):
    """小批发布不得整仓重读、不得扫描无关合同的明细、不得重算整张账单。"""

    def init_large_repo(self, n_contracts=300, charges_per_contract=100):
        rows = {"payer": [{"id": p} for p in range(1, 21)],
                "contract": [], "charge": []}
        cid = 1000
        hid = 100000
        for p in range(1, 21):
            for _ in range(n_contracts // 20):
                rows["contract"].append(
                    {"id": cid, "code": "C%d" % cid, "payer_id": p})
                for j in range(charges_per_contract):
                    rows["charge"].append(
                        {"id": hid, "contract_id": cid,
                         "period": "2026-%02d" % (j % 3 + 1),
                         "invoice_no": "INV-%d" % (hid % 50),
                         "cents": j})
                    hid += 1
                cid += 1
        return self.init_repo(rows)

    def test_charge_replace_reads_only_scoped_rows(self):
        repo = self.init_large_repo()
        result = repo.publish(0, [
            repl("r", "charge", 100000,
                 {"id": 100000, "contract_id": 1000, "period": "2026-01",
                  "invoice_no": "INV-32", "cents": 7})])
        reads = result["reads"]
        self.assertLessEqual(reads["charge"], 2)    # 本批行改前状态
        self.assertLessEqual(reads["contract"], 2)  # 归属链合同 + 外键核对
        self.assertEqual(reads["payer"], 0)         # 本批不涉及 payer 核对
        self.assertLessEqual(reads["bill"], 1)      # 只读触及的账单
        self.assertLessEqual(reads["bill_invoice"], 1)
        self.assertEqual(result["summary"]["affected_charges"], 1)
        repo.close()

    def test_contract_repoint_reads_only_own_charges(self):
        repo = self.init_large_repo(charges_per_contract=10)
        # 合同 1000 有 10 条明细；改其归属只读这 10 条，不扫描其他合同
        result = repo.publish(0, [
            repl("mv", "contract", 1000,
                 {"id": 1000, "code": "C1000", "payer_id": 2})])
        self.assertEqual(result["reads"]["charge"], 10)
        self.assertEqual(result["summary"]["affected_charges"], 10)
        repo.close()

    def test_code_only_replace_reads_no_charges(self):
        repo = self.init_large_repo()
        result = repo.publish(0, [
            repl("rc", "contract", 1000,
                 {"id": 1000, "code": "C1000-X", "payer_id": 1})])
        self.assertEqual(result["reads"]["charge"], 0)
        self.assertEqual(result["reads"]["bill"], 0)
        self.assertEqual(result["changes"], [])
        repo.close()


class TestConcurrency(RepoTestCase):
    def test_concurrent_publish_exactly_one_wins(self):
        repo = self.init_repo()
        repo.close()
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(batch):
            try:
                r = Repository.open(self.path)
                barrier.wait(timeout=10)
                result = r.publish(0, batch)
                outcomes.append(("ok", result["version"]))
            except billing.VersionConflict as e:
                outcomes.append(("conflict", e.current_version))
            finally:
                r.close()

        batch_a = [delete("da", "charge", 103)]
        batch_b = [delete("db", "charge", 104)]
        threads = [threading.Thread(target=worker, args=(b,))
                   for b in (batch_a, batch_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(o[0] for o in outcomes), ["conflict", "ok"])
        # 一个版本只被一批发布推进：胜出版本为 1，另一批未生效
        repo = Repository.open(self.path)
        self.assertEqual(repo.version, 1)
        self.assertEqual(repo.verify()["status"], "ok")
        repo.close()


def _crash_worker(path, batch):
    repo = Repository.open(path)
    # 整批落库后、提交前“进程崩溃”
    repo.publish(0, batch, _crash_hook=lambda: os._exit(1))


@unittest.skipUnless(hasattr(os, "fork"), "需要 fork")
class TestCrashSafety(RepoTestCase):
    def test_crash_mid_publish_leaves_no_partial_data(self):
        repo = self.init_repo()
        before_bills = repo.current_bills()["bills"]
        before_rows = repo.dump_warehouse()["rows"]
        repo.close()
        batch = [delete("d", "charge", 103),
                 repl("r", "contract", 10,
                      {"id": 10, "code": "C-Z", "payer_id": 1})]
        p = multiprocessing.Process(target=_crash_worker,
                                    args=(self.path, batch))
        p.start()
        p.join(30)
        self.assertEqual(p.exitcode, 1)
        # 崩溃后重新打开：看不到一半的新数据
        repo = Repository.open(self.path)
        self.assertEqual(repo.version, 0)
        self.assertEqual(repo.current_bills()["bills"], before_bills)
        self.assertEqual(repo.dump_warehouse()["rows"], before_rows)
        self.assertEqual(repo.verify()["status"], "ok")
        # 仓库仍可正常发布
        result = repo.publish(0, batch)
        self.assertEqual(result["version"], 1)
        repo.close()


class TestVerify(RepoTestCase):
    def test_verify_detects_tamper(self):
        repo = self.init_repo()
        repo.publish(0, [delete("d", "charge", 103)])
        self.assertEqual(repo.verify()["status"], "ok")
        repo.close()
        conn = sqlite3.connect(os.path.join(self.path, "billing.db"))
        conn.execute("UPDATE bill SET net_cents = net_cents + 1")
        conn.commit()
        conn.close()
        repo = Repository.open(self.path)
        result = repo.verify()
        self.assertEqual(result["status"], "error")
        by_name = {c["name"]: c for c in result["checks"]}
        self.assertFalse(by_name["bills_recomputed"]["ok"])
        self.assertFalse(by_name["changes_replay"]["ok"])
        repo.close()


# ---------------------------------------------------------------------------
# 随机等价测试：小批发布 vs 全量重算参考实现
# ---------------------------------------------------------------------------

PERIODS = ["2026-01", "2026-02", "2026-03"]
INVOICES = ["INV-1", "INV-2", "INV-3", "INV-4"]


def ref_bills(state):
    out = {}
    for ch in state["charge"].values():
        ctid = ch["contract_id"]
        if ctid is None:
            continue
        pid = state["contract"][ctid]["payer_id"]
        if pid is None:
            continue
        key = (pid, ch["period"])
        acc = out.get(key)
        if acc is None:
            acc = out[key] = [0, set()]
        acc[0] += ch["cents"]
        acc[1].add(ch["invoice_no"])
    return {k: (v[0], len(v[1])) for k, v in out.items()}


def ref_diff(prev, new):
    out = []
    for key in sorted(set(prev) | set(new)):
        if key not in prev:
            out.append({"kind": "upsert", "payer_id": key[0],
                        "period": key[1], "net_cents": new[key][0],
                        "invoice_count": new[key][1]})
        elif key not in new:
            out.append({"kind": "revoke", "payer_id": key[0],
                        "period": key[1]})
        elif prev[key] != new[key]:
            out.append({"kind": "upsert", "payer_id": key[0],
                        "period": key[1], "net_cents": new[key][0],
                        "invoice_count": new[key][1]})
    return out


def apply_batch(state, batch):
    for c in batch:
        t = c["table"]
        if c["op"] == "add":
            state[t][c["row"]["id"]] = dict(c["row"])
        elif c["op"] == "delete":
            del state[t][c["row_id"]]
        else:
            state[t][c["row_id"]] = dict(c["row"])


def gen_valid_batch(rng, state, next_ids):
    """构造性生成合法批次（不依赖被测代码校验）。"""
    batch = []
    used = set()            # (table, id) 已有更正的行
    deleted_contracts = set()
    deleted_payers = set()
    batch_charge_refs = set()    # 本批明细改后引用的合同
    batch_contract_refs = set()  # 本批合同改后引用的付款方
    taken_codes = {c["code"] for c in state["contract"].values()}

    def fresh_id(table):
        next_ids[table] += 1
        return next_ids[table]

    def avail_payers():
        return [p for p in state["payer"]
                if p not in deleted_payers and ("payer", p) not in used]

    def avail_contracts():
        return [c for c in state["contract"]
                if c not in deleted_contracts]

    ops = (["charge_replace"] * 3 + ["charge_add"] * 2 + ["charge_delete"] * 2
           + ["contract_repoint"] * 2 + ["contract_recode", "code_swap",
              "contract_add", "contract_delete", "payer_add", "payer_delete"])
    for _ in range(rng.randint(1, 7)):
        op = rng.choice(ops)
        if op == "charge_replace":
            cand = [c for c in state["charge"] if ("charge", c) not in used]
            if not cand:
                continue
            cid = rng.choice(cand)
            old = state["charge"][cid]
            ct = rng.choice([None] + avail_contracts())
            batch.append(repl("rc%d" % cid, "charge", cid,
                              {"id": cid, "contract_id": ct,
                               "period": rng.choice(PERIODS),
                               "invoice_no": rng.choice(INVOICES),
                               "cents": rng.randint(-500, 1500)}))
            used.add(("charge", cid))
            if ct is not None:
                batch_charge_refs.add(ct)
        elif op == "charge_add":
            cid = fresh_id("charge")
            ct = rng.choice([None] + avail_contracts())
            batch.append(add("ac%d" % cid, "charge",
                             {"id": cid, "contract_id": ct,
                              "period": rng.choice(PERIODS),
                              "invoice_no": rng.choice(INVOICES),
                              "cents": rng.randint(-500, 1500)}))
            used.add(("charge", cid))
            if ct is not None:
                batch_charge_refs.add(ct)
        elif op == "charge_delete":
            cand = [c for c in state["charge"] if ("charge", c) not in used]
            if not cand:
                continue
            cid = rng.choice(cand)
            batch.append(delete("dc%d" % cid, "charge", cid))
            used.add(("charge", cid))
        elif op == "contract_repoint":
            cand = [c for c in avail_contracts()
                    if ("contract", c) not in used]
            if not cand:
                continue
            cid = rng.choice(cand)
            old = state["contract"][cid]
            pid = rng.choice([None] + avail_payers())
            batch.append(repl("rp%d" % cid, "contract", cid,
                              {"id": cid, "code": old["code"],
                               "payer_id": pid}))
            used.add(("contract", cid))
            if pid is not None:
                batch_contract_refs.add(pid)
        elif op == "contract_recode":
            cand = [c for c in avail_contracts()
                    if ("contract", c) not in used]
            if not cand:
                continue
            cid = rng.choice(cand)
            old = state["contract"][cid]
            code = "N%d" % fresh_id("code")
            batch.append(repl("rk%d" % cid, "contract", cid,
                              {"id": cid, "code": code,
                               "payer_id": old["payer_id"]}))
            used.add(("contract", cid))
            taken_codes.discard(old["code"])
            taken_codes.add(code)
        elif op == "code_swap":
            cand = [c for c in avail_contracts()
                    if ("contract", c) not in used]
            if len(cand) < 2:
                continue
            a, b = rng.sample(cand, 2)
            ca, cb = state["contract"][a], state["contract"][b]
            batch.append(repl("sa%d" % a, "contract", a,
                              {"id": a, "code": cb["code"],
                               "payer_id": ca["payer_id"]}))
            batch.append(repl("sb%d" % b, "contract", b,
                              {"id": b, "code": ca["code"],
                               "payer_id": cb["payer_id"]}))
            used.add(("contract", a))
            used.add(("contract", b))
        elif op == "contract_add":
            cid = fresh_id("contract")
            code = "N%d" % fresh_id("code")
            pid = rng.choice([None] + avail_payers())
            batch.append(add("an%d" % cid, "contract",
                             {"id": cid, "code": code, "payer_id": pid}))
            used.add(("contract", cid))
            taken_codes.add(code)
            if pid is not None:
                batch_contract_refs.add(pid)
        elif op == "contract_delete":
            cand = [c for c in avail_contracts()
                    if ("contract", c) not in used
                    and c not in batch_charge_refs]
            rng.shuffle(cand)
            done = False
            for cid in cand:
                refs = [h for h in state["charge"]
                        if state["charge"][h]["contract_id"] == cid]
                if any(("charge", h) in used for h in refs):
                    continue
                for h in refs:
                    batch.append(delete("dx%d" % h, "charge", h))
                    used.add(("charge", h))
                batch.append(delete("dt%d" % cid, "contract", cid))
                used.add(("contract", cid))
                deleted_contracts.add(cid)
                taken_codes.discard(state["contract"][cid]["code"])
                done = True
                break
            if not done:
                continue
        elif op == "payer_add":
            pid = fresh_id("payer")
            batch.append(add("ap%d" % pid, "payer", {"id": pid}))
            used.add(("payer", pid))
        elif op == "payer_delete":
            cand = [p for p in state["payer"]
                    if ("payer", p) not in used
                    and p not in batch_contract_refs
                    and not any(c["payer_id"] == p
                                for c in state["contract"].values()
                                if c["id"] not in deleted_contracts)]
            if not cand:
                continue
            pid = rng.choice(cand)
            batch.append(delete("dp%d" % pid, "payer", pid))
            used.add(("payer", pid))
            deleted_payers.add(pid)
    return batch


def gen_invalid_batch(rng, state):
    options = []
    for c in state["contract"].values():
        if any(ch["contract_id"] == c["id"]
               for ch in state["charge"].values()):
            options.append([delete("bad_dc", "contract", c["id"])])
            break
    for p in state["payer"].values():
        if any(c["payer_id"] == p["id"]
               for c in state["contract"].values()):
            options.append([delete("bad_dp", "payer", p["id"])])
            break
    contracts = list(state["contract"].values())
    if len(contracts) >= 2:
        a, b = contracts[0], contracts[1]
        options.append([repl("bad_code", "contract", a["id"],
                             {"id": a["id"], "code": b["code"],
                              "payer_id": a["payer_id"]})])
    options.append([add("bad_fk", "charge",
                        {"id": 999999, "contract_id": 888888,
                         "period": "2026-01", "invoice_no": "X",
                         "cents": 1})])
    return rng.choice(options)


class TestRandomEquivalence(RepoTestCase):
    def test_random_batches_match_full_recompute(self):
        rng = random.Random(20261124)
        state = {"payer": {}, "contract": {}, "charge": {}}
        for i in range(1, 4):
            state["payer"][i] = {"id": i}
        for i, cid in enumerate(range(10, 16)):
            state["contract"][cid] = {
                "id": cid, "code": "C%02d" % i,
                "payer_id": rng.choice([None, 1, 2, 3])}
        for cid in range(100, 112):
            state["charge"][cid] = {
                "id": cid,
                "contract_id": rng.choice([None] + list(state["contract"])),
                "period": rng.choice(PERIODS),
                "invoice_no": rng.choice(INVOICES),
                "cents": rng.randint(-500, 1500)}
        rows = {t: [dict(r) for r in state[t].values()] for t in
                ("payer", "contract", "charge")}
        repo = self.init_repo(rows)
        next_ids = {"payer": 1000, "contract": 1000, "charge": 1000,
                    "code": 0}
        prev = ref_bills(state)
        version = 0
        for rnd in range(80):
            if rnd % 7 == 3:
                # 非法批次：拒绝且不留痕迹
                bad = gen_invalid_batch(rng, state)
                with self.assertRaises(billing.Refusal,
                                       msg="round %d: %s" % (rnd, bad)):
                    repo.publish(version, bad)
                self.assertEqual(repo.version, version)
                self.assertEqual(bills_view(repo), prev)
                continue
            batch = gen_valid_batch(rng, state, next_ids)
            result = repo.publish(version, batch)
            apply_batch(state, batch)
            new = ref_bills(state)
            expect = ref_diff(prev, new)
            self.assertEqual(result["changes"], expect,
                             msg="round %d batch %s" % (rnd, batch))
            self.assertEqual(result["version"], version + 1)
            self.assertEqual(bills_view(repo), new)
            # 该次变更可重读且一致
            self.assertEqual(repo.changes(version + 1)["changes"], expect)
            # 仓表与参考模型一致
            dump = repo.dump_warehouse()["rows"]
            for t in ("payer", "contract", "charge"):
                self.assertEqual({r["id"]: r for r in dump[t]}, state[t],
                                 msg="round %d table %s" % (rnd, t))
            prev = new
            version += 1
            if rnd % 10 == 9:
                self.assertEqual(repo.verify()["status"], "ok")
        self.assertEqual(repo.verify()["status"], "ok")
        repo.close()


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

class TestCli(RepoTestCase):
    def run_cli(self, *argv, stdin=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "corrsplit"] + list(argv),
            capture_output=True, text=True, cwd=ROOT, env=env, input=stdin)

    def test_full_cli_flow(self):
        ex = lambda n: os.path.join("examples", n)
        p = self.run_cli("init", self.path, ex("billing_init.json"))
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["version"], 0)
        self.assertEqual(len(out["changes"]), 3)

        p = self.run_cli("publish", self.path, ex("billing_batch1.json"))
        self.assertEqual(p.returncode, 0, p.stderr)
        out1 = json.loads(p.stdout)
        self.assertEqual(out1["version"], 1)
        self.assertEqual(len(out1["changes"]), 3)

        # 版本不符：退出码 4
        p = self.run_cli("publish", self.path, ex("billing_batch1.json"))
        self.assertEqual(p.returncode, 4)
        self.assertEqual(json.loads(p.stdout)["kind"], "version_conflict")

        p = self.run_cli("publish", self.path, ex("billing_batch2.json"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["version"], 2)

        # 整批后仓表不合法：退出码 3，未产生新版本
        p = self.run_cli("publish", self.path,
                         ex("billing_batch_invalid.json"))
        self.assertEqual(p.returncode, 3)
        out = json.loads(p.stdout)
        self.assertEqual(out["kind"], "invalid_batch_state")
        self.assertTrue(out["violations"])

        p = self.run_cli("bills", self.path)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["version"], 2)
        self.assertEqual(
            {(b["payer_id"], b["period"]):
             (b["net_cents"], b["invoice_count"]) for b in out["bills"]},
            {(1, "2026-01"): (2200, 3), (1, "2026-03"): (250, 1)})

        # 重读历史变更：与发布时交付的一致
        p = self.run_cli("changes", self.path, "1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["changes"], out1["changes"])

        p = self.run_cli("changes", self.path, "9")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["kind"], "unknown_version")

        p = self.run_cli("verify", self.path)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "ok")

        p = self.run_cli("dump", self.path)
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = json.loads(p.stdout)["rows"]
        self.assertEqual(len(rows["payer"]), 2)
        self.assertEqual(len(rows["charge"]), 7)

    def test_cli_stdin_and_not_a_repo(self):
        with open(os.path.join(ROOT, "examples", "billing_init.json"),
                  encoding="utf-8") as f:
            p = self.run_cli("init", self.path, "-", stdin=f.read())
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_cli("bills", os.path.join(self.tmp, "ghost"))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(json.loads(p.stdout)["kind"], "not_a_repository")

    def test_analysis_commands_still_work(self):
        # 原有只读分析入口继续可用
        p = self.run_cli("analyze", "examples/renumber_swap.json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["summary"]
                         ["must_batch_groups"], 1)
        p = self.run_cli("check", "examples/master_detail.json", "del_m2")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(json.loads(p.stdout)["valid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
