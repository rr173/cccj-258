# corrsplit —— 更正单拆批审批分析（只读）+ 三表仓版本化发布、账单交付与本地收款对账

本仓库提供三件事：

1. **只读分析器**（`corrsplit.core`）：输入当前仓表、表间约束与完整更正单，
   输出**不可再拆的同批组**与**必要的先后关系**，并支持两类审批查询
   （`check` / `closure`）。不修改任何数据。
2. **发布流程**（`corrsplit.billing`）：把审批通过的批次真正发布到本地仓库，
   维护版本化的仓表与账单，向账单消费者只交付**本次需要替换或撤销的账单**，
   无需下载整仓。
3. **本地收款对账**（`corrsplit.reconciliation`）：财务导入带账单版本的银行
   到账回执后，按完整发票键查看应收、累计到账、未结金额，并标出未匹配和超收。
   回执幂等、只追加，导入整批原子；对账结果始终按当前账单实时计算。

## 只读分析器

更正一套主档和明细时，整张更正单一次装入是合规的，但审批人希望拆成小批。
只读分析器输入当前仓表、表间约束与完整更正单，输出
**不可再拆的同批组**与**必要的先后关系**，并支持两类审批查询：

- 任意点选一组更正，判定它是不是一个**可合法发布的阶段**（`check`）；
- 对点选的若干更正，给出**包含它们的最小合法集合**，补齐的每项都能沿
  证据追溯回请求（`closure`）。

分析器不修改任何数据，不自动级联、不补行；所有结论只来自输入的清单本身。

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
`4` 发布版本不符；`1` 其他错误。

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

---

# 发布流程：三表仓的版本化发布与账单交付（corrsplit.billing）

结算同事用只读分析器确定哪些更正可以同批审批后，用本模块把批准批次
**真正发布到本地仓库**；账单消费者按版本只接收本次需要替换或撤销的账单，
不用再下载整仓。

## 模型（仅三张表）

- `payer(id)`：付款方。`id` 是不可更改的整数行身份。
- `contract(id, code, payer_id)`：合同。`code` 非空且唯一；`payer_id` 可空，
  非空时须指向存在的付款方。
- `charge(id, contract_id, period, invoice_no, cents)`：计费明细。
  `contract_id` 可空，非空时须指向存在的合同；`period` 为有效的
  `YYYY-MM` 月份；`invoice_no` 为非空字符串；`cents` 为有符号整数分；
  这三个字段都不能为 null。

**账单**：键 = (付款方行身份, 月份)。每条明细沿合同找到付款方后才计入账单，
链上任一引用为空就不计入。账单值 = **净额**（所有归属明细的分值相加，
不去掉负数）+ **不同 `invoice_no` 的数量**。同号明细可来自不同合同：金额
都计入，票号在同一账单内只计一次。还有归属明细、但净额为零的账单仍然存在。
改合同归属时，未直接更正的明细也随合同从旧付款方迁出、计入新付款方；同一批
若还改了这些明细，以整批前后实际归属为准，每条明细在各自状态只计一次。

仓库 = 一个目录，内含单个 SQLite 库文件 `billing.db`（仓表 + 当前账单 +
各版本变更）。**更正语义与只读分析器一致**：每行至多一条更正，新增 `add` /
删除 `delete` / 整行替换 `replace`（不得改变行身份），不自动补更正。

## 快速上手（命令行）

```bash
cd corrsplit   # 包所在目录（或设置 PYTHONPATH 指向它）

# 1) 初始化：用一份完整的三表数据建立工作库及版本 0 的账单（全量扫描底稿）
python -m corrsplit init /tmp/repo examples/billing_init.json --pretty

# 2) 发布：给出预期基准版本和一批已选好的更正，成功则产生下一版本
#    并交付该版本的账单变更
python -m corrsplit publish /tmp/repo examples/billing_batch1.json --pretty
python -m corrsplit publish /tmp/repo examples/billing_batch2.json --pretty

# 3) 读取：带版本的当前账单；按成功版本重新读取该次变更
python -m corrsplit bills   /tmp/repo --pretty
python -m corrsplit changes /tmp/repo 1 --pretty

# 4) 验证：仓表约束复核 + 账单全量重算比对 + 逐版本变更回放 + 版本链连续性
python -m corrsplit verify  /tmp/repo --pretty
python -m corrsplit dump    /tmp/repo --pretty   # 导出当前仓表（核对用）
```

