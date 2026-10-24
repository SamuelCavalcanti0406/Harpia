from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
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
ALLOWED_EXTENSIONS = {'.csv', '.xlsx', '.pdf'}
ANALYSIS_WORDS = re.compile(
    r'\b(analis\w*|dados?|econom\w*|fatur\w*|receit\w*|despes\w*|'
    r'custos?|lucros?|margens?|vend\w*|estoques?|pedidos?|quantidades?|'
    r'crescimentos?|tendencias?|medias?|medianas?|compar\w*|caixa|'
    r'rentabilidades?|ticket|financeir\w*|produtos?|estabelecimentos?|'
    r'balancos?|fluxos?|resultados?|precos?|gastos?|pagamentos?|fluxo?|caixa?)\b', re.I)
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


def _extrair_linhas_pdfium(path):
    try:
        import pypdfium2
    except ImportError as exc:
        raise EconomicDataError('O pacote pypdfium2 é necessário para leitura de PDFs.') from exc
    try:
        doc = pypdfium2.PdfDocument(str(path))
    except Exception as exc:
        raise EconomicDataError('Não foi possível ler o arquivo PDF. Verifique se o arquivo não está corrompido ou protegido por senha.') from exc

    all_lines = []
    try:
        for page in doc:
            textpage = page.get_textpage()
            chars = []
            n_chars = textpage.count_chars()
            for i in range(n_chars):
                c = textpage.get_text_range(i, 1)
                if c in ('\r', '\n'):
                    continue
                box = textpage.get_charbox(i)
                chars.append({'char': c, 'l': box[0], 'b': box[1], 'r': box[2]})

            chars.sort(key=lambda x: -x['b'])
            lines = []
            cur_line = []
            cur_y = None
            for c in chars:
                if cur_y is None or abs(c['b'] - cur_y) <= 3:
                    cur_line.append(c)
                    cur_y = c['b'] if cur_y is None else (cur_y + c['b']) / 2
                else:
                    cur_line.sort(key=lambda x: x['l'])
                    lines.append(cur_line)
                    cur_line = [c]
                    cur_y = c['b']
            if cur_line:
                cur_line.sort(key=lambda x: x['l'])
                lines.append(cur_line)

            for line in lines:
                buf = ''
                prev_r = None
                for c in line:
                    if prev_r is not None:
                        gap = c['l'] - prev_r
                        if gap > 10:
                            buf += '    '
                        elif gap > 3.5:
                            buf += ' '
                    buf += c['char']
                    prev_r = c['r']
                if buf.strip():
                    all_lines.append(buf.strip())
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return all_lines


def _extrair_linhas_texto_simples(path):
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(str(path))
        lines = []
        try:
            for page in doc:
                txt = page.get_textpage().get_text_range()
                for l in txt.splitlines():
                    if l.strip():
                        lines.append(l.strip())
        finally:
            try:
                doc.close()
            except Exception:
                pass
        return lines
    except Exception:
        return []


def _parse_row(line):
    s = line.strip()
    if not s:
        return None
    if '|' in s:
        parts = [p.strip() for p in s.strip('|').split('|')]
        if all(re.match(r'^:?-+:?$', p) for p in parts if p):
            return 'SEPARATOR'
        if len(parts) >= 2:
            return parts
    if ';' in s:
        parts = [p.strip() for p in s.split(';')]
        if len(parts) >= 2:
            return parts
    parts = [p.strip() for p in re.split(r'\s{2,}|\t', s) if p.strip()]
    if len(parts) >= 2:
        return parts
    if ',' in s and not re.search(r'^\d+,\d+$', s):
        parts = [p.strip() for p in s.split(',') if p.strip()]
        if len(parts) >= 2:
            return parts
    return None


