"""Per-user local tabular uploads and deterministic economic summaries."""
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import math
import re
import uuid
from pathlib import Path

import numpy as np
import pandas as pd


DATA_ROOT = Path(__file__).resolve().parent.parent / '.local' / 'economic_data'
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ROWS = 200_000
MAX_COLUMNS = 100
ALLOWED_EXTENSIONS = {'.csv', '.xlsx'}
ANALYSIS_WORDS = re.compile(
    r'\b(analis\w*|dados?|econom\w*|fatur\w*|receit\w*|despes\w*|'
    r'custos?|lucros?|margens?|vend\w*|estoques?|pedidos?|quantidades?|'
    r'crescimentos?|tendencias?|medias?|medianas?|compar\w*|caixa|'
    r'rentabilidades?|ticket|financeir\w*|produtos?|estabelecimentos?|'
    r'balancos?|fluxos?|resultados?|precos?|gastos?|pagamentos?)\b', re.I)
REQUESTS_DATA = re.compile(
    r'\b(analis\w*|calcule|mostre|resuma|compar\w*|quanto|total|'
    r'dados?|base|planilha|arquivo|meus|minhas|minha|meu|variou|evoluiu)\b', re.I)


class EconomicDataError(ValueError):
    pass


def _owner_dir(owner_id):
    digest = hashlib.sha256(str(owner_id).encode('utf-8')).hexdigest()
    return DATA_ROOT / digest


def _safe_name(filename):
    name = Path(str(filename or '')).name
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    stem = re.sub(r'[^\w.-]+', '_', stem, flags=re.UNICODE).strip('._')[:80]
    return (stem or 'base_economica') + suffix


def _files(owner_id):
    folder = _owner_dir(owner_id)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS),
                  key=lambda p: p.stat().st_mtime_ns, reverse=True)


def listar_bases(owner_id):
    result = []
    for path in _files(owner_id):
        stat = path.stat()
        stored_id, _, original = path.name.partition('__')
        result.append({
            'id': stored_id,
            'nome': original or path.name,
            'tamanho_bytes': stat.st_size,
            'enviada_em': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            'ativa': not result,
        })
    return result


