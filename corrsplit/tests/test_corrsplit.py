# -*- coding: utf-8 -*-
"""corrsplit 测试套件。运行：python tests/test_corrsplit.py"""
import itertools
import json
import os
import random
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from corrsplit import core  # noqa: E402
from corrsplit.core import Analyzer, InputError, Refusal  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def item_table(key_type="string"):
    return {"identity": "id",
            "columns": {"id": "int", "code": key_type},
            "unique_keys": [["code"]], "references": {}}


def doc_single(rows, corrections, key_type="string"):
    return {"tables": {"item": item_table(key_type)},
            "rows": {"item": rows}, "corrections": corrections}


def master_detail_doc(master_rows, detail_rows, corrections):
    return {
        "tables": {
            "master": {"identity": "id",
                       "columns": {"id": "int", "code": "string"},
                       "unique_keys": [["code"]], "references": {}},
            "detail": {"identity": "id",
                       "columns": {"id": "int", "master_id": "int",
                                   "item": "string"},
                       "unique_keys": [["master_id", "item"]],
                       "references": {"master_id": "master"}},
        },
        "rows": {"master": master_rows, "detail": detail_rows},
        "corrections": corrections,
    }


def repl(cid, table, rid, row):
    return {"id": cid, "op": "replace", "table": table, "row_id": rid,
            "row": row}


def add(cid, table, row):
    return {"id": cid, "op": "add", "table": table, "row": row}


def delete(cid, table, rid):
    return {"id": cid, "op": "delete", "table": table, "row_id": rid}


class TestUniqueKey(unittest.TestCase):
    def test_swap_same_batch(self):
        doc = doc_single(
            [{"id": 1, "code": "A"}, {"id": 2, "code": "B"}],
            [repl("r1", "item", 1, {"id": 1, "code": "B"}),
             repl("r2", "item", 2, {"id": 2, "code": "A"})])
        an = Analyzer(doc)
        result = an.analysis()
        self.assertEqual(result["summary"]["must_batch_groups"], 1)
        groups = [set(g["corrections"]) for g in result["groups"]]
        self.assertIn({"r1", "r2"}, groups)
        self.assertEqual(result["order"], [])
        # 单独批准任何一个都不是合法阶段
        self.assertFalse(an.check(["r1"])["valid"])
        self.assertFalse(an.check(["r2"])["valid"])
        self.assertTrue(an.check(["r1", "r2"])["valid"])
        self.assertTrue(an.check([])["valid"])
        # 最小合法集合
        self.assertEqual(set(an.closure(["r1"])["minimal_set"]), {"r1", "r2"})
        # 与直接求值一致
        self.assertFalse(an.evaluate(["r1"])["valid"])
        self.assertTrue(an.evaluate(["r1", "r2"])["valid"])

    def test_delete_add_order_only(self):
        doc = doc_single(
            [{"id": 1, "code": "A"}],
            [delete("d1", "item", 1),
             add("a1", "item", {"id": 2, "code": "A"})])
        an = Analyzer(doc)
        result = an.analysis()
        # 只须先做，不该绑成同批
        self.assertEqual(result["summary"]["must_batch_groups"], 0)
        impls = {(i["when"], i["requires"]) for i in result["implications"]}
        self.assertEqual(impls, {("a1", "d1")})
        self.assertTrue(an.check(["d1"])["valid"])
        self.assertFalse(an.check(["a1"])["valid"])
        self.assertEqual(set(an.closure(["a1"])["minimal_set"]), {"a1", "d1"})
        self.assertEqual(an.closure(["d1"])["minimal_set"], ["d1"])

    def test_chain_renumber(self):
        rows = [{"id": i, "code": "k%d" % i} for i in (1, 2, 3)]
        corrections = [repl("r1", "item", 1, {"id": 1, "code": "k2"}),
                       repl("r2", "item", 2, {"id": 2, "code": "k3"}),
                       repl("r3", "item", 3, {"id": 3, "code": "k4"})]
        an = Analyzer(doc_single(rows, corrections))
        impls = {(i["when"], i["requires"]) for i in an.analysis()["implications"]}
        self.assertEqual(impls, {("r1", "r2"), ("r2", "r3")})
        self.assertTrue(an.check(["r3"])["valid"])
        self.assertFalse(an.check(["r2"])["valid"])
        self.assertTrue(an.check(["r2", "r3"])["valid"])
        self.assertEqual(set(an.closure(["r1"])["minimal_set"]),
                         {"r1", "r2", "r3"})

    def test_replace_same_key_noop(self):
        doc = {"tables": {"item": {
            "identity": "id",
            "columns": {"id": "int", "code": "string", "name": "string"},
            "unique_keys": [["code"]], "references": {}}},
            "rows": {"item": [{"id": 1, "code": "A", "name": "x"}]},
            "corrections": [repl("r1", "item", 1,
                                 {"id": 1, "code": "A", "name": "y"})]}
        an = Analyzer(doc)
        self.assertEqual(an.analysis()["implications"], [])
        self.assertTrue(an.check(["r1"])["valid"])

    def test_composite_key(self):
        doc = {
            "tables": {"t": {"identity": "id",
                             "columns": {"id": "int", "a": "string",
                                         "b": "int"},
                             "unique_keys": [["a", "b"]], "references": {}}},
            "rows": {"t": [{"id": 1, "a": "x", "b": 1},
                           {"id": 2, "a": "x", "b": 2}]},
            # 只改一个组件，不与行 2 冲突
            "corrections": [repl("r1", "t", 1, {"id": 1, "a": "x", "b": 3})],
        }
        an = Analyzer(doc)
        self.assertEqual(an.analysis()["implications"], [])
        self.assertTrue(an.evaluate(["r1"])["valid"])

    def test_typed_key_distinct(self):
        # 整数 5 与字符串 "5" 是不同键值
        doc = {
            "tables": {"t": {"identity": "id",
                             "columns": {"id": "int", "k": "string"},
                             "unique_keys": [["k"]], "references": {}}},
            "rows": {"t": [{"id": 1, "k": "5"}]},
            "corrections": [add("a1", "t", {"id": 2, "k": "6"})],
        }
        an = Analyzer(doc)
        self.assertTrue(an.check(["a1"])["valid"])

    def test_null_key_no_conflict(self):
        doc = {
            "tables": {"t": {"identity": "id",
                             "columns": {"id": "int", "k": "string"},
                             "unique_keys": [["k"]], "references": {}}},
            "rows": {"t": [{"id": 1, "k": None}, {"id": 2, "k": None}]},
            "corrections": [add("a1", "t", {"id": 3, "k": None})],
        }
        an = Analyzer(doc)
        self.assertEqual(an.analysis()["implications"], [])
        self.assertTrue(an.evaluate(["a1"])["valid"])