def _tabelas_de_linhas(lines):
    raw_tables = []
    current_table = []
    current_cols = None

    for l in lines:
        parsed = _parse_row(l)
        if parsed == 'SEPARATOR':
            continue
        if parsed is None:
            if current_table and len(current_table) >= 2:
                raw_tables.append(current_table)
            current_table = []
            current_cols = None
            continue

        if current_table and [p.lower() for p in parsed] == [p.lower() for p in current_table[0]]:
            continue

        ncols = len(parsed)
        if current_cols is None:
            current_cols = ncols
            current_table = [parsed]
        elif ncols == current_cols:
            current_table.append(parsed)
        else:
            if len(current_table) >= 2:
                raw_tables.append(current_table)
            current_cols = ncols
            current_table = [parsed]

    if current_table and len(current_table) >= 2:
        raw_tables.append(current_table)

    merged = []
    for t in raw_tables:
        if not merged:
            merged.append(t)
        else:
            last = merged[-1]
            if [p.lower() for p in t[0]] == [p.lower() for p in last[0]]:
                last.extend(t[1:])
            else:
                merged.append(t)

    frames = []
    for t in merged:
        headers = [str(c).strip().replace('\n', ' ')[:100] or f'coluna_{i + 1}'
                   for i, c in enumerate(t[0])]
        unique_headers = []
        counts = {}
        for h in headers:
            if h in counts:
                counts[h] += 1
                unique_headers.append(f'{h}_{counts[h]}')
            else:
                counts[h] = 0
                unique_headers.append(h)
        frames.append(pd.DataFrame(t[1:], columns=unique_headers))
    return frames


def _score_table(df):
    if df.empty or len(df.columns) < 2 or len(df) < 1:
        return -1
    numeric_cols = _colunas_numericas(df.copy())
    if not numeric_cols:
        return -1
    economic_matches = sum(1 for col in df.columns if _relevante(str(col)))
    date_col, _ = _data_column(df.copy())
    score = (
        len(numeric_cols) * 20
        + economic_matches * 15
        + (10 if date_col else 0)
        + min(len(df), 200)
    )
    return score




def _ler_pdf(path):
    path = Path(path)
    if not path.is_file():
        raise EconomicDataError('Arquivo não encontrado.')
    if path.stat().st_size == 0:
        raise EconomicDataError('A base está vazia.')

    candidate_frames = []
    try:
        lines = _extrair_linhas_pdfium(path)
        candidate_frames.extend(_tabelas_de_linhas(lines))
        if not candidate_frames:
            lines_simple = _extrair_linhas_texto_simples(path)
            candidate_frames.extend(_tabelas_de_linhas(lines_simple))
    except EconomicDataError:
        raise
    except Exception as exc:
        raise EconomicDataError('Não foi possível ler o arquivo PDF. Verifique se o arquivo não está corrompido ou protegido por senha.') from exc

    scored = []
    for f in candidate_frames:
        score = _score_table(f)
        if score > 0:
            scored.append((score, f))

    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    raise EconomicDataError(
        'Não foi possível identificar dados econômicos tabulares no PDF. '
        'Certifique-se de que o documento contém tabelas com cabeçalhos e valores numéricos.'
    )


def _ler_arquivo(path, ext):
    try:
        if ext == '.csv':
            try:
                frame = pd.read_csv(path, sep=None, engine='python', encoding='utf-8-sig',
                                    nrows=MAX_ROWS + 1)
            except UnicodeDecodeError:
                frame = pd.read_csv(path, sep=None, engine='python', encoding='cp1252',
                                    nrows=MAX_ROWS + 1)
        elif ext == '.xlsx':
            frame = pd.read_excel(path, sheet_name=0, engine='openpyxl',
                                  nrows=MAX_ROWS + 1)
        elif ext == '.pdf':
            frame = _ler_pdf(path)
        else:
            raise EconomicDataError('Formato não aceito. Envie um arquivo CSV, XLSX ou PDF.')
    except EconomicDataError:
        raise
    except Exception as exc:
        raise EconomicDataError('Não consegui ler a base. Confira se o CSV, XLSX ou PDF está válido e tem cabeçalhos na primeira linha.') from exc
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
        raise EconomicDataError('Formato não aceito. Envie um arquivo CSV, XLSX ou PDF.')
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
        try:
            series = pd.to_datetime(df[column], errors='coerce', dayfirst=True, format='mixed')
        except (ValueError, TypeError):
            series = pd.to_datetime(df[column], errors='coerce', dayfirst=True)
        if int(series.notna().sum()) >= max(1, math.ceil(df[column].notna().sum() * 0.7)):
            return column, series
    return None, None


def _relevante(nome):
    return bool(ANALYSIS_WORDS.search(_normalizar(nome)))


def _valor_json(value):
    if pd.isna(value):
        return None
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def percentual_variacao(valor_inicial, valor_final):
    """Aplica ((final - inicial) / inicial) * 100 sem inventar divisão por zero."""
    try:
        inicial = float(valor_inicial)
        final = float(valor_final)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(inicial) or not np.isfinite(final) or inicial == 0:
        return None
    return ((final - inicial) / inicial) * 100


