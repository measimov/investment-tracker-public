from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.user import User
from ..schemas.user import (
    User as UserSchema,
    UserCreate,
    UserUpdate,
    UserPasswordReset
)
from ..core.security import get_password_hash
from ..services.auth_session_service import revoke_user_sessions
from ..core.deps import get_current_admin_user

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=List[UserSchema])
def list_users(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """
    Get list of all users (admin only).

    Args:
        skip: Number of records to skip
        limit: Maximum number of records to return
        current_user: Current admin user
        db: Database session

    Returns:
        List of users
    """
    users = db.query(User).offset(skip).limit(limit).all()
    return [UserSchema.model_validate(user) for user in users]


@router.post("", response_model=UserSchema, status_code=status.HTTP_201_CREATED)
def create_user(
    user_data: UserCreate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """
    Create a new user (admin only).

    Args:
        user_data: User creation data
        current_user: Current admin user
        db: Database session

    Returns:
        Created user

    Raises:
        HTTPException: If username or email already exists
    """
    # Check if username already exists
    existing_user = db.query(User).filter(User.username == user_data.username).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )

    # Check if email already exists (if provided)
    if user_data.email:
        existing_email = db.query(User).filter(User.email == user_data.email).first()
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

    # Create new user
    db_user = User(
        username=user_data.username,
        email=user_data.email,
        hashed_password=get_password_hash(user_data.password),
        is_active=user_data.is_active,
        is_admin=user_data.is_admin
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    return UserSchema.model_validate(db_user)



@router.get("/{user_id}", response_model=UserSchema)
def get_user(
    user_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """Get user by ID (admin only). 兼容保留：外部 API 客户端可能依赖。"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    return UserSchema.model_validate(user)

# 所有"会缩小活跃管理员集合"的路径共用的事务级顾问锁。任意常量即可，只要
# 全仓唯一；取 users 表名的稳定哈希，避免和别处的 advisory lock 撞号。
_ADMIN_GUARD_LOCK_KEY = 0x7553_6572_4164_6D6E  # b"UserAdmn"


def _guard_last_active_admin(
    db: Session, *, target: User, current_user: User, removing: bool = False
) -> None:
    """挡住会让系统失去管理入口的改动（在 commit 之前调用）。

    两人小系统里一次误操作就能全员失去管理入口，之后只能进库改——所以拦在
    这里而不是靠使用者小心。两条独立的守卫：

    1. 管理员把**自己**降权或停用：停用还会顺带 revoke 掉自己的会话，
       当场把自己锁在门外；
    2. 改完之后系统里一个活跃管理员都不剩。

    **必须串行化**：光数一遍是不够的。两个活跃管理员并发互相降权时，每个
    事务都把对方排除在自己的目标之外、于是各自都看到"还剩一个活跃管理员"，
    两笔更新落在不同的行、都能提交，最终活跃管理员归零。update/delete 交叉
    同样触发。这里先取一把事务级顾问锁再计数：锁随事务结束自动释放，后来者
    必须等前者提交后重新计数，看到的就是更新后的真实剩余量。

    removing=True 用于删除路径（目标行整条消失，没有"新值"可言）。

    调用点在字段赋值之后、commit 之前：此时 target 已带上待写入的值，抛出即
    整笔回滚，不必手工还原字段。剩余数量排除 target 自己，它的新值用内存里
    的值算——还没进库，而且删除路径下它根本不该被计入。
    """
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADMIN_GUARD_LOCK_KEY})

    target_stays_admin = (not removing) and target.is_admin and target.is_active
    if target.id == current_user.id and not target_stays_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="不能撤销自己的管理员权限或停用自己的账号",
        )
    remaining = (
        db.query(User)
        .filter(User.is_admin.is_(True), User.is_active.is_(True), User.id != target.id)
        .count()
    )
    if remaining == 0 and not target_stays_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="系统必须保留至少一个活跃的管理员账号",
        )


@router.put("/{user_id}", response_model=UserSchema)
def update_user(
    user_id: int,
    user_data: UserUpdate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """
    Update user information (admin only).

    Args:
        user_id: User ID
        user_data: User update data
        current_user: Current admin user
        db: Database session

    Returns:
        Updated user

    Raises:
        HTTPException: If user not found or username/email already exists
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Check if new username already exists
    if user_data.username and user_data.username != user.username:
        existing_user = db.query(User).filter(User.username == user_data.username).first()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already registered"
            )
        user.username = user_data.username

    # Check if new email already exists
    if user_data.email and user_data.email != user.email:
        existing_email = db.query(User).filter(User.email == user_data.email).first()
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        user.email = user_data.email

    # Update other fields
    deactivated = False
    if user_data.is_active is not None:
        deactivated = user.is_active and not user_data.is_active
        user.is_active = user_data.is_active

    if user_data.is_admin is not None:
        user.is_admin = user_data.is_admin

    _guard_last_active_admin(db, target=user, current_user=current_user)

    db.commit()
    db.refresh(user)

    # Deactivation invalidates outstanding sessions, so re-enabling the user
    # later cannot resurrect tokens issued before the deactivation (issue #36).
    if deactivated:
        revoke_user_sessions(db, user.id)

    return UserSchema.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """
    Delete a user (admin only).

    Note: This will cascade delete all associated data (holdings, transactions, corporate actions).

    Args:
        user_id: User ID
        current_user: Current admin user
        db: Database session

    Raises:
        HTTPException: If user not found or trying to delete yourself
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Prevent deleting yourself
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account"
        )

    # 删除同样会缩小活跃管理员集合，必须和 update 走同一把锁重新计数：
    # A 删 B 与 B 降权 A 并发时，各自都以为对方还在。
    _guard_last_active_admin(db, target=user, current_user=current_user, removing=True)

    # 所有用户级表的 user_id 外键均为 ON DELETE CASCADE，删除交由数据库完成。
    db.delete(user)
    db.commit()


@router.put("/{user_id}/password")
def reset_user_password(
    user_id: int,
    password_data: UserPasswordReset,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    """
    Reset user password (admin only).

    Args:
        user_id: User ID
        password_data: New password data
        current_user: Current admin user
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: If user not found
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    user.hashed_password = get_password_hash(password_data.new_password)
    db.commit()

    # An admin reset invalidates every outstanding session of that user.
    revoke_user_sessions(db, user.id)

    return {"message": "Password reset successfully"}
