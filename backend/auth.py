"""Opaque individual credentials. Only hashes are persisted; no public signup."""
import hashlib
import secrets
import uuid
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from storage import get_conn

bearer = HTTPBearer(auto_error=False)

def token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def usuario_atual(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if credentials is None or credentials.scheme.lower() != 'bearer' or len(credentials.credentials) > 256:
        raise HTTPException(401, 'Credencial ausente ou inválida.', headers={'WWW-Authenticate': 'Bearer'})
    with get_conn() as conn:
        row = conn.execute('SELECT usuario_id FROM credenciais WHERE token_hash=? AND revogada=0',
                           (token_hash(credentials.credentials),)).fetchone()
    if row is None:
        raise HTTPException(401, 'Credencial ausente ou inválida.', headers={'WWW-Authenticate': 'Bearer'})
    return row['usuario_id']

def criar_usuario(nome):
    nome = nome.strip()
    if not nome or len(nome) > 80:
        raise ValueError('Nome deve ter entre 1 e 80 caracteres.')
    user_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute('INSERT INTO usuarios(id,nome) VALUES (?,?)', (user_id,nome))
        conn.execute('INSERT INTO credenciais(token_hash,usuario_id) VALUES (?,?)', (token_hash(token),user_id))
    return user_id, token
