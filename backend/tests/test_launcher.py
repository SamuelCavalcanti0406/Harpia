import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import iniciar
import storage
import auth


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.local = patch.object(iniciar, 'LOCAL', Path(self.tmp.name) / 'local')
        self.database = patch.object(storage, 'DB_PATH', Path(self.tmp.name) / 'test.db')
        self.local.start(); self.database.start()

    def tearDown(self):
        self.local.stop(); self.database.stop(); self.tmp.cleanup()

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_encrypted_credential_reused_without_new_user(self):
        with patch('getpass.getuser', return_value='TesteLocal'):
            token = iniciar.credencial_local()
            path = iniciar.caminho_credencial()
            self.assertNotIn(token.encode(), path.read_bytes())
            self.assertEqual(iniciar.credencial_local(), token)
            self.assertEqual(iniciar.proteger(path.read_bytes(), decrypt=True).decode(), token)
        with storage.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM usuarios').fetchone()[0], 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_existing_user_requires_valid_token(self):
        storage.init_db()
        uid, token = auth.criar_usuario('Existente')
        with patch('getpass.getpass', return_value='invalid'):
            with self.assertRaises(RuntimeError): iniciar.credencial_local()
        with patch('getpass.getpass', return_value=token):
            self.assertEqual(iniciar.credencial_local(), token)
        with storage.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM usuarios').fetchone()[0], 1)

    def test_rejects_nonlocal_ollama(self):
        for url in ['http://0.0.0.0:11434', 'https://example.com', 'http://user:pw@localhost:11434', 'http://localhost:11434/?x=1']:
            with patch.dict(os.environ, {'OLLAMA_HOST': url}):
                with self.assertRaises(RuntimeError): iniciar.url_ollama()

    def test_port_collision_uses_another_port(self):
        first = iniciar.abrir_socket()
        try:
            second = iniciar.abrir_socket()
            try:
                self.assertNotEqual(first.getsockname()[1], second.getsockname()[1])
                self.assertEqual(second.getsockname()[0], '127.0.0.1')
            finally: second.close()
        finally: first.close()

    def test_http_failure_retry_preserves_id(self):
        requests = []
        def handler(request):
            import json
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(502, json={'detail': {'conversa_id': 'abc', 'mensagem': 'falhou'}})
            return httpx.Response(200, json={'conversa_id': 'abc', 'resposta': 'ok'})
        with httpx.Client(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
            with patch('builtins.input', side_effect=['Oi', '/repetir', '/sair']):
                iniciar.conversar_terminal(client)
        self.assertEqual(requests[0]['requisicao_id'], requests[1]['requisicao_id'])
        self.assertEqual(requests[1]['conversa_id'], 'abc')
        self.assertEqual(requests[0]['mensagem'], requests[1]['mensagem'])

    def test_actual_backend_start_authenticate_and_stop(self):
        storage.init_db()
        _, token = auth.criar_usuario('Smoke')
        def interact(client):
            self.assertEqual(client.get('/').status_code, 200)
            self.assertEqual(client.get('/conversas').json(), [])
            response = httpx.get(str(client.base_url).rstrip('/') + '/conversas', trust_env=False)
            self.assertEqual(response.status_code, 401)
        with patch.object(iniciar, 'preparar_ollama'), patch.object(iniciar, 'credencial_local', return_value=token), patch.object(iniciar, 'conversar_terminal', side_effect=interact):
            iniciar.executar()

    def test_import_command_supports_pdf(self):
        posted = []
        def handler(request):
            if request.url.path == '/dados' and request.method == 'POST':
                posted.append(request)
                return httpx.Response(200, json={'nome': 'dados.pdf', 'linhas': 10, 'colunas': ['a', 'b']})
            return httpx.Response(200, json=[])
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_pdf:
            temp_pdf.write(b'%PDF-fake')
            pdf_path = temp_pdf.name
        try:
            with httpx.Client(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
                with patch('builtins.input', side_effect=[f'/importar "{pdf_path}"', '/sair']):
                    iniciar.conversar_terminal(client)
            self.assertEqual(len(posted), 1)
        finally:
            Path(pdf_path).unlink(missing_ok=True)

    def test_documento_command_supports_pdf(self):
        doc_posted = []
        doc_imported = []
        def handler(request):
            if request.url.path == '/documentos' and request.method == 'POST':
                doc_posted.append(request)
                return httpx.Response(200, json={'id': 'doc-123', 'nome': 'relatorio.pdf'})
            if request.url.path == '/documentos/doc-123/importar' and request.method == 'POST':
                doc_imported.append(request)
                return httpx.Response(200, json={'id': 'doc-123', 'nome': 'relatorio.pdf', 'paginas': [1, 2], 'status': 'processado'})
            if request.url.path == '/documentos' and request.method == 'GET':
                return httpx.Response(200, json=[{'id': 'doc-123', 'nome': 'relatorio.pdf', 'paginas': [1, 2], 'status': 'processado'}])
            return httpx.Response(200, json=[])
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_pdf:
            temp_pdf.write(b'%PDF-fake')
            pdf_path = temp_pdf.name
        try:
            with httpx.Client(base_url='http://test', transport=httpx.MockTransport(handler)) as client:
                with patch('builtins.input', side_effect=[f'/documento "{pdf_path}"', '/documentos', '/sair']):
                    iniciar.conversar_terminal(client)
            self.assertEqual(len(doc_posted), 1)
            self.assertEqual(len(doc_imported), 1)
        finally:
            Path(pdf_path).unlink(missing_ok=True)


if __name__ == '__main__': unittest.main()
