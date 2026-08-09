from sqlalchemy import Column, Integer, String, Boolean, DateTime, text
from sqlalchemy.sql import func
from ..database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    # server_default 与 Python default 成对（CLAUDE.md 约定）：绕过 ORM 的写入
    # （修数 SQL、COPY、conftest 的裸 INSERT）不该在这些列上报错。
    is_active = Column(Boolean, default=True, server_default=text("true"), nullable=False)
    is_admin = Column(Boolean, default=False, server_default=text("false"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
