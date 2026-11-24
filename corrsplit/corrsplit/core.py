# -*- coding: utf-8 -*-
"""corrsplit.core —— 更正单拆批审批分析的核心模型、校验与图算法（只读）。

术语
----
- 仓表：若干张表。每张表有不变的行身份（主键）、可配置的复合业务唯一键、
  以及指向某表行身份的可空引用。键与非空字段是有类型的整数或字符串；
  空值不参与业务键冲突，引用为空时不检查。
- 更正单：一组更正，每行至多一条：新增(add) / 删除(delete) / 整行替换(replace)。
  不允许把一行中间改成一个临时业务号，不做自动级联或补行。
- 阶段：已批准更正的任意子集；已批准行取更正后状态，未批准行取更正前状态。
- 合法阶段：应用该子集后，*完整*仓表（所有表，而非仅本批涉及的表）满足全部约束。

核心事实（正确性依据）
--------------------
每一种约束违反至多涉及两行：唯一键冲突是两行取值相等；外键违反是引用行存在
而被引用行缺失。每行至多一条更正，因此每行的状态只取决于一个比特（该行的
更正是否已批准），每个潜在违反就是至多两个字面量的合取。又因初始仓表
（全部未批准）与全部更正后的仓表（全部批准）都已校验合法，剩下的每个违反
条件必然是混合极性的，即恰好等价于一条简单蕴含：

    “若批准 X，则 Y 必须同批或更早批准”   记作  X ⇒ Y

于是：
- 合法阶段 ⟺ 在蕴含关系下封闭的集合（X 在集合内且 X ⇒ Y，则 Y 也在）；
- 不可再拆的同批组 = 蕴含图上的强连通分量；
- 必要的先后关系 = 强连通分量缩点后的 DAG 边（无必要时不强加顺序）；
- 包含所选更正的最小合法集合 = 所选节点在蕴含图上的可达闭包（唯一、最小）。

复杂度：设 R 为总行数、C 为更正数、K 为唯一键定义总数。预处理 O((R+C)·K)；
由于初始/最终态合法，每个键值在每种状态下至多一行，每条引用至多产生一条
蕴含，故蕴含边数 O(C + 引用非空数)；强连通分量与闭包均为 O(C + E)。
不枚举任何审批子集，不对无关联行做两两比较（仅同键值行、引用目标行参与）。
"""

from collections import deque

MAX_PROBLEMS = 100
MAX_VIOLATIONS = 500

TYPE_INT = "int"
TYPE_STRING = "string"
_VALID_TYPES = (TYPE_INT, TYPE_STRING)
_OPS = ("add", "delete", "replace")


class InputError(Exception):
    """模式、仓表数据或更正单本身不合法。"""

    def __init__(self, problems):
        self.problems = list(problems)
        message = "; ".join(p.get("message", "") for p in self.problems[:5])
        super().__init__(message or "输入不合法")


class Refusal(Exception):
    """初始或全部更正后的仓表本就不合法，拒绝给出拆批结论。"""

    def __init__(self, kind, violations):
        self.kind = kind  # "invalid_initial_state" | "invalid_final_state"
        self.violations = list(violations)
        super().__init__(kind)