class TestForeignKey(unittest.TestCase):
    def test_master_detail_adds(self):
        doc = master_detail_doc(
            [{"id": 1, "code": "M1"}], [],
            [add("am", "master", {"id": 2, "code": "M2"}),
             add("ad", "detail", {"id": 10, "master_id": 2, "item": "x"})])
        an = Analyzer(doc)
        impls = {(i["when"], i["requires"]) for i in an.analysis()["implications"]}
        self.assertEqual(impls, {("ad", "am")})
        self.assertTrue(an.check(["am"])["valid"])
        self.assertFalse(an.check(["ad"])["valid"])
        self.assertEqual(set(an.closure(["ad"])["minimal_set"]), {"ad", "am"})

    def test_master_detail_deletes(self):
        doc = master_detail_doc(
            [{"id": 1, "code": "M1"}],
            [{"id": 10, "master_id": 1, "item": "x"}],
            [delete("dm", "master", 1), delete("dd", "detail", 10)])
        an = Analyzer(doc)
        impls = {(i["when"], i["requires"]) for i in an.analysis()["implications"]}
        self.assertEqual(impls, {("dm", "dd")})
        self.assertTrue(an.check(["dd"])["valid"])
        self.assertFalse(an.check(["dm"])["valid"])

    def test_repoint_detail_chain(self):
        doc = master_detail_doc(
            [{"id": 2, "code": "M2"}],
            [{"id": 11, "master_id": 2, "item": "b"}],
            [add("am3", "master", {"id": 3, "code": "M3"}),
             repl("rp", "detail", 11, {"id": 11, "master_id": 3, "item": "b"}),
             delete("dm2", "master", 2)])
        an = Analyzer(doc)
        impls = {(i["when"], i["requires"]) for i in an.analysis()["implications"]}
        self.assertEqual(impls, {("rp", "am3"), ("dm2", "rp")})
        self.assertEqual(set(an.closure(["dm2"])["minimal_set"]),
                         {"dm2", "rp", "am3"})
        self.assertTrue(an.check(["am3", "rp"])["valid"])
        self.assertFalse(an.check(["am3", "dm2"])["valid"])

    def test_null_ref_unchecked(self):
        doc = master_detail_doc(
            [], [{"id": 10, "master_id": None, "item": "x"}],
            [add("a1", "detail",
                 {"id": 11, "master_id": None, "item": "y"})])
        an = Analyzer(doc)
        self.assertEqual(an.analysis()["implications"], [])
        self.assertTrue(an.evaluate(["a1"])["valid"])

    def test_self_reference(self):
        doc = {
            "tables": {"node": {"identity": "id",
                                "columns": {"id": "int", "parent": "int"},
                                "unique_keys": [],
                                "references": {"parent": "node"}}},
            "rows": {"node": [{"id": 1, "parent": 1}]},
            "corrections": [add("a1", "node", {"id": 2, "parent": 2})],
        }
        an = Analyzer(doc)
        self.assertEqual(an.analysis()["implications"], [])
        self.assertTrue(an.evaluate(["a1"])["valid"])


