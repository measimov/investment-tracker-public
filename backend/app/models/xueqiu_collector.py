"""雪球发言采集器的表（`services/xueqiu_collector/`）。

五张 `xueqiu_archiver_*` 表原由独立仓库 xueqiu-timeline-archiver 用原生 DDL
`create table if not exists` 建出并写入，采集收纳进本仓后纳入 Alembic
（迁移 20260927_0024）。**列、类型、默认值、索引名与原 DDL 逐字一致**——生产库
里这几张表早已存在且有存量数据，迁移对它们是零改动；这里的声明只为让模型成为
事实来源（autogenerate 不再提议删表、`test_model_server_defaults` 能核对默认值）。
原仓库的列一律没有 comment（加 comment 会让 autogenerate 对存量表出 diff），
因此说明写在类 docstring 里。

写入语义（冲突键与各列的覆盖规则）在 `services/xueqiu_collector/store.py`，同样
逐字照搬原实现，与存量行无缝衔接。
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Date,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..database import Base


class XueqiuArchiverPost(Base):
    """候选帖（作者主页出现过的原帖/被转发帖）。detail_enriched=已抓过全文。"""

    __tablename__ = "xueqiu_archiver_posts"

    post_id = Column(Text, primary_key=True)
    url = Column(Text, nullable=False)
    title = Column(Text, nullable=False, server_default=sa_text("''"))
    author_id = Column(Text, nullable=False, server_default=sa_text("''"))
    author_name = Column(Text, nullable=False, server_default=sa_text("''"))
    text = Column(Text, nullable=False, server_default=sa_text("''"))
    # 原仓库遗留：本地时间文本（业务时区），时间比较一律用 created_at_ms
    created_at = Column(Text, nullable=False, server_default=sa_text("''"))
    created_at_ms = Column(BigInteger, nullable=False, server_default=sa_text("0"))
    source = Column(Text, nullable=False, server_default=sa_text("''"))
    detail_enriched = Column(Boolean, nullable=False, server_default=sa_text("false"))
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_detail_fetch_at = Column(DateTime(timezone=True))


class XueqiuArchiverReply(Base):
    """评论区里命中的目标作者回复。reply_key = comment_id（缺时退回组合键）。"""

    __tablename__ = "xueqiu_archiver_replies"
    __table_args__ = (
        Index(
            "idx_xar_replies_target_created",
            "target_user_id",
            sa_text("created_at_ms DESC"),
        ),
        Index("idx_xar_replies_post", "post_id"),
    )

    reply_key = Column(Text, primary_key=True)
    target_user_id = Column(Text, nullable=False)
    post_id = Column(
        Text,
        ForeignKey("xueqiu_archiver_posts.post_id", ondelete="CASCADE"),
        nullable=False,
    )
    post_url = Column(Text, nullable=False)
    comment_id = Column(Text, nullable=False, server_default=sa_text("''"))
    created_at = Column(Text, nullable=False, server_default=sa_text("''"))
    author_id = Column(Text, nullable=False, server_default=sa_text("''"))
    author_name = Column(Text, nullable=False, server_default=sa_text("''"))
    text = Column(Text, nullable=False, server_default=sa_text("''"))
    like_count = Column(Integer, nullable=False, server_default=sa_text("0"))
    created_at_ms = Column(BigInteger, nullable=False, server_default=sa_text("0"))
    reply_to = Column(Text, nullable=False, server_default=sa_text("''"))
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class XueqiuArchiverPostScanState(Base):
    """(作者, 帖) 的评论扫描状态：冷却期判断用 last_scanned_at。"""

    __tablename__ = "xueqiu_archiver_post_scan_state"

    target_user_id = Column(Text, primary_key=True)
    post_id = Column(
        Text,
        ForeignKey("xueqiu_archiver_posts.post_id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_scanned_at = Column(DateTime(timezone=True))
    last_max_page = Column(Integer, nullable=False, server_default=sa_text("0"))
    last_checked_page_count = Column(Integer, nullable=False, server_default=sa_text("0"))
    last_waf_at = Column(DateTime(timezone=True))


class XueqiuArchiverScanRun(Base):
    """每位作者每轮一行。原仓库建了表但从不写；收纳后开始写入。

    status / error_message / waf_hit / utterance_count 是收纳时追加的列
    （ALTER ... ADD COLUMN IF NOT EXISTS，只增不改）。status：running / ok /
    partial / error / failed / waf / interrupted。观点页活性判据 = 最近一次 ok/partial 的
    finished_at（error = 时间线首页拿不到合法响应，绝不算成功）。
    """

    __tablename__ = "xueqiu_archiver_scan_runs"

    run_id = Column(BigInteger, primary_key=True, autoincrement=True)
    target_user_id = Column(Text, nullable=False)
    author_user_id = Column(Text)
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at = Column(DateTime(timezone=True))
    candidate_count = Column(Integer, nullable=False, server_default=sa_text("0"))
    reply_count = Column(Integer, nullable=False, server_default=sa_text("0"))
    stopped_early = Column(Boolean, nullable=False, server_default=sa_text("false"))
    status = Column(Text, nullable=False, server_default=sa_text("''"))
    error_message = Column(Text, nullable=False, server_default=sa_text("''"))
    waf_hit = Column(Boolean, nullable=False, server_default=sa_text("false"))
    utterance_count = Column(Integer, nullable=False, server_default=sa_text("0"))


class XueqiuArchiverUtterance(Base):
    """关注作者的全部发言（主页帖/回复/转发 + 评论区回复）——观点摘要的唯一输入。

    utterance_key：主页 `profile:{uid}:{post_id}`、评论 `comment:{comment_id}`。
    """

    __tablename__ = "xueqiu_archiver_utterances"
    __table_args__ = (
        Index(
            "idx_xar_utterances_target_created",
            "target_user_id",
            sa_text("created_at_ms DESC"),
        ),
    )

    utterance_key = Column(Text, primary_key=True)
    target_user_id = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    source_id = Column(Text, nullable=False, server_default=sa_text("''"))
    kind = Column(Text, nullable=False)
    post_id = Column(Text, nullable=False, server_default=sa_text("''"))
    post_url = Column(Text, nullable=False, server_default=sa_text("''"))
    created_at = Column(Text, nullable=False, server_default=sa_text("''"))
    created_at_ms = Column(BigInteger, nullable=False, server_default=sa_text("0"))
    author_id = Column(Text, nullable=False, server_default=sa_text("''"))
    author_name = Column(Text, nullable=False, server_default=sa_text("''"))
    text = Column(Text, nullable=False, server_default=sa_text("''"))
    context_post_id = Column(Text, nullable=False, server_default=sa_text("''"))
    context_url = Column(Text, nullable=False, server_default=sa_text("''"))
    context_author_name = Column(Text, nullable=False, server_default=sa_text("''"))
    context_text = Column(Text, nullable=False, server_default=sa_text("''"))
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class XueqiuCollectorAuthor(Base):
    """关注作者名单（取代原仓库的 config/monitor_users.txt），观点页管理员维护。"""

    __tablename__ = "xueqiu_collector_authors"
    __table_args__ = (
        CheckConstraint("xueqiu_user_id ~ '^[0-9]{1,20}$'", name="ck_xueqiu_collector_authors_user_id"),
    )

    xueqiu_user_id = Column(Text, primary_key=True, comment="雪球用户数字 ID")
    display_name = Column(Text, nullable=False, server_default=sa_text("''"), comment="展示名")
    enabled = Column(Boolean, nullable=False, server_default=sa_text("true"))
    note = Column(Text, nullable=False, server_default=sa_text("''"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_run_at = Column(DateTime(timezone=True), comment="最近一次采集结束时间")
    last_status = Column(
        Text, nullable=False, server_default=sa_text("''"), comment="ok / failed / waf / interrupted"
    )
    last_message = Column(Text, nullable=False, server_default=sa_text("''"))


class XueqiuCollectorState(Base):
    """采集器进程状态单行表（id=1）：心跳、上一轮结果、WAF 冷却与「立即运行」请求。

    Web 进程只写 run_requested_at（管理员点「立即运行」），采集器进程轮询消费——
    抓取永远不在 Web 进程里跑。
    """

    __tablename__ = "xueqiu_collector_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_xueqiu_collector_state_singleton"),)

    id = Column(Integer, primary_key=True, autoincrement=False)
    heartbeat_at = Column(DateTime(timezone=True))
    run_requested_at = Column(DateTime(timezone=True))
    last_cycle_started_at = Column(DateTime(timezone=True))
    last_cycle_finished_at = Column(DateTime(timezone=True))
    last_cycle_status = Column(Text, nullable=False, server_default=sa_text("''"))
    last_cycle_message = Column(Text, nullable=False, server_default=sa_text("''"))
    last_waf_at = Column(DateTime(timezone=True))
    # 每日按标的采集（迁移 0025）：与作者轮次分开记——观点页活性判据只看作者的
    # scan_runs，按标的采集不写 scan_runs，免得「标的采集成功」掩盖作者采集停摆
    symbols_run_requested_at = Column(
        DateTime(timezone=True), comment="管理员请求立即跑一轮按标的采集"
    )
    symbols_last_started_at = Column(DateTime(timezone=True))
    symbols_last_finished_at = Column(DateTime(timezone=True))
    symbols_last_status = Column(Text, nullable=False, server_default=sa_text("''"))
    symbols_last_message = Column(Text, nullable=False, server_default=sa_text("''"))
    symbols_last_business_date = Column(
        Date, comment="上一次每日一轮所属的业务日（同一业务日不重跑）"
    )
    symbols_last_stats = Column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"), comment="上一轮计数"
    )
    symbols_pending = Column(
        JSONB,
        comment="当日待重试：{date, attempts, items|null}；items=null 表示整轮重跑",
    )


# --------------------------------------------------------------------------- #
# 按标的监控（迁移 20260927_0025）：公告/讨论、热帖快照、组合调仓、组合名单。
# 与上面五张原表不同，这几张是本仓新建的表：列带 comment、载荷一律 JSONB。
# --------------------------------------------------------------------------- #
class XueqiuSymbolPost(Base):
    """标的的雪球公告流 / 讨论流（每日一轮，每标的每类最新 N 条）。

    身份键是**本仓的 (symbol, market)**——雪球 symbol（SH600519 / 00700）只在采集时
    经 `to_xueqiu` 在内存里生成，绝不作身份键落库（url 只是跳转地址，知道作者时用
    `/{uid}/{id}` 形态）。
    同一条讨论帖同时提及两只被跟踪的标的时会出现在两只的讨论流里，因此唯一键带上
    (symbol, market)：只按 (kind, post_id) 唯一会让它在两只标的之间来回改归属。
    """

    __tablename__ = "xueqiu_symbol_posts"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "market", "kind", "post_id", name="uq_xueqiu_symbol_posts_identity"
        ),
        CheckConstraint(
            "kind IN ('announcement', 'discussion')", name="ck_xueqiu_symbol_posts_kind"
        ),
        Index(
            "ix_xueqiu_symbol_posts_feed",
            "symbol",
            "market",
            "kind",
            sa_text("created_at_ms DESC"),
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, comment="本仓标的代码（裸码），不是雪球 symbol")
    market = Column(String(20), nullable=False, comment="本仓市场（A股/B股/港股/美股）")
    kind = Column(String(20), nullable=False, comment="announcement=公告 / discussion=讨论")
    post_id = Column(Text, nullable=False, comment="雪球帖子 ID")
    created_at_ms = Column(
        BigInteger, nullable=False, server_default=sa_text("0"), comment="发帖时间（毫秒）"
    )
    title = Column(Text, nullable=False, server_default=sa_text("''"))
    text = Column(Text, nullable=False, server_default=sa_text("''"), comment="纯文本正文")
    author_id = Column(Text, nullable=False, server_default=sa_text("''"))
    author_name = Column(Text, nullable=False, server_default=sa_text("''"))
    url = Column(Text, nullable=False, server_default=sa_text("''"), comment="雪球原帖链接")
    payload = Column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"),
        comment="裁剪后的原始字段（互动计数、公告附件链接等）",
    )
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class XueqiuHotPost(Base):
    """雪球市场热帖快照（statuses/hots）。rank / snapshot_at = 最近一次上榜的名次与
    快照时间；「今日热帖」= 该 scope 最新一次快照里的行。"""

    __tablename__ = "xueqiu_hot_posts"
    __table_args__ = (
        UniqueConstraint("scope", "post_id", name="uq_xueqiu_hot_posts_scope_post"),
        Index("ix_xueqiu_hot_posts_snapshot", "scope", sa_text("snapshot_at DESC"), "rank"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    scope = Column(String(20), nullable=False, comment="热帖口径（day / week）")
    post_id = Column(Text, nullable=False)
    rank = Column(Integer, nullable=False, server_default=sa_text("0"), comment="快照内名次（1 起）")
    snapshot_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="最近一次出现在热帖榜上的快照时间",
    )
    created_at_ms = Column(BigInteger, nullable=False, server_default=sa_text("0"))
    title = Column(Text, nullable=False, server_default=sa_text("''"))
    text = Column(Text, nullable=False, server_default=sa_text("''"))
    author_id = Column(Text, nullable=False, server_default=sa_text("''"))
    author_name = Column(Text, nullable=False, server_default=sa_text("''"))
    url = Column(Text, nullable=False, server_default=sa_text("''"))
    payload = Column(JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"))
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class XueqiuCubeRebalancing(Base):
    """雪球组合调仓记录（cubes/rebalancing/history）。payload 的调仓明细已把雪球
    symbol 换成本仓 (symbol, market)，识别不了的只留名称。"""

    __tablename__ = "xueqiu_cube_rebalancing"
    __table_args__ = (
        UniqueConstraint(
            "cube_id", "rebalancing_id", name="uq_xueqiu_cube_rebalancing_identity"
        ),
        Index(
            "ix_xueqiu_cube_rebalancing_cube_created", "cube_id", sa_text("created_at_ms DESC")
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cube_id = Column(String(32), nullable=False, comment="组合代号（ZH 开头）")
    rebalancing_id = Column(Text, nullable=False)
    created_at_ms = Column(BigInteger, nullable=False, server_default=sa_text("0"))
    payload = Column(JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"))
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class XueqiuCollectorCube(Base):
    """组合跟踪名单（取代原仓库的 config/monitor_cubes.txt；作者名单的兄弟表）。"""

    __tablename__ = "xueqiu_collector_cubes"
    __table_args__ = (
        CheckConstraint("cube_id ~ '^[A-Z]{2}[0-9]{1,20}$'", name="ck_xueqiu_collector_cubes_id"),
    )

    cube_id = Column(String(32), primary_key=True, comment="组合代号，如 ZH000001")
    display_name = Column(Text, nullable=False, server_default=sa_text("''"))
    enabled = Column(Boolean, nullable=False, server_default=sa_text("true"))
    note = Column(Text, nullable=False, server_default=sa_text("''"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_run_at = Column(DateTime(timezone=True))
    last_status = Column(Text, nullable=False, server_default=sa_text("''"))
    last_message = Column(Text, nullable=False, server_default=sa_text("''"))
