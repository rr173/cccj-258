# -*- coding: utf-8 -*-
"""corrsplit 命令行入口。

只读分析（输入为一份 JSON 文档，不修改任何数据）：
    python -m corrsplit analyze  输入.json [--pretty]
    python -m corrsplit validate 输入.json [--pretty]
    python -m corrsplit check    输入.json 更正id... [--pretty]
    python -m corrsplit closure  输入.json 更正id... [--chains] [--pretty]
    python -m corrsplit evaluate 输入.json 更正id... [--pretty]

本地仓库（三表仓的版本化发布与账单交付）：
    python -m corrsplit init    仓库目录 初始化.json [--pretty]
    python -m corrsplit publish 仓库目录 批次.json [--pretty]
    python -m corrsplit bills   仓库目录 [--pretty]
    python -m corrsplit changes 仓库目录 版本号 [--pretty]
    python -m corrsplit verify  仓库目录 [--pretty]
    python -m corrsplit dump    仓库目录 [--pretty]

输入路径为 "-" 时从标准输入读取。退出码：
    0 正常（包括 check/evaluate 判定为不合法——那是一个“答案”）；
    2 模式、仓表、更正单或参数不合法；
    3 仓表不合法被拒绝（分析：初始或全部更正后不合法；发布：整批应用后不合法）；
    4 发布版本不符（期望基准版本与当前版本不一致，未发布）；
    1 其他错误（文件读取、JSON 解析、用法错误、复核未通过等）。
"""

import argparse
import json
import sys

from . import billing, core

_ANALYSIS_COMMANDS = ("analyze", "validate", "check", "closure", "evaluate")


def _load_json(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _emit_error(kind, message):
    json.dump({"status": "error", "kind": kind, "message": message},
              sys.stderr, ensure_ascii=False)
    sys.stderr.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="corrsplit",
        description="更正单拆批审批分析（只读）与三表仓的版本化发布、账单交付")
    sub = parser.add_subparsers(dest="command", metavar="命令", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="缩进美化输出")

    def add_analysis(name, help_text, select=False):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("input", help="输入 JSON 文件路径，'-' 表示标准输入")
        if select:
            p.add_argument("select", nargs="*", metavar="ID",
                           help="参与查询的更正 id，可空格分隔或逗号分隔")
        return p

    add_analysis("analyze", "同批组 + 必要先后关系（含证据）")
    add_analysis("validate", "仅校验输入与前置条件")
    add_analysis("check", "所选集合是否为可合法发布的阶段", select=True)
    p = add_analysis("closure", "包含所选的最小合法集合", select=True)
    p.add_argument("--chains", action="store_true",
                   help="为每个补齐项展开完整证据链")
    add_analysis("evaluate", "对所选阶段直接做全约束求值", select=True)

    p = sub.add_parser("init", parents=[common],
                       help="用完整三表数据初始化工作库及版本 0 的账单")
    p.add_argument("repo", help="仓库目录（须不存在或为空目录）")
    p.add_argument("input", help="初始化 JSON 文件路径，'-' 表示标准输入")
    p = sub.add_parser("publish", parents=[common],
                       help="按预期基准版本发布一批更正并交付账单变更")
    p.add_argument("repo", help="仓库目录")
    p.add_argument("input", help="批次 JSON 文件路径，'-' 表示标准输入")
    p = sub.add_parser("bills", parents=[common],
                       help="读取带版本的当前账单")
    p.add_argument("repo", help="仓库目录")
    p = sub.add_parser("changes", parents=[common],
                       help="重新读取某次成功发布的账单变更")
    p.add_argument("repo", help="仓库目录")
    p.add_argument("version", type=int, help="成功发布的版本号")
    p = sub.add_parser("verify", parents=[common],
                       help="复核仓库：仓表约束、账单重算、变更回放、版本链")
    p.add_argument("repo", help="仓库目录")
    p = sub.add_parser("dump", parents=[common],
                       help="导出当前仓表（核对用）")
    p.add_argument("repo", help="仓库目录")

    args = parser.parse_args(argv)

    try:
        if args.command in _ANALYSIS_COMMANDS:
            doc = _load_json(args.input)
            selected = [s for arg in (getattr(args, "select", None) or [])
                        for s in arg.split(",") if s]
            result, code = core.run(doc, args.command, selected,
                                    chains=getattr(args, "chains", False))
        elif args.command == "init":
            result, code = billing.run_init(args.repo, _load_json(args.input))
        elif args.command == "publish":
            result, code = billing.run_publish(args.repo, _load_json(args.input))
        elif args.command == "bills":
            result, code = billing.run_bills(args.repo)
        elif args.command == "changes":
            result, code = billing.run_changes(args.repo, args.version)
        elif args.command == "verify":
            result, code = billing.run_verify(args.repo)
        else:  # dump
            result, code = billing.run_dump(args.repo)
    except (OSError, ValueError) as e:
        _emit_error("io_error", "读取或解析输入失败: %s" % e)
        return 1
    except Exception as e:  # 兜底：内部错误不应以 traceback 形式暴露给调用方
        _emit_error("internal_error", "%s: %s" % (type(e).__name__, e))
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False,
              indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