初始化输入：`{"batch_id": 可选, "rows": {"payer": [...], "contract": [...],
"charge": [...]}}`。批次输入：`{"base_version": 预期基准版本,
"batch_id": 可选, "corrections": [与分析器相同的更正条目]}`。
样例见 `examples/billing_*.json`：

- `billing_init.json`：2 付款方、4 合同、7 明细，覆盖同号跨合同、负数、
  空链（不计入）等情形；版本 0 交付 3 张账单的 upsert。
- `billing_batch1.json`：合同改挂付款方（未直接更正的明细随之迁移，
  旧账单撤销）+ 明细改金额 + 同票号新增（票号不重复计）。
- `billing_batch2.json`：两条合同**同批互换 code**（合法换号，整批落库后
  才核对，不受中间状态影响；对账单无影响，故该部分不交付）+ 删除使一张
  账单撤销 + 新增一张账单。
- `billing_batch_invalid.json`：删除仍被明细引用的合同——整批后仓表不合法，
  拒绝并回滚（退出码 3）。

## 发布语义

- 每次成功发布产生**下一个版本**（当前版本 + 1），交付该版本的账单变更：
  - **upsert**：新增账单，以及金额或票数变化后仍存在的账单——交付完整新值
    `{"kind":"upsert","payer_id","period","net_cents","invoice_count"}`；
  - **revoke**：不再有归属明细的旧账单——只交付撤销键
    `{"kind":"revoke","payer_id","period"}`；
  - 账单值未变就不交付；允许没有账单变更的成功版本（如纯换号批）。
- **拒绝**（不产生新版本，不留任何痕迹）：
  - 期望基准版本与当前版本不符（`version_conflict`，退出码 4）；
  - 应用整批后仓表不合法（`invalid_batch_state`，退出码 3）——整批先全部
    落库再统一核对约束，因此批内合法换号不会因逐条写入的中间状态被拒绝；
  - 清单本身不合法（`invalid_input`，退出码 2）。
- **原子性**：更正后的仓表、当前账单、该版本的变更在同一个 SQLite 事务内
  生效；拒绝或进程意外退出后都看不到一半的新数据（崩溃测试见
  `tests/test_billing.py::TestCrashSafety`）。
- **并发**：发布在 `BEGIN IMMEDIATE` 写锁内比对版本号，一个版本只能被一批
  发布推进；并发提交同一基准版本时恰有一个成功，另一个只会看到版本不符，
  无法覆盖别人已成功发布的结果。
- **不可改写**：`version_log` / `version_change` 由数据库触发器保护，
  后续发布不能改写旧变更；`changes` 按版本重读的内容与发布时交付的逐字节
  一致。

## 小批发布的读取范围（性能设计）

全量初始化可以扫描底稿；之后的小批发布**不整仓重读、不扫描无关合同的
明细、不重算整张受影响账单**。发布所读的业务数据限于：

| 数据 | 定位方式 |
| --- | --- |
| 本批行的改前状态 | 按行身份点查 |
| 约束核对所需的关联行/占用记录 | `code` 唯一占用、`payer_id`/`contract_id` 索引、被删 payer/contract 的引用行 |
| 前后归属可能改变的明细 | 本批直接更正的明细 + `payer_id` 发生变化的合同名下的明细（`contract_id` 索引） |
| 直接涉及的账单与票号状态 | 按 (付款方, 月份[, 票号]) 点查，按增量套用 |

