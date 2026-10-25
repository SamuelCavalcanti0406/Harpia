"""Processamento documental local com PyMuPDF (fitz), sem executar conteúdo dos arquivos.

O módulo utiliza PyMuPDF para extração eficiente de texto página por página,
otimizado para baixo consumo de memória RAM (gerenciador de contexto with fitz.open()).
Divide o texto em blocos (chunks) de 1000 caracteres com sobreposição de 100 caracteres e
os grava no índice FTS5 (indice.py) à medida que são gerados. Mantém os originais e os
resultados derivados em diretórios separados por usuário.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
import uuid

try:
    import pymupdf as fitz
except ImportError:
    import fitz

import busca
from conectores import ConectorPDF, dividir_em_chunks, paginas_pdf
import indice

DOCUMENT_ROOT = Path(__file__).resolve().parent.parent / ".local" / "documents"
MAX_UPLOAD_BYTES = int(os.getenv("HARPIA_DOCUMENT_MAX_BYTES", str(25 * 1024 * 1024)))
MAX_INDEX_CHUNKS = int(os.getenv("HARPIA_DOCUMENT_MAX_INDEX_CHUNKS", "5000"))
CHUNK_SIZE_DEFAULT = 1000
CHUNK_OVERLAP_DEFAULT = 100
SUPPORTED_EXTENSIONS = {".pdf"}
COLECAO_DOCUMENTOS = "documentos"


class DocumentProcessingError(ValueError):
    """Erro seguro para apresentar ao cliente sem expor caminhos locais."""


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalizar(texto: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", str(texto).lower())
        if unicodedata.category(char) != "Mn"
    )


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
    """Lê o PDF página por página e divide o texto em janelas de 1000 caracteres com
    sobreposição de 100. A indexação usa o mesmo gerador sem montar esta lista."""
    path = Path(caminho)
    if not path.is_file():
        raise DocumentProcessingError("O documento original não foi encontrado.")
    if path.stat().st_size == 0:
        raise DocumentProcessingError("O documento está vazio.")
    if chunk_size - overlap <= 0:
        raise ValueError("chunk_size deve ser estritamente maior que overlap.")
    try:
        return [trecho.conteudo for trecho in dividir_em_chunks(
            paginas_pdf(path), chunk_size, overlap, limite=MAX_INDEX_CHUNKS)]
    except Exception as exc:
        raise DocumentProcessingError(f"Erro ao ler PDF com PyMuPDF: {exc}") from exc


# Aliases convenientes para interoperabilidade
processar_pdf = extrair_chunks_pdf
ler_pdf_chunks = extrair_chunks_pdf
extrair_texto_pdf_chunks = extrair_chunks_pdf


def _validar_pdf(path: Path) -> None:
    if not path.is_file():
        raise DocumentProcessingError("O documento original não foi encontrado.")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise DocumentProcessingError("Formato não aceito. Envie PDF.")
    if path.stat().st_size == 0:
        raise DocumentProcessingError("O documento está vazio.")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise DocumentProcessingError("O documento excede o limite configurado.")


def _extrair_texto_e_tabelas(path: Path, markdown_destino: Path, tabelas_dir: Path) -> tuple[list[int], list[dict]]:
    """Grava o texto e as tabelas página a página, sem acumular o documento na memória."""
    paginas: list[int] = []
    tabelas: list[dict] = []
    try:
        with fitz.open(str(path)) as doc, markdown_destino.open("w", encoding="utf-8") as markdown:
            for page_num, page in enumerate(doc, start=1):
                paginas.append(page_num)
                text = (page.get_text() or "").strip()
                if text:
                    markdown.write(text + "\n\n")
                try:
                    finder = page.find_tables()
                except Exception:
                    continue
                for tab in getattr(finder, "tables", []):
                    try:
                        df = tab.to_pandas()
                    except Exception:
                        continue
                    if df is None or df.empty:
                        continue
                    numero = len(tabelas) + 1
                    df.columns = [str(c) for c in df.columns]
                    df.to_csv(tabelas_dir / f"tabela_{numero}.csv", index=False, encoding="utf-8-sig")
                    tabelas.append({
                        "numero": numero,
                        "linhas": int(df.shape[0]),
                        "colunas": int(df.shape[1]),
                        "nomes_colunas": df.columns.tolist(),
                        "paginas": [page_num],
                    })
    except DocumentProcessingError:
        raise
    except Exception as exc:
        raise DocumentProcessingError(f"Falha ao processar o PDF com PyMuPDF: {exc}") from exc
    return paginas, tabelas


def _conector(owner_id: str, records: list[dict]) -> ConectorPDF:
    owner = _owner_dir(owner_id)
    docs = [{"id": r["id"], "nome": r["nome"], "sha256": r.get("sha256"),
             "caminho": owner / r["original_rel"]} for r in records]
    return ConectorPDF(docs, dono=owner_id, colecao=COLECAO_DOCUMENTOS, limite_trechos=MAX_INDEX_CHUNKS)


def _ativos_importados(owner_id: str) -> list[dict]:
    return [r for r in _read_registry(owner_id) if r.get("ativo", True) and r.get("importado")]


def sincronizar_documentos(owner_id: str) -> dict:
    """Mantém o índice igual aos documentos ativos: indexa importações antigas (catálogo JSON
    de versões anteriores) e remove versões substituídas. Sem mudanças, custa uma consulta."""
    return indice.sincronizar(_conector(owner_id, _ativos_importados(owner_id)))


def importar_documento(owner_id: str, document_id: str) -> dict:
    records, record = _record(owner_id, document_id)
    owner = _owner_dir(owner_id)
    original = owner / record["original_rel"]
    try:
        _validar_pdf(original)
        extracted = owner / "extracted" / f"{document_id}.md"
        tables_dir = owner / "tables" / document_id
        extracted.parent.mkdir(parents=True, exist_ok=True)
        tables_dir.mkdir(parents=True, exist_ok=True)
        paginas, tabelas = _extrair_texto_e_tabelas(original, extracted, tables_dir)
        resumo = indice.sincronizar(_conector(owner_id, [record]), remover_ausentes=False)
        if resumo["falhas"]:
            raise DocumentProcessingError("O texto do PDF não pôde ser indexado.")
        record.update(
            {
                "processada_em": _agora(),
                "status": "processado",
                "importado": True,
                "extraido_rel": str(extracted.relative_to(owner)),
                "indice_rel": None,
                "indice": "fts5",
                "tabelas": tabelas,
                "paginas": paginas,
                "secoes": [],
                "quantidade_tabelas": len(tabelas),
            }
        )
        _write_registry(owner_id, records)
        return _public_record(record)
    except DocumentProcessingError:
        record["status"] = "erro"
        _write_registry(owner_id, records)
        raise


TERMOS_RESUMO = {"resumo", "resuma", "resumir", "documento", "arquivo", "relatorio", "analise",
                 "conteudo", "pdf", "principal", "principais", "geral"}


def documentos_contexto(owner_id: str, question: str, limite: int = 3) -> list[dict]:
    """Trechos dos documentos importados e ativos do usuário, no máximo 5."""
    termos = busca.termos(question)
    if not termos or limite <= 0:
        return []
    ativos = _ativos_importados(owner_id)
    if not ativos:
        return []
    sincronizar_documentos(owner_id)
    chaves = [r["id"] for r in ativos]
    if set(termos) <= TERMOS_RESUMO:
        # Pedido genérico ("resuma o documento"): capa, sumário e introdução.
        resultados = busca.primeiros(COLECAO_DOCUMENTOS, owner_id, chaves, limite)
    else:
        resultados = busca.buscar(question, COLECAO_DOCUMENTOS, owner_id, chaves, limite,
                                  cobertura_minima=0.5, max_por_fonte=2)
    return [{**r.como_dict(), "tipo": "documento"} for r in resultados]


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
