"""Busca lexical sobre o índice FTS5: normalização, consulta, filtro e ranqueamento.

O ranqueamento usa uma única medida, o BM25 do próprio SQLite, calculado sobre a mesma
tabela FTS para todas as coleções. A cobertura da consulta funciona só como filtro
(aceita ou descarta); ela nunca é somada ao BM25.
"""
from dataclasses import dataclass, asdict
import re
import unicodedata

import indice

MIN_RESULTADOS = 3
MAX_RESULTADOS = 5
CANDIDATOS = 40

STOP = set('''a o as os um uma uns umas de do da dos das d e ou em no na nos nas num numa ao aos
para pra pro por pelo pela pelos pelas com sem que qual quais quanto quantos como sobre entre ate
me te se lhe nos vos eu tu ele ela eles elas voce voces meu minha seu sua seus suas teu tua
isso isto esse essa este esta aquele aquela aquilo disso nisso nesse nessa deles delas dele dela
e eh sao ser foi era sera estar esta estao tem ter ha havia mais menos muito muita muitos muitas
porque pq quem quando onde diga explique explica fale conte falar contar saber sabe quero queria
pode poderia funciona funcionam significa entao tambem ja nao sim so qualquer algum alguma
and or not near'''.split())


def normalizar(texto: str) -> str:
    """Minúsculas e sem acentos: mesmo tratamento do tokenizer unicode61 do índice."""
    return ''.join(c for c in unicodedata.normalize('NFD', str(texto).lower())
                   if unicodedata.category(c) != 'Mn')


def tokens(texto: str) -> list[str]:
    return re.findall(r'[a-z0-9]+', normalizar(texto))


def termos(texto: str) -> list[str]:
    """Tokens com significado (sem stopwords e sem letras soltas), em ordem e sem repetição."""
    vistos, saida = set(), []
    for tok in tokens(texto):
        if len(tok) > 1 and tok not in STOP and tok not in vistos:
            vistos.add(tok); saida.append(tok)
    return saida


def raiz(termo: str) -> str:
    """Radical simples para busca por prefixo: inflação/inflações, causa/causas, preço/preços."""
    for sufixo in ('oes', 'aes', 'ais', 'eis', 'ois'):
        if termo.endswith(sufixo) and len(termo) - len(sufixo) >= 4:
            return termo[:-len(sufixo)]
    if len(termo) >= 7:
        return termo[:-2]
    if len(termo) >= 5 and termo.endswith('s'):
        return termo[:-1]
    return termo


def expressao_fts(lista_termos) -> str:
    """OR de prefixos entre aspas; os termos são [a-z0-9], então não há injeção de sintaxe."""
    return ' OR '.join(f'"{raiz(t)}"*' for t in lista_termos)


def cobertura(lista_termos, texto: str) -> float:
    if not lista_termos:
        return 0.0
    presentes = set(tokens(texto))
    achados = sum(1 for t in lista_termos if any(p.startswith(raiz(t)) for p in presentes))
    return achados / len(lista_termos)


@dataclass
class Resultado:
    id: int
    fonte: str          # arquivo / documento de origem
    chave: str
    trecho: str         # 'trecho 3' ou 'página 12'
    conteudo: str
    relevancia: float   # -BM25: maior = mais relevante
    tipo: str
    colecao: str
    pagina: int | None = None

    def como_dict(self):
        return asdict(self)


def _resultado(row, relevancia=None) -> Resultado:
    rotulo = f"página {row['pagina']}" if row['pagina'] else f"trecho {row['ordem'] + 1}"
    return Resultado(id=row['id'], fonte=row['nome'], chave=row['chave'], trecho=rotulo,
                     conteudo=' '.join(row['conteudo'].split()),
                     relevancia=round(-row['bm25'] if relevancia is None else relevancia, 4),
                     tipo=row['tipo'], colecao=row['colecao'], pagina=row['pagina'])


def _limite(valor: int) -> int:
    return max(1, min(int(valor), MAX_RESULTADOS))


def buscar(consulta: str, colecao: str = 'base', dono: str = '', chaves=None, limite: int = MIN_RESULTADOS,
           cobertura_minima: float = 0.65, max_por_fonte: int = 1) -> list[Resultado]:
    """Os trechos mais relevantes, no máximo `limite` (entre 1 e 5)."""
    lista = termos(consulta)
    if not lista:
        return []
    limite = _limite(limite)
    candidatos = indice.consultar(expressao_fts(lista), colecao, dono, chaves, limite=CANDIDATOS)
    selecionados, por_fonte, conteudos = [], {}, set()
    for row in candidatos:  # já em ordem de BM25
        if cobertura(lista, row['conteudo'] + ' ' + row['titulo']) < cobertura_minima:
            continue
        if por_fonte.get(row['chave'], 0) >= max_por_fonte:
            continue
        assinatura = normalizar(' '.join(row['conteudo'].split()))[:300]
        if assinatura in conteudos:
            continue
        conteudos.add(assinatura)
        por_fonte[row['chave']] = por_fonte.get(row['chave'], 0) + 1
        selecionados.append(_resultado(row))
        if len(selecionados) >= limite:
            break
    return selecionados


def primeiros(colecao: str, dono: str, chaves, limite: int = MIN_RESULTADOS) -> list[Resultado]:
    rows = indice.primeiros_trechos(colecao, dono, chaves, _limite(limite))
    return [_resultado(row, relevancia=0.0) for row in rows]
