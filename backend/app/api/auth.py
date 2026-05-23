from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.user import User
from ..schemas.user import (
    LoginRequest,
    LoginResponse,
    User as UserSchema,
    UserPasswordUpdate
)
from ..core.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    ACCESS_TOKEN_EXPIRE_MINUTES
)
from ..core.deps import get_current_active_user
from ..config import settings
from ..core.logging import get_app_logger

# Get logger
logger = get_app_logger("auth")

router = APIRouter(prefix="/api/auth", tags=["authentication"])


def is_secure_request(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded_proto.lower() == "https"


@router.post("/login", response_model=LoginResponse)
def login(
    request: Request,
    login_data: LoginRequest,
    db: Session = Depends(get_db)
):
    """
    User login endpoint with audit logging.

    Args:
        request: HTTP request object for logging client info
        login_data: Login credentials (username and password)
        db: Database session

    Returns:
        LoginResponse with access token and user info

    Raises:
        HTTPException: If credentials are invalid
    """
    if settings.require_https and not is_secure_request(request):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Login requires HTTPS"
        )

    # Get client information for logging
    client_ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "Unknown")

    # Find user by username
    user = db.query(User).filter(User.username == login_data.username).first()

    # Verify user exists and password is correct
    if not user or not verify_password(login_data.password, user.hashed_password):
        # Log failed login attempt
        logger.warning(
            f"Failed login attempt - Username: {login_data.username}, "
            f"IP: {client_ip}, User-Agent: {user_agent}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if user is active
    if not user.is_active:
        logger.warning(
            f"Inactive user login attempt - Username: {login_data.username}, "
            f"IP: {client_ip}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user"
        )

    # Create access token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username},
        expires_delta=access_token_expires
    )

    # Log successful login
    logger.info(
        f"Successful login - Username: {user.username}, "
        f"IP: {client_ip}, Admin: {user.is_admin}"
    )

    # Return token and user info
    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=UserSchema.model_validate(user),
    )


@router.get("/me", response_model=UserSchema)
def get_current_user_info(
    current_user: User = Depends(get_current_active_user)
):
    """
    Get current logged-in user information.

    Args:
        current_user: The current authenticated user

    Returns:
        User information
    """
    return UserSchema.model_validate(current_user)


@router.put("/me/password")
def change_password(
    password_data: UserPasswordUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    Change current user's password.

    Args:
        password_data: Old and new password
        current_user: The current authenticated user
        db: Database session

    Returns:
        Success message

    Raises:
        HTTPException: If old password is incorrect
    """
    # Verify old password
    if not verify_password(password_data.old_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect password"
        )

    # Update password
    current_user.hashed_password = get_password_hash(password_data.new_password)
    db.commit()

    return {"message": "Password updated successfully"}