账单内部维护票号多重集合（`invoice_no` → 归属明细数），交付的票数即集合
大小；增量套用只触及变化的票号，**同一账单汇集再多未变合同也无需重算**。
读取整个当前账单视图（`bills`）与复核（`verify`）不受此限制。
每次发布的结果中带 `reads` 统计（按表的读取行数），可直接核对读取规模；
`tests/test_billing.py::TestScopedReads` 在 3 万条明细的仓上验证小批发布
的读取量与仓规模无关。

## 验证结果

`verify` 做四项独立复核（任一不过则整体失败，退出码 1）：

1. `warehouse_constraints`：当前仓表满足全部约束（全量复核）；
2. `bills_recomputed`：由仓表全量重算的账单与增量维护的账单状态
   （净额、明细数、票号多重集合）完全一致；
3. `changes_replay`：从版本 0 起逐版本回放已交付的变更，恰好重建当前
   账单视图——交付给消费者的变更流是完备的；
4. `version_chain`：版本链 0..当前版本 连续无缺口。

## Python 入口（发布）

```python
from corrsplit import Repository, VersionConflict, BillingRefusal

repo = Repository.init("仓库目录", rows)   # rows = {"payer": [...], ...}
repo = Repository.open("仓库目录")
result = repo.publish(base_version, corrections, batch_id=None)
#   -> {"status":"ok","version","changes":[...],"summary":{...},"reads":{...}}
repo.current_bills()     # {"version": v, "bills": [...]}
repo.changes(version)    # 重读该次成功发布的变更（含批次内容）
repo.verify()            # 四项复核
repo.dump_warehouse()    # 导出当前仓表
```

版本不符抛 `VersionConflict`；整批后仓表不合法抛 `BillingRefusal`
（`kind="invalid_batch_state"`，带冲突定位）；清单不合法抛 `InputError`；
均已在抛出前完整回滚。`Repository` 非线程安全，并发发布请每个线程/进程
各自 `open` 同一目录。

---

# 本地收款对账：导入回执并核对发票

收款对账复用上面的版本化仓库（`目录/billing.db`），不需要另起服务。新初始化的
仓库会自动包含 `receipt` 表；用旧版本代码初始化的仓库在第一次 `Repository.open`
或执行对账命令时，会幂等补上该表和只追加触发器。

## 回执批次格式

一批回执必须声明它对应的当前账单版本，并包含若干收款记录：

```json
{
  "bill_version": 0,
  "batch_id": "receipts-2026-01-demo",
  "receipts": [
    {"receipt_no": "R-0001", "payer_id": 1, "period": "2026-01",
     "invoice_no": "INV-1", "cents": 600},
    {"receipt_no": "R-0002", "payer_id": 1, "period": "2026-01",
     "invoice_no": "INV-1", "cents": 400}
  ]
}
```

- `bill_version`（也接受 `base_version` / `version`）必须等于仓库当前版本；
- `receipt_no` 是全局、稳定、非空的回执编号；
- 发票完整键 = `(payer_id, period, invoice_no)`，相同发票号在不同付款方或月份
  下分别对账；
- `cents` 必须是大于 `0` 的整数分；
- 付款方或发票当前不存在时，回执仍原样保存，不会被丢弃或挪到其他发票。
  输入也接受 `payments`/`records`、`month`、`receipt_id`、`paid_cents` 等常见
  近义字段名。

## 命令行入口

```bash
cd corrsplit

# 先有一个账单仓库（若已 init 可跳过）
python -m corrsplit init /tmp/corrsplit-recon examples/billing_init.json --pretty

# 导入回执；import-receipts 可简写为 receipts <repo> <file>
python -m corrsplit import-receipts /tmp/corrsplit-recon \
  examples/receipts_batch.json --pretty

# 查询当前对账结果；reconciliation 是同义命令
python -m corrsplit reconcile /tmp/corrsplit-recon --pretty
python -m corrsplit receipts /tmp/corrsplit-recon --pretty

# 查看原始回执
python -m corrsplit receipt-list /tmp/corrsplit-recon --pretty
```

