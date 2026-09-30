from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from ..config import settings

# JWT settings from config
SECRET_KEY = settings.secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes
AUTH_COOKIE_NAME = "investment_session"
CSRF_COOKIE_NAME = "investment_csrf"
# bcrypt 只处理前 72 字节；bcrypt 5.x 对更长的口令直接抛 ValueError（不再静默截断）
MAX_PASSWORD_BYTES = 72


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain password against a hashed password using bcrypt directly.

    Args:
        plain_password: The plain text password
        hashed_password: The hashed password from database

    Returns:
        True if password matches, False otherwise
    """
    encoded = plain_password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False  # 不可能是任何已存口令（设置时就不允许超长）
    try:
        return bcrypt.checkpw(encoded, hashed_password.encode("utf-8"))
    except Exception:
        return False


def get_password_hash(password: str) -> str:
    """
    Hash a password using bcrypt directly.

    Args:
        password: The plain text password

    Returns:
        The hashed password
    """
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        # API 入口的 schema 已拦下（中文 422）；走到这里的是 seed 等绕过 schema 的调用方
        raise ValueError(f"password exceeds bcrypt limit of {MAX_PASSWORD_BYTES} bytes")
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(encoded, salt)
    return hashed.decode("utf-8")


def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
    *,
    expires_at: Optional[datetime] = None,
) -> str:
    """
    Create a JWT access token.

    Args:
        data: The data to encode in the token (typically {"sub": username})
        expires_delta: Optional custom expiration time
        expires_at: 绝对到期时刻（tz-aware）。优先于 expires_delta——调用方已按
            会话的绝对截止点算好时，不能让 helper 在更晚的时刻重新加一遍 delta。

    Returns:
        The encoded JWT token string
    """
    to_encode = data.copy()

    if expires_at is not None:
        # 绝对时刻优先：调用方按会话的绝对截止点钳过位时，必须原样落到 exp 上。
        # 走 delta 的话，helper 在**更晚**的 t1 上重新取当前时间，exp 会变成
        # deadline + (t1 − t0)，钳位白做（慢 commit 时偏移尤其明显）。
        expire = expires_at.astimezone(timezone.utc)
    elif expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[dict]:
    """
    Decode and verify a JWT access token.

    Args:
        token: The JWT token string

    Returns:
        The decoded token data if valid, None otherwise
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None