def inspecionar_dados(frame, amostra=5):
    """Retorna um diagnóstico serializável antes de qualquer indicador."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise EconomicDataError('A base está vazia.')
    df = frame.copy()
    original_columns = list(df.columns)
    numeric = _colunas_numericas(df)
    date_name, dates = _data_column(df)
    details = []
    format_issues = []
    categorical = []
    for column in original_columns:
        series = df[column]
        inferred = 'numérica' if column in numeric else 'data' if column == date_name else 'texto'
        if inferred == 'texto' and series.nunique(dropna=True) <= min(50, max(5, len(df) // 2)):
            categorical.append(str(column))
        non_null = int(frame[column].notna().sum())
        converted = frame[column].map(_parse_number) if inferred == 'texto' else pd.Series(dtype=float)
        if inferred == 'texto' and non_null and 0 < int(converted.notna().sum()) < math.ceil(non_null * 0.8):
            format_issues.append({
                'coluna': str(column),
                'problema': 'valores misturam números e formatos não reconhecidos',
            })
        details.append({
            'nome': str(column),
            'tipo_original': str(frame[column].dtype),
            'tipo_inferido': inferred,
            'ausentes': int(frame[column].isna().sum()),
            'unicos': int(frame[column].nunique(dropna=True)),
        })
    return {
        'linhas': int(len(frame)),
        'colunas': int(len(frame.columns)),
        'colunas_detalhes': details,
        'colunas_numericas': [str(column) for column in numeric],
        'coluna_data': str(date_name) if date_name else None,
        'colunas_categoricas': categorical,
        'valores_ausentes': {str(column): int(frame[column].isna().sum())
                             for column in frame.columns if frame[column].isna().any()},
        'duplicatas': int(frame.duplicated().sum()),
        'possiveis_erros_formatacao': format_issues,
        'amostra': [{str(key): _valor_json(value) for key, value in row.items()}
                    for row in frame.head(max(0, int(amostra))).to_dict(orient='records')],
        'periodo': ({'inicio': dates.dropna().min().date().isoformat(),
                     'fim': dates.dropna().max().date().isoformat()}
                    if date_name and dates is not None and not dates.dropna().empty else None),
    }


def estatistica_descritiva(frame, colunas=None):
    df = frame.copy()
    numeric = _colunas_numericas(df)
    if colunas is not None:
        requested = {str(column) for column in colunas}
        numeric = [column for column in numeric if str(column) in requested]
    result = {}
    for column in numeric:
        values = pd.to_numeric(df[column], errors='coerce').replace([np.inf, -np.inf], np.nan).dropna()
        if values.empty:
            continue
        result[str(column)] = {
            'contagem': int(values.count()),
            'soma': float(values.sum()),
            'media': float(values.mean()),
            'mediana': float(values.median()),
            'minimo': float(values.min()),
            'maximo': float(values.max()),
            'desvio_padrao': float(values.std(ddof=1)) if len(values) > 1 else None,
            'variancia': float(values.var(ddof=1)) if len(values) > 1 else None,
            'quartis': {str(key): float(value) for key, value in values.quantile([.25, .5, .75]).items()},
        }
    return result


def distribuicao_categorias(frame, limite=20):
    result = {}
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_numeric_dtype(series) or series.nunique(dropna=True) > 50:
            continue
        counts = series.fillna('ausente').value_counts(dropna=False).head(limite)
        result[str(column)] = {str(key): int(value) for key, value in counts.items()}
    return result


def comparar_periodos(frame, coluna_data=None, coluna_valor=None, frequencia='M', agregacao='sum'):
    """Compara períodos agregados e expõe valores, fórmula e limitações."""
    df = frame.copy()
    if coluna_data is None:
        coluna_data, datas = _data_column(df)
    else:
        try:
            datas = pd.to_datetime(df[coluna_data], errors='coerce', dayfirst=True, format='mixed')
        except (ValueError, TypeError):
            datas = pd.to_datetime(df[coluna_data], errors='coerce', dayfirst=True)
    if not coluna_data or coluna_valor not in df.columns:
        return {'disponivel': False, 'motivo': 'É necessária uma coluna de data e uma coluna numérica.'}
    values = pd.to_numeric(df[coluna_valor].map(_parse_number), errors='coerce')
    valid = pd.DataFrame({'periodo': datas.dt.to_period(frequencia), 'valor': values}).dropna()
    if valid.empty:
        return {'disponivel': False, 'motivo': 'Não há datas e valores válidos para comparar.'}
    grouped = valid.groupby('periodo', observed=True)['valor']
    if agregacao == 'mean':
        series = grouped.mean().sort_index()
    else:
        series = grouped.sum().sort_index()
    periods = []
    previous = None
    for period, value in series.items():
        current = float(value)
        periods.append({
            'periodo': str(period),
            'valor': current,
            'variacao_absoluta': None if previous is None else current - previous,
            'variacao_percentual': None if previous is None else percentual_variacao(previous, current),
        })
        previous = current
    return {
        'disponivel': True,
        'coluna_data': str(coluna_data),
        'coluna_valor': str(coluna_valor),
        'agregacao': agregacao,
        'periodos': periods,
        'formula_variacao_percentual': '((valor_final - valor_inicial) / valor_inicial) * 100; nula quando o inicial é zero ou inválido.',
    }


def identificar_atipicos(frame, coluna):
    if coluna not in frame.columns:
        return {'disponivel': False, 'motivo': 'Coluna não encontrada.'}
    values = pd.to_numeric(frame[coluna].map(_parse_number), errors='coerce').dropna()
    if len(values) < 4:
        return {'disponivel': False, 'motivo': 'São necessários pelo menos quatro valores válidos.'}
    q1, q3 = values.quantile(.25), values.quantile(.75)
    iqr = q3 - q1
    lower, upper = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return {
        'disponivel': True,
        'coluna': str(coluna),
        'metodo': 'IQR: abaixo de Q1 - 1,5*IQR ou acima de Q3 + 1,5*IQR',
        'limite_inferior': float(lower),
        'limite_superior': float(upper),
        'indices': [int(index) for index, value in values.items() if value < lower or value > upper],
    }


def calcular_indicadores(frame):
    df = frame.copy()
    numeric = _colunas_numericas(df)
    date_name, _ = _data_column(df)
    indicators = {'formula_variacao_percentual': '((valor_final - valor_inicial) / valor_inicial) * 100'}
    if date_name and numeric:
        indicators['evolucao'] = comparar_periodos(df, date_name, numeric[0])
    revenue = next((column for column in numeric if any(term in _normalizar(column)
                    for term in ('receita', 'faturamento', 'venda'))), None)
    profit = next((column for column in numeric if any(term in _normalizar(column)
                   for term in ('lucro', 'resultado'))), None)
    if revenue and profit:
        valid = pd.DataFrame({'receita': df[revenue].map(_parse_number), 'lucro': df[profit].map(_parse_number)}).dropna()
        valid = valid[valid['receita'] != 0]
        if not valid.empty:
            indicators['margem_media_percentual'] = float((valid['lucro'] / valid['receita'] * 100).mean())
            indicators['formula_margem'] = '(lucro / receita) * 100, somente quando receita != 0'
    price = next((column for column in numeric if any(term in _normalizar(column)
                 for term in ('preco', 'precos', 'price'))), None)
    if price and date_name:
        indicators['variacao_observada_serie_de_precos'] = comparar_periodos(df, date_name, price, agregacao='mean')
        indicators['limitacao_inflacao'] = ('Isto mede a variação da série de preços fornecida; não é inflação geral sem cesta, pesos, cobertura e metodologia representativos.')
    if len(numeric) >= 2:
        corr = df[numeric].corr(method='pearson', min_periods=3)
        indicators['correlacoes'] = [
            {'variaveis': [str(left), str(right)], 'correlacao_pearson': float(corr.loc[left, right]),
             'limitacao': 'associação linear, não causalidade'}
            for i, left in enumerate(corr.columns) for right in corr.columns[i + 1:]
            if pd.notna(corr.loc[left, right])
        ]
    indicators['atipicos'] = {str(column): identificar_atipicos(df, column)
                              for column in numeric[:8]}
    return indicators


def _relatorio(frame, filename):
    df = frame.copy()
    inspection = inspecionar_dados(df, amostra=3)
    stats = estatistica_descritiva(df)
    indicators = calcular_indicadores(df)
    sections = [f'Base: {filename}. Linhas: {inspection["linhas"]}. Colunas: {inspection["colunas"]}.']
    if inspection['periodo']:
        sections.append(f'Período: {inspection["periodo"]["inicio"]} a {inspection["periodo"]["fim"]}.')
    sections.append('Colunas numéricas: ' + (', '.join(inspection['colunas_numericas']) or 'nenhuma') + '.')
    if inspection['colunas_categoricas']:
        sections.append('Colunas categóricas: ' + ', '.join(inspection['colunas_categoricas'][:12]) + '.')
    if inspection['valores_ausentes']:
        sections.append('Valores ausentes: ' + '; '.join(f'{key}: {value}' for key, value in inspection['valores_ausentes'].items()) + '.')
    if inspection['duplicatas']:
        sections.append(f'Duplicatas de linhas: {inspection["duplicatas"]}.')
    if inspection['possiveis_erros_formatacao']:
        sections.append('Possíveis erros de formatação: ' + '; '.join(item['coluna'] for item in inspection['possiveis_erros_formatacao']) + '.')
    if inspection['amostra']:
        sections.append('Amostra inicial: ' + json.dumps(inspection['amostra'], ensure_ascii=False, default=str) + '.')
    if stats:
        measures = []
        for column, values in list(stats.items())[:8]:
            measures.append(f'{column}: soma {values["soma"]:.2f}; média {values["media"]:.2f}; mediana {values["mediana"]:.2f}; mínimo {values["minimo"]:.2f}; máximo {values["maximo"]:.2f}; desvio-padrão {values["desvio_padrao"] if values["desvio_padrao"] is not None else "indisponível"}.')
        sections.append('Estatística descritiva (unidades do arquivo): ' + ' '.join(measures))
    categories = distribuicao_categorias(df)
    if categories:
        sections.append('Distribuição categórica: ' + json.dumps(categories, ensure_ascii=False)[:1600] + '.')
    for column in inspection['colunas_numericas'][:6]:
        evolution = comparar_periodos(df, inspection['coluna_data'], column) if inspection['coluna_data'] else {'disponivel': False}
        if evolution.get('disponivel') and len(evolution['periodos']) >= 2:
            first, last = evolution['periodos'][0], evolution['periodos'][-1]
            change = last['variacao_percentual']
            if change is not None:
                sections.append(f'Variação de {column}: {first["valor"]:.2f} em {first["periodo"]} para {last["valor"]:.2f} em {last["periodo"]}; variação absoluta {last["variacao_absoluta"]:.2f}; percentual {change:.2f}%. Fórmula: ((valor_final - valor_inicial) / valor_inicial) * 100.')
    if indicators.get('margem_media_percentual') is not None:
        sections.append(f'Margem média observada: {indicators["margem_media_percentual"]:.2f}%. Fórmula: (lucro / receita) * 100; só calculada para receitas diferentes de zero.')
    if indicators.get('variacao_observada_serie_de_precos', {}).get('disponivel'):
        sections.append('A série de preços tem variação observada, mas não é uma medida de inflação geral: faltam metodologia, cesta, pesos e representatividade.' )
    correlations = indicators.get('correlacoes', [])
    if correlations:
        strongest = sorted(correlations, key=lambda item: abs(item['correlacao_pearson']), reverse=True)[:5]
        sections.append('Correlações de Pearson: ' + '; '.join(f'{item["variaveis"][0]} e {item["variaveis"][1]}: {item["correlacao_pearson"]:.2f}' for item in strongest) + '. Associação não implica causalidade.')
    outliers = [f'{column}: {len(values.get("indices", []))}' for column, values in indicators.get('atipicos', {}).items() if values.get('disponivel') and values.get('indices')]
    if outliers:
        sections.append('Possíveis valores atípicos pelo método IQR: ' + '; '.join(outliers) + '.')
    return '\n'.join(sections)


def documentos_analise(owner_id, question):
    if not _relevante(question):
        return []
    files = _files(owner_id)
    if not files:
        if not REQUESTS_DATA.search(_normalizar(question)):
            return []
        content = ('Nenhuma base econômica foi enviada para este usuário. Oriente a pessoa a usar '
                   '`/importar "caminho do arquivo.csv"`, `/importar "caminho do arquivo.xlsx"` '
                   'ou `/importar "caminho do arquivo.pdf"` '
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
