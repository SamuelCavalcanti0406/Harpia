import io
from pathlib import Path
import tempfile
import unittest

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

    def test_report_explains_period_values_and_limitations(self):
        report = economic._relatorio(self.frame(), 'base.csv')
        self.assertIn('Fórmula:', report)
        self.assertIn('Período:', report)
        self.assertIn('Valores ausentes:', report)


if __name__ == '__main__':
    unittest.main()
