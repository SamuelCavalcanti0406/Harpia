"""Processamento documental local com Docling, sem executar conteúdo dos arquivos.

O pacote Docling é opcional no ambiente atual. O módulo mantém os originais,
os resultados derivados e o índice em diretórios separados por usuário. A
conversão só ocorre quando o usuário chama a operação de importação.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
import uuid

import pandas as pd


DOCUMENT_ROOT = Path(__file__).resolve().parent.parent / ".local" / "documents"
MAX_UPLOAD_BYTES = int(os.getenv("HARPIA_DOCUMENT_MAX_BYTES", str(25 * 1024 * 1024)))
MAX_INDEX_CHUNKS = int(os.getenv("HARPIA_DOCUMENT_MAX_INDEX_CHUNKS", "5000"))
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".html", ".htm"}


class DocumentProcessingError(ValueError):
    """Erro seguro para apresentar ao cliente sem expor caminhos locais."""


class DoclingUnavailableError(DocumentProcessingError):
    """Docling não está instalado no ambiente virtual ativo."""


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
        raise DocumentProcessingError("Formato não aceito. Envie PDF, DOCX ou HTML.")
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
    result = {
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
    return result


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


def _load_docling():
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise DoclingUnavailableError(
            "O Docling não está instalado no ambiente virtual da HarpIA. "
            "Instale a dependência opcional e tente novamente."
        ) from exc
    return DocumentConverter


@lru_cache(maxsize=1)
def _converter():
    return _load_docling()()


def docling_disponivel() -> bool:
    try:
        _load_docling()
    except DoclingUnavailableError:
        return False
    return True


def _pages(item) -> list[int]:
    pages = []
    for provenance in getattr(item, "prov", []) or []:
        page = getattr(provenance, "page_no", None)
        if isinstance(page, int) and page > 0:
            pages.append(page)
    return sorted(set(pages))


def _label(item) -> str:
    return str(getattr(item, "label", "")).lower()


def _is_section(item) -> bool:
    label = _label(item)
    return any(term in label for term in ("title", "section_header", "heading"))


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return value
    if hasattr(value, "item"):
        return _json_safe(value.item())
    return str(value)


def _table_data(table, document, number: int) -> tuple[dict, str]:
    try:
        frame = table.export_to_dataframe(doc=document)
    except Exception as exc:
        raise DocumentProcessingError(f"A tabela {number} não pôde ser estruturada.") from exc
    frame = frame.copy()
    frame.columns = [str(column) for column in frame.columns]
    records = [
        {str(key): _json_safe(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]
    if frame.empty:
        markdown = "(tabela vazia)"
    else:
        try:
            markdown = frame.to_markdown(index=False)
        except (ImportError, ModuleNotFoundError):
            markdown = frame.to_csv(index=False)
    return {
        "numero": number,
        "linhas": int(frame.shape[0]),
        "colunas": int(frame.shape[1]),
        "nomes_colunas": frame.columns.tolist(),
        "dados": records,
        "paginas": _pages(table),
    }, markdown


def processar_arquivo(path: Path, converter=None) -> ProcessedDocument:
    """Converte um original com a API oficial DocumentConverter.convert."""
    path = Path(path)
    if not path.is_file():
        raise DocumentProcessingError("O documento original não foi encontrado.")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise DocumentProcessingError("Formato não aceito. Envie PDF, DOCX ou HTML.")
    if path.stat().st_size == 0:
        raise DocumentProcessingError("O documento está vazio.")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise DocumentProcessingError("O documento excede o limite configurado.")
    converter = converter or _converter()
    try:
        result = converter.convert(str(path))
    except Exception as exc:
        raise DocumentProcessingError("O Docling não conseguiu converter o documento.") from exc
    status = str(getattr(result, "status", "")).upper()
    if "FAILURE" in status:
        raise DocumentProcessingError("O Docling informou falha na conversão do documento.")
    document = getattr(result, "document", None)
    if document is None:
        raise DocumentProcessingError("A conversão não retornou um documento estruturado.")

    try:
        markdown = str(document.export_to_markdown())
    except Exception as exc:
        raise DocumentProcessingError("Não foi possível exportar o texto estruturado.") from exc

    chunks = []
    pages = set()
    sections = []
    current_section = None
    for entry in document.iterate_items():
        item = entry[0] if isinstance(entry, (tuple, list)) else entry
        text = str(getattr(item, "text", "") or "").strip()
        item_pages = _pages(item)
        pages.update(item_pages)
        if _is_section(item) and text:
            current_section = text[:200]
            sections.append(current_section)
        if text:
            chunks.append(
                {
                    "conteudo": text[:1200],
                    "pagina": item_pages[0] if item_pages else None,
                    "paginas": item_pages,
                    "secao": current_section,
                    "nivel": entry[1] if isinstance(entry, (tuple, list)) and len(entry) > 1 else None,
                }
            )
        if len(chunks) >= MAX_INDEX_CHUNKS:
            break

    tables = []
    table_markdown = []
    for number, table in enumerate(getattr(document, "tables", []) or [], 1):
        data, table_md = _table_data(table, document, number)
        tables.append(data)
        table_markdown.append(f"Tabela {number}:\n{table_md[:4000]}")
        pages.update(data["paginas"])

    metadata = {
        "paginas": sorted(pages),
        "secoes": list(dict.fromkeys(sections)),
        "quantidade_itens_texto": len(chunks),
        "quantidade_tabelas": len(tables),
    }
    if table_markdown:
        chunks.extend(
            {"conteudo": text, "pagina": None, "paginas": [], "secao": None, "nivel": None}
            for text in table_markdown
        )
    return ProcessedDocument(markdown=markdown, tables=tables, metadata=metadata, chunks=chunks[:MAX_INDEX_CHUNKS])


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
    for record in _read_registry(owner_id):
        if not record.get("ativo", True) or not record.get("importado"):
            continue
        for chunk in _load_index(owner_id, record):
            content = str(chunk.get("conteudo", ""))
            words = _tokens(content)
            matched = query & words
            if not matched:
                continue
            score = len(matched) / max(1, len(query))
            candidates.append(
                {
                    "tipo": "documento",
                    "fonte": record["nome"],
                    "trecho": "página " + str(chunk.get("pagina")) if chunk.get("pagina") else "seção",
                    "conteudo": content,
                    "pontuacao": round(score, 4),
                    "secao": chunk.get("secao"),
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
