# -*- coding: utf-8 -*-
"""本地收款对账测试。运行：python3 tests/test_reconciliation.py"""

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from corrsplit.billing import ReceiptConflict, Repository, VersionConflict  # noqa: E402
from corrsplit.core import InputError  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rows():
    return {
        "payer": [{"id": 1}, {"id": 2}],
        "contract": [
            {"id": 10, "code": "C1", "payer_id": 1},
            {"id": 11, "code": "C2", "payer_id": 1},
            {"id": 12, "code": "C3", "payer_id": 2},
        ],
        "charge": [
            {"id": 100, "contract_id": 10, "period": "2026-03",
             "invoice_no": "INV-MULT", "cents": 1000},
            {"id": 101, "contract_id": 11, "period": "2026-03",
             "invoice_no": "INV-MULT", "cents": 500},
            {"id": 102, "contract_id": 10, "period": "2026-03",
             "invoice_no": "INV-PART", "cents": 900},
            {"id": 103, "contract_id": 11, "period": "2026-03",
             "invoice_no": "INV-OVER", "cents": 300},
            {"id": 104, "contract_id": 12, "period": "2026-03",
             "invoice_no": "INV-UNPAID", "cents": 250},
            {"id": 105, "contract_id": 10, "period": "2026-03",
             "invoice_no": "INV-ZERO", "cents": 400},
            {"id": 106, "contract_id": 11, "period": "2026-03",
             "invoice_no": "INV-ZERO", "cents": -400},
            {"id": 107, "contract_id": 10, "period": "2026-03",
             "invoice_no": "DUP", "cents": 100},
            {"id": 108, "contract_id": 12, "period": "2026-03",
             "invoice_no": "DUP", "cents": 100},
            {"id": 109, "contract_id": 10, "period": "2026-04",
             "invoice_no": "DUP", "cents": 100},
        ],
    }


def receipt(no, pid=1, period="2026-03", inv="INV-MULT", cents=1):
    return {"receipt_no": no, "payer_id": pid, "period": period,
            "invoice_no": inv, "cents": cents}


def all_receipts():
    return [
        receipt("R1", inv="INV-MULT", cents=1000),
        receipt("R2", inv="INV-MULT", cents=500),
        receipt("R3", inv="INV-PART", cents=400),
        receipt("R4", inv="INV-OVER", cents=500),
        receipt("R5", inv="DUP", cents=100),
        receipt("R6", pid=999, inv="GHOST", cents=50),
        receipt("R7", pid=2, inv="DUP", cents=100),
    ]


class ReconTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="corrsplit-recon-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "repo")
        self.repo = Repository.init(self.path, rows())

    def tearDown(self):
        self.repo.close()

    def map_rows(self, report):
        return {(r["payer_id"], r["period"], r["invoice_no"]): r
                for r in report["items"]}

    def test_statuses_amounts_zero_invoice_and_complete_key(self):
        result = self.repo.import_receipts(0, all_receipts())
        self.assertEqual(result["inserted_count"], 7)

        report = self.repo.reconcile()
        m = self.map_rows(report)

        mult = m[(1, "2026-03", "INV-MULT")]
        self.assertEqual((mult["receivable_cents"], mult["received_cents"],
                          mult["outstanding_cents"], mult["status"]),
                         (1500, 1500, 0, "settled"))
        self.assertEqual(mult["receipt_count"], 2)

        part = m[(1, "2026-03", "INV-PART")]
        self.assertEqual((part["receivable_cents"], part["received_cents"],
                          part["outstanding_cents"], part["status"]),
                         (900, 400, 500, "partial"))

        over = m[(1, "2026-03", "INV-OVER")]
        self.assertEqual((over["receivable_cents"], over["received_cents"],
                          over["outstanding_cents"], over["status"]),
                         (300, 500, -200, "overpaid"))

        unpaid = m[(2, "2026-03", "INV-UNPAID")]
        self.assertEqual((unpaid["receivable_cents"], unpaid["received_cents"],
                          unpaid["outstanding_cents"], unpaid["status"]),
                         (250, 0, 250, "unpaid"))

        zero = m[(1, "2026-03", "INV-ZERO")]
        self.assertTrue(zero["invoice_exists"])
        self.assertEqual((zero["receivable_cents"], zero["received_cents"],
                          zero["outstanding_cents"], zero["status"]),
                         (0, 0, 0, "unpaid"))

        # 相同 invoice_no 在不同付款方/月份下是三个独立键。
        p1mar = m[(1, "2026-03", "DUP")]
        p2mar = m[(2, "2026-03", "DUP")]
        p1apr = m[(1, "2026-04", "DUP")]
        self.assertEqual(p1mar["status"], "settled")
        self.assertEqual(p2mar["status"], "settled")
        self.assertEqual(p1apr["status"], "unpaid")
        self.assertEqual(p1mar["receivable_cents"], 100)
        self.assertEqual(p2mar["receivable_cents"], 100)
        self.assertEqual(p1apr["receivable_cents"], 100)

        ghost = m[(999, "2026-03", "GHOST")]
        self.assertEqual(ghost["status"], "unmatched")
        self.assertFalse(ghost["invoice_exists"])
        self.assertEqual(ghost["received_cents"], 50)
        self.assertEqual(len(report["unmatched_receipts"]), 1)
        self.assertEqual(report["unmatched_receipts"][0]["receipt_no"], "R6")

        # 常见别名字段可直接读取。
        self.assertEqual(part["due_cents"], 900)
        self.assertEqual(part["paid_cents"], 400)
        self.assertEqual(part["balance_cents"], 500)
        self.assertEqual(part["month"], "2026-03")
        self.assertEqual(part["status"], "partial_payment")
        self.assertEqual(mult["status"], "paid")

    def test_duplicate_receipt_is_idempotent(self):
        self.repo.import_receipts(0, all_receipts())
        before = self.repo.reconcile()["summary"]
        again = self.repo.import_receipts(
            {"bill_version": 0, "receipts": all_receipts()})
        self.assertEqual(again["inserted_count"], 0)
        self.assertEqual(again["duplicate_count"], 7)
        after = self.repo.reconcile()["summary"]
        self.assertEqual(before["received_cents_total"],
                         after["received_cents_total"])
        self.assertEqual(after["receipt_total"], 7)

    def test_conflicting_existing_receipt_rejects_whole_batch(self):
        self.repo.import_receipts(0, all_receipts())
        bad = [
            receipt("R1", inv="INV-MULT", cents=1001),  # 同号不同金额
            receipt("RNEW", inv="INV-PART", cents=11),
        ]
        with self.assertRaises(ReceiptConflict) as cm:
            self.repo.import_receipts(0, bad)
        self.assertEqual(cm.exception.conflicts[0]["receipt_no"], "R1")
        numbers = {r["receipt_no"]
                   for r in self.repo.list_receipts()["receipts"]}
        self.assertNotIn("RNEW", numbers)
        self.assertEqual(len(numbers), 7)
        m = self.map_rows(self.repo.reconcile())
        self.assertEqual(m[(1, "2026-03", "INV-MULT")]["received_cents"], 1500)

    def test_duplicate_id_conflict_inside_one_batch_is_atomic(self):
        bad = [receipt("X", cents=10), receipt("X", cents=20)]
        with self.assertRaises(ReceiptConflict):
            self.repo.import_receipts(0, bad)
        self.assertEqual(self.repo.list_receipts()["receipts"], [])

    def test_wrong_bill_version_rejected(self):
        with self.assertRaises(VersionConflict) as cm:
            self.repo.import_receipts(1, [receipt("X")])
        self.assertEqual(cm.exception.current_version, 0)
        self.assertEqual(self.repo.list_receipts()["receipts"], [])

    def test_malformed_receipts_do_not_write(self):
        bad_batches = [
            [receipt("")],
            [receipt(True)],
            [receipt("X", pid="1")],
            [receipt("X", period="2026-13")],
            [receipt("X", inv="")],
            [receipt("X", cents=0)],
            [receipt("X", cents=-3)],
            [receipt("X", cents=True)],
        ]
        for bad in bad_batches:
            with self.assertRaises(InputError):
                self.repo.import_receipts(0, bad)
        self.assertEqual(self.repo.list_receipts()["receipts"], [])

    def test_bill_corrections_update_view_but_not_receipts(self):
        self.repo.import_receipts(0, all_receipts())
        self.repo.publish(0, [
            {"id": "move_c3", "op": "replace", "table": "contract",
             "row_id": 12,
             "row": {"id": 12, "code": "C3", "payer_id": 1}},
            {"id": "change_over", "op": "replace", "table": "charge",
             "row_id": 103,
             "row": {"id": 103, "contract_id": 11, "period": "2026-03",
                     "invoice_no": "INV-OVER", "cents": 600}},
        ])
        report = self.repo.reconcile()
        m = self.map_rows(report)

        # 原 payer=2 的 DUP 发票随合同迁走；回执仍在 payer=2，变为未匹配。
        p2 = m[(2, "2026-03", "DUP")]
        self.assertEqual(p2["status"], "unmatched")
        self.assertEqual(p2["received_cents"], 100)
        p1 = m[(1, "2026-03", "DUP")]
        self.assertEqual(p1["receivable_cents"], 200)
        self.assertEqual(p1["received_cents"], 100)
        self.assertEqual(p1["status"], "partial")

        # 应收金额变化后，原“超收”可变为部分付款。
        over = m[(1, "2026-03", "INV-OVER")]
        self.assertEqual(over["receivable_cents"], 600)
        self.assertEqual(over["status"], "partial")

        original_r7 = [r for r in self.repo.list_receipts()["receipts"]
                       if r["receipt_no"] == "R7"][0]
        self.assertEqual((original_r7["payer_id"], original_r7["period"],
                          original_r7["invoice_no"], original_r7["cents"]),
                         (2, "2026-03", "DUP", 100))

        # 发票消失后，款项仍留在原键并显示未匹配。
        self.repo.publish(1, [
            {"id": "del_over", "op": "delete", "table": "charge", "row_id": 103}
        ])
        m = self.map_rows(self.repo.reconcile())
        ghost_over = m[(1, "2026-03", "INV-OVER")]
        self.assertEqual(ghost_over["status"], "unmatched")
        self.assertEqual(ghost_over["received_cents"], 500)
        self.assertEqual(len(self.repo.list_receipts()["receipts"]), 7)

    def test_old_repository_open_migrates_receipt_tables(self):
        # 模拟旧库：删除新增表后仍可打开并获得幂等的本地收款能力。
        self.repo.close()
        import sqlite3
        conn = sqlite3.connect(self.path + "/billing.db")
        conn.execute("DROP TRIGGER receipt_no_update")
        conn.execute("DROP TRIGGER receipt_no_delete")
        conn.execute("DROP INDEX idx_receipt_key")
        conn.execute("DROP TABLE receipt")
        conn.commit()
        conn.close()

        repo = Repository.open(self.path)
        repo.import_receipts(0, [receipt("M1")])
        self.assertEqual(repo.reconcile()["summary"]["receipt_total"], 1)
        repo.close()
        self.repo = Repository.open(self.path)