class TestIndependence(unittest.TestCase):
    def test_independent_swaps(self):
        doc = doc_single(
            [{"id": 1, "code": "A"}, {"id": 2, "code": "B"},
             {"id": 3, "code": "X"}, {"id": 4, "code": "Y"}],
            [repl("ab1", "item", 1, {"id": 1, "code": "B"}),
             repl("ab2", "item", 2, {"id": 2, "code": "A"}),
             repl("xy1", "item", 3, {"id": 3, "code": "Y"}),
             repl("xy2", "item", 4, {"id": 4, "code": "X"})])
        an = Analyzer(doc)
        result = an.analysis()
        groups = sorted(frozenset(g["corrections"]) for g in result["groups"]
                        if g["must_batch"])
        self.assertEqual(groups, [frozenset({"ab1", "ab2"}),
                                  frozenset({"xy1", "xy2"})])
        # 互不依赖：不应有任何组间先后关系
        self.assertEqual(result["order"], [])
        # 两套换号可单独审批
        self.assertTrue(an.check(["ab1", "ab2"])["valid"])
        self.assertTrue(an.check(["xy1", "xy2"])["valid"])
        self.assertEqual(set(an.closure(["xy1"])["minimal_set"]),
                         {"xy1", "xy2"})


class TestRefusal(unittest.TestCase):
    def test_invalid_initial_unique(self):
        doc = doc_single([{"id": 1, "code": "A"}, {"id": 2, "code": "A"}], [])
        with self.assertRaises(Refusal) as cm:
            Analyzer(doc)
        self.assertEqual(cm.exception.kind, "invalid_initial_state")
        v = cm.exception.violations[0]
        self.assertEqual(v["constraint"], "unique_key")
        self.assertEqual({r["id"] for r in v["rows"]}, {1, 2})

    def test_invalid_initial_fk(self):
        doc = master_detail_doc([], [{"id": 10, "master_id": 9, "item": "x"}],
                                [])
        with self.assertRaises(Refusal) as cm:
            Analyzer(doc)
        self.assertEqual(cm.exception.kind, "invalid_initial_state")
        self.assertEqual(cm.exception.violations[0]["constraint"],
                         "foreign_key")

    def test_invalid_final_unique(self):
        doc = doc_single(
            [{"id": 1, "code": "A"}, {"id": 2, "code": "B"}],
            [repl("r1", "item", 1, {"id": 1, "code": "B"})])
        with self.assertRaises(Refusal) as cm:
            Analyzer(doc)
        self.assertEqual(cm.exception.kind, "invalid_final_state")

    def test_invalid_final_fk(self):
        # 删主档却不删引用它的明细：清单不完整，拒绝
        doc = master_detail_doc(
            [{"id": 1, "code": "M1"}],
            [{"id": 10, "master_id": 1, "item": "x"}],
            [delete("dm", "master", 1)])
        with self.assertRaises(Refusal) as cm:
            Analyzer(doc)
        self.assertEqual(cm.exception.kind, "invalid_final_state")
        v = cm.exception.violations[0]
        self.assertEqual(v["constraint"], "foreign_key")
        self.assertEqual(v["referenced_id"], 1)

    def test_two_adds_same_key_final_invalid(self):
        doc = doc_single(
            [], [add("a1", "item", {"id": 1, "code": "A"}),
                 add("a2", "item", {"id": 2, "code": "A"})])
        with self.assertRaises(Refusal) as cm:
            Analyzer(doc)
        self.assertEqual(cm.exception.kind, "invalid_final_state")


