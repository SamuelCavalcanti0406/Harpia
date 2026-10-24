"""Processamento documental local com PyMuPDF (fitz), sem executar conteúdo dos arquivos.

O módulo utiliza PyMuPDF para extração eficiente de texto página por página,
otimizado para baixo consumo de memória RAM (gerenciador de contexto with fitz.open()).
Divide o texto em blocos (chunks) de 1000 caracteres com sobreposição de 100 caracteres,
prontos para consumo pelo LLM. Mantém os originais, os resultados derivados e o índice
em diretórios separados por usuário.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
import uuid
import pandas as pd

try:
    import pymupdf as fitz
except ImportError:
    import fitz
    
DOCUMENT_ROOT = Path(__file__).resolve().parent.parent / ".local" / "documents"
MAX_UPLOAD_BYTES = int(os.getenv("HARPIA_DOCUMENT_MAX_BYTES", str(25 * 1024 * 1024)))
MAX_INDEX_CHUNKS = int(os.getenv("HARPIA_DOCUMENT_MAX_INDEX_CHUNKS", "5000"))
CHUNK_SIZE_DEFAULT = 1000
CHUNK_OVERLAP_DEFAULT = 100
SUPPORTED_EXTENSIONS = {".pdf"}


class DocumentProcessingError(ValueError):
    """Erro seguro para apresentar ao cliente sem expor caminhos locais."""


@dataclass
class ProcessedDocument:
    markdown: str
    tables: list[dict]
    metadata: dict
    chunks: list[dict]


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalizar(texto: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", str(texto).lower())
        if unicodedata.category(char) != "Mn"
    )


def _tokens(texto: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[^\W_]+", _normalizar(texto))
        if len(token) > 1
    }


def _safe_name(filename: str | None) -> str:
    original = Path(str(filename or "documento"))
    stem = re.sub(r"[^\w.-]+", "_", original.stem, flags=re.UNICODE).strip("._")[:80]
    suffix = original.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise DocumentProcessingError("Formato não aceito. Envie PDF.")
    return (stem or "documento") + suffix


def _owner_dir(owner_id: str) -> Path:
    digest = hashlib.sha256(str(owner_id).encode("utf-8")).hexdigest()
    return DOCUMENT_ROOT / digest


def _registry_path(owner_id: str) -> Path:
    return _owner_dir(owner_id) / "metadata.json"


def _read_registry(owner_id: str) -> list[dict]:
    path = _registry_path(owner_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DocumentProcessingError("O catálogo local de documentos não pôde ser lido.") from exc
    return data if isinstance(data, list) else []


def _write_registry(owner_id: str, records: list[dict]) -> None:
    path = _registry_path(owner_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _public_record(record: dict) -> dict:
    return {
        "id": record["id"],
        "nome": record["nome"],
        "tipo": record["tipo"],
        "tamanho_bytes": record["tamanho_bytes"],
        "sha256": record["sha256"],
        "enviada_em": record["enviada_em"],
        "processada_em": record.get("processada_em"),
        "status": record["status"],
        "importado": bool(record.get("importado")),
        "ativo": bool(record.get("ativo", True)),
        "paginas": record.get("paginas", []),
        "secoes": record.get("secoes", []),
        "tabelas": record.get("tabelas", []),
    }


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return value
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def registrar_upload(owner_id: str, filename: str | None, stream) -> dict:
    """Guarda um original, deduplicando conteúdo idêntico.

    Um novo arquivo com o mesmo nome e conteúdo diferente vira uma nova versão;
    a versão anterior permanece no armazenamento, mas deixa de ser ativa.
    """
    safe_name = _safe_name(filename)
    owner = _owner_dir(owner_id)
    originals = owner / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    temporary = originals / f"{uuid.uuid4().hex}.upload"
    digest = hashlib.sha256()
    total = 0
    try:
        with temporary.open("wb") as target:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise DocumentProcessingError(
                        f"O documento excede o limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
                    )
                digest.update(chunk)
                target.write(chunk)
        if total == 0:
            raise DocumentProcessingError("O documento está vazio.")

        records = _read_registry(owner_id)
        normalized_name = _normalizar(safe_name)
        for record in records:
            if record.get("sha256") == digest.hexdigest():
                for other in records:
                    if (other.get("id") != record.get("id") and other.get("ativo", True)
                            and _normalizar(other.get("nome", "")) == normalized_name):
                        other["ativo"] = False
                record["ativo"] = True
                _write_registry(owner_id, records)
                temporary.unlink(missing_ok=True)
                return {**_public_record(record), "duplicado": True}

        document_id = uuid.uuid4().hex
        original_path = originals / f"{document_id}__{safe_name}"
        temporary.replace(original_path)
        for record in records:
            if record.get("ativo", True) and _normalizar(record.get("nome", "")) == normalized_name:
                record["ativo"] = False
        record = {
            "id": document_id,
            "nome": str(filename or safe_name),
            "tipo": safe_name.rsplit(".", 1)[-1].lower(),
            "tamanho_bytes": total,
            "sha256": digest.hexdigest(),
            "enviada_em": _agora(),
            "processada_em": None,
            "status": "pendente",
            "importado": False,
            "ativo": True,
            "original_rel": str(original_path.relative_to(owner)),
            "extraido_rel": None,
            "indice_rel": None,
            "tabelas": [],
            "paginas": [],
            "secoes": [],
        }
        records.append(record)
        _write_registry(owner_id, records)
        return {**_public_record(record), "duplicado": False}
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def listar_documentos(owner_id: str) -> list[dict]:
    records = [record for record in _read_registry(owner_id) if record.get("ativo", True)]
    records.sort(key=lambda item: item.get("enviada_em", ""), reverse=True)
    return [_public_record(record) for record in records]


def _record(owner_id: str, document_id: str) -> tuple[list[dict], dict]:
    records = _read_registry(owner_id)
    for record in records:
        if record.get("id") == document_id and record.get("ativo", True):
            return records, record
    raise DocumentProcessingError("Documento não encontrado.")


def extrair_chunks_pdf(
    caminho: str | Path,
    chunk_size: int = CHUNK_SIZE_DEFAULT,
    overlap: int = CHUNK_OVERLAP_DEFAULT,
) -> list[str]:
    """Lê o PDF iterando página por página usando with fitz.open() para poupar memória RAM.

    O texto extraído é imediatamente dividido em chunks de 1000 caracteres, com uma
    sobreposição (overlap) de 100 caracteres entre eles.
    Retorna uma lista de strings fragmentadas prontas para o LLM.
    """
    path = Path(caminho)
    if not path.is_file():
        raise DocumentProcessingError("O documento original não foi encontrado.")
    if path.stat().st_size == 0:
        raise DocumentProcessingError("O documento está vazio.")

    step = chunk_size - overlap
    if step <= 0:
        raise ValueError("chunk_size deve ser estritamente maior que overlap.")

    chunks: list[str] = []
    buffer = ""

    try:
        with fitz.open(str(path)) as doc:
            for page in doc:
                texto_pagina = page.get_text() or ""
                if not texto_pagina:
                    continue
                buffer += texto_pagina
                while len(buffer) >= chunk_size:
                    chunks.append(buffer[:chunk_size])
                    buffer = buffer[step:]
                    if len(chunks) >= MAX_INDEX_CHUNKS:
                        break
                if len(chunks) >= MAX_INDEX_CHUNKS:
                    break
    except Exception as exc:
        if isinstance(exc, DocumentProcessingError):
            raise
        raise DocumentProcessingError(f"Erro ao ler PDF com PyMuPDF: {exc}") from exc

    if len(chunks) == 0:
        if buffer.strip():
            chunks.append(buffer)
    elif len(buffer) > overlap and buffer.strip():
        chunks.append(buffer[:chunk_size])

    return chunks


# Aliases convenientes para interoperabilidade
processar_pdf = extrair_chunks_pdf
ler_pdf_chunks = extrair_chunks_pdf
extrair_texto_pdf_chunks = extrair_chunks_pdf


def processar_arquivo(path: Path, converter=None) -> ProcessedDocument:
    """Processa o PDF localmente utilizando PyMuPDF (fitz) página por página."""
    path = Path(path)
    if not path.is_file():
        raise DocumentProcessingError("O documento original não foi encontrado.")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise DocumentProcessingError("Formato não aceito. Envie PDF.")
    if path.stat().st_size == 0:
        raise DocumentProcessingError("O documento está vazio.")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise DocumentProcessingError("O documento excede o limite configurado.")

    raw_chunks = extrair_chunks_pdf(path, chunk_size=CHUNK_SIZE_DEFAULT, overlap=CHUNK_OVERLAP_DEFAULT)

    pages_list = []
    tables = []
    markdown_parts = []
    table_markdown = []

    try:
        with fitz.open(str(path)) as doc:
            table_idx = 1
            for page_num, page in enumerate(doc, start=1):
                pages_list.append(page_num)
                text = page.get_text() or ""
                if text.strip():
                    markdown_parts.append(text.strip())

                # Extrai tabelas caso existam na página
                try:
                    tab_finder = page.find_tables()
                    for tab in getattr(tab_finder, "tables", []):
                        df = tab.to_pandas()
                        if df is not None and not df.empty:
                            df.columns = [str(c) for c in df.columns]
                            records = [
                                {str(k): _json_safe(v) for k, v in row.items()}
                                for row in df.to_dict(orient="records")
                            ]
                            try:
                                t_md = df.to_markdown(index=False)
                            except Exception:
                                t_md = df.to_csv(index=False)
                            tables.append({
                                "numero": table_idx,
                                "linhas": int(df.shape[0]),
                                "colunas": int(df.shape[1]),
                                "nomes_colunas": df.columns.tolist(),
                                "dados": records,
                                "paginas": [page_num],
                            })
                            table_markdown.append(f"Tabela {table_idx}:\n{t_md[:4000]}")
                            table_idx += 1
                except Exception:
                    pass
    except Exception as exc:
        if isinstance(exc, DocumentProcessingError):
            raise
        raise DocumentProcessingError(f"Falha ao processar o PDF com PyMuPDF: {exc}") from exc

    full_markdown = "\n\n".join(markdown_parts)

    structured_chunks = [
        {
            "conteudo": chunk,
            "pagina": None,
            "paginas": pages_list,
            "secao": None,
            "nivel": None,
        }
        for chunk in raw_chunks
    ]

    if table_markdown:
        structured_chunks.extend(
            {"conteudo": t_md, "pagina": None, "paginas": [], "secao": None, "nivel": None}
            for t_md in table_markdown
        )

    metadata = {
        "paginas": pages_list,
        "secoes": [],
        "quantidade_itens_texto": len(raw_chunks),
        "quantidade_tabelas": len(tables),
    }

    return ProcessedDocument(
        markdown=full_markdown,
        tables=tables,
        metadata=metadata,
        chunks=structured_chunks[:MAX_INDEX_CHUNKS],
    )


def importar_documento(owner_id: str, document_id: str) -> dict:
    records, record = _record(owner_id, document_id)
    owner = _owner_dir(owner_id)
    original = owner / record["original_rel"]
    try:
        processed = processar_arquivo(original)
        extracted = owner / "extracted" / f"{document_id}.md"
        index = owner / "indexes" / f"{document_id}.json"
        tables_dir = owner / "tables" / document_id
        extracted.parent.mkdir(parents=True, exist_ok=True)
        index.parent.mkdir(parents=True, exist_ok=True)
        tables_dir.mkdir(parents=True, exist_ok=True)
        extracted.write_text(processed.markdown, encoding="utf-8")
        table_records = []
        for table in processed.tables:
            table_path = tables_dir / f"tabela_{table['numero']}.csv"
            frame = pd.DataFrame(table["dados"], columns=table["nomes_colunas"])
            frame.to_csv(table_path, index=False, encoding="utf-8-sig")
            table_records.append({key: value for key, value in table.items() if key != "dados"})
        index.write_text(json.dumps({"chunks": processed.chunks}, ensure_ascii=False), encoding="utf-8")
        record.update(
            {
                "processada_em": _agora(),
                "status": "processado",
                "importado": True,
                "extraido_rel": str(extracted.relative_to(owner)),
                "indice_rel": str(index.relative_to(owner)),
                "tabelas": table_records,
                **processed.metadata,
            }
        )
        _write_registry(owner_id, records)
        return _public_record(record)
    except DocumentProcessingError:
        record["status"] = "erro"
        _write_registry(owner_id, records)
        raise


def _load_index(owner_id: str, record: dict) -> list[dict]:
    path = _owner_dir(owner_id) / str(record.get("indice_rel") or "")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    chunks = payload.get("chunks", []) if isinstance(payload, dict) else []
    return chunks if isinstance(chunks, list) else []


def documentos_contexto(owner_id: str, question: str, limite: int = 3) -> list[dict]:
    """Recupera somente trechos indexados de documentos explicitamente importados."""
    query = _tokens(question)
    if not query or limite <= 0:
        return []
    candidates = []

    # Detecta se a pergunta é um pedido genérico de resumo / apresentação do documento
    termos_genericos = {"resumo", "resuma", "documento", "arquivo", "relatorio", "analise", "conteudo", "esse", "este", "sobre"}
    eh_pedido_resumo = len(query - termos_genericos) == 0

    for record in _read_registry(owner_id):
        if not record.get("ativo", True) or not record.get("importado"):
            continue
        chunks = _load_index(owner_id, record)
        for i, chunk in enumerate(chunks):
            content = str(chunk.get("conteudo", "")) if isinstance(chunk, dict) else str(chunk)
            words = _tokens(content)
            matched = query & words
            if matched:
                score = len(matched) / max(1, len(query))
                candidates.append(
                    {
                        "tipo": "documento",
                        "fonte": record["nome"],
                        "trecho": f"página {chunk.get('pagina')}" if isinstance(chunk, dict) and chunk.get("pagina") else f"trecho {i+1}",
                        "conteudo": content,
                        "pontuacao": round(score, 4),
                        "secao": chunk.get("secao") if isinstance(chunk, dict) else None,
                    }
                )
            elif eh_pedido_resumo and i < limite:
                # Em pedidos de resumo geral, prioriza os chunks iniciais (capa, sumário executivo, introdução)
                candidates.append(
                    {
                        "tipo": "documento",
                        "fonte": record["nome"],
                        "trecho": f"página {chunk.get('pagina')}" if isinstance(chunk, dict) and chunk.get("pagina") else f"trecho {i+1}",
                        "conteudo": content,
                        "pontuacao": round(0.5 - (i * 0.05), 4),
                        "secao": chunk.get("secao") if isinstance(chunk, dict) else None,
                    }
                )

    candidates.sort(key=lambda item: (-item["pontuacao"], item["fonte"], item["trecho"]))
    selected = []
    seen = set()
    for item in candidates:
        key = (item["fonte"], item["conteudo"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= limite:
            break
    return selected


def exportar_tabela_csv(owner_id: str, document_id: str, table_number: int) -> tuple[bytes, str]:
    if table_number < 1:
        raise DocumentProcessingError("Número de tabela inválido.")
    _, record = _record(owner_id, document_id)
    tables = record.get("tabelas", [])
    table = next((item for item in tables if item.get("numero") == table_number), None)
    if table is None:
        raise DocumentProcessingError("Tabela não encontrada.")
    path = _owner_dir(owner_id) / "tables" / document_id / f"tabela_{table_number}.csv"
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise DocumentProcessingError("O CSV derivado não está disponível.") from exc
    stem = re.sub(r"[^\w.-]+", "_", Path(record["nome"]).stem, flags=re.UNICODE).strip("._")
    return content, f"{stem or 'documento'}_tabela_{table_number}.csv"
