# -*- coding: utf-8 -*-
"""corrsplit 命令行入口。

用法：
    python -m corrsplit analyze  输入.json [--pretty]
    python -m corrsplit validate 输入.json [--pretty]
    python -m corrsplit check    输入.json 更正id... [--pretty]
    python -m corrsplit closure  输入.json 更正id... [--pretty]
    python -m corrsplit evaluate 输入.json 更正id... [--pretty]

输入路径为 "-" 时从标准输入读取。退出码：
    0 正常（包括 check/evaluate 判定为不合法——那是一个“答案”）；
    2 模式、仓表或更正单不合法；
    3 初始或全部更正后的仓表不合法，拒绝拆批；
    1 其他错误（文件读取、JSON 解析、用法错误等）。
"""

import argparse
import json
import sys

from . import core


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="corrsplit",
        description="更正单拆批审批分析（只读）：同批组、必要先后关系、"
                    "阶段合法性判定、最小合法集合")
    parser.add_argument("command",
                        choices=["analyze", "validate", "check",
                                 "closure", "evaluate"],
                        help="analyze=同批组与先后关系；check=所选集合是否为合法阶段；"
                             "closure=包含所选的最小合法集合；"
                             "evaluate=对所选阶段直接做全约束求值；"
                             "validate=仅校验输入与前置条件")
    parser.add_argument("input", help="输入 JSON 文件路径，'-' 表示标准输入")
    parser.add_argument("select", nargs="*", metavar="ID",
                        help="参与 check/closure/evaluate 的更正 id，"
                             "可空格分隔或逗号分隔")
    parser.add_argument("--pretty", action="store_true",
                        help="缩进美化输出")
    parser.add_argument("--chains", action="store_true",
                        help="closure：为每个补齐项展开完整证据链"
                             "（默认只给直接证据边 via，沿 via 可递归追溯）")
    args = parser.parse_args(argv)

    try:
        if args.input == "-":
            doc = json.load(sys.stdin)
        else:
            with open(args.input, "r", encoding="utf-8") as f:
                doc = json.load(f)
    except (OSError, ValueError) as e:
        json.dump({"status": "error", "kind": "io_error",
                   "message": "读取或解析输入失败: %s" % e},
                  sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 1

    selected = [s for arg in args.select for s in arg.split(",") if s]
    try:
        result, code = core.run(doc, args.command, selected,
                                chains=args.chains)
    except Exception as e:  # 兜底：内部错误不应以 traceback 形式暴露给调用方
        json.dump({"status": "error", "kind": "internal_error",
                   "message": "%s: %s" % (type(e).__name__, e)},
                  sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False,
              indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