class TestInputValidation(unittest.TestCase):
    def assert_input_error(self, doc, needle=None):
        with self.assertRaises(InputError) as cm:
            Analyzer(doc)
        if needle is not None:
            text = json.dumps(cm.exception.problems, ensure_ascii=False)
            self.assertIn(needle, text)

    def test_duplicate_op_same_row(self):
        doc = doc_single(
            [{"id": 1, "code": "A"}],
            [delete("d1", "item", 1),
             repl("r1", "item", 1, {"id": 1, "code": "B"})])
        self.assert_input_error(doc, "多条更正")

    def test_add_existing_identity(self):
        doc = doc_single([{"id": 1, "code": "A"}],
                         [add("a1", "item", {"id": 1, "code": "B"})])
        self.assert_input_error(doc, "已存在")

    def test_delete_missing_row(self):
        doc = doc_single([], [delete("d1", "item", 99)])
        self.assert_input_error(doc, "不存在")

    def test_replace_changes_identity(self):
        doc = doc_single([{"id": 1, "code": "A"}],
                         [repl("r1", "item", 1, {"id": 2, "code": "B"})])
        self.assert_input_error(doc, "行身份")

    def test_bad_field_type(self):
        doc = doc_single([{"id": 1, "code": 5}], [])
        self.assert_input_error(doc, "类型")

    def test_bool_not_int(self):
        doc = doc_single([{"id": True, "code": "A"}], [])
        self.assert_input_error(doc)

    def test_unknown_column(self):
        doc = doc_single([{"id": 1, "code": "A", "zz": 1}], [])
        self.assert_input_error(doc, "未声明")

    def test_unknown_table(self):
        doc = {"tables": {"item": item_table()},
               "rows": {"ghost": []}, "corrections": []}
        self.assert_input_error(doc, "未声明的表")

    def test_unknown_ref_target(self):
        doc = {"tables": {"t": {"identity": "id",
                                "columns": {"id": "int", "m": "int"},
                                "unique_keys": [],
                                "references": {"m": "ghost"}}},
               "rows": {}, "corrections": []}
        self.assert_input_error(doc, "目标表")

    def test_duplicate_correction_id(self):
        doc = doc_single([{"id": 1, "code": "A"}, {"id": 2, "code": "B"}],
                         [delete("x", "item", 1), delete("x", "item", 2)])
        self.assert_input_error(doc, "重复")

    def test_unknown_select_id(self):
        doc = doc_single([{"id": 1, "code": "A"}], [])
        an = Analyzer(doc)
        with self.assertRaises(InputError):
            an.check(["nope"])