导入成功结果给出 `inserted_count`、`duplicate_count` 和 `total_receipts`。
退出码沿用仓库约定：版本不符为 `4`，回执内容冲突或清单不合法为 `2`，
仓库/IO 问题为 `1`。

## Python 入口

```python
from corrsplit import Repository, ReceiptConflict, VersionConflict

repo = Repository.open("/tmp/corrsplit-recon")
repo.import_receipts(0, receipts, batch_id="receipts-2026-01-demo")
report = repo.reconcile()

for row in report["items"]:
    print(row["payer_id"], row["period"], row["invoice_no"],
          row["receivable_cents"], row["received_cents"],
          row["outstanding_cents"], row["status"])

report["invoices"]             # 仅当前仍存在的发票
report["unmatched_receipts"]   # 无当前发票的原始回执（扁平列表）
report["overpaid_invoices"]    # 超收发票分组
report["summary"]              # 各状态数量与金额合计
repo.close()
```

也可用模块级函数直接传仓库目录或已打开的 `Repository`：

```python
from corrsplit.reconciliation import import_receipts, reconcile

import_receipts("/tmp/corrsplit-recon", doc)
report = reconcile("/tmp/corrsplit-recon")
```

Python 行对象支持常见别名读取：`row["month"]`、`row["due_cents"]`、
`row["paid_cents"]`、`row["balance_cents"]`；状态对象与 `"paid"`、
`"partial_payment"`、`"未匹配"` 等常见别名/中文值相等。CLI 的 JSON 默认展开
这些别名字段；加 `--canonical-keys` 可只输出规范键。

## 金额与状态口径

| 概念 | 当前实现 |
| --- | --- |
| 应收 `receivable_cents` | 当前 `charge → contract → payer` 归属后，按 `(payer_id, period, invoice_no)` 对 `cents` 原值求和；负数不剔除，零合计但仍有归属明细的发票也存在 |
| 已收 `received_cents` | 该完整发票键下所有**不同回执编号**的金额之和；同号同内容重导不重复计算 |
| 未结 `outstanding_cents` | `receivable_cents - received_cents`，超收时为负数 |
| `unmatched` | 当前没有该完整发票键，但存在回执；完整外连接行中应收按零行合计显示为 `0`、未结为 `-received_cents`，同时在 `unmatched_receipts` 给出原始回执 |
| `unpaid` | 发票存在且已收为 `0`；零金额发票无回款时也按“未付款”处理 |
| `partial` | `0 < received_cents < receivable_cents` |
| `settled` | `received_cents == receivable_cents`（状态也兼容别名义 `paid`） |
| `overpaid` | `received_cents > receivable_cents` |

## 幂等、冲突与原子性

- 同一 `receipt_no` 再次导入且规范内容
  `(payer_id, period, invoice_no, cents)` 完全相同时跳过，不增加到账额；
- 编号已存在但上述任一字段不同，或同一批次内同号内容不同，抛
  `ReceiptConflict` / CLI `kind="receipt_conflict"`，整批回滚；
- 批次版本不等于当前账单版本时抛 `VersionConflict` / CLI 退出码 `4`，整批不写入；
- 结构校验、版本检查、全部编号冲突检查和插入都在一个 `BEGIN IMMEDIATE`
  SQLite 事务中；拒绝或 COMMIT 前进程退出都会回滚，不会留下部分新回执；
- 不保存可能过期的“对账结果表”。每次查询按当前仓表重算应收，因此账单更正
  发布后，金额变化、付款方迁移或发票消失会立即反映到新对账结果；
- 回执保存在原始 `(payer_id, period, invoice_no)` 下，账单发布逻辑不会改写
  或删除它们；数据库触发器也禁止 `UPDATE/DELETE receipt`。失去当前发票的款项
  自动变为 `unmatched`。

## 可复现小型样例

仓库自带 `examples/receipts_batch.json`，配合 `billing_init.json` 可一次看到
部分付款、未付款、结清、超收和未匹配：

