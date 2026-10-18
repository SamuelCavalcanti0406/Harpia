
"""SQLite persistence and explicit, backwards-compatible schema migration."""
from contextlib import contextmanager, closing
import os
from pathlib import Path
import sqlite3
import uuid

DB_PATH = Path(os.getenv('HARPIA_DB_PATH', str(Path(__file__).resolve().parent / 'harpia.db')))

@contextmanager
def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        with conn:
            yield conn
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        # Serialize migration across server workers before inspecting the schema.
        conn.execute('BEGIN IMMEDIATE')
        columns = {r['name'] for r in conn.execute('PRAGMA table_info(conversas)')}
        if columns and 'owner_id' not in columns:
            # Use SQLite backup (also captures WAL), not a raw file copy.
            backup_path = DB_PATH.with_name(DB_PATH.name + '.pre_auth_' + uuid.uuid4().hex + '.bak')
            with closing(sqlite3.connect(str(DB_PATH))) as source:
                with closing(sqlite3.connect(str(backup_path))) as target:
                    source.backup(target)
        conn.execute('CREATE TABLE IF NOT EXISTS usuarios (id TEXT PRIMARY KEY, nome TEXT NOT NULL UNIQUE)')
        conn.execute('''CREATE TABLE IF NOT EXISTS credenciais (
            token_hash TEXT PRIMARY KEY, usuario_id TEXT NOT NULL REFERENCES usuarios(id),
            revogada INTEGER NOT NULL DEFAULT 0)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS conversas (
            id TEXT PRIMARY KEY, titulo TEXT NOT NULL, criada_em TEXT NOT NULL,
            atualizada_em TEXT NOT NULL, owner_id TEXT REFERENCES usuarios(id))''')
        if columns and 'owner_id' not in columns:
            conn.execute('ALTER TABLE conversas ADD COLUMN owner_id TEXT REFERENCES usuarios(id)')
        conn.execute('''CREATE TABLE IF NOT EXISTS mensagens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversa_id TEXT NOT NULL REFERENCES conversas(id), papel TEXT NOT NULL,
            conteudo TEXT NOT NULL, criada_em TEXT NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS tentativas (
            id TEXT NOT NULL, usuario_id TEXT NOT NULL REFERENCES usuarios(id),
            conversa_id TEXT NOT NULL REFERENCES conversas(id), pergunta TEXT NOT NULL,
            estado TEXT NOT NULL CHECK(estado IN ('pendente','concluida','falhou')),
            expira_em REAL NOT NULL, resposta TEXT, fontes TEXT NOT NULL DEFAULT '[]',
            erro TEXT, criada_em TEXT NOT NULL, PRIMARY KEY(usuario_id,id))''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_conversas_owner ON conversas(owner_id, atualizada_em)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_mensagens_conversa ON mensagens(conversa_id,id)')
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tentativa_pendente ON tentativas(conversa_id) WHERE estado='pendente'")
        conn.execute('PRAGMA user_version=1')