class TestClosureEvidence(unittest.TestCase):
    def test_chain_traces_to_request(self):
        doc = master_detail_doc(
            [{"id": 2, "code": "M2"}],
            [{"id": 11, "master_id": 2, "item": "b"}],
            [add("am3", "master", {"id": 3, "code": "M3"}),
             repl("rp", "detail", 11, {"id": 11, "master_id": 3, "item": "b"}),
             delete("dm2", "master", 2)])
        an = Analyzer(doc)
        # 默认：每项给直接证据边 via，沿 via 可递归追溯回请求
        result = an.closure(["dm2"])
        self.assertEqual(result["requested"], ["dm2"])
        by_corr = {a["correction"]: a["via"] for a in result["added"]}
        self.assertEqual(by_corr["rp"]["when"], "dm2")
        self.assertEqual(by_corr["am3"]["when"], "rp")
        # 沿 via 回溯必到达所请求的更正
        for start in by_corr:
            cur, seen = start, set()
            while cur in by_corr:
                self.assertNotIn(cur, seen)
                seen.add(cur)
                cur = by_corr[cur]["when"]
            self.assertIn(cur, result["requested"])
        # chains=True：展开完整证据链
        full = an.closure(["dm2"], chains=True)
        chains = {a["correction"]: a["chain"] for a in full["added"]}
        self.assertEqual([c["when"] for c in chains["am3"]], ["dm2", "rp"])
        self.assertEqual(chains["am3"][-1]["requires"], "am3")
        for chain in chains.values():
            for step in chain:
                self.assertTrue(step["evidence"])
                ev = step["evidence"][0]
                self.assertIn("rows", ev)
                self.assertIn("constraint", ev)

    def test_check_violations_have_evidence(self):
        doc = doc_single(
            [{"id": 1, "code": "A"}, {"id": 2, "code": "B"}],
            [repl("r1", "item", 1, {"id": 1, "code": "B"}),
             repl("r2", "item", 2, {"id": 2, "code": "A"})])
        an = Analyzer(doc)
        result = an.check(["r1"])
        self.assertFalse(result["valid"])
        self.assertEqual(result["missing"], ["r2"])
        ev = result["violations"][0]["evidence"][0]
        self.assertEqual(ev["constraint"], "unique_key")
        self.assertEqual(ev["key_value"], ["B"])
        self.assertEqual({r["id"] for r in ev["rows"]}, {1, 2})


class TestEquivalenceBruteForce(unittest.TestCase):
    """小规模随机实例上，图判定与直接全约束求值逐一子集对比。"""

    def _check_instance(self, doc):
        try:
            an = Analyzer(doc)
        except (Refusal, InputError):
            return False
        m = len(an.model.corrections)
        if m > 8:
            return False
        exts = an.model.ext_ids
        for bits in range(2 ** m):
            sel = [exts[i] for i in range(m) if bits & (1 << i)]
            self.assertEqual(an.check(sel)["valid"],
                             an.evaluate(sel)["valid"],
                             msg="subset %r of %s" % (sel, json.dumps(doc)))
            clo = an.closure(sel)
            clo_set = set(clo["minimal_set"])
            self.assertTrue(set(sel) <= clo_set)
            self.assertTrue(an.check(list(clo_set))["valid"])
            # 最小性：不存在 sel ⊆ T ⊊ closure 的合法 T
            rest = sorted(clo_set - set(sel))
            for r in range(len(rest)):
                for sub in itertools.combinations(rest, r):
                    cand = set(sel) | set(sub)
                    self.assertFalse(an.check(list(cand))["valid"])
        return True

    def test_random_single_table(self):
        rng = random.Random(20261124)
        accepted = 0
        attempts = 0
        while accepted < 25 and attempts < 2000:
            attempts += 1
            n = rng.randint(0, 5)
            rows = [{"id": i, "code": "k%d" % i} for i in range(n)]
            corrections = []
            for i in range(n):
                r = rng.random()
                if r < 0.25:
                    corrections.append(delete("d%d" % i, "item", i))
                elif r < 0.6:
                    corrections.append(repl("r%d" % i, "item", i,
                                            {"id": i,
                                             "code": "k%d" % rng.randint(0, n + 2)}))
            for j in range(rng.randint(0, 2)):
                corrections.append(add("a%d" % j, "item",
                                       {"id": n + j,
                                        "code": "k%d" % rng.randint(0, n + 2)}))
            doc = doc_single(rows, corrections)
            if self._check_instance(doc):
                accepted += 1
        self.assertGreaterEqual(accepted, 25)

    def test_random_master_detail(self):
        rng = random.Random(20261125)
        accepted = 0
        attempts = 0
        while accepted < 20 and attempts < 3000:
            attempts += 1
            p = rng.randint(1, 3)
            masters = [{"id": i, "code": "M%d" % i} for i in range(p)]
            details = []
            did = 100
            for i in range(p):
                for _ in range(rng.randint(0, 2)):
                    details.append({"id": did, "master_id": i,
                                    "item": "it%d" % did})
                    did += 1
            corrections = []
            for i in range(p):
                if rng.random() < 0.3:
                    corrections.append(delete("dm%d" % i, "master", i))
            for d in details:
                r = rng.random()
                if r < 0.3:
                    corrections.append(delete("dd%d" % d["id"], "detail",
                                              d["id"]))
                elif r < 0.5:
                    corrections.append(repl(
                        "rp%d" % d["id"], "detail", d["id"],
                        {"id": d["id"], "master_id": rng.randint(0, p),
                         "item": d["item"]}))
            if rng.random() < 0.7:
                corrections.append(add("am", "master",
                                       {"id": p, "code": "M%d" % p}))
                if rng.random() < 0.8:
                    corrections.append(add("ad", "detail",
                                           {"id": did, "master_id": p,
                                            "item": "new"}))
            doc = master_detail_doc(masters, details, corrections)
            if self._check_instance(doc):
                accepted += 1
        self.assertGreaterEqual(accepted, 20)


