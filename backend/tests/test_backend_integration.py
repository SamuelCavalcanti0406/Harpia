import _isolamento  # noqa: F401  (índice FTS5 temporário; precisa vir antes dos módulos da HarpIA)
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import auth
import main
import storage


class BackendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.old_db = storage.DB_PATH
        storage.DB_PATH = Path(self.temp.name) / 'integration.db'
        storage.init_db()
        self.user, _ = auth.criar_usuario('Integração')

    def tearDown(self):
        storage.DB_PATH = self.old_db
        self.temp.cleanup()

    def test_chat_uses_imported_document_context_and_keeps_history(self):
        document = {
            'tipo': 'documento',
            'fonte': 'relatorio.pdf',
            'trecho': 'página 2',
            'conteudo': 'A receita líquida cresceu no período.',
        }
        with patch.object(main, 'documentos_analise', return_value=[]), \
             patch.object(main, 'documentos_contexto', return_value=[document]), \
             patch.object(main, 'gerar', return_value='Resposta documentada.'):
            first = main.conversar(main.EntradaChat(mensagem='Qual foi o crescimento?'), self.user)
            second = main.conversar(main.EntradaChat(mensagem='E em qual página?', conversa_id=first['conversa_id']), self.user)
        self.assertEqual(first['estado'], 'concluida')
        self.assertEqual(second['conversa_id'], first['conversa_id'])
        with storage.get_conn() as conn:
            messages = conn.execute('SELECT papel FROM mensagens WHERE conversa_id=? ORDER BY id', (first['conversa_id'],)).fetchall()
        self.assertEqual([row['papel'] for row in messages], ['user', 'assistant', 'user', 'assistant'])

    def test_document_routes_are_registered_without_replacing_chat(self):
        paths = [str(route.path) for route in main.app.routes]
        self.assertIn('/chat', paths)
        self.assertIn('/documentos', paths)
        self.assertIn('/documentos/{documento_id}/importar', paths)
        self.assertIn('/documentos/{documento_id}/tabelas/{numero}/csv', paths)


if __name__ == '__main__':
    unittest.main()
