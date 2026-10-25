"""Conectores de conhecimento local: cada um descreve fontes e produz trechos sob demanda.

Nada aqui carrega uma coleção inteira na memória. Os trechos são gerados um a um
(geradores), e PDFs são lidos página por página com PyMuPDF.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Iterable, Iterator, Protocol

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - nome antigo do pacote
    import fitz

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100
TEXT_EXTENSIONS = {'.txt', '.md'}


@dataclass(frozen=True)
class Fonte:
    """Uma unidade indexável (arquivo ou documento importado)."""
    colecao: str
    chave: str          # identificador estável dentro da coleção (caminho relativo ou id)
    nome: str           # nome exibido ao usuário
    tipo: str           # 'texto' ou 'documento'
    assinatura: str     # muda quando o conteúdo muda; evita reindexar o que não mudou
    caminho: Path
    dono: str = ''      # '' para a base pública; id do usuário para documentos privados


@dataclass(frozen=True)
class Trecho:
    ordem: int
    pagina: int | None
    conteudo: str


class Conector(Protocol):
    colecao: str
    dono: str

    def fontes(self) -> Iterable[Fonte]: ...

    def trechos(self, fonte: Fonte) -> Iterator[Trecho]: ...


def dividir_em_chunks(partes: Iterable[tuple[int | None, str]], tamanho: int = CHUNK_SIZE,
                      sobreposicao: int = CHUNK_OVERLAP, limite: int | None = None) -> Iterator[Trecho]:
    """Divisão matemática em janelas fixas de `tamanho` com passo `tamanho - sobreposicao`.

    Recebe pares (página, texto) em sequência e mantém em memória apenas o buffer
    corrente. Cada trecho registra a página onde começa.
    """
    passo = tamanho - sobreposicao
    if sobreposicao < 0 or passo <= 0:
        raise ValueError('chunk_size deve ser estritamente maior que overlap.')
    buffer = ''
    inicio = 0                  # deslocamento absoluto de buffer[0]
    marcas: list[tuple[int, int | None]] = []   # (deslocamento absoluto, página)
    ordem = 0

    def pagina_atual():
        while len(marcas) > 1 and marcas[1][0] <= inicio:
            marcas.pop(0)
        return marcas[0][1] if marcas else None

    for pagina, texto in partes:
        if not texto:
            continue
        marcas.append((inicio + len(buffer), pagina))
        buffer += texto
        while len(buffer) >= tamanho:
            yield Trecho(ordem, pagina_atual(), buffer[:tamanho])
            ordem += 1
            buffer = buffer[passo:]
            inicio += passo
            if limite and ordem >= limite:
                return
    if ordem == 0:
        if buffer.strip():
            yield Trecho(0, pagina_atual(), buffer)
    elif len(buffer) > sobreposicao and buffer.strip():
        yield Trecho(ordem, pagina_atual(), buffer[:tamanho])


def paginas_pdf(caminho: Path) -> Iterator[tuple[int, str]]:
    """Texto de cada página; o documento fica aberto só durante a iteração."""
    with fitz.open(str(caminho)) as doc:
        for numero, page in enumerate(doc, start=1):
            yield numero, page.get_text() or ''


def _titulo(relativo: str) -> str:
    """'economia/02_macro/inflacao.txt' -> 'economia macro inflacao' (pesa no ranking)."""
    partes = Path(relativo).with_suffix('').parts
    return ' '.join(p.replace('_', ' ').lstrip('0123456789 ') or p for p in partes)


class ConectorTexto:
    """Arquivos .txt e .md de um diretório (recursivo)."""

    def __init__(self, raiz: Path, colecao: str = 'base', dono: str = ''):
        self.raiz = Path(raiz)
        self.colecao = colecao
        self.dono = dono

    def fontes(self) -> Iterator[Fonte]:
        if not self.raiz.exists():
            return
        for caminho in sorted(self.raiz.rglob('*')):
            if not caminho.is_file() or caminho.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            info = caminho.stat()
            relativo = caminho.relative_to(self.raiz).as_posix()
            yield Fonte(self.colecao, relativo, relativo, 'texto',
                        f'{info.st_mtime_ns}:{info.st_size}', caminho, self.dono)

    def trechos(self, fonte: Fonte) -> Iterator[Trecho]:
        texto = ' '.join(fonte.caminho.read_text(encoding='utf-8').split())
        return dividir_em_chunks([(None, texto)])

    @staticmethod
    def titulo(fonte: Fonte) -> str:
        return _titulo(fonte.chave)


class ConectorPDF:
    """PDFs importados por um usuário; o texto é extraído incrementalmente com PyMuPDF."""

    def __init__(self, documentos: Iterable[dict], dono: str, colecao: str = 'documentos',
                 limite_trechos: int | None = None):
        # Cada item: {'id', 'nome', 'sha256', 'caminho'}
        self.documentos = list(documentos)
        self.dono = dono
        self.colecao = colecao
        self.limite_trechos = limite_trechos

    def fontes(self) -> Iterator[Fonte]:
        for doc in self.documentos:
            caminho = Path(doc['caminho'])
            assinatura = doc.get('sha256') or _sha256(caminho)
            yield Fonte(self.colecao, doc['id'], doc['nome'], 'documento', assinatura, caminho, self.dono)

    def trechos(self, fonte: Fonte) -> Iterator[Trecho]:
        return dividir_em_chunks(paginas_pdf(fonte.caminho), limite=self.limite_trechos)

    @staticmethod
    def titulo(fonte: Fonte) -> str:
        return _titulo(fonte.nome)


def _sha256(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open('rb') as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b''):
            digest.update(bloco)
    return digest.hexdigest()
