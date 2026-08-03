import type { FullConfig } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// 每次 E2E 运行前重置 e2e 库：跨运行残留数据是"既有用例偶发失败"的根因。
// 保留 alembic_version 与种子用户（admin/demo），其余数据表全部清空；
// 与后端进程启动顺序无关——后端无内存态，先起服务或先清库都成立。
const RESET_SCRIPT = `
import os
from sqlalchemy import create_engine, text

url = os.environ["DATABASE_URL"]
db_name = url.rsplit("/", 1)[-1].split("?")[0]
if "e2e" not in db_name and "test" not in db_name:
    raise SystemExit(f"refusing to reset non-e2e database: {db_name}")

engine = create_engine(url)
with engine.begin() as conn:
    tables = {
        row[0]
        for row in conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
    }
    keep = {"alembic_version", "users"}
    targets = sorted(tables - keep)
    if targets:
        quoted = ", ".join('"' + name + '"' for name in targets)
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
    if "users" in tables:
        conn.execute(text("DELETE FROM users WHERE username NOT IN ('admin', 'demo')"))
engine.dispose()
print(f"E2E database reset: {db_name} ({len(targets)} tables truncated)")
`

export default function globalSetup(_config: FullConfig) {
  const databaseUrl =
    process.env.E2E_DATABASE_URL || 'postgresql://postgres:postgres@127.0.0.1:5432/investment_e2e'
  const backendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../backend')

  execFileSync('python', ['-c', RESET_SCRIPT], {
    cwd: backendDir,
    env: { ...process.env, DATABASE_URL: databaseUrl },
    stdio: 'inherit'
  })
}
