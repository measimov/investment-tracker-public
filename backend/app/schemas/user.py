from typing import Optional
from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field

# 口令下限。6 位对暴露公网的部署过弱（在线爆破的可行域太小），提到 10。
# 只约束 API 侧新设/改设的口令；seed 用的初始口令来自 env，不经这层校验。
MIN_PASSWORD_LENGTH = 10


class UserBase(BaseModel):
    """Base user schema with common fields"""

    username: str = Field(..., min_length=3, max_length=50)
    email: Optional[EmailStr] = None
    is_active: bool = True
    is_admin: bool = False


class UserCreate(UserBase):
    """Schema for creating a new user（继承 UserBase，约束只维护一份，issue #137）"""

    password: str = Field(..., min_length=MIN_PASSWORD_LENGTH)


class UserUpdate(BaseModel):
    """Schema for updating user information"""

    username: Optional[str] = Field(None, min_length=3, max_length=50)
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None


class UserPasswordUpdate(BaseModel):
    """Schema for updating user password"""

    old_password: str
    new_password: str = Field(..., min_length=MIN_PASSWORD_LENGTH)


class UserPasswordReset(BaseModel):
    """Schema for admin resetting user password"""

    new_password: str = Field(..., min_length=MIN_PASSWORD_LENGTH)


class User(UserBase):
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
