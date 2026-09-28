"""雪球发言采集器（收纳自 xueqiu-timeline-archiver 的用户发言监控）。

分层：`signer`（签名，纯函数）/ `client`（HTTP、限速、WAF、Cookie）/ `timeline` /
`comments` / `detail`（解析 + 翻页 IO）/ `store`（归档表 upsert，SQL 逐字照搬原实现）/
`state`（scan_runs、作者名单、进程状态、心跳）/ `runner`（一轮采集与常驻循环）/
`cookie_health`（Cookie 到期判据 + Uptime Kuma 推送）。

按标的监控（收纳自 monitor_symbols，改为落库）：`feed_parsing`（请求构造与解析，纯函数）/
`feed_store`（公告·讨论 / 组合调仓的幂等 upsert 与读取；热帖表只剩旧 Markdown 导入写入）/
`symbols`（标的范围与每日一轮）。市场热帖的采集与展示已于 2026-09-28 下线。

运行在独立进程（`manage.py xueqiu-collector`，compose 服务 xueqiu-collector），
不占 Web 进程的 worker 车道。
"""