```bash
rm -rf /tmp/corrsplit-recon
python3 -m corrsplit init /tmp/corrsplit-recon examples/billing_init.json
python3 -m corrsplit import-receipts /tmp/corrsplit-recon \
  examples/receipts_batch.json
python3 -m corrsplit reconcile /tmp/corrsplit-recon --canonical-keys
```

`items` 中对应结果为：

| payer | period | invoice | 应收 | 已收 | 未结 | status |
| ---: | --- | --- | ---: | ---: | ---: | --- |
| 1 | 2026-01 | INV-1 | 1500 | 1000 | 500 | partial |
| 1 | 2026-01 | INV-2 | -200 | 0 | -200 | unpaid |
| 1 | 2026-02 | INV-3 | 300 | 500 | -200 | overpaid |
| 2 | 2026-01 | INV-9 | 800 | 800 | 0 | settled |
| 999 | 2026-01 | INV-GHOST | 0 | 250 | -250 | unmatched |

重复导入同一文件会显示 `inserted_count: 0, duplicate_count: 5`，到账合计不变：

```bash
python3 -m corrsplit import-receipts /tmp/corrsplit-recon \
  examples/receipts_batch.json
```

`examples/receipts_conflict.json` 把 `R-0001` 的金额从 `600` 改成 `650`，并夹带
一条新回执 `R-NEW`。该命令以退出码 `2` 拒绝；随后查询 `receipt-list` 仍只有
原来的 5 条，`R-NEW` 不存在：

```bash
python3 -m corrsplit import-receipts /tmp/corrsplit-recon \
  examples/receipts_conflict.json
echo $?   # 2
```

继续发布现有账单更正，可以看到“回执不动、当前对账更新”：

```bash
python3 -m corrsplit publish /tmp/corrsplit-recon examples/billing_batch1.json
python3 -m corrsplit reconcile /tmp/corrsplit-recon --canonical-keys

python3 -m corrsplit publish /tmp/corrsplit-recon examples/billing_batch2.json
python3 -m corrsplit reconcile /tmp/corrsplit-recon --canonical-keys
```

第一版更正把合同 `12` 从付款方 `2` 改挂到付款方 `1`：`INV-9` 发票移动到
`(1, 2026-01, INV-9)` 且未付款，而 `R-0003` 仍留在 `(2, 2026-01, INV-9)`，
显示 `unmatched`。第二版更正删除 `INV-3` 的明细后，`R-0004` 仍留在
`(1, 2026-02, INV-3)`，同样变为 `unmatched`。此时再用版本 0 的回执批次导入会
得到退出码 `4` 和 `kind: version_conflict`；把批次版本改为当前版本后，同号同
内容仍按重复回执处理，不会重复计款。

## 目录结构与测试

```
corrsplit/                  # 包：core.py（只读分析）、billing.py（版本化发布）、
                            #   reconciliation.py（回执导入/对账）、cli.py（命令行）
tests/test_corrsplit.py     # 39 个测试：分析语义、拒绝、随机实例穷举等价、性能
tests/test_billing.py       # 44 个测试：初始化、发布、交付、拒绝回滚、并发、崩溃原子性、
                            #   读取范围、随机批次 vs 全量重算等价、CLI 全流程
tests/test_reconciliation.py # 对账测试：五种状态、完整发票键、幂等/冲突、版本拒绝、
                            #   账单更正后重算、崩溃回滚、CLI 退出码
examples/                   # 分析示例 + billing_* 发布示例 + receipts_* 回执示例
```

运行测试：`python3 tests/test_corrsplit.py`、`python3 tests/test_billing.py`、
`python3 tests/test_reconciliation.py`。
其中只读分析的随机测试在小实例上枚举**全部**审批子集，验证图判定与直接
全约束求值完全一致；发布流程的随机测试连续发布 80 个构造合法的随机批次
（穿插非法批次），逐版本与"全量重算 + 差分"的参考实现比对账单、变更与
仓表，并复核 `verify`。
