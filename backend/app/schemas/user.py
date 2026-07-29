from typing import Optional
from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserBase(BaseModel):
    """Base user schema with common fields"""

    username: str = Field(..., min_length=3, max_length=50)
    email: Optional[EmailStr] = None
    is_active: bool = True
    is_admin: bool = False


class UserCreate(BaseModel):
    """Schema for creating a new user"""

    username: str = Field(..., min_length=3, max_length=50)
    email: Optional[EmailStr] = None
    password: str = Field(..., min_length=6)
    is_active: bool = True
    is_admin: bool = False


class UserUpdate(BaseModel):
    """Schema for updating user information"""

    username: Optional[str] = Field(None, min_length=3, max_length=50)
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None


class UserPasswordUpdate(BaseModel):
    """Schema for updating user password"""

    old_password: str
    new_password: str = Field(..., min_length=6)


class UserPasswordReset(BaseModel):
    """Schema for admin resetting user password"""

    new_password: str = Field(..., min_length=6)


class User(UserBase):
    """Schema for user responses (without password)"""

    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserInDB(User):
    """Schema for user in database (includes hashed password)"""

    hashed_password: str


class Token(BaseModel):
    """Schema for JWT token response"""

    access_token: str
    token_type: str


class TokenData(BaseModel):
    """Schema for decoded token data"""

    username: Optional[str] = None


class LoginRequest(BaseModel):
    """Schema for login request"""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Schema for login response"""

    user: User