def _parse_number(value):
    if pd.isna(value):
        return np.nan
    if isinstance(value, (int, float, np.number)):
        return float(value)
    text = str(value).strip().replace('\u00a0', '').replace(' ', '')
    if not text or text.lower() in {'nan', 'none', 'null', '-'}:
        return np.nan
    text = text.strip('()').replace('R$', '').replace('$', '').replace('%', '')
    negative = str(value).strip().startswith('(') and str(value).strip().endswith(')')
    if ',' in text and '.' in text:
        if text.rfind(',') > text.rfind('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    elif ',' in text:
        text = text.replace(',', '.')
    elif '.' in text:
        parts = text.split('.')
        if len(parts) > 1 and len(parts[0]) <= 3 and all(len(part) == 3 for part in parts[1:]):
            text = text.replace('.', '')
    text = re.sub(r'[^0-9.+-]', '', text)
    try:
        number = float(text)
    except ValueError:
        return np.nan
    return -number if negative else number


def _colunas_numericas(df):
    identifiers = re.compile(r'(^|_)(id|cpf|cnpj|cep|telefone|celular|codigo|cod|conta)(_|$)')
    for column in list(df.columns):
        normalized = re.sub(r'[^a-z0-9]+', '_', _normalizar(column)).strip('_')
        if identifiers.search(normalized) or pd.api.types.is_datetime64_any_dtype(df[column]):
            continue
        if pd.api.types.is_numeric_dtype(df[column]):
            df[column] = pd.to_numeric(df[column], errors='coerce')
            continue
        original_count = int(df[column].notna().sum())
        if not original_count:
            continue
        converted = df[column].map(_parse_number)
        if int(converted.notna().sum()) >= max(1, math.ceil(original_count * 0.8)):
            df[column] = converted
    return [c for c in df.select_dtypes(include='number').columns
            if int(df[c].notna().sum()) > 0]


def _normalizar(text):
    import unicodedata
    return ''.join(ch for ch in unicodedata.normalize('NFD', str(text).lower())
                   if unicodedata.category(ch) != 'Mn')


def _ler_arquivo(path, ext):
    try:
        if ext == '.csv':
            try:
                frame = pd.read_csv(path, sep=None, engine='python', encoding='utf-8-sig',
                                    nrows=MAX_ROWS + 1)
            except UnicodeDecodeError:
                frame = pd.read_csv(path, sep=None, engine='python', encoding='cp1252',
                                    nrows=MAX_ROWS + 1)
        else:
            frame = pd.read_excel(path, sheet_name=0, engine='openpyxl',
                                  nrows=MAX_ROWS + 1)
    except Exception as exc:
        raise EconomicDataError('Não consegui ler a planilha. Confira se o CSV ou XLSX está válido e tem cabeçalhos na primeira linha.') from exc
    if len(frame) > MAX_ROWS:
        raise EconomicDataError(f'A base excede o limite de {MAX_ROWS:,} linhas.')
    if len(frame.columns) > MAX_COLUMNS:
        raise EconomicDataError(f'A base excede o limite de {MAX_COLUMNS} colunas.')
    frame.columns = [str(c).strip().replace('\n', ' ')[:100] or f'coluna_{i + 1}'
                     for i, c in enumerate(frame.columns)]
    frame = frame.dropna(how='all').dropna(axis=1, how='all')
    if frame.empty or len(frame.columns) == 0:
        raise EconomicDataError('A planilha está vazia. Inclua cabeçalhos e pelo menos uma linha de dados.')
    return frame


@lru_cache(maxsize=8)
def _carregar_cache(path, modified_ns, size, ext):
    return _ler_arquivo(path, ext)


def carregar_arquivo(path):
    stat = path.stat()
    return _carregar_cache(str(path), stat.st_mtime_ns, stat.st_size, path.suffix.lower()).copy()


def salvar_upload(owner_id, filename, stream):
    safe = _safe_name(filename)
    ext = Path(safe).suffix
    if ext not in ALLOWED_EXTENSIONS:
        raise EconomicDataError('Formato não aceito. Envie um arquivo CSV ou XLSX.')
    folder = _owner_dir(owner_id)
    folder.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    temporary = folder / (upload_id + '.upload')
    total = 0
    try:
        with temporary.open('wb') as target:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise EconomicDataError('O arquivo excede o limite de 25 MB.')
                target.write(chunk)
        frame = _ler_arquivo(temporary, ext)
        destination = folder / f'{upload_id}__{safe}'
        temporary.replace(destination)
        _carregar_cache.cache_clear()
        return {'id': upload_id, 'nome': safe, 'linhas': len(frame),
                'colunas': [str(c) for c in frame.columns], 'tamanho_bytes': total}
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _data_column(df):
    for column in df.columns:
        key = _normalizar(column)
        if not any(term in key for term in ('data', 'date', 'periodo', 'competencia')):
            continue
        series = pd.to_datetime(df[column], errors='coerce', dayfirst=True)
        if int(series.notna().sum()) >= max(1, math.ceil(df[column].notna().sum() * 0.7)):
            return column, series
    return None, None


def _relevante(nome):
    return bool(ANALYSIS_WORDS.search(_normalizar(nome)))


def _relatorio(frame, filename):
    df = frame.copy()
    numeric = _colunas_numericas(df)
    date_name, dates = _data_column(df)
    sections = [f'Base: {filename}. Linhas: {len(df)}. Colunas: {len(df.columns)}.']
    if date_name:
        valid_dates = dates.dropna()
        if not valid_dates.empty:
            sections.append(f'Período da coluna {date_name}: {valid_dates.min():%Y-%m-%d} a {valid_dates.max():%Y-%m-%d}.')
    sections.append('Colunas: ' + ', '.join(str(c)[:50] for c in df.columns[:25]) + '.')
    missing = [(str(c), int(df[c].isna().sum())) for c in df.columns if df[c].isna().any()]
    if missing:
        sections.append('Valores ausentes: ' + '; '.join(f'{c}: {n}' for c, n in missing[:8]) + '.')

    measures = []
    for column in numeric[:8]:
        values = pd.to_numeric(df[column], errors='coerce').replace([np.inf, -np.inf], np.nan).dropna()
        if values.empty:
            continue
        line = (f'{column}: média {values.mean():.2f}; mediana {values.median():.2f}; '
                f'mínimo {values.min():.2f}; máximo {values.max():.2f}')
        if _relevante(column) or _normalizar(column) in {'valor', 'total'}:
            line += f'; soma {values.sum():.2f}'
        measures.append(line + '.')
    if measures:
        sections.append('Resumo numérico (unidades conforme o arquivo): ' + ' '.join(measures))

    amount_columns = [c for c in numeric if _relevante(c) and any(
        term in _normalizar(c) for term in ('receita', 'faturamento', 'venda', 'despesa', 'custo', 'lucro', 'gasto'))]
    if date_name and amount_columns:
        series = df[amount_columns[:4]].copy()
        series['_periodo'] = dates.dt.to_period('M')
        monthly = series.dropna(subset=['_periodo']).groupby('_periodo', observed=True)[amount_columns[:4]].sum().sort_index()
        if len(monthly):
            rows = []
            for period, row in monthly.tail(12).iterrows():
                values = ', '.join(f'{c} {row[c]:.2f}' for c in amount_columns[:4])
                rows.append(f'{period}: {values}')
            sections.append('Somas mensais dos indicadores selecionados (até os 12 meses mais recentes): ' + '; '.join(rows) + '.')
            for column in amount_columns[:4]:
                values = monthly[column].dropna()
                if len(values) >= 2 and float(values.iloc[0]) != 0:
                    change = (float(values.iloc[-1]) - float(values.iloc[0])) / abs(float(values.iloc[0])) * 100
                    sections.append(f'Variação de {column} entre os meses disponíveis: {change:.2f}% (de {values.index[0]} a {values.index[-1]}).')

    group_terms = ('estabelecimento', 'loja', 'filial', 'unidade', 'produto',
                   'categoria', 'canal', 'segmento', 'regiao')
    group_columns = [c for c in df.columns
                     if any(term in _normalizar(c) for term in group_terms)
                     and 1 < int(df[c].nunique(dropna=True)) <= 50]
    if amount_columns:
        rankings = []
        for group_column in group_columns[:3]:
            for amount_column in amount_columns[:2]:
                grouped = df.groupby(group_column, dropna=True)[amount_column].sum()
                if len(grouped) > 1:
                    top = grouped.sort_values(ascending=False).head(5)
                    entries = ', '.join(f'{str(label)[:50]}: {value:.2f}' for label, value in top.items())
                    rankings.append(f'{amount_column} por {group_column} (maiores): {entries}')
        if rankings:
            sections.append('Comparações por grupo: ' + '; '.join(rankings[:4]) + '.')

    if len(numeric) >= 2:
        corr = df[numeric[:8]].corr(method='pearson', min_periods=3)
        pairs = []
        for i, left in enumerate(corr.columns):
            for right in corr.columns[i + 1:]:
                value = corr.loc[left, right]
                if pd.notna(value) and abs(float(value)) >= 0.7:
                    pairs.append((abs(float(value)), f'{left} e {right}: {float(value):.2f}'))
        if pairs:
            sections.append('Correlações lineares mais fortes (associação, não causalidade): ' + '; '.join(x[1] for x in sorted(pairs, reverse=True)[:5]) + '.')

    return '\n'.join(sections)


def documentos_analise(owner_id, question):
    if not _relevante(question):
        return []
    files = _files(owner_id)
    if not files:
        if not REQUESTS_DATA.search(_normalizar(question)):
            return []
        content = ('Nenhuma base econômica foi enviada para este usuário. Oriente a pessoa a usar '
                   '`/importar "caminho do arquivo.csv"` ou `/importar "caminho do arquivo.xlsx"` '
                   'no chat local e depois fazer a pergunta novamente.')
        return [{'tipo': 'analise_economica', 'fonte': 'Orientação de importação',
                 'trecho': '1', 'conteudo': content}]
    path = files[0]
    try:
        report = _relatorio(carregar_arquivo(path), path.name.partition('__')[2] or path.name)
    except EconomicDataError as exc:
        report = f'Não foi possível analisar a base {path.name}: {exc}'
    pieces = []
    remaining = report
    while remaining and len(pieces) < 3:
        cut = min(650, len(remaining))
        if cut < len(remaining):
            boundary = remaining.rfind(' ', 0, cut)
            if boundary > 350:
                cut = boundary
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [{'tipo': 'analise_economica', 'fonte': f'Análise local: {path.name.partition("__")[2] or path.name}',
             'trecho': str(i + 1), 'conteudo': piece} for i, piece in enumerate(pieces)]
