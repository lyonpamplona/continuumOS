"""Autenticação web baseada em senha bcrypt e cookie JWT."""

import jwt
import bcrypt
import redis.asyncio as redis
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from fastapi import Request
from loguru import logger
from config import settings
import database as db
from tenant_context import set_current_tenant
from tenant_config import load_tenant_config

# Chave secreta para assinar os cookies de login.
# Fase 0 (Hardening): sem fallback hardcoded. O segredo é obrigatório e já
# validado no boot por config.Config (JWT_SECRET required=True).
SECRET_KEY = settings.JWT_SECRET
ALGORITHM = getattr(settings, "JWT_ALGORITHM", "HS256") or "HS256"
SESSION_TTL_SECONDS = int(getattr(settings, "SESSION_TTL_SECONDS", 24 * 60 * 60))
ENFORCE_SINGLE_SESSION = bool(getattr(settings, "ENFORCE_SINGLE_SESSION", False))
session_redis = redis.from_url(settings.REDIS_URL)

async def verify_user(username: str, password: str):
    """Valida login e senha contra usuários ativos cadastrados no banco."""
    try:
        users = await db.get_all_system_users()
        for user in users:
            if user.get('username', '').lower() == username.lower() and user.get('is_active', 1):
                stored_hash = user.get('password')
                if stored_hash:
                    # Verifica se a senha digitada bate com a criptografia do banco
                    if bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8')):
                        return user
        return None
    except Exception as e:
        logger.error(f"Erro na verificação de usuário: {e}")
        return None

def create_access_token(data: dict):
    """Cria um JWT de sessão web usando o TTL configurado para login."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def register_login_session(username: str, role: str):
    """Registra o `jti` no Redis somente quando a trava de sessão única está ativa."""
    session_id = str(uuid4())
    normalized = username.strip().lower()
    if role == "admin" or not ENFORCE_SINGLE_SESSION:
        return session_id, None
    created = await session_redis.set(f"active_session:{normalized}", session_id, ex=SESSION_TTL_SECONDS, nx=True)
    if not created:
        return None, "Este usuário já está logado em outro local. Peça para sair da sessão anterior ou chame o admin."
    return session_id, None

async def validate_login_session(username: str, role: str, session_id: str):
    """Confirma a sessão ativa quando a trava Redis estiver habilitada."""
    if role == "admin" or not ENFORCE_SINGLE_SESSION:
        return True
    if not session_id:
        logger.warning(f"Sessão rejeitada sem jti: {username}")
        return False
    current = await session_redis.get(f"active_session:{username.strip().lower()}")
    if isinstance(current, bytes):
        current = current.decode("utf-8")
    if current != session_id:
        logger.warning(f"Sessão rejeitada por jti inválido ou ausente: {username}")
        return False
    await session_redis.expire(f"active_session:{username.strip().lower()}", SESSION_TTL_SECONDS)
    return True

async def clear_login_session(username: str, session_id: str = None):
    """Remove a sessão ativa quando a trava de sessão única estiver habilitada."""
    if not username or not ENFORCE_SINGLE_SESSION:
        return
    key = f"active_session:{username.strip().lower()}"
    if session_id:
        current = await session_redis.get(key)
        if isinstance(current, bytes):
            current = current.decode("utf-8")
        if current != session_id:
            return
    await session_redis.delete(key)

async def clear_session_from_request(request: Request):
    """Encerra a sessão Redis associada ao cookie atual, quando a trava estiver ativa."""
    if not ENFORCE_SINGLE_SESSION:
        return
    token = request.cookies.get("session_token")
    if not token:
        return
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        await clear_login_session(payload.get("sub"), payload.get("jti"))
    except Exception:
        return

async def get_current_user(request: Request):
    """Lê o cookie de sessão, valida o JWT e retorna o usuário ativo."""
    # Fase 1 (Multi-tenancy): resolve o tenant da requisição e ativa o contexto,
    # de modo que a busca do usuário e todas as queries seguintes fiquem
    # restritas a esse tenant via RLS/GUC.
    tenant_id = await db.resolve_request_tenant_id(request)
    set_current_tenant(tenant_id)
    # Fase 3: aquece a config do tenant para leituras síncronas nos handlers.
    await load_tenant_config(tenant_id)
    try:
        request.state.tenant_id = tenant_id
    except Exception:
        pass
    # Fase 4: bloqueia o acesso de tenants suspensos.
    if await db.get_tenant_status(tenant_id) != "active":
        return None
    token = request.cookies.get("session_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        session_id: str = payload.get("jti")
        if not username:
            logger.warning("Token de sessão sem usuário (`sub`).")
            return None
            
        users = await db.get_all_system_users()
        for user in users:
            if user.get('username', '').lower() == username.lower() and user.get('is_active', 1):
                if not await validate_login_session(username, user.get("role"), session_id):
                    return None
                return user
        logger.warning(f"Usuário do token não encontrado ou inativo: {username}")
    except jwt.ExpiredSignatureError:
        logger.info("Token de sessão expirado.")
    except jwt.InvalidTokenError as e:
        logger.warning(f"Token de sessão inválido: {e}")
    except Exception as e:
        logger.error(f"Erro ao validar sessão web: {e}")
        return None
    return None
