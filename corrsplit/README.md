# corrsplit —— 更正单拆批审批分析（只读）

更正一套主档和明细时，整张更正单一次装入是合规的，但审批人希望拆成小批。
本工具是**只读**分析器：输入当前仓表、表间约束与完整更正单，输出
**不可再拆的同批组**与**必要的先后关系**，并支持两类审批查询：

- 任意点选一组更正，判定它是不是一个**可合法发布的阶段**（`check`）；
- 对点选的若干更正，给出**包含它们的最小合法集合**，补齐的每项都能沿
  证据追溯回请求（`closure`）。

工具不修改任何数据，不自动级联、不补行；所有结论只来自输入的清单本身。

## 模型与约定

- 每张表有**不变的行身份**（主键，`identity`，默认 `id`）、可配置的
  **复合业务唯一键**（`unique_keys`）与指向某表行身份的**可空引用**
  （`references`）。
- 键和非空字段是有类型的整数（`int`）或字符串（`string`）；两者永不相等
  （`5 ≠ "5"`）。**空值不参与业务键冲突**（复合键任一分量为空即不参与），
  **引用为空时不检查**。
- 更正单中每行至多一条更正：新增 `add` / 删除 `delete` / 整行替换
  `replace`（不得改变行身份）。因此不存在“先改成临时业务号”的中间态。
- **阶段** = 已批准更正的任意子集：已批准行取更正后状态，未批准行取更正前
  状态。一个阶段可发布给下游的条件是**完整仓表（所有表）满足所有约束**，
  而非仅检查本批涉及的表。

## 为什么结果恰好是“同批组 + 先后关系”

每种约束违反至多涉及两行：唯一键冲突是两行键值相等；外键违反是引用行在场
而被引用行缺失。每行至多一条更正，所以每行的状态只取决于一个比特（该行的
更正是否已批准），每个潜在违反就是至多两个字面量的合取。在**初始仓表与
全部更正后的仓表均合法**（工具会先校验，不满足则拒绝）的前提下，剩下的
每个违反条件必然等价于一条简单蕴含：

> 若批准 X，则 Y 必须同批或更早批准（记作 X ⇒ Y）

由此得到严格而完整的结论：

| 概念 | 计算 |
| --- | --- |
| 合法阶段 | 在蕴含关系下封闭的集合（X 入选且 X⇒Y，则 Y 也入选） |
| 不可再拆的同批组 | 蕴含图上的**强连通分量** |
| 必要的先后关系 | 分量缩点后的 **DAG 边**（无必要时不强加顺序） |
| 包含所选的最小合法集合 | 所选节点的**可达闭包**（唯一、最小） |

互不依赖的两套换号分属不同分量且无 DAG 边，保持可单独审批；任一合法阶段
也都能按 DAG 的汇优先顺序扩展成一条每步合法的完整审批序列。

## 输入格式（JSON）

```json
{
  "tables": {
    "master": {
      "identity": "id",
      "columns": {"id": "int", "code": "string"},
      "unique_keys": [["code"]],
      "references": {}
    },
    "detail": {
      "identity": "id",
      "columns": {"id": "int", "master_id": "int", "item": "string"},
      "unique_keys": [["master_id", "item"]],
      "references": {"master_id": "master"}
    }
  },
  "rows": {
    "master": [{"id": 1, "code": "M1"}],
    "detail": [{"id": 10, "master_id": 1, "item": "a"}]
  },
  "corrections": [
    {"id": "add_m2", "op": "add", "table": "master",
     "row": {"id": 2, "code": "M2"}},
    {"id": "repoint", "op": "replace", "table": "detail", "row_id": 10,
     "row": {"id": 10, "master_id": 2, "item": "a"}},
    {"id": "del_m1", "op": "delete", "table": "master", "row_id": 1}
  ]
}
```

- `identity` 可省略，默认 `"id"`；`unique_keys` / `references` / `rows` /
  `corrections` 可省略，默认为空。
- `add` 用 `row` 给出整行（含行身份）；`delete` 用 `row_id`；`replace`
  两者都要，`row` 中的行身份必须等于 `row_id`。
- 更正 `id` 可省略，自动编号为 `c1..cn`；输出中以此引用更正。

## 命令行入口

