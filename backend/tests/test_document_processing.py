import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import pymupdf as fitz
except ImportError:
    import fitz

import document_processing as documents


def criar_pdf_teste(caminho: Path, paginas_texto: list[str]) -> Path:
    """Cria um PDF de teste localmente usando PyMuPDF."""
    doc = fitz.open()
    for texto in paginas_texto:
        page = doc.new_page(width=3000, height=3000)
        linhas = [texto[i : i + 60] for i in range(0, len(texto), 60)]
        page.insert_textbox(page.rect, "\n".join(linhas))
    doc.save(str(caminho))
    doc.close()
    return caminho


class DocumentProcessingTests(unittest.TestCase):
    def test_extrair_chunks_pdf_retorna_lista_strings_com_chunk_1000_overlap_100(self):
        """Verifica a função eficiente de extração página por página com chunking de 1000 e overlap de 100."""
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            # Cria 3 páginas totalizando > 2500 caracteres
            p1 = "A" * 900 + "B" * 300  # 1200 chars
            p2 = "C" * 800 + "D" * 400  # 1200 chars
            p3 = "E" * 500              # 500 chars
            pdf_path = Path(folder) / "documento_longo.pdf"
            criar_pdf_teste(pdf_path, [p1, p2, p3])

            chunks = documents.extrair_chunks_pdf(pdf_path, chunk_size=1000, overlap=100)

            # Garante que o retorno é uma lista de strings
            self.assertIsInstance(chunks, list)
            self.assertTrue(all(isinstance(c, str) for c in chunks))
            self.assertGreaterEqual(len(chunks), 3)

            # Verifica o tamanho máximo de cada chunk
            for c in chunks:
                self.assertLessEqual(len(c), 1000)

            # Verifica a sobreposição (overlap) de 100 caracteres entre chunks consecutivos
            for i in range(len(chunks) - 1):
                tail = chunks[i][-100:]
                head = chunks[i + 1][:100]
                self.assertEqual(tail, head, f"Falha de overlap entre chunk {i} e {i+1}")

    def test_pdf_curto_retorna_chunk_unico(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            pdf_path = Path(folder) / "curto.pdf"
            criar_pdf_teste(pdf_path, ["Texto curto de teste para LLM."])

            chunks = documents.extrair_chunks_pdf(pdf_path, chunk_size=1000, overlap=100)
            self.assertEqual(len(chunks), 1)
            self.assertIn("Texto curto de teste para LLM.", chunks[0])

    def test_aliases_de_funcao_disponiveis(self):
        self.assertEqual(documents.processar_pdf, documents.extrair_chunks_pdf)
        self.assertEqual(documents.ler_pdf_chunks, documents.extrair_chunks_pdf)
        self.assertEqual(documents.extrair_texto_pdf_chunks, documents.extrair_chunks_pdf)

    def test_processar_arquivo_e_importacao_completa(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(documents, "DOCUMENT_ROOT", Path(folder)):
                pdf_path = Path(folder) / "orig.pdf"
                criar_pdf_teste(pdf_path, ["Relatório Financeiro Página 1", "Dados Econômicos Página 2"])
                content = pdf_path.read_bytes()

                uploaded = documents.registrar_upload("user-teste", "relatorio.pdf", io.BytesIO(content))
                self.assertEqual(uploaded["nome"], "relatorio.pdf")

                imported = documents.importar_documento("user-teste", uploaded["id"])
                self.assertTrue(imported["importado"])
                self.assertEqual(imported["status"], "processado")
                self.assertEqual(imported["paginas"], [1, 2])

                # Testa recuperação de contexto para LLM
                contexto = documents.documentos_contexto("user-teste", "Relatório Econômicos")
                self.assertGreater(len(contexto), 0)
                self.assertEqual(contexto[0]["fonte"], "relatorio.pdf")

    def test_duplicate_and_updated_version_behavior(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(documents, "DOCUMENT_ROOT", Path(folder)):
                pdf1 = Path(folder) / "p1.pdf"
                criar_pdf_teste(pdf1, ["Versao 1"])
                b1 = pdf1.read_bytes()

                pdf2 = Path(folder) / "p2.pdf"
                criar_pdf_teste(pdf2, ["Versao 2 diferente"])
                b2 = pdf2.read_bytes()

                first = documents.registrar_upload("user-a", "relatorio.pdf", io.BytesIO(b1))
                duplicate = documents.registrar_upload("user-a", "relatorio.pdf", io.BytesIO(b1))
                updated = documents.registrar_upload("user-a", "relatorio.pdf", io.BytesIO(b2))
                listed = documents.listar_documentos("user-a")

                self.assertTrue(duplicate["duplicado"])
                self.assertNotEqual(first["id"], updated["id"])
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["id"], updated["id"])

    def test_empty_and_oversized_files_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "vazio.pdf"
            path.write_bytes(b"")
            with self.assertRaises(documents.DocumentProcessingError):
                documents.extrair_chunks_pdf(path)

            old = documents.MAX_UPLOAD_BYTES
            try:
                documents.MAX_UPLOAD_BYTES = 2
                with self.assertRaises(documents.DocumentProcessingError):
                    documents.registrar_upload("user-a", "grande.pdf", io.BytesIO(b"123"))
            finally:
                documents.MAX_UPLOAD_BYTES = old


if __name__ == "__main__":
    unittest.main()
