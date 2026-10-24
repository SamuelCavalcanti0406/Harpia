from pathlib import Path
from types import SimpleNamespace
import io
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import document_processing as documents


class Provenance:
    def __init__(self, page_no):
        self.page_no = page_no


class Item:
    def __init__(self, text, label, page, level=1):
        self.text = text
        self.label = label
        self.prov = [Provenance(page)]
        self.level = level


class Table:
    def __init__(self):
        self.prov = [Provenance(2)]

    def export_to_dataframe(self, doc=None):
        return pd.DataFrame({'Produto': ['A', 'B'], 'Valor': [10, 20]})


class FakeDocument:
    tables = [Table()]

    def export_to_markdown(self):
        return '# Relatório\n\nTexto estruturado.'

    def iterate_items(self):
        return [(Item('Relatório', 'section_header', 1), 1),
                (Item('Texto estruturado.', 'text', 1), 2)]


class FakeConverter:
    def convert(self, source):
        return SimpleNamespace(status='success', document=FakeDocument())


class DocumentProcessingTests(unittest.TestCase):
    def test_pdf_text_sections_and_tables_are_extracted(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'exemplo.pdf'
            path.write_bytes(b'%PDF-fake-for-adapter-test')
            result = documents.processar_arquivo(path, converter=FakeConverter())
        self.assertIn('Texto estruturado', result.markdown)
        self.assertEqual(result.metadata['secoes'], ['Relatório'])
        self.assertEqual(result.metadata['paginas'], [1, 2])
        self.assertEqual(result.tables[0]['linhas'], 2)
        self.assertEqual(result.tables[0]['nomes_colunas'], ['Produto', 'Valor'])

    def test_controlled_import_and_csv_export_keep_original_name(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(documents, 'DOCUMENT_ROOT', Path(folder)):
                uploaded = documents.registrar_upload('user-a', 'relatorio.pdf', io.BytesIO(b'pdf'))
                self.assertEqual(uploaded['nome'], 'relatorio.pdf')
                with patch.object(documents, '_converter', return_value=FakeConverter()):
                    imported = documents.importar_documento('user-a', uploaded['id'])
                self.assertTrue(imported['importado'])
                content, filename = documents.exportar_tabela_csv('user-a', uploaded['id'], 1)
        self.assertEqual(filename, 'relatorio_tabela_1.csv')
        self.assertIn('Produto', content.decode('utf-8-sig'))

    def test_duplicate_and_updated_version_behavior(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(documents, 'DOCUMENT_ROOT', Path(folder)):
                first = documents.registrar_upload('user-a', 'relatorio.pdf', io.BytesIO(b'one'))
                duplicate = documents.registrar_upload('user-a', 'relatorio.pdf', io.BytesIO(b'one'))
                updated = documents.registrar_upload('user-a', 'relatorio.pdf', io.BytesIO(b'two'))
                listed = documents.listar_documentos('user-a')
        self.assertTrue(duplicate['duplicado'])
        self.assertNotEqual(first['id'], updated['id'])
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['id'], updated['id'])

    def test_empty_and_oversized_files_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'vazio.pdf'
            path.write_bytes(b'')
            with self.assertRaises(documents.DocumentProcessingError):
                documents.processar_arquivo(path, converter=FakeConverter())
            old = documents.MAX_UPLOAD_BYTES
            try:
                documents.MAX_UPLOAD_BYTES = 2
                with self.assertRaises(documents.DocumentProcessingError):
                    documents.registrar_upload('user-a', 'grande.pdf', io.BytesIO(b'123'))
            finally:
                documents.MAX_UPLOAD_BYTES = old

    def test_documentling_unavailability_is_explicit(self):
        if documents.docling_disponivel():
            self.skipTest('Docling está instalado neste ambiente.')
        with self.assertRaises(documents.DoclingUnavailableError):
            documents._load_docling()


if __name__ == '__main__':
    unittest.main()
