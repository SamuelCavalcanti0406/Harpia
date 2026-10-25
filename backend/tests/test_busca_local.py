"""Motor de busca local: chunking, índice FTS5, ranqueamento e roteador de intenção."""
import _isolamento  # noqa: F401  (índice FTS5 temporário; precisa vir antes dos módulos da HarpIA)
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import pymupdf as fitz
except ImportError:
    import fitz

import busca
import conectores
import indice
import roteador
from roteador import Intencao


class IndiceTemporario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.indice_patch = patch.object(indice, 'INDEX_PATH', Path(self.tmp.name) / 'indice.db')
        self.indice_patch.start()
        self.pasta = Path(self.tmp.name) / 'base'
        self.pasta.mkdir()

    def tearDown(self):
        self.indice_patch.stop()
        self.tmp.cleanup()

    def escrever(self, nome, texto):
        caminho = self.pasta / nome
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(texto, encoding='utf-8')
        return caminho


class ChunkingTests(unittest.TestCase):
    def test_divisao_matematica_1000_com_sobreposicao_100(self):
        texto = ''.join(chr(65 + (i // 37) % 26) for i in range(3500))
        trechos = list(conectores.dividir_em_chunks([(None, texto)]))
        self.assertTrue(all(len(t.conteudo) <= 1000 for t in trechos))
        for anterior, atual in zip(trechos, trechos[1:]):
            self.assertEqual(anterior.conteudo[-100:], atual.conteudo[:100])
        self.assertEqual([t.ordem for t in trechos], list(range(len(trechos))))
        self.assertTrue(texto.endswith(trechos[-1].conteudo[-50:]))

    def test_trecho_registra_pagina_onde_comeca(self):
        paginas = [(1, 'a' * 950), (2, 'b' * 950), (3, 'c' * 950)]
        trechos = list(conectores.dividir_em_chunks(paginas))
        self.assertEqual([t.pagina for t in trechos], [1, 1, 2, 3])

    def test_conector_produz_trechos_sob_demanda(self):
        self.assertTrue(inspect.isgenerator(conectores.dividir_em_chunks([(1, 'x')])))
        with self.assertRaises(ValueError):
            list(conectores.dividir_em_chunks([(1, 'x')], tamanho=100, sobreposicao=100))


class IndiceTests(IndiceTemporario):
    def test_nao_reindexa_inalterados_e_acompanha_mudancas(self):
        self.escrever('frutas.txt', 'A jabuticaba é uma fruta brasileira.')
        self.escrever('arvores.md', 'O ipê floresce no inverno.')
        conector = conectores.ConectorTexto(self.pasta)
        primeiro = indice.sincronizar(conector)
        self.assertEqual((primeiro['indexadas'], primeiro['inalteradas']), (2, 0))
        self.assertEqual(indice.sincronizar(conector)['inalteradas'], 2)

        self.escrever('frutas.txt', 'A manga é uma fruta tropical muito doce.')
        mudou = indice.sincronizar(conector)
        self.assertEqual((mudou['indexadas'], mudou['inalteradas']), (1, 1))
        self.assertEqual(busca.buscar('jabuticaba'), [])
        self.assertEqual(busca.buscar('manga tropical')[0].fonte, 'frutas.txt')

        (self.pasta / 'frutas.txt').unlink()
        self.assertEqual(indice.sincronizar(conector)['removidas'], 1)
        self.assertEqual(busca.buscar('manga'), [])

    def test_indice_fica_em_arquivo_proprio(self):
        self.escrever('a.txt', 'texto')
        indice.sincronizar(conectores.ConectorTexto(self.pasta))
        self.assertTrue(Path(indice.INDEX_PATH).is_file())
        with indice.conexao() as conn:
            tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('trechos_fts', tabelas)
        self.assertNotIn('conversas', tabelas)


class BuscaTests(IndiceTemporario):
    def setUp(self):
        super().setUp()
        self.escrever('economia/inflacao.txt',
                      'Inflação é o aumento generalizado dos preços. Suas causas incluem demanda aquecida '
                      'e choques de custos. A inflação reduz o poder de compra.')
        self.escrever('economia/juros.txt', 'Os juros são o preço do dinheiro no tempo e afetam a inflação.')
        self.escrever('historia/roma.txt', 'O Império Romano dominou o Mediterrâneo por séculos.')
        for n in range(8):
            self.escrever(f'repetidos/copia_{n}.txt', f'Documento {n}: preços sobem com a inflação de custos.')
        indice.sincronizar(conectores.ConectorTexto(self.pasta))

    def test_resultado_estruturado_com_origem_e_relevancia(self):
        resultado = busca.buscar('aumento generalizado dos preços')[0]
        self.assertEqual(resultado.fonte, 'economia/inflacao.txt')
        self.assertIsInstance(resultado.id, int)
        self.assertEqual(resultado.trecho, 'trecho 1')
        self.assertGreater(resultado.relevancia, 0)
        self.assertIn('aumento generalizado', resultado.conteudo)

    def test_acentos_plural_e_caixa_nao_importam(self):
        self.assertEqual(busca.buscar('INFLACOES')[0].fonte, 'economia/inflacao.txt')
        self.assertEqual(busca.buscar('império romano')[0].fonte, 'historia/roma.txt')

    def test_limite_estrito_de_cinco_e_ordem_por_bm25(self):
        resultados = busca.buscar('inflação preços', limite=50)
        self.assertLessEqual(len(resultados), busca.MAX_RESULTADOS)
        relevancias = [r.relevancia for r in resultados]
        self.assertEqual(relevancias, sorted(relevancias, reverse=True))
        self.assertEqual(len({r.chave for r in resultados}), len(resultados))

    def test_pergunta_sem_cobertura_nao_traz_fontes(self):
        self.assertEqual(busca.buscar('capital da França'), [])
        self.assertEqual(busca.buscar('o que é isso?'), [])

    def test_sintaxe_fts_do_usuario_nao_quebra_a_busca(self):
        # Aspas, operadores e parênteses do usuário viram termos comuns, nunca sintaxe FTS5.
        self.assertEqual(busca.buscar('inflação" AND (preços* OR'), busca.buscar('inflação preços'))


class PdfIndexadoTests(IndiceTemporario):
    def test_pdf_e_indexado_por_pagina(self):
        pdf = Path(self.tmp.name) / 'relatorio.pdf'
        doc = fitz.open()
        for texto in ('Capa do relatório anual.', 'A receita líquida cresceu oito por cento no período.'):
            doc.new_page().insert_text((72, 72), texto)
        doc.save(str(pdf)); doc.close()
        conector = conectores.ConectorPDF([{'id': 'doc1', 'nome': 'relatorio.pdf', 'caminho': pdf}],
                                          dono='u1')
        self.assertEqual(indice.sincronizar(conector)['indexadas'], 1)
        resultado = busca.buscar('receita líquida', 'documentos', 'u1')
        self.assertEqual((resultado[0].fonte, resultado[0].trecho), ('relatorio.pdf', 'página 1'))
        self.assertEqual(busca.buscar('receita líquida', 'documentos', 'outro-usuario'), [])
        self.assertEqual(indice.sincronizar(conector)['inalteradas'], 1)


class RoteadorTests(unittest.TestCase):
    def test_identidade(self):
        for pergunta in ('Quem és tu?', 'quem é você?', 'me fale quem é você.', 'eu digo você, harpia.',
                         'quem te criou?', 'qual é o seu nome?', 'o que é a HarpIA?', 'se apresente',
                         'como te chamas?'):
            self.assertEqual(roteador.classificar(pergunta), Intencao.IDENTIDADE, pergunta)

    def test_casual(self):
        for pergunta in ('Olá', 'oi!', 'bom dia', 'olá, tudo bem com você?', 'e aí, beleza?',
                         'obrigado!', 'valeu pela ajuda', 'kkkk', 'tchau, até amanhã', 'ok, entendi'):
            self.assertEqual(roteador.classificar(pergunta), Intencao.CASUAL, pergunta)

    def test_atualidade(self):
        for pergunta in ('Quais são as notícias de hoje?', 'qual a cotação do dólar?',
                         'quem ganhou o jogo ontem?', 'previsão do tempo para amanhã'):
            self.assertEqual(roteador.classificar(pergunta), Intencao.ATUALIDADE, pergunta)

    def test_conhecimento(self):
        for pergunta in ('O que é inflação?', 'oi, o que é inflação?', 'você sabe quem foi Getúlio Vargas?',
                         'como surgiu o império romano', 'resuma o documento', 'você pode me ajudar com SQL?',
                         'qual o cenário atual da economia'):
            self.assertEqual(roteador.classificar(pergunta), Intencao.CONHECIMENTO, pergunta)

    def test_identidade_e_casual_nao_consultam_o_indice(self):
        self.assertFalse(roteador.consulta_ao_indice(Intencao.IDENTIDADE))
        self.assertFalse(roteador.consulta_ao_indice(Intencao.CASUAL))
        self.assertTrue(roteador.consulta_ao_indice(Intencao.CONHECIMENTO))
        self.assertTrue(roteador.consulta_ao_indice(Intencao.ATUALIDADE))


if __name__ == '__main__':
    unittest.main()
