import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import economic_analysis as economic


class EconomicAnalysisTests(unittest.TestCase):
    def frame(self):
        return pd.DataFrame({
            'data': ['01/01/2025', '01/02/2025', '01/03/2025', '01/04/2025'],
            'categoria': ['A', 'A', 'B', 'B'],
            'receita': ['100,00', '120,00', '150,00', '180,00'],
            'custo': [40, 45, np.nan, 60],
        })

    def test_inspecao_detects_types_missing_duplicates_and_sample(self):
        frame = self.frame()
        result = economic.inspecionar_dados(frame)
        self.assertEqual(result['linhas'], 4)
        self.assertEqual(result['colunas'], 4)
        self.assertEqual(result['coluna_data'], 'data')
        self.assertEqual(result['valores_ausentes']['custo'], 1)
        self.assertEqual(len(result['amostra']), 4)
        self.assertIn('receita', result['colunas_numericas'])

    def test_descriptive_statistics_and_category_distribution(self):
        stats = economic.estatistica_descritiva(self.frame())
        self.assertAlmostEqual(stats['receita']['soma'], 550.0)
        self.assertAlmostEqual(stats['receita']['mediana'], 135.0)
        self.assertEqual(economic.distribuicao_categorias(self.frame())['categoria']['A'], 2)

    def test_percent_change_is_explicit_and_safe(self):
        self.assertAlmostEqual(economic.percentual_variacao(100, 125), 25.0)
        self.assertIsNone(economic.percentual_variacao(0, 125))
        self.assertIsNone(economic.percentual_variacao('x', 125))

    def test_period_comparison_and_outliers(self):
        result = economic.comparar_periodos(self.frame(), 'data', 'receita')
        self.assertTrue(result['disponivel'])
        self.assertEqual(len(result['periodos']), 4)
        self.assertAlmostEqual(result['periodos'][-1]['variacao_percentual'], 20.0)
        outliers = economic.identificar_atipicos(pd.DataFrame({'x': [1, 2, 2, 3, 100]}), 'x')
        self.assertTrue(outliers['disponivel'])
        self.assertIn(4, outliers['indices'])

    def test_excel_read_and_invalid_empty(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'base.xlsx'
            self.frame().to_excel(path, index=False)
            loaded = economic._ler_arquivo(path, '.xlsx')
            self.assertEqual(list(loaded.columns), ['data', 'categoria', 'receita', 'custo'])
        with self.assertRaises(economic.EconomicDataError):
            economic._ler_arquivo(Path(folder) / 'missing.csv', '.csv')

    def test_large_file_limit(self):
        frame = pd.DataFrame({'valor': [1, 2, 3]})
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'base.csv'
            frame.to_csv(path, index=False)
            old = economic.MAX_ROWS
            try:
                economic.MAX_ROWS = 2
                with self.assertRaises(economic.EconomicDataError):
                    economic._ler_arquivo(path, '.csv')
            finally:
                economic.MAX_ROWS = old

    def make_pdf(self, stream_content):
        stream_bytes = stream_content.encode('latin1')
        length = len(stream_bytes)
        return (
            b'%PDF-1.4\n'
            b'1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n'
            b'2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n'
            b'3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
            b'/Resources <</Font <</F1 4 0 R>>>> /Contents 5 0 R>> endobj\n'
            b'4 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj\n'
            b'5 0 obj <</Length ' + str(length).encode('ascii') + b'>> stream\n'
            + stream_bytes + b'\nendstream endobj\n'
            b'xref\n0 6\n'
            b'0000000000 65535 f \n'
            b'0000000009 00000 n \n'
            b'0000000054 00000 n \n'
            b'0000000105 00000 n \n'
            b'0000000207 00000 n \n'
            b'0000000277 00000 n \n'
            b'trailer <</Size 6 /Root 1 0 R>>\n'
            b'startxref\n500\n%%EOF'
        )

    def test_report_explains_period_values_and_limitations(self):
        report = economic._relatorio(self.frame(), 'base.csv')
        self.assertIn('Fórmula:', report)
        self.assertIn('Período:', report)
        self.assertIn('Valores ausentes:', report)

    def test_pdf_read_and_full_analysis(self):
        stream = (
            'BT\n/F1 10 Tf\n'
            '50 700 Td (Data) Tj 60 0 Td (Categoria) Tj 70 0 Td (Receita) Tj 60 0 Td (Custo) Tj\n'
            '-190 -20 Td (01/01/2025) Tj 60 0 Td (Vendas) Tj 70 0 Td (1500,50) Tj 60 0 Td (450,00) Tj\n'
            '-190 -20 Td (01/02/2025) Tj 60 0 Td (Servicos) Tj 70 0 Td (2300,00) Tj 60 0 Td (600,00) Tj\n'
            'ET'
        )
        pdf_bytes = self.make_pdf(stream)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'base.pdf'
            path.write_bytes(pdf_bytes)
            loaded = economic._ler_arquivo(path, '.pdf')
            self.assertEqual(list(loaded.columns), ['Data', 'Categoria', 'Receita', 'Custo'])
            self.assertEqual(len(loaded), 2)
            report = economic._relatorio(loaded, 'base.pdf')
            self.assertIn('Base: base.pdf.', report)
            self.assertIn('Receita:', report)

    def test_pdf_upload_and_listing(self):
        stream = (
            'BT\n/F1 10 Tf\n'
            '50 700 Td (Data) Tj 60 0 Td (Receita) Tj 60 0 Td (Custo) Tj\n'
            '-120 -20 Td (01/01/2025) Tj 60 0 Td (100,00) Tj 60 0 Td (40,00) Tj\n'
            'ET'
        )
        pdf_bytes = self.make_pdf(stream)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            with patch.object(economic, 'DATA_ROOT', Path(folder)):
                uploaded = economic.salvar_upload('user-1', 'demonstrativo.pdf', io.BytesIO(pdf_bytes))
                self.assertEqual(uploaded['nome'], 'demonstrativo.pdf')
                self.assertEqual(uploaded['linhas'], 1)
                self.assertEqual(uploaded['colunas'], ['Data', 'Receita', 'Custo'])
                bases = economic.listar_bases('user-1')
                self.assertEqual(len(bases), 1)
                self.assertEqual(bases[0]['nome'], 'demonstrativo.pdf')
                self.assertTrue(bases[0]['ativa'])
                docs = economic.documentos_analise('user-1', 'Qual foi a receita?')
                self.assertTrue(len(docs) > 0)
                self.assertIn('Receita', docs[0]['conteudo'])

    def test_pdf_pipe_markdown_format(self):
        stream = (
            'BT\n/F1 10 Tf\n'
            '50 700 Td (| Data | Categoria | Receita | Custo |) Tj\n'
            '0 -20 Td (| --- | --- | --- | --- |) Tj\n'
            '0 -20 Td (| 01/01/2025 | Vendas | 1000,00 | 300,00 |) Tj\n'
            '0 -20 Td (| 01/02/2025 | Vendas | 1200,00 | 350,00 |) Tj\n'
            'ET'
        )
        pdf_bytes = self.make_pdf(stream)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'markdown.pdf'
            path.write_bytes(pdf_bytes)
            loaded = economic._ler_arquivo(path, '.pdf')
            self.assertEqual(list(loaded.columns), ['Data', 'Categoria', 'Receita', 'Custo'])
            self.assertEqual(len(loaded), 2)

    def test_pdf_invalid_empty_or_non_tabular(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            empty_path = Path(folder) / 'vazio.pdf'
            empty_path.write_bytes(b'')
            with self.assertRaises(economic.EconomicDataError):
                economic._ler_arquivo(empty_path, '.pdf')

            corrupt_path = Path(folder) / 'corrupto.pdf'
            corrupt_path.write_bytes(b'not a pdf at all')
            with self.assertRaises(economic.EconomicDataError):
                economic._ler_arquivo(corrupt_path, '.pdf')

            non_tabular_stream = (
                'BT\n/F1 12 Tf\n50 700 Td (Este texto e apenas um paragrafo corrido sem tabelas ou numeros.) Tj\nET'
            )
            text_pdf = Path(folder) / 'texto.pdf'
            text_pdf.write_bytes(self.make_pdf(non_tabular_stream))
            with self.assertRaises(economic.EconomicDataError):
                economic._ler_arquivo(text_pdf, '.pdf')


if __name__ == '__main__':
    unittest.main()