def _crash_worker(path, batch):
    repo = Repository.open(path)
    repo.import_receipts(0, batch, _crash_hook=lambda: os._exit(1))


@unittest.skipUnless(hasattr(os, "fork"), "需要 fork")
class ReceiptCrashSafety(ReconTestCase):
    def test_crash_before_commit_leaves_no_receipts(self):
        batch = all_receipts()[:3]
        self.repo.close()
        p = multiprocessing.Process(target=_crash_worker,
                                    args=(self.path, batch))
        p.start()
        p.join(30)
        self.assertEqual(p.exitcode, 1)

        self.repo = Repository.open(self.path)
        self.assertEqual(self.repo.list_receipts()["receipts"], [])
        report = self.repo.reconcile()
        self.assertEqual(report["summary"]["receipt_total"], 0)
        result = self.repo.import_receipts(0, batch)
        self.assertEqual(result["inserted_count"], 3)


class ReceiptCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="corrsplit-recon-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "repo")

    def run_cli(self, *argv):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "corrsplit"] + list(argv),
            cwd=ROOT, env=env, capture_output=True, text=True)

    def test_cli_import_reconcile_conflict_and_version_codes(self):
        examples = os.path.join(ROOT, "examples")
        p = self.run_cli("init", self.path,
                         os.path.join(examples, "billing_init.json"))
        self.assertEqual(p.returncode, 0, p.stderr)

        p = self.run_cli("import-receipts", self.path,
                         os.path.join(examples, "receipts_batch.json"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["inserted_count"], 5)

        p = self.run_cli("receipts", self.path,
                         os.path.join(examples, "receipts_batch.json"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["inserted_count"], 0)

        p = self.run_cli("reconcile", self.path)
        self.assertEqual(p.returncode, 0, p.stderr)
        report = json.loads(p.stdout)
        self.assertEqual(report["bill_version"], 0)
        self.assertEqual(report["summary"]["unmatched_receipt_count"], 1)
        # JSON 兼容别名默认展开。
        self.assertIn("paid_cents", p.stdout)
        self.assertIn("month", p.stdout)

        p = self.run_cli("import-receipts", self.path,
                         os.path.join(examples, "receipts_conflict.json"))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["kind"], "receipt_conflict")

        p = self.run_cli("publish", self.path,
                         os.path.join(examples, "billing_batch1.json"))
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_cli("import-receipts", self.path,
                         os.path.join(examples, "receipts_batch.json"))
        self.assertEqual(p.returncode, 4)
        self.assertEqual(json.loads(p.stdout)["kind"], "version_conflict")


if __name__ == "__main__":
    unittest.main(verbosity=2)
