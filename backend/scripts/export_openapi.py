"""导出 OpenAPI schema 到 stdout，供前端类型生成（issue #141）。

用法（frontend 的 `npm run generate:api-types` 会调用）：

    python scripts/export_openapi.py > openapi.json

只生成 schema：不连库、不签发令牌、不启动 worker——但 app 配置是必填的，
这里为缺失项提供哑值（本脚本可能在没有 .env 的 CI 前端 job 里跑）。
输出做 sort_keys，保证同一后端两次导出逐字节一致（CI 漂移检查靠 diff）。
"""

import json
import os
import sys

for key, value in {
    "DATABASE_URL": "postgresql://localhost/schema_export_dummy",
    "SECRET_KEY": "schema-export-dummy",
    "ADMIN_INITIAL_PASSWORD": "schema-export-dummy",
    "DEMO_INITIAL_PASSWORD": "schema-export-dummy",
}.items():
    os.environ.setdefault(key, value)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app  # noqa: E402

json.dump(app.openapi(), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
sys.stdout.write("\n")
