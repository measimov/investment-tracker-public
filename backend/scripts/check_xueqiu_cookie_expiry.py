#!/usr/bin/env python3
"""雪球 Cookie 探活/告警。

雪球的 xq_a_token 约 15 天过期，而过期在本项目里**不会**表现为一个显眼的
故障：行情侧雪球只是兜底，Tushare 成功就永远轮不到它；档案侧只在
`sync_symbol_profile` 的 `failed` 里多两条数据集记录。等到被发现时，
xueqiu_* 数据集往往已经断更很久。

两道检查，各自独立可用：

1. **到期日**（离线，读 Cookie 文件的 expirationDate）——唯一能在过期
   **之前**告警的信号，但只有浏览器插件的完整导出才带这个字段；
   {name: value} 形状的 Cookie 只能靠第 2 道。
2. **探活**（`--probe`，发一次真实请求）——权威判据，但只能在已经坏掉
   之后告警，且要花一次 2-4s 的限速请求。

退出码：0 正常 / 1 warning / 2 critical / 3 配置缺失。适合挂 cron 或
Uptime Kuma 的 push 监控。

    python scripts/check_xueqiu_cookie_expiry.py            # 只看到期日
    python scripts/check_xueqiu_cookie_expiry.py --probe    # 顺带发一次真实请求
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings  # noqa: E402

# 登录态的主凭证：这两个一过期，全部接口立刻失效
PRIMARY_AUTH_COOKIES = ("xq_a_token", "xqat")

EXIT_OK, EXIT_WARNING, EXIT_CRITICAL, EXIT_UNCONFIGURED = 0, 1, 2, 3


def load_expirations(cookie_file: Path) -> Dict[str, float]:
    """读取浏览器导出里的 expirationDate（秒）。缺字段的形状返回空 dict。"""
    data = json.loads(cookie_file.read_text(encoding="utf-8"))
    cookies = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(cookies, list):
        return {}
    out: Dict[str, float] = {}
    for cookie in cookies:
        name = str(cookie.get("name", ""))
        expiration = cookie.get("expirationDate")
        if name in PRIMARY_AUTH_COOKIES and expiration is not None:
            out[name] = float(expiration)
    return out


def check_expiry(cookie_file: Optional[str], *, warn_days: float, critical_days: float,
                 now: Optional[float] = None) -> Dict[str, Any]:
    """到期日检查。返回 {level, message, days_left, cookie}。"""
    now = now or time.time()
    if not cookie_file:
        return {
            "level": "unconfigured",
            "message": "未配置 XUEQIU_COOKIE_FILE，跳过到期日检查"
                       "（XUEQIU_COOKIES 形状不含 expirationDate）",
            "days_left": None, "cookie": "",
        }
    path = Path(cookie_file)
    if not path.is_file():
        return {"level": "critical", "message": f"Cookie 文件不存在：{path}",
                "days_left": None, "cookie": ""}
    try:
        expirations = load_expirations(path)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return {"level": "critical", "message": f"Cookie 文件无法解析：{exc}",
                "days_left": None, "cookie": ""}

    if not expirations:
        return {
            "level": "unconfigured",
            "message": f"{path} 不含 expirationDate（非浏览器完整导出），"
                       "无法预判到期；请改用 --probe",
            "days_left": None, "cookie": "",
        }

    days = {name: (value - now) / 86400 for name, value in expirations.items()}
    cookie, days_left = min(days.items(), key=lambda item: item[1])
    if days_left <= critical_days:
        level = "critical"
    elif days_left <= warn_days:
        level = "warning"
    else:
        level = "normal"
    return {
        "level": level, "cookie": cookie, "days_left": days_left,
        "message": (f"雪球 Cookie {level}：{cookie} 还有 {days_left:.1f} 天到期"
                    f"（warn={warn_days:g}d critical={critical_days:g}d）"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检查雪球 Cookie 到期日，并可选发一次真实请求探活。")
    parser.add_argument("--cookie-file", default=settings.xueqiu_cookie_file or "")
    parser.add_argument("--warn-days", type=float, default=settings.xueqiu_cookie_warn_days)
    parser.add_argument("--critical-days", type=float, default=settings.xueqiu_cookie_critical_days)
    parser.add_argument("--probe", action="store_true",
                        help="额外发一次真实请求确认登录态（花一次 2-4s 限速请求）")
    args = parser.parse_args()

    result = check_expiry(
        args.cookie_file, warn_days=args.warn_days, critical_days=args.critical_days
    )
    print(result["message"])
    exit_code = {
        "normal": EXIT_OK, "warning": EXIT_WARNING,
        "critical": EXIT_CRITICAL, "unconfigured": EXIT_UNCONFIGURED,
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

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