```bash
cd corrsplit   # 包所在目录（或设置 PYTHONPATH 指向它）

python -m corrsplit analyze  输入.json            # 同批组 + 必要先后关系（含证据）
python -m corrsplit check    输入.json c3 c7      # 所选集合是否为可合法发布的阶段
python -m corrsplit closure  输入.json c3         # 包含所选的最小合法集合
python -m corrsplit evaluate 输入.json c3 c7      # 对所选阶段直接做全约束求值
python -m corrsplit validate 输入.json            # 仅校验输入与前置条件
```

输入路径 `-` 表示标准输入；`--pretty` 美化输出；`closure` 加 `--chains`
为每个补齐项展开完整证据链（默认只给直接证据边 `via`，沿 `via.when`
递归回溯必到达某个所请求的更正）。

退出码：`0` 正常（包括 check/evaluate 判定为不合法——那是一个答案）；
`2` 模式/仓表/清单不合法；`3` 初始或全部更正后的仓表不合法（拒绝拆批）；
`1` 其他错误。

## Python 入口

```python
from corrsplit import Analyzer, InputError, Refusal

analyzer = Analyzer(doc)            # doc 为输入 JSON 对应的 dict
analyzer.analysis()                 # 同批组、先后关系、蕴含及证据
analyzer.check(["del_m1"])          # -> {"valid": False, "missing": [...], "violations": [...]}
analyzer.closure(["del_m1"])        # -> {"minimal_set": [...], "added": [{"correction": ..., "via": ...}]}
analyzer.evaluate(["del_m1"])       # 直接全约束求值（与 check 互为印证）
```

多次查询请复用同一个 `Analyzer`；一次性调用可用 `analyze_document(doc)`、
`check_stage(doc, ids)`、`minimal_closure(doc, ids)`、`evaluate_stage(doc, ids)`。

## 输出要点

- `implications`：`{"when", "requires", "evidence"}`——`when` 若在某阶段被
  批准，`requires` 必须同批或更早批准。`evidence` 给出导致该结论的**实际
  行、字段与约束**（表、键列、键值/被引用行身份、两侧更正及其角色）。
- `groups`：同批组，`must_batch=true` 的组不可再拆。
- `order`：组间必要先后关系及证据；`levels` 只是一种合法调度提示，
  **层内各组互不约束**。
- `check`：`valid` 及缺口 `missing`/`violations`（含证据）。
- `closure`：`minimal_set` 与 `added`；每个补齐项的 `via` 是引入它的那条
  蕴含边，沿 `via.when` 递归回溯必到达某个所请求的更正（`--chains` 或
  `chains=True` 可直接展开完整链）。
- 拒绝：`{"status":"error","kind":"invalid_initial_state"|
  "invalid_final_state","violations":[...]}`，冲突定位到行、字段与约束。

## 复杂度

设 R 为总行数、C 为更正数、K 为唯一键定义总数：

- 预处理（校验 + 两次全表求值 + 蕴含生成）：O((R + C)·K + 引用非空数)。
  唯一键按键值散列分组，只有同键值的行才会相遇；引用按行身份散列定位，
  **不对无关联行做两两比较**。
- 由于初始/最终态合法，每个键值在每种状态下至多一行，故蕴含边数
  E = O(C + 引用非空数)；强连通分量、缩点、闭包均为 O(C + E)。
- `check` / `closure` 单次 O(C + E)，**不枚举任何审批子集**。

## 限制

- 只读：不修改仓表，不生成自动级联或补行建议；清单不完整导致最终态不合法
  时，拒绝并定位冲突，由人工补齐清单后重新分析。
- 字段类型仅支持 `int` / `string`（空值通用）。
- 行身份不可变；`replace` 为整行替换。

## 目录结构与测试

```
corrsplit/            # 包：core.py（模型+算法）、cli.py（命令行）
tests/test_corrsplit.py # 39 个测试：语义、拒绝、随机实例穷举等价、性能
examples/               # 换号对调、主从联动、互不依赖、最终态不合法示例
```

运行测试：`python tests/test_corrsplit.py`（其中随机测试在小实例上枚举
**全部**审批子集，验证图判定与直接全约束求值完全一致，并验证闭包最小性；
性能测试用 2.5 万条更正验证长清单下不枚举子集、不做两两比较）。
