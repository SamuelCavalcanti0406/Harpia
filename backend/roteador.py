"""Roteador de intenção determinístico: decide, antes de qualquer busca, o que a pergunta exige.

- identidade: perguntas sobre a própria HarpIA → ficha da HarpIA, sem consultar o índice.
- casual: cumprimentos, agradecimentos, despedidas → só o modelo e o histórico.
- atualidade: notícias, cotações, placares → índice local; sem fonte, aviso de modo offline.
- conhecimento: o resto → índice local (documentos do usuário e base de conhecimento).
"""
from enum import Enum
import re

from busca import normalizar, termos


class Intencao(str, Enum):
    IDENTIDADE = 'identidade'
    CASUAL = 'casual'
    ATUALIDADE = 'atualidade'
    CONHECIMENTO = 'conhecimento'


_VOCE = r'(voce|vc|tu|a harpia|harpia)'
_IDENTIDADE = re.compile('|'.join((
    rf'\bquem (e|es|eh|seria) {_VOCE}\b',
    rf'\bo que (e|es|eh|significa) {_VOCE}\b',
    r'\bquem (te|a|lhe) (criou|fez|desenvolveu|programou)\b',
    rf'\bquem (criou|fez|desenvolveu|programou) {_VOCE}\b',
    r'\b(sobre|de) (voce|ti)\b',
    r'\b(seu|teu) (nome|criador|objetivo|proposito)\b',
    r'\bcomo (te|se) chamas?\b|\bcomo voce se chama\b',
    r'\bse apresent\w*|\bapresente-se\b|\bapresenta-te\b',
    rf'\b{_VOCE} (e|es|foi|esta) (uma|um|criada|feita|desenvolvida|baseada)\b',
    rf'\b(qual|que) (modelo|ia|llm) {_VOCE}\b',
    r'\b(voce|vc)\b.*\bharpia\b|\bharpia\b.*\b(voce|vc)\b',
)))

# Palavras que, sozinhas, não pedem informação: cumprimentos, cortesias e reações.
_CASUAIS = set('''oi ola ole opa eai ei hey hello alo salve fala bom boa dia tarde noite tudo bem
beleza blz suave tranquilo td tb tbm joia obrigado obrigada obg brigado brigada valeu vlw grato grata
agradeco tchau adeus ate logo mais breve amanha flw falou ok okay certo entendi entendido legal show
otimo otima perfeito massa top bacana kkk kkkk kkkkk haha hahaha rs rsrs sim nao claro pois uau nossa
prazer conhecer contigo voce vc tu ai ta como vai vais esta estas estou e o a com por favor pfv
explicacao ajuda resposta'''.split())
_INICIO_CORTESIA = re.compile(r'^(obrigad[oa]|valeu|vlw|brigad[oa]|tchau|ate (logo|mais)|boa noite|bom dia|boa tarde)\b')

_ATUALIDADE = re.compile('|'.join((
    r'\bnoticias?\b', r'\bultimas\b', r'\bhoje\b', r'\bontem\b', r'\besta semana\b', r'\beste mes\b',
    r'\bcotacao\b', r'\b(dolar|euro|bitcoin) (hoje|agora)\b', r'\bpreco atual\b', r'\bprevisao do tempo\b',
    r'\bquem ganhou\b', r'\bplacar\b', r'\bresultado do jogo\b', r'\bpresidente atual\b',
    r'\b(acabou|acaba) de (acontecer|sair)\b', r'\bem tempo real\b',
)))


def pergunta_sobre_harpia(pergunta: str) -> bool:
    return bool(_IDENTIDADE.search(normalizar(pergunta)))


def eh_casual(pergunta: str) -> bool:
    texto = normalizar(pergunta).strip()
    palavras = re.findall(r'[a-z0-9]+', texto)
    if not palavras:
        return True
    conteudo = [t for t in termos(texto) if t not in _CASUAIS]
    if not conteudo:
        return True
    # "obrigado pela ajuda com o relatório" ainda é cortesia; frases longas não.
    return bool(_INICIO_CORTESIA.match(texto)) and len(palavras) <= 6 and '?' not in pergunta


def classificar(pergunta: str) -> Intencao:
    if pergunta_sobre_harpia(pergunta):
        return Intencao.IDENTIDADE
    if eh_casual(pergunta):
        return Intencao.CASUAL
    if _ATUALIDADE.search(normalizar(pergunta)):
        return Intencao.ATUALIDADE
    return Intencao.CONHECIMENTO


def consulta_ao_indice(intencao: Intencao) -> bool:
    """Identidade e conversa casual nunca consultam o índice SQLite."""
    return intencao in (Intencao.ATUALIDADE, Intencao.CONHECIMENTO)
