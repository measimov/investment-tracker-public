from typing import Annotated, Optional
from datetime import datetime
from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

from ..core.security import MAX_PASSWORD_BYTES
from .read_models import read_model

# 口令下限。6 位对暴露公网的部署过弱（在线爆破的可行域太小），提到 10。
# 只约束 API 侧新设/改设的口令；seed 用的初始口令来自 env，不经这层校验。
MIN_PASSWORD_LENGTH = 10


def _within_bcrypt_limit(value: str) -> str:
    # bcrypt 只取前 72 **字节**，bcrypt 5.x 对超长口令直接抛错（此前改密/建用户会 500，#269）。
    # 按 UTF-8 字节数计：一个汉字占 3 字节
    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"密码过长：最多 {MAX_PASSWORD_BYTES} 个字节（约 {MAX_PASSWORD_BYTES} 个英文字符"
            f"或 {MAX_PASSWORD_BYTES // 3} 个汉字）"
        )
    return value


# 新设/改设的口令：长度下限 + bcrypt 字节上限
NewPassword = Annotated[
    str, Field(min_length=MIN_PASSWORD_LENGTH), AfterValidator(_within_bcrypt_limit)
]


class UserBase(BaseModel):
    """Base user schema with common fields"""

    username: str = Field(..., min_length=3, max_length=50)
    email: Optional[EmailStr] = None
    is_active: bool = True
    is_admin: bool = False


class UserCreate(UserBase):
    """Schema for creating a new user（继承 UserBase，约束只维护一份，issue #137）"""

    password: NewPassword


class UserUpdate(BaseModel):
    """Schema for updating user information"""

    username: Optional[str] = Field(None, min_length=3, max_length=50)
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None


class UserPasswordUpdate(BaseModel):
    """Schema for updating user password"""

    old_password: str
    new_password: NewPassword


class UserPasswordReset(BaseModel):
    """Schema for admin resetting user password"""

    new_password: NewPassword


class User(read_model(UserBase, email=(Optional[str], None))):
    """Schema for user responses (without password)"""

    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class Token(BaseModel):
    """Schema for JWT token response"""

    access_token: str
    token_type: str


class LoginRequest(BaseModel):
    """Schema for login request"""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Schema for login response"""

    user: User
