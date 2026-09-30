#!/usr/bin/env python3
"""雪球 Cookie 探活/告警。

雪球的 xq_a_token 约 15 天过期，而过期在本项目里**不会**表现为一个显眼的
故障：行情侧雪球只是兜底，Tushare 成功就永远轮不到它；档案侧只在
`sync_symbol_profile` 的 `failed` 里多两条数据集记录；发言采集器（xueqiu-collector）
则开始整轮失败。等到被发现时，xueqiu_* 数据集与观点数据往往已经断更很久。

两道检查，各自独立可用：

1. **到期日**（离线，读 Cookie 文件的 expirationDate；主凭证 xq_a_token/xqat
   缺失直接 critical）——唯一能在过期**之前**告警的信号，但只有浏览器插件的
   完整导出才带这个字段；{name: value} 形状的 Cookie 只能靠第 2 道。
2. **探活**（`--probe`，发一次真实请求）——权威判据，但只能在已经坏掉
   之后告警，且要花一次 2-4s 的限速请求。

判据与采集器每轮自检、观点页状态卡共用 `app.services.xueqiu_collector.cookie_health`。

可选推送 Uptime Kuma（`--push-url` 或 `XUEQIU_COOKIE_PUSH_URL`）：normal 推 up，
warning/critical/配置缺失推 down。`--status up|down --message ...` 推一条自定义状态
（不做检查）。

退出码：0 正常 / 1 warning / 2 critical / 3 配置缺失或推送失败。适合挂 cron 或
Uptime Kuma 的 push 监控。

    python scripts/check_xueqiu_cookie_expiry.py            # 只看到期日
    python scripts/check_xueqiu_cookie_expiry.py --probe    # 顺带发一次真实请求
    python scripts/check_xueqiu_cookie_expiry.py --push-url https://kuma/api/push/xxx
"""

import argparse
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings  # noqa: E402
from app.services.xueqiu_collector.cookie_health import (  # noqa: E402
    PRIMARY_AUTH_COOKIES,  # noqa: F401  # 兼容旧导入
    check_expiry,
    push_status,
)

EXIT_OK, EXIT_WARNING, EXIT_CRITICAL, EXIT_UNCONFIGURED = 0, 1, 2, 3


def _push(url: str, status: str, message: str, timeout: float) -> bool:
    import requests

    try:
        push_status(url, status=status, message=message, timeout=timeout)
    except requests.RequestException as exc:
        print(f"推送 Uptime Kuma 失败：{exc}", file=sys.stderr)
        return False
    print(f"已推送 Uptime Kuma：{status}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查雪球 Cookie 到期日，并可选发一次真实请求探活。"
    )
    parser.add_argument("--cookie-file", default=settings.xueqiu_cookie_file or "")
    parser.add_argument("--warn-days", type=float, default=settings.xueqiu_cookie_warn_days)
    parser.add_argument("--critical-days", type=float, default=settings.xueqiu_cookie_critical_days)
    parser.add_argument(
        "--probe", action="store_true", help="额外发一次真实请求确认登录态（花一次 2-4s 限速请求）"
    )
    parser.add_argument(
        "--push-url",
        default=os.environ.get("XUEQIU_COOKIE_PUSH_URL", ""),
        help="Uptime Kuma push URL（默认取 XUEQIU_COOKIE_PUSH_URL）",
    )
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    parser.add_argument(
        "--status", choices=["up", "down"], help="推送一条自定义状态（不做 Cookie 检查）"
    )
    parser.add_argument("--message", help="与 --status 配合的消息")
    args = parser.parse_args()

    if args.status:
        message = args.message or f"雪球采集状态：{args.status}"
        print(message)
        if args.push_url and not args.dry_run:
            return EXIT_OK if _push(args.push_url, args.status, message, args.timeout) else 3
        if not args.push_url:
            print("未设置 push URL，跳过推送。")
        return EXIT_OK

    result = check_expiry(
        args.cookie_file, warn_days=args.warn_days, critical_days=args.critical_days
    )
    print(result["message"])
    exit_code = {
        "normal": EXIT_OK,
        "warning": EXIT_WARNING,
        "critical": EXIT_CRITICAL,
        "unconfigured": EXIT_UNCONFIGURED,
    }[result["level"]]

    if args.probe:
        from app.services.xueqiu_source import probe

        probe_result = probe()
        print(f"探活：{'成功' if probe_result['ok'] else '失败'} —— {probe_result['detail']}")
        # 探活是权威判据：它失败就一定是 critical，哪怕到期日看着还早
        if not probe_result["ok"]:
            exit_code = max(exit_code, EXIT_CRITICAL)
        elif exit_code == EXIT_UNCONFIGURED:
            exit_code = EXIT_OK

    if args.push_url and not args.dry_run:
        status = "up" if exit_code == EXIT_OK else "down"
        if not _push(args.push_url, status, result["message"], args.timeout):
            return max(exit_code, EXIT_UNCONFIGURED)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
