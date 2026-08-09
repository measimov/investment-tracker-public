from datetime import timedelta
from secrets import token_urlsafe
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.user import User
from ..schemas.user import (
    LoginRequest,
    LoginResponse,
    Token,
    User as UserSchema,
    UserPasswordUpdate,
)
from ..core.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    AUTH_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    decode_access_token,
    get_password_hash,
    verify_password,
)
from ..core.deps import get_current_active_user, security
from ..services.auth_session_service import (
    cleanup_expired_sessions,
    issue_session,
    renew_session,
    revoke_session,
    revoke_user_sessions,
)
from ..config import settings
from ..core.logging import get_app_logger

# Get logger
logger = get_app_logger("auth")

router = APIRouter(prefix="/api/auth", tags=["authentication"])


def is_secure_request(request: Request) -> bool:
    """本次请求是否走在 HTTPS 上。

    这里**不再自己读 X-Forwarded-Proto**：反代头由最外层的
    core.proxy_headers.ProxyHeadersMiddleware 在路由之前按 trust_proxy_headers
    统一改写 scope，本函数只读改写后的结果。

    单点解析不只是整洁问题——路由之前发生的事（Starlette 的 slash redirect）
    也依赖 scheme，只在端点里判断修不了那条降级重定向。同理 uvicorn 自带的
    那层必须关掉（所有启动命令带 --no-proxy-headers），否则它按自己的信任规则
    抢先改写，应用层的开关就成了摆设。
    """
    return request.url.scheme == "https"


def audit_client_ip(request: Request) -> str:
    """审计日志用的客户端 IP（同样只读中间件改写后的 scope）。"""
    return request.client.host if request.client else "unknown"


def _require_secure_auth(request: Request) -> None:
    if settings.require_https and not is_secure_request(request):
        # 客户端只看得到 "Login requires HTTPS"，看不出是自己没走 HTTPS、还是
        # 后端在反代后面却没开 trust_proxy_headers。把两种可能连同补救办法写进
        # 服务端日志——否则每个撞上它的人都要从头查一遍配置。
        logger.warning(
            "Rejected plaintext auth request: scheme=%s, trust_proxy_headers=%s, "
            "x-forwarded-proto=%r. 开发环境请设 REQUIRE_HTTPS=false；"
            "反代后面请设 TRUST_PROXY_HEADERS=true 并确认反代覆写了 X-Forwarded-Proto。",
            request.url.scheme,
            settings.trust_proxy_headers,
            request.headers.get("x-forwarded-proto"),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Login requires HTTPS",
        )


def _authenticate_user(request: Request, login_data: LoginRequest, db: Session) -> User:
    _require_secure_auth(request)
    client_ip = audit_client_ip(request)
    user_agent = request.headers.get("user-agent", "Unknown")
    user = db.query(User).filter(User.username == login_data.username).first()

    if not user or not verify_password(login_data.password, user.hashed_password):
        logger.warning(
            "Failed login attempt - Username: %s, IP: %s, User-Agent: %s",
            login_data.username,
            client_ip,
            user_agent,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        logger.warning(
            "Inactive user login attempt - Username: %s, IP: %s",
            login_data.username,
            client_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )

    logger.info(
        "Successful login - Username: %s, IP: %s, Admin: %s",
        user.username,
        client_ip,
        user.is_admin,
    )
    return user


def _create_user_access_token(db: Session, user: User) -> str:
    # Opportunistically drop this user's expired sessions to bound table growth.
    cleanup_expired_sessions(db, user.id)
    token, _ = issue_session(
        db,
        user,
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return token


def _set_auth_cookies(response: Response, access_token: str) -> None:
    max_age = ACCESS_TOKEN_EXPIRE_MINUTES * 60
    cookie_options = {
        "max_age": max_age,
        "secure": settings.require_https,
        "samesite": "strict",
        "path": "/",
    }
    response.set_cookie(
        AUTH_COOKIE_NAME,
        access_token,
        httponly=True,
        **cookie_options,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token_urlsafe(32),
        httponly=False,
        **cookie_options,
    )


@router.post("/login", response_model=LoginResponse)
def login(
    request: Request, response: Response, login_data: LoginRequest, db: Session = Depends(get_db)
):
    """
    User login endpoint with audit logging.

    Args:
        request: HTTP request object for logging client info
        login_data: Login credentials (username and password)
        db: Database session

    Returns:
        LoginResponse with user info while authentication is stored in cookies

    Raises:
        HTTPException: If credentials are invalid
    """
    user = _authenticate_user(request, login_data, db)
    _set_auth_cookies(response, _create_user_access_token(db, user))
    return LoginResponse(
        user=UserSchema.model_validate(user),
    )


@router.post("/token", response_model=Token)
def create_api_token(
    request: Request,
    login_data: LoginRequest,
    db: Session = Depends(get_db),
):
    """Create a Bearer token for non-browser API clients."""
    user = _authenticate_user(request, login_data, db)
    return Token(
        access_token=_create_user_access_token(db, user),
        token_type="bearer",
    )


@router.post("/refresh", response_model=LoginResponse)
def refresh_session(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_active_user),
    credentials=Depends(security),
    db: Session = Depends(get_db),
):
    """滑动会话续期：延长当前会话（同一 jti）并重签 Cookie。

    不新建会话行——整条续期链共享一个 jti，显式登出吊销该 jti 即同时终止
    链上所有 JWT（含续期前已泄露的旧令牌）；同时在途请求携带的旧 Cookie
    仍指向同一有效会话，不会被轮换误判成 401。
    """
    token = (
        credentials.credentials if credentials else None
    ) or request.cookies.get(AUTH_COOKIE_NAME)
    payload = decode_access_token(token) if token else None
    jti = payload.get("jti") if payload else None
    renewed = renew_session(db, current_user, jti) if jti else None
    if renewed is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session can no longer be renewed",
        )
    _set_auth_cookies(response, renewed)
    return LoginResponse(
        user=UserSchema.model_validate(current_user),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_active_user),
    credentials=Depends(security),
    db: Session = Depends(get_db),
):
    """Revoke the current server-side session and clear the browser cookies."""
    token = (
        credentials.credentials if credentials else None
    ) or request.cookies.get(AUTH_COOKIE_NAME)
    payload = decode_access_token(token) if token else None
    jti = payload.get("jti") if payload else None
    if jti:
        revoke_session(db, jti)
        logger.info("Session revoked on logout - Username: %s", current_user.username)
    response.delete_cookie(
        AUTH_COOKIE_NAME,
        path="/",
        secure=settings.require_https,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=settings.require_https,
        httponly=False,
        samesite="strict",
    )


@router.get("/me", response_model=UserSchema)
def get_current_user_info(current_user: User = Depends(get_current_active_user)):
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
    db: Session = Depends(get_db),
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect password")

    # Update password
    current_user.hashed_password = get_password_hash(password_data.new_password)
    db.commit()

    # Invalidate every outstanding session (all devices and API tokens).
    revoked = revoke_user_sessions(db, current_user.id)
    logger.info(
        "Password changed - Username: %s, sessions revoked: %s",
        current_user.username,
        revoked,
    )

    return {"message": "Password updated successfully"}