class InternalError(Exception):
    """前置校验结论与后续推导不一致（正常输入下不应出现）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _type_of(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return TYPE_INT
    if isinstance(value, str):
        return TYPE_STRING
    return None


def _canon(value):
    """类型敏感的规范形式：整数 5 与字符串 "5" 不相等。"""
    return (_type_of(value), value)


def _key_tuple(row, cols):
    """复合业务键的规范取值；任一组件为空则该行不参与该键冲突。"""
    out = []
    for c in cols:
        v = row[c]
        if v is None:
            return None
        out.append(_canon(v))
    return tuple(out)


class _Correction(object):
    __slots__ = ("index", "ext_id", "op", "table", "identity", "row")

    def __init__(self, index, ext_id, op, table, identity, row):
        self.index = index
        self.ext_id = ext_id
        self.op = op
        self.table = table
        self.identity = identity  # 原始行身份（int 或 str）
        self.row = row            # add/replace 的整行内容；delete 为 None


class _Model(object):
    def __init__(self):
        self.table_order = []   # 表名，按输入顺序
        self.tables = {}        # 表名 -> {"identity","columns","unique_keys","references"}
        self.current = {}       # 表名 -> {规范行身份: 行dict}
        self.corrections = []   # _Correction，下标即内部编号
        self.corr_by_row = {}   # (表名, 规范行身份) -> 更正下标
        self.ext_ids = []       # 下标 -> 外部更正 id


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def validate_document(doc):
    problems = []

    def err(path, message):
        if len(problems) < MAX_PROBLEMS:
            problems.append({"path": path, "message": message})

    if not isinstance(doc, dict):
        raise InputError([{"path": "$", "message": "输入必须是 JSON 对象"}])

    model = _Model()

    # ---- 模式：表、列、唯一键、引用 ----
    tables_doc = doc.get("tables")
    if not isinstance(tables_doc, dict) or not tables_doc:
        raise InputError([{"path": "$.tables", "message": "tables 必须是非空对象"}])

    for tname, tdoc in tables_doc.items():
        path = "$.tables.%s" % tname
        if not isinstance(tdoc, dict):
            err(path, "表定义必须是对象")
            continue
        identity = tdoc.get("identity", "id")
        columns = tdoc.get("columns")
        ukeys = tdoc.get("unique_keys", [])
        refs = tdoc.get("references", {})
        ok = True
        if not isinstance(identity, str) or not identity:
            err(path + ".identity", "identity 必须是非空字符串")
            ok = False
        if not isinstance(columns, dict) or not columns:
            err(path + ".columns", "columns 必须是非空对象")
            ok = False
        else:
            for col, typ in columns.items():
                if typ not in _VALID_TYPES:
                    err("%s.columns.%s" % (path, col),
                        "字段类型必须是 int 或 string")
                    ok = False
            if ok and identity not in columns:
                err(path + ".identity", "行身份列必须在 columns 中声明")
                ok = False
        if not ok:
            continue
        keys_ok = True
        if not isinstance(ukeys, list):
            err(path + ".unique_keys", "unique_keys 必须是数组")
            keys_ok = False
        else:
            for ki, key in enumerate(ukeys):
                kpath = "%s.unique_keys[%d]" % (path, ki)
                if (not isinstance(key, list) or not key
                        or any(not isinstance(c, str) for c in key)):
                    err(kpath, "业务键必须是非空字段名数组")
                    keys_ok = False
                    continue
                if len(set(key)) != len(key):
                    err(kpath, "业务键内字段重复")
                    keys_ok = False
                for c in key:
                    if c not in columns:
                        err(kpath, "业务键字段未声明: %s" % c)
                        keys_ok = False
        refs_ok = True
        if not isinstance(refs, dict):
            err(path + ".references", "references 必须是对象")
            refs_ok = False
        else:
            for col, target in refs.items():
                if col not in columns:
                    err("%s.references.%s" % (path, col), "引用字段未声明")
                    refs_ok = False
                if not isinstance(target, str):
                    err("%s.references.%s" % (path, col), "引用目标必须是表名")
                    refs_ok = False
        if not (keys_ok and refs_ok):
            continue
        model.table_order.append(tname)
        model.tables[tname] = {
            "identity": identity,
            "columns": dict(columns),
            "unique_keys": [list(k) for k in ukeys],
            "references": dict(refs),
        }

    # 引用目标表存在性（第二遍，允许前向引用）
    for tname, sch in model.tables.items():
        for col, target in sch["references"].items():
            if target not in model.tables:
                err("$.tables.%s.references.%s" % (tname, col),
                    "引用目标表不存在: %s" % target)

    # ---- 当前仓表行 ----
    rows_doc = doc.get("rows", {})
    if not isinstance(rows_doc, dict):
        err("$.rows", "rows 必须是对象")
        rows_doc = {}
    for tname in rows_doc:
        if tname not in model.tables:
            err("$.rows.%s" % tname, "未声明的表")
    for tname in model.table_order:
        model.current[tname] = {}
    for tname, rows in rows_doc.items():
        if tname not in model.tables:
            continue
        if not isinstance(rows, list):
            err("$.rows.%s" % tname, "表数据必须是行数组")
            continue
        seen = model.current[tname]
        for i, row in enumerate(rows):
            rpath = "$.rows.%s[%d]" % (tname, i)
            rid = _check_row(model, tname, row, rpath, err)
            if rid is None:
                continue
            ck = _canon(rid)
            if ck in seen:
                err(rpath, "行身份重复: %r" % (rid,))
                continue
            seen[ck] = row

    # ---- 更正单 ----
    corr_doc = doc.get("corrections", [])
    if not isinstance(corr_doc, list):
        err("$.corrections", "corrections 必须是数组")
        corr_doc = []
    used_ext = set()
    for i, c in enumerate(corr_doc):
        cpath = "$.corrections[%d]" % i
        if not isinstance(c, dict):
            err(cpath, "更正必须是对象")
            continue
        ext = c.get("id")
        if ext is None:
            ext = "c%d" % (i + 1)
        elif isinstance(ext, bool) or not isinstance(ext, (str, int)):
            err(cpath + ".id", "id 必须是字符串或数字")
            continue
        else:
            ext = str(ext)
        if ext in used_ext:
            err(cpath + ".id", "更正 id 重复: %s" % ext)
            continue
        op = c.get("op")
        table = c.get("table")
        if op not in _OPS:
            err(cpath + ".op", "op 必须是 add/delete/replace")
            continue
        if table not in model.tables:
            err(cpath + ".table", "未声明的表: %r" % (table,))
            continue
        sch = model.tables[table]
        ident_col = sch["identity"]
        ident_type = sch["columns"][ident_col]
        row = None
        if op == "add":
            row = c.get("row")
            rid = _check_row(model, table, row, cpath + ".row", err)
            if rid is None:
                continue
            ck = _canon(rid)
            if ck in model.current[table]:
                err(cpath + ".row", "新增的行身份已存在: %r" % (rid,))
                continue
        else:
            rid = c.get("row_id")
            if _type_of(rid) != ident_type:
                err(cpath + ".row_id", "row_id 缺失或类型应为 %s" % ident_type)
                continue
            ck = _canon(rid)
            if ck not in model.current[table]:
                err(cpath + ".row_id", "更正的行不存在: %r" % (rid,))
                continue
            if op == "replace":
                row = c.get("row")
                rid2 = _check_row(model, table, row, cpath + ".row", err)
                if rid2 is None:
                    continue
                if _canon(rid2) != ck:
                    err(cpath + ".row", "整行替换不得改变行身份")
                    continue
        key = (table, ck)
        if key in model.corr_by_row:
            err(cpath, "同一行在清单中出现多条更正: %r" % (rid,))
            continue
        model.corr_by_row[key] = len(model.corrections)
        model.corrections.append(
            _Correction(len(model.corrections), ext, op, table, rid, row))
        model.ext_ids.append(ext)
        used_ext.add(ext)

    if problems:
        raise InputError(problems)
    return model


def _check_row(model, tname, row, path, err):
    """校验一整行（当前行或更正行）；通过则返回原始行身份，否则 None。"""
    sch = model.tables[tname]
    if not isinstance(row, dict):
        err(path, "行必须是对象")
        return None
    ok = True
    for col in sch["columns"]:
        if col not in row:
            err(path, "缺少字段: %s" % col)
            ok = False
    for col in row:
        if col not in sch["columns"]:
            err("%s.%s" % (path, col), "未声明的字段")
            ok = False
    if not ok:
        return None
    for col, typ in sch["columns"].items():
        v = row[col]
        if v is None:
            continue
        if _type_of(v) != typ:
            err("%s.%s" % (path, col), "类型应为 %s" % typ)
            ok = False
    if row[sch["identity"]] is None:
        err("%s.%s" % (path, sch["identity"]), "行身份不能为空")
        ok = False
    if not ok:
        return None
    return row[sch["identity"]]


# ---------------------------------------------------------------------------
# 状态求值：直接检查“完整仓表满足所有约束”（用于前置校验与 evaluate 查询）
# ---------------------------------------------------------------------------

def _state_rows(model, approved):
    """approved 为已批准更正下标集合；返回 {表名: {规范行身份: 行}}。"""
    state = {}
    for tname in model.table_order:
        state[tname] = dict(model.current[tname])
    for i in approved:
        c = model.corrections[i]
        ck = _canon(c.identity)
        if c.op == "delete":
            state[c.table].pop(ck, None)
        else:
            state[c.table][ck] = c.row
    return state


def _row_ref(model, tname, ck, row, approved):
    d = {"table": tname, "id": row[model.tables[tname]["identity"]]}
    ci = model.corr_by_row.get((tname, ck))
    if ci is not None:
        d["correction"] = model.ext_ids[ci]
        d["correction_approved"] = ci in approved
    return d


def find_violations(model, approved):
    """对指定阶段（已批准集合）做完整仓表的全约束检查，返回冲突定位列表。"""
    state = _state_rows(model, approved)
    violations = []
    overflow = [False]

    def emit(v):
        if len(violations) < MAX_VIOLATIONS:
            violations.append(v)
        else:
            overflow[0] = True

    for tname in model.table_order:
        sch = model.tables[tname]
        rows = state[tname]
        for cols in sch["unique_keys"]:
            seen = {}
            for ck, row in rows.items():
                kt = _key_tuple(row, cols)
                if kt is None:
                    continue
                if kt in seen:
                    ck0, row0 = seen[kt]
                    emit({
                        "constraint": "unique_key",
                        "table": tname,
                        "key_columns": list(cols),
                        "key_value": [v for (_, v) in kt],
                        "rows": [
                            _row_ref(model, tname, ck0, row0, approved),
                            _row_ref(model, tname, ck, row, approved),
                        ],
                        "message": "唯一键 %s(%s) 取值 %s 冲突"
                                   % (tname, ", ".join(cols),
                                      [v for (_, v) in kt]),
                    })
                else:
                    seen[kt] = (ck, row)
        for col, target in sch["references"].items():
            target_ids = set(state[target].keys())
            for ck, row in rows.items():
                v = row[col]
                if v is None:
                    continue
                if _canon(v) not in target_ids:
                    emit({
                        "constraint": "foreign_key",
                        "table": tname,
                        "column": col,
                        "target_table": target,
                        "referenced_id": v,
                        "rows": [_row_ref(model, tname, ck, row, approved)],
                        "message": "外键 %s.%s → %s：行 %r 引用了不存在的行身份 %r"
                                   % (tname, col, target,
                                      row[sch["identity"]], v),
                    })
    if overflow[0]:
        violations.append({
            "constraint": "truncated",
            "message": "冲突过多，仅展示前 %d 条" % MAX_VIOLATIONS,
        })
    return violations


# ---------------------------------------------------------------------------
# 蕴含生成：每个潜在约束违反归结为一条 “X ⇒ Y”（批准 X 要求 Y 同批或更早）
# ---------------------------------------------------------------------------

def _ev_unique(model, tname, cols, kt, ck_pre, ci_pre, ck_post, ci_post):
    ext = model.ext_ids
    raw = [v for (_, v) in kt]
    rid_pre, rid_post = ck_pre[1], ck_post[1]
    return {
        "constraint": "unique_key",
        "table": tname,
        "key_columns": list(cols),
        "key_value": raw,
        "rows": [
            {"table": tname, "id": rid_post, "correction": ext[ci_post],
             "role": "takes_key_when_approved"},
            {"table": tname, "id": rid_pre, "correction": ext[ci_pre],
             "role": "vacates_key_when_approved"},
        ],
        "message": ("唯一键 %s(%s) 取值 %s：更正 %s（行 %r）批准后占用该键，"
                    "故 %s（行 %r）必须同批或更早批准以让出该键"
                    % (tname, ", ".join(cols), raw,
                       ext[ci_post], rid_post, ext[ci_pre], rid_pre)),
    }


def _ev_fk_add(model, tname, col, target, ck_ref, ci_ref, value, ci_target):
    ext = model.ext_ids
    rid_ref = ck_ref[1]
    return {
        "constraint": "foreign_key",
        "table": tname,
        "column": col,
        "target_table": target,
        "referenced_id": value,
        "rows": [
            {"table": tname, "id": rid_ref, "correction": ext[ci_ref],
             "role": "references_when_approved"},
            {"table": target, "id": value, "correction": ext[ci_target],
             "role": "exists_only_when_approved"},
        ],
        "message": ("外键 %s.%s → %s：更正 %s（行 %r）批准后引用 %s 行 %r，"
                    "被引用行仅当 %s 批准后存在，故 %s 必须同批或更早批准"
                    % (tname, col, target, ext[ci_ref], rid_ref,
                       target, value, ext[ci_target], ext[ci_target])),
    }


def _ev_fk_del(model, tname, col, target, ck_ref, ci_ref, value, ci_target):
    ext = model.ext_ids
    rid_ref = ck_ref[1]
    return {
        "constraint": "foreign_key",
        "table": tname,
        "column": col,
        "target_table": target,
        "referenced_id": value,
        "rows": [
            {"table": target, "id": value, "correction": ext[ci_target],
             "role": "removed_when_approved"},
            {"table": tname, "id": rid_ref, "correction": ext[ci_ref],
             "role": "references_until_approved"},
        ],
        "message": ("外键 %s.%s → %s：更正 %s 批准后删除被引用行 %r，"
                    "而行 %r 在 %s 批准前仍引用它，故 %s 必须同批或更早批准"
                    % (tname, col, target, ext[ci_target], value,
                       rid_ref, ext[ci_ref], ext[ci_ref])),
    }


def generate_implications(model):
    """返回 {(when_idx, requires_idx): [证据, ...]}。

    前置条件（初始/最终仓表合法）保证每个潜在违反都是混合极性的，
    即每条记录都是一条真正的蕴含；否则说明前置校验漏检，抛 InternalError。
    """
    edges = {}

    def add_edge(u, v, ev):
        edges.setdefault((u, v), []).append(ev)

    per_table_corr = {t: [] for t in model.table_order}
    for c in model.corrections:
        per_table_corr[c.table].append(c)

    # ---- 业务唯一键：同一键值的“让出者”与“占用者” ----
    for tname in model.table_order:
        sch = model.tables[tname]
        for cols in sch["unique_keys"]:
            pre = {}   # 键值 -> (规范行身份, 更正下标或 None)
            post = {}
            for ck, row in model.current[tname].items():
                kt = _key_tuple(row, cols)
                if kt is None:
                    continue
                ci = model.corr_by_row.get((tname, ck))
                if ci is None:
                    if kt in pre:
                        raise InternalError("初始状态唯一键冲突未被前置校验发现")
                    pre[kt] = (ck, None)
                    post[kt] = (ck, None)
                else:
                    op = model.corrections[ci].op
                    if op in ("delete", "replace"):
                        if kt in pre:
                            raise InternalError("初始状态唯一键冲突未被前置校验发现")
                        pre[kt] = (ck, ci)
            for c in per_table_corr[tname]:
                if c.op == "delete":
                    continue
                kt = _key_tuple(c.row, cols)
                if kt is None:
                    continue
                ck = _canon(c.identity)
                if kt in post:
                    raise InternalError("最终状态唯一键冲突未被前置校验发现")
                post[kt] = (ck, c.index)
            for kt, (ck1, c1) in pre.items():
                e2 = post.get(kt)
                if e2 is None:
                    continue
                ck2, c2 = e2
                if ck1 == ck2:
                    continue  # 同一行在更正前后持有同一键（如未改键的整行替换）
                if c1 is None or c2 is None:
                    raise InternalError("唯一键单元约束未被前置校验发现")
                add_edge(c2, c1,
                         _ev_unique(model, tname, cols, kt, ck1, c1, ck2, c2))

    # ---- 引用（外键）：引用行与被引用行 ----
    existence_cache = {}

    def existence(tname):
        if tname in existence_cache:
            return existence_cache[tname]
        ex = {}
        for ck in model.current[tname]:
            ci = model.corr_by_row.get((tname, ck))
            if ci is None or model.corrections[ci].op != "delete":
                ex[ck] = ("const", None)  # 常量行与整行替换行始终存在
            else:
                ex[ck] = ("del", ci)
        for c in per_table_corr[tname]:
            if c.op == "add":
                ex[_canon(c.identity)] = ("add", c.index)
        existence_cache[tname] = ex
        return ex

    for tname in model.table_order:
        sch = model.tables[tname]
        for col, target in sch["references"].items():
            ex = existence(target)
            entries = []  # (规范行身份, 字面量, 引用值)；字面量 None/"not"/"yes"
            for ck, row in model.current[tname].items():
                v = row[col]
                ci = model.corr_by_row.get((tname, ck))
                if ci is None:
                    if v is not None:
                        entries.append((ck, None, v))
                else:
                    op = model.corrections[ci].op
                    if op == "delete":
                        if v is not None:
                            entries.append((ck, ("not", ci), v))
                    elif op == "replace":
                        if v is not None:
                            entries.append((ck, ("not", ci), v))
                        v2 = model.corrections[ci].row[col]
                        if v2 is not None:
                            entries.append((ck, ("yes", ci), v2))
            for c in per_table_corr[tname]:
                if c.op != "add":
                    continue
                v = c.row[col]
                if v is not None:
                    entries.append((_canon(c.identity), ("yes", c.index), v))
            for ck, lit, v in entries:
                if tname == target and ck == _canon(v):
                    continue  # 自引用：存在性与自身一致，永不违反
                e = ex.get(_canon(v))
                if e is None:
                    raise InternalError("悬空引用未被前置校验发现")
                kind, cm = e
                if kind == "const":
                    continue
                if kind == "add":
                    # 违反条件：引用行带该引用在场 ∧ 被引用行尚未新增
                    if lit is None or lit[0] == "not":
                        raise InternalError("引用约束未被前置校验发现")
                    cr = lit[1]
                    add_edge(cr, cm,
                             _ev_fk_add(model, tname, col, target,
                                        ck, cr, v, cm))
                else:  # "del"
                    # 违反条件：引用行带该引用在场 ∧ 被引用行已被删除
                    if lit is None or lit[0] == "yes":
                        raise InternalError("引用约束未被前置校验发现")
                    cr = lit[1]
                    add_edge(cm, cr,
                             _ev_fk_del(model, tname, col, target,
                                        ck, cr, v, cm))
    return edges


# ---------------------------------------------------------------------------
# 图算法：强连通分量（迭代 Tarjan）、缩点、分层
# ---------------------------------------------------------------------------

def _tarjan_scc(n, adj):
    index = [None] * n
    low = [0] * n
    on_stack = [False] * n
    stack = []
    comps = []
    counter = 0
    for root in range(n):
        if index[root] is not None:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        work = [[root, 0]]
        while work:
            frame = work[-1]
            node = frame[0]
            if frame[1] < len(adj[node]):
                w = adj[node][frame[1]]
                frame[1] += 1
                if index[w] is None:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append([w, 0])
                elif on_stack[w] and index[w] < low[node]:
                    low[node] = index[w]
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    if low[node] < low[parent]:
                        low[parent] = low[node]
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        comp.append(w)
                        if w == node:
                            break
                    comps.append(comp)
    return comps


# ---------------------------------------------------------------------------
# 分析器
# ---------------------------------------------------------------------------

class Analyzer(object):
    """对一份（仓表 + 约束 + 完整更正单）的只读审批分析。

    构造时完成输入校验与前置校验（初始/最终仓表合法性），
    不满足时抛 InputError / Refusal。之后可任意多次查询。
    """

    def __init__(self, doc):
        self.model = validate_document(doc)
        m = len(self.model.corrections)
        initial = find_violations(self.model, set())
        if initial:
            raise Refusal("invalid_initial_state", initial)
        final = find_violations(self.model, set(range(m)))
        if final:
            raise Refusal("invalid_final_state", final)
        self._edges = generate_implications(self.model)
        self._adj = [[] for _ in range(m)]
        for (u, v) in self._edges:
            self._adj[u].append(v)
        for a in self._adj:
            a.sort()
        self._index_of_ext = {e: i for i, e in enumerate(self.model.ext_ids)}
        self._build_groups()

    # -- 内部 ------------------------------------------------------------

    def _build_groups(self):
        m = len(self.model.corrections)
        comps = [sorted(c) for c in _tarjan_scc(m, self._adj)]
        comps.sort(key=lambda c: c[0])
        self._groups = comps
        self._group_of = [0] * m
        for gi, comp in enumerate(comps):
            for i in comp:
                self._group_of[i] = gi
        gedges = {}
        for (u, v), evs in self._edges.items():
            gu, gv = self._group_of[u], self._group_of[v]
            if gu != gv:
                gedges.setdefault((gu, gv), []).extend(evs)
        self._group_edges = gedges
        # 分层：level 0 = 不依赖任何组，可最先批准；仅作调度提示，层内互不约束
        g = len(comps)
        succ = [set() for _ in range(g)]
        indeg = [0] * g
        for (gu, gv) in gedges:
            if gv not in succ[gu]:
                succ[gu].add(gv)
                indeg[gv] += 1
        topo = []
        dq = deque([x for x in range(g) if indeg[x] == 0])
        while dq:
            x = dq.popleft()
            topo.append(x)
            for y in succ[x]:
                indeg[y] -= 1
                if indeg[y] == 0:
                    dq.append(y)
        level = [0] * g
        for x in reversed(topo):
            if succ[x]:
                level[x] = 1 + max(level[y] for y in succ[x])
        self._levels = level

    def _resolve(self, selected):
        out = []
        for s in selected:
            if isinstance(s, str):
                key = s
            elif isinstance(s, int) and not isinstance(s, bool):
                key = str(s)
            else:
                key = None
            if key is None or key not in self._index_of_ext:
                raise InputError([{"path": "$.select",
                                   "message": "未知的更正 id: %r" % (s,)}])
            out.append(self._index_of_ext[key])
        return sorted(set(out))

    def _edge_json(self, u, v):
        ext = self.model.ext_ids
        return {"when": ext[u], "requires": ext[v],
                "evidence": self._edges[(u, v)]}

    def _corr_brief(self, c):
        return {"id": c.ext_id, "op": c.op, "table": c.table,
                "row_id": c.identity, "group": self._group_of[c.index]}

    # -- 查询 ------------------------------------------------------------

    def analysis(self):
        """不可再拆的同批组 + 必要的先后关系（含证据）。"""
        ext = self.model.ext_ids
        implications = [self._edge_json(u, v)
                        for (u, v) in sorted(self._edges)]
        groups = [{"id": gi,
                   "corrections": [ext[i] for i in comp],
                   "must_batch": len(comp) > 1}
                  for gi, comp in enumerate(self._groups)]
        order = [{"group": gu, "requires_group": gv,
                  "evidence": self._group_edges[(gu, gv)]}
                 for (gu, gv) in sorted(self._group_edges)]
        g = len(self._groups)
        max_level = max(self._levels) if g else -1
        levels = [[x for x in range(g) if self._levels[x] == lv]
                  for lv in range(max_level + 1)]
        return {
            "status": "ok",
            "semantics": {
                "when_requires": "若 when 对应的更正在某阶段被批准，"
                                 "则 requires 必须同批或更早批准",
                "group": "同组更正不可再拆，必须同批；"
                         "order 中 group 若被批准，requires_group 必须同批或更早批准；"
                         "levels 仅为一种合法调度提示，层内各组互不约束",
            },
            "summary": {
                "tables": len(self.model.table_order),
                "rows": sum(len(self.model.current[t])
                            for t in self.model.table_order),
                "corrections": len(self.model.corrections),
                "implications": len(self._edges),
                "groups": g,
                "must_batch_groups": sum(1 for x in groups if x["must_batch"]),
                "order_edges": len(order),
            },
            "corrections": [self._corr_brief(c) for c in self.model.corrections],
            "implications": implications,
            "groups": groups,
            "order": order,
            "levels": levels,
        }

    def check(self, selected):
        """判断所选更正集合是否为可合法发布的阶段，并给出缺口的证据。"""
        idx = self._resolve(selected)
        sel = set(idx)
        ext = self.model.ext_ids
        violations = []
        missing = set()
        for u in sorted(sel):
            for v in self._adj[u]:
                if v not in sel:
                    missing.add(v)
                    violations.append(self._edge_json(u, v))
        return {
            "status": "ok",
            "selected": [ext[i] for i in idx],
            "valid": not violations,
            "missing": [ext[i] for i in sorted(missing)],
            "violations": violations,
        }

    def closure(self, selected, chains=False):
        """包含所选更正的最小合法集合；补齐的每项带证据追溯回请求。

        每个补齐项给出 via：引入它的那条蕴含边（含证据）。沿 via.when
        递归回溯必到达某个所请求的更正——追溯链唯一由 BFS 父指针确定。
        chains=True 时额外展开完整证据链（长链上链长可能为 O(n)，
        全量展开总代价 O(n²)，交互核对小清单时使用）。
        """
        idx = self._resolve(selected)
        ext = self.model.ext_ids
        seen = set(idx)
        parent = {}
        dq = deque(idx)
        while dq:
            u = dq.popleft()
            for v in self._adj[u]:
                if v not in seen:
                    seen.add(v)
                    parent[v] = u
                    dq.append(v)
        added = []
        for x in sorted(seen - set(idx)):
            entry = {"correction": ext[x],
                     "via": self._edge_json(parent[x], x)}
            if chains:
                chain = []
                y = x
                while y in parent:
                    p = parent[y]
                    chain.append(self._edge_json(p, y))
                    y = p
                chain.reverse()
                entry["chain"] = chain
            added.append(entry)
        return {
            "status": "ok",
            "requested": [ext[i] for i in idx],
            "minimal_set": [ext[i] for i in sorted(seen)],
            "added": added,
        }

    def evaluate(self, selected):
        """直接对所选阶段做完整仓表的全约束求值（与 check 互为印证）。"""
        idx = self._resolve(selected)
        ext = self.model.ext_ids
        violations = find_violations(self.model, set(idx))
        return {
            "status": "ok",
            "approved": [ext[i] for i in idx],
            "valid": not violations,
            "violations": violations,
        }


# ---------------------------------------------------------------------------
# 函数式入口
# ---------------------------------------------------------------------------

def analyze_document(doc):
    return Analyzer(doc).analysis()


def check_stage(doc, selected):
    return Analyzer(doc).check(selected)


def minimal_closure(doc, selected, chains=False):
    return Analyzer(doc).closure(selected, chains=chains)


def evaluate_stage(doc, selected):
    return Analyzer(doc).evaluate(selected)


def run(doc, command="analyze", selected=None, chains=False):
    """面向 JSON 的入口：返回 (结果dict, 退出码)，不抛业务异常。

    退出码：0 正常（含 valid=false 的判定结果）；2 输入不合法；3 拒绝拆批。
    """
    try:
        analyzer = Analyzer(doc)
        if command == "analyze":
            return analyzer.analysis(), 0
        if command == "validate":
            return {"status": "ok",
                    "summary": analyzer.analysis()["summary"]}, 0
        selected = selected or []
        if command == "check":
            return analyzer.check(selected), 0
        if command == "closure":
            return analyzer.closure(selected, chains=chains), 0
        if command == "evaluate":
            return analyzer.evaluate(selected), 0
        return {"status": "error", "kind": "usage",
                "message": "未知命令: %r" % (command,)}, 1
    except InputError as e:
        return {"status": "error", "kind": "invalid_input",
                "message": "模式、仓表或更正单不合法",
                "problems": e.problems}, 2
    except Refusal as e:
        msg = ("更正前仓表已不满足约束" if e.kind == "invalid_initial_state"
               else "全部更正后的仓表不满足约束")
        return {"status": "error", "kind": e.kind,
                "message": msg + "，拒绝给出拆批结论",
                "violations": e.violations}, 3
