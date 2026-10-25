"""Índice de conhecimento persistente em SQLite FTS5, separado do banco de conversas.

O arquivo fica em `.local/conhecimento.db` (ou `HARPIA_INDEX_PATH`). Cada fonte guarda uma
assinatura (mtime+tamanho para textos, SHA-256 para PDFs); fontes inalteradas não são
reindexadas. Os trechos são inseridos em lotes, direto do gerador do conector.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sqlite3
import threading

from conectores import Conector, Fonte

INDEX_PATH = Path(os.getenv('HARPIA_INDEX_PATH',
                            str(Path(__file__).resolve().parent.parent / '.local' / 'conhecimento.db')))
LOTE = 200
logger = logging.getLogger('harpia.indice')
_trava_escrita = threading.Lock()
_preparados: set[str] = set()

ESQUEMA = '''
CREATE TABLE IF NOT EXISTS fontes (
    id INTEGER PRIMARY KEY,
    colecao TEXT NOT NULL,
    dono TEXT NOT NULL DEFAULT '',
    chave TEXT NOT NULL,
    nome TEXT NOT NULL,
    tipo TEXT NOT NULL,
    assinatura TEXT NOT NULL,
    trechos INTEGER NOT NULL DEFAULT 0,
    indexada_em TEXT NOT NULL,
    UNIQUE(colecao, dono, chave));
CREATE TABLE IF NOT EXISTS trechos (
    id INTEGER PRIMARY KEY,
    fonte_id INTEGER NOT NULL REFERENCES fontes(id),
    ordem INTEGER NOT NULL,
    pagina INTEGER);
CREATE INDEX IF NOT EXISTS idx_trechos_fonte ON trechos(fonte_id, ordem);
CREATE VIRTUAL TABLE IF NOT EXISTS trechos_fts USING fts5(
    conteudo, titulo, tokenize = 'unicode61 remove_diacritics 2');
'''


@contextmanager
def conexao():
    caminho = Path(INDEX_PATH)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    existia = caminho.exists()
    conn = sqlite3.connect(str(caminho), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # Apagar o arquivo com a HarpIA aberta apenas recria um índice vazio.
        if str(caminho) not in _preparados or not existia:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(ESQUEMA)
            _preparados.add(str(caminho))
        with conn:
            yield conn
    finally:
        conn.close()


def _remover(conn, fonte_id):
    conn.execute('DELETE FROM trechos_fts WHERE rowid IN (SELECT id FROM trechos WHERE fonte_id=?)', (fonte_id,))
    conn.execute('DELETE FROM trechos WHERE fonte_id=?', (fonte_id,))
    conn.execute('DELETE FROM fontes WHERE id=?', (fonte_id,))


def _indexar_fonte(conn, conector, fonte: Fonte):
    stamp = datetime.now(timezone.utc).isoformat()
    fonte_id = conn.execute(
        'INSERT INTO fontes(colecao,dono,chave,nome,tipo,assinatura,indexada_em) VALUES (?,?,?,?,?,?,?)',
        (fonte.colecao, fonte.dono, fonte.chave, fonte.nome, fonte.tipo, fonte.assinatura, stamp)).lastrowid
    titulo = conector.titulo(fonte)
    lote, total = [], 0
    for trecho in conector.trechos(fonte):
        lote.append(trecho)
        if len(lote) >= LOTE:
            total += _gravar_lote(conn, fonte_id, titulo, lote); lote = []
    total += _gravar_lote(conn, fonte_id, titulo, lote)
    conn.execute('UPDATE fontes SET trechos=? WHERE id=?', (total, fonte_id))
    return total


def _gravar_lote(conn, fonte_id, titulo, lote):
    for trecho in lote:
        rowid = conn.execute('INSERT INTO trechos(fonte_id,ordem,pagina) VALUES (?,?,?)',
                             (fonte_id, trecho.ordem, trecho.pagina)).lastrowid
        conn.execute('INSERT INTO trechos_fts(rowid,conteudo,titulo) VALUES (?,?,?)',
                     (rowid, trecho.conteudo, titulo))
    return len(lote)


def sincronizar(conector: Conector, remover_ausentes: bool = True) -> dict:
    """Indexa fontes novas ou alteradas e (opcionalmente) remove as que sumiram.

    Cada fonte é gravada na sua própria transação: uma falha no meio de um PDF não
    deixa a fonte pela metade no índice.
    """
    resumo = {'indexadas': 0, 'inalteradas': 0, 'removidas': 0, 'trechos': 0, 'falhas': []}
    with _trava_escrita:
        with conexao() as conn:
            existentes = {row['chave']: (row['id'], row['assinatura']) for row in conn.execute(
                'SELECT id,chave,assinatura FROM fontes WHERE colecao=? AND dono=?',
                (conector.colecao, conector.dono))}
        vistas = set()
        for fonte in conector.fontes():
            vistas.add(fonte.chave)
            atual = existentes.get(fonte.chave)
            if atual and atual[1] == fonte.assinatura:
                resumo['inalteradas'] += 1
                continue
            try:
                with conexao() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    if atual:
                        _remover(conn, atual[0])
                    resumo['trechos'] += _indexar_fonte(conn, conector, fonte)
                resumo['indexadas'] += 1
            except Exception as exc:  # um arquivo ilegível não interrompe os demais
                logger.warning('Falha ao indexar %s/%s: %s', fonte.colecao, fonte.nome, type(exc).__name__)
                resumo['falhas'].append(fonte.chave)
        if remover_ausentes:
            ausentes = [fid for chave, (fid, _) in existentes.items() if chave not in vistas]
            if ausentes:
                with conexao() as conn:
                    for fonte_id in ausentes:
                        _remover(conn, fonte_id)
                resumo['removidas'] = len(ausentes)
    return resumo


def remover_fonte(colecao: str, dono: str, chave: str) -> bool:
    with _trava_escrita, conexao() as conn:
        row = conn.execute('SELECT id FROM fontes WHERE colecao=? AND dono=? AND chave=?',
                           (colecao, dono, chave)).fetchone()
        if row:
            _remover(conn, row['id'])
        return row is not None


def consultar(expressao: str, colecao: str, dono: str = '', chaves=None, limite: int = 40,
              peso_titulo: float = 2.0) -> list[sqlite3.Row]:
    """Executa MATCH no FTS5 e devolve candidatos ordenados por BM25 (menor = melhor)."""
    filtro_chaves, parametros = '', [expressao, colecao, dono]
    if chaves is not None:
        chaves = list(chaves)
        if not chaves:
            return []
        filtro_chaves = f" AND f.chave IN ({','.join('?' * len(chaves))})"
        parametros += chaves
    sql = (f'SELECT t.id, f.chave, f.nome, f.tipo, f.colecao, t.ordem, t.pagina, '
           f'trechos_fts.conteudo AS conteudo, trechos_fts.titulo AS titulo, '
           f'bm25(trechos_fts, 1.0, {float(peso_titulo)}) AS bm25 '
           f'FROM trechos_fts JOIN trechos t ON t.id = trechos_fts.rowid '
           f'JOIN fontes f ON f.id = t.fonte_id '
           f'WHERE trechos_fts MATCH ? AND f.colecao = ? AND f.dono = ?{filtro_chaves} '
           f'ORDER BY bm25 LIMIT ?')
    with conexao() as conn:
        return conn.execute(sql, parametros + [limite]).fetchall()


def primeiros_trechos(colecao: str, dono: str, chaves, limite: int) -> list[sqlite3.Row]:
    """Trechos iniciais (capa, sumário, introdução) para pedidos genéricos de resumo."""
    chaves = list(chaves)
    if not chaves:
        return []
    sql = (f"SELECT t.id, f.chave, f.nome, f.tipo, f.colecao, t.ordem, t.pagina, "
           f"trechos_fts.conteudo AS conteudo, trechos_fts.titulo AS titulo, 0.0 AS bm25 "
           f"FROM trechos t JOIN fontes f ON f.id = t.fonte_id "
           f"JOIN trechos_fts ON trechos_fts.rowid = t.id "
           f"WHERE f.colecao = ? AND f.dono = ? AND f.chave IN ({','.join('?' * len(chaves))}) "
           f"ORDER BY f.indexada_em DESC, t.ordem LIMIT ?")
    with conexao() as conn:
        return conn.execute(sql, [colecao, dono, *chaves, limite]).fetchall()


def estatisticas() -> list[dict]:
    with conexao() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT colecao, dono, count(*) AS fontes, sum(trechos) AS trechos FROM fontes GROUP BY colecao, dono')]