class TestPerformance(unittest.TestCase):
    def test_long_chain_and_many_pairs(self):
        # 长换号链：不得枚举子集、不得两两比较，应近线性完成
        n = 20000
        rows = [{"id": i, "code": "k%d" % i} for i in range(n + 1)]
        corrections = [repl("c%d" % i, "item", i,
                            {"id": i, "code": "k%d" % (i + 1)})
                       for i in range(n + 1)]
        # 大量互不相干的换号对
        base = n + 1
        for j in range(5000):
            rows.append({"id": base + 2 * j, "code": "p%da" % j})
            rows.append({"id": base + 2 * j + 1, "code": "p%db" % j})
            corrections.append(repl("pa%d" % j, "item", base + 2 * j,
                                    {"id": base + 2 * j, "code": "p%db" % j}))
            corrections.append(repl("pb%d" % j, "item", base + 2 * j + 1,
                                    {"id": base + 2 * j + 1,
                                     "code": "p%da" % j}))
        doc = doc_single(rows, corrections)
        t0 = time.time()
        an = Analyzer(doc)
        result = an.analysis()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 60)
        # 链头需要整条链（via 形式输出，长链也应近线性返回）
        t0 = time.time()
        clo = an.closure(["c0"])
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(len(clo["minimal_set"]), n + 1)
        self.assertEqual(len(clo["added"]), n)
        # 换号对各自成组、互不影响
        self.assertEqual(result["summary"]["must_batch_groups"], 5000)
        self.assertEqual(set(an.closure(["pa0"])["minimal_set"]),
                         {"pa0", "pb0"})


class TestCli(unittest.TestCase):
    def run_cli(self, *argv, stdin=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "corrsplit"] + list(argv),
            capture_output=True, text=True, cwd=ROOT, env=env,
            input=stdin)

    def test_analyze_example(self):
        p = self.run_cli("analyze", "examples/renumber_swap.json")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["summary"]["must_batch_groups"], 1)

    def test_check_and_closure(self):
        p = self.run_cli("check", "examples/master_detail.json", "del_m2")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertFalse(out["valid"])
        self.assertEqual(set(out["missing"]), {"repoint_d11"})
        p = self.run_cli("closure", "examples/master_detail.json", "del_m2")
        out = json.loads(p.stdout)
        self.assertEqual(set(out["minimal_set"]),
                         {"del_m2", "repoint_d11", "add_m3"})

    def test_refusal_exit_code(self):
        p = self.run_cli("analyze", "examples/invalid_final.json")
        self.assertEqual(p.returncode, 3)
        out = json.loads(p.stdout)
        self.assertEqual(out["kind"], "invalid_final_state")
        self.assertTrue(out["violations"])

    def test_stdin_and_pretty(self):
        with open(os.path.join(ROOT, "examples", "independent_renumber.json"),
                  encoding="utf-8") as f:
            p = self.run_cli("analyze", "-", "--pretty", stdin=f.read())
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["summary"]["must_batch_groups"], 2)
        self.assertEqual(out["order"], [])

    def test_output_json_serializable(self):
        for name in ("renumber_swap", "master_detail", "independent_renumber"):
            with open(os.path.join(ROOT, "examples", name + ".json"),
                      encoding="utf-8") as f:
                doc = json.load(f)
            json.dumps(Analyzer(doc).analysis(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
