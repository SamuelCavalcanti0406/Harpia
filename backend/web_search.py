from datetime import date, timedelta
from email.utils import parsedate_to_datetime
import os
import re
import unicodedata
import httpx

TAVILY_URL = 'https://api.tavily.com/search'
MAX_RESULT_CHARS = 900


def normalizar(texto):
    return ''.join(c for c in unicodedata.normalize('NFD', texto.lower())
                   if unicodedata.category(c) != 'Mn')


def precisa_pesquisa(pergunta):
    texto = normalizar(pergunta)
    if re.search(r'\bnoticias?\b', texto):
        return True
    termos = (
        'hoje', 'agora', 'atualmente', 'atual', 'ultimas noticias', 'noticias de',
        'noticias sobre', 'noticias hoje', 'ontem', 'esta semana', 'este mes',
        'recentemente', 'mais recente', 'cotacao', 'preco atual', 'precos atuais',
        'quanto custa', 'taxa selic', 'dolar hoje', 'euro hoje', 'previsao do tempo',
        'como esta o tempo', 'quem e o presidente', 'presidente atual', 'quem ganhou',
        'resultado de hoje', 'pesquise', 'pesquisar na internet', 'pesquisa na internet',
        'busque na internet', 'procure na internet', 'pesquise na web',
    )
    return any(termo in texto for termo in termos)


def _data_publicacao(valor):
    if not valor:
        return None
    texto = str(valor).strip()
    try:
        return date.fromisoformat(texto[:10])
    except ValueError:
        try:
            return parsedate_to_datetime(texto).date()
        except (TypeError, ValueError, OverflowError):
            return None


def _janela_noticias(consulta):
    """Notícias atuais ficam limitadas no tempo; consultas a anos passados ficam livres."""
    ano_historico = re.search(r'\b(?:19|20)\d{2}\b', consulta)
    marcadores_atuais = ('hoje', 'agora', 'atual', 'ultimas', 'recentes', 'semana', 'mes')
    if ano_historico and not any(marcador in consulta for marcador in marcadores_atuais):
        return None, None, None

    hoje = date.today()
    if 'ontem' in consulta:
        ontem = hoje - timedelta(days=1)
        return 'week', ontem, ontem
    if any(t in consulta for t in ('hoje', 'agora', 'atual')):
        return 'day', hoje, hoje
    if any(t in consulta for t in ('mes', 'mês')):
        return 'month', hoje - timedelta(days=30), None
    return 'week', hoje - timedelta(days=7), None


def pesquisar_web(pergunta, limite=3):
    """Retorna (fontes, erro). Erros são descritos sem expor chave ou resposta remota."""
    chave = os.getenv('TAVILY_API_KEY', '').strip()
    if not chave:
        return [], 'TAVILY_API_KEY não configurada.'

    consulta = normalizar(pergunta)
    noticias = any(t in consulta for t in (
        'noticia', 'ultimas noticias', 'acontecimento'))
    janela, data_minima, data_maxima = _janela_noticias(consulta) if noticias else (None, None, None)
    limite_saida = max(1, min(limite, 3))
    limite_api = min(10, limite_saida * 3) if noticias and janela else limite_saida
    payload = {
        'query': pergunta,
        'topic': 'news' if noticias else 'general',
        'search_depth': 'basic',
        'max_results': limite_api,
        'include_answer': False,
        'include_raw_content': False,
        'include_published_date': True,
        'language': 'pt',
    }
    if janela:
        payload['time_range'] = janela
        payload['filter_by_published_date'] = True

    try:
        response = httpx.post(TAVILY_URL, json=payload,
                              headers={'Authorization': f'Bearer {chave}'}, timeout=12.0)
        if response.status_code == 401:
            return [], 'A chave do Tavily foi recusada.'
        if response.status_code == 429:
            return [], 'O limite de consultas do Tavily foi atingido.'
        response.raise_for_status()
        dados = response.json()
        if not isinstance(dados, dict):
            return [], 'A resposta do serviço de pesquisa veio em formato inválido.'
        resultados = []
        for item in dados.get('results', [])[:limite_api]:
            if not isinstance(item, dict):
                continue
            url = item.get('url')
            data_publicacao = item.get('published_date')
            if janela:
                data_resultado = _data_publicacao(data_publicacao)
                if (data_resultado is None or data_resultado < data_minima
                        or (data_maxima and data_resultado > data_maxima)):
                    continue
            trecho = ' '.join(str(item.get('content') or '').split())[
                :MAX_RESULT_CHARS]
            if not isinstance(url, str) or not url.startswith(('https://', 'http://')) or not trecho:
                continue
            resultados.append({
                'tipo': 'web',
                'titulo': str(item.get('title') or 'Fonte consultada')[:200],
                'url': url,
                'conteudo': trecho,
                'data_publicacao': data_publicacao,
            })
            if len(resultados) >= limite_saida:
                break
        return resultados, None
    except (httpx.HTTPError, ValueError, TypeError):
        return [], 'Não foi possível consultar o Tavily agora.'
