"""Base de conhecimento pública (.txt/.md em knowledge_base/) servida pelo índice FTS5."""
from functools import lru_cache
from pathlib import Path
import re

import busca
import indice
from conectores import ConectorTexto

KNOWLEDGE_DIR = Path(__file__).resolve().parent / 'knowledge_base'
COLECAO = 'base'
PERFIL_HARPIA = 'harpia.txt'


def sincronizar_base() -> dict:
    """Só reindexa arquivos cuja assinatura (mtime+tamanho) mudou; o custo normal é um stat por arquivo."""
    return indice.sincronizar(ConectorTexto(KNOWLEDGE_DIR, COLECAO))


def depende_do_assunto(pergunta):
    return bool(re.search(r'\b(isso|isto|suas|seus|deles|delas|disso|nesse|nessa)\b', busca.normalizar(pergunta)))


def assunto_anterior(linhas):
    """Keep the last explicit topic across a chain of pronoun-only follow-ups."""
    for linha in reversed(linhas):
        if linha['papel'] == 'user' and not depende_do_assunto(linha['conteudo']):
            return linha['conteudo']
    return ''


def consulta_contextual(pergunta, anterior=''):
    clean = busca.normalizar(pergunta).strip()
    # Only borrow a topic for explicit anaphoric follow-ups, not a new question.
    if anterior and depende_do_assunto(pergunta):
        return anterior + ' ' + pergunta
    if re.search(r'\b(coisas|produtos|precos)\b.*\b(caras|caros|sobem|aumentam)\b', clean):
        return 'inflação preços'
    return pergunta


def pesquisar(pergunta, limite=3, anterior=''):
    """Trechos da base pública: [{id, fonte, trecho, conteudo, relevancia, ...}], no máximo 5."""
    if limite <= 0:
        return []
    sincronizar_base()
    resultados = busca.buscar(consulta_contextual(pergunta, anterior), COLECAO, limite=limite)
    return [r.como_dict() for r in resultados]


@lru_cache(maxsize=4)
def _perfil(caminho, assinatura):
    return ' '.join(Path(caminho).read_text(encoding='utf-8').split())


def perfil_harpia():
    """Ficha da HarpIA lida direto do arquivo, sem passar pelo índice."""
    caminho = KNOWLEDGE_DIR / PERFIL_HARPIA
    if not caminho.is_file():
        return []
    info = caminho.stat()
    return [{'tipo': 'perfil', 'fonte': PERFIL_HARPIA, 'trecho': 'ficha',
             'conteudo': _perfil(str(caminho), (info.st_mtime_ns, info.st_size))}]
