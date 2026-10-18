"""One-window local launcher and authenticated terminal client for Windows."""
import argparse
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlparse

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / 'backend'
LOCAL = ROOT / '.local'


def proteger(data: bytes, decrypt=False) -> bytes:
    """Windows DPAPI, CurrentUser scope. Never write a plaintext token."""
    if os.name != 'nt':
        raise RuntimeError('Este inicializador foi preparado para Windows.')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; omit LOCAL_MACHINE so another login cannot decrypt.
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def caminho_credencial():
    from storage import DB_PATH
    identity = str(DB_PATH.resolve()) + '\0' + getpass.getuser()
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return LOCAL / ('credencial-' + digest + '.bin')


def credencial_local():
    from auth import criar_usuario, token_hash
    from storage import get_conn, init_db
    init_db()
    path = caminho_credencial()

    def valida(token):
        with get_conn() as conn:
            return conn.execute('SELECT 1 FROM credenciais WHERE token_hash=? AND revogada=0',
                                (token_hash(token),)).fetchone() is not None

    if path.exists():
        try:
            token = proteger(path.read_bytes(), decrypt=True).decode('utf-8')
            if valida(token):
                return token
            print('A credencial salva foi revogada. Informe uma credencial atual.')
        except (OSError, UnicodeError):
            print('Nao foi possivel abrir a credencial salva neste usuario do Windows.')
    with get_conn() as conn:
        users = conn.execute('SELECT count(*) FROM usuarios').fetchone()[0]
    if not users:
        _, token = criar_usuario(getpass.getuser())
        print('Primeiro acesso preparado para:', getpass.getuser())
        print('Conversas antigas continuam privadas ate atribuicao explicita pelo administrador.')
    else:
        token = getpass.getpass('Cole sua credencial da HarpIA (nao aparecera na tela): ').strip()
        if not valida(token):
            raise RuntimeError('Credencial invalida. Use admin.py para criar ou renovar sua credencial.')
    encrypted = proteger(token.encode('utf-8'))
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_bytes(encrypted)
    temporary.replace(path)
    return token


def localizar_ollama():
    found = shutil.which('ollama')
    if found:
        return found
    fallback = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'Ollama' / 'ollama.exe'
    if fallback.is_file():
        return str(fallback)
    raise RuntimeError('Ollama nao encontrado. Instale o Ollama e abra este inicializador novamente.')


def url_ollama():
    raw = os.environ.get('OLLAMA_HOST', 'http://127.0.0.1:11434').rstrip('/')
    parsed = urlparse(raw)
    # This launcher is deliberately local; don't accidentally start a public service.
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise RuntimeError('Para este inicializador, OLLAMA_HOST deve ser uma URL HTTP local, como http://127.0.0.1:11434.')
    return raw


def modelos(client, url):
    response = client.get(url + '/api/tags')
    response.raise_for_status()
    return {m['name'] for m in response.json()['models']}


def preparar_ollama():
    executable = localizar_ollama()
    url = url_ollama()
    env = os.environ.copy()
    env['OLLAMA_HOST'] = url
    LOCAL.mkdir(exist_ok=True)
    timeout = httpx.Timeout(connect=3, read=10, write=10, pool=3)
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        try:
            names = modelos(client, url)
        except httpx.ConnectError:
            print('Iniciando o Ollama...')
            with (LOCAL / 'ollama.log').open('ab') as log:
                process = subprocess.Popen([executable, 'serve'], env=env, stdout=log,
                                           stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 45
            while True:
                try:
                    names = modelos(client, url)
                    break
                except (httpx.ConnectError, httpx.TimeoutException):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Ollama nao iniciou. Consulte .local/ollama.log.')
                    time.sleep(0.5)
        except httpx.TimeoutException:
            # Ollama can be listening while it is still loading its model/runtime.
            # Give an already-running instance time to answer instead of spawning
            # a second server or failing on the first slow /api/tags response.
            print('O Ollama esta demorando para responder; aguardando...')
            deadline = time.monotonic() + 45
            while True:
                try:
                    names = modelos(client, url)
                    break
                except (httpx.ConnectError, httpx.TimeoutException):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('O Ollama nao respondeu em 45 segundos. Verifique se o Ollama esta aberto e tente novamente.')
                    time.sleep(0.5)
        model = os.getenv('HARPIA_MODEL', 'harpia')
        exists = model in names or model + ':latest' in names
        signature = hashlib.sha256((BACKEND / 'ModelFile').read_bytes()).hexdigest()
        stamp = LOCAL / ('modelo-' + hashlib.sha256((url + model).encode()).hexdigest()[:16] + '.sha256')
        changed = model == 'harpia' and (not stamp.exists() or stamp.read_text() != signature)
        if not exists or changed:
            if model != 'harpia':
                raise RuntimeError('O modelo personalizado configurado nao esta instalado. Instale-o no Ollama antes de iniciar.')
            print('Preparando o modelo HarpIA. Se a base estiver ausente, o download pode demorar.')
            subprocess.run([executable, 'create', model, '-f', str(BACKEND / 'ModelFile')],
                           cwd=BACKEND, env=env, check=True)
            if not (model in modelos(client, url) or model + ':latest' in modelos(client, url)):
                raise RuntimeError('Ollama nao confirmou a criacao do modelo.')
            stamp.write_text(signature)
        print('Ollama e modelo prontos.')


def abrir_socket():
    """Bind before launch to avoid port races; let Windows choose another if busy."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        listener.bind(('127.0.0.1', 8000))
    except OSError:
        listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    return listener


def conversar_terminal(client):
    cid = None
    pending = None
    print('\nHarpIA pronta. Digite sua pergunta e pressione Enter.')
    print('Comandos: /nova, /conversas, /abrir ID, /repetir, /sair\n')
    while True:
        try:
            question = input('Voce: ').strip()
        except EOFError:
            return
        if not question:
            continue
        if question == '/sair':
            return
        if question == '/nova':
            cid, pending = None, None
            print('Nova conversa.'); continue
        try:
            if question == '/conversas':
                response = client.get('/conversas')
                response.raise_for_status()
                for row in response.json():
                    print(row['id'], '-', row['titulo'])
                if not response.json(): print('Nenhuma conversa neste usuario.')
                continue
            if question.startswith('/abrir '):
                selected = question.split(maxsplit=1)[1]
                response = client.get('/conversas/' + selected)
                response.raise_for_status()
                data = response.json()
                cid, pending = data['conversa']['id'], None
                for row in data['mensagens']:
                    print(('Voce' if row['papel'] == 'user' else 'HarpIA') + ': ' + row['conteudo'])
                failed = [a for a in data['tentativas'] if a['estado'] == 'falhou']
                if failed:
                    last = failed[-1]
                    pending = {'mensagem': last['pergunta'], 'requisicao_id': last['id'], 'conversa_id': cid}
                    print('Ha uma tentativa falha. Use /repetir para tentar novamente.')
                continue
            if question == '/repetir':
                if pending is None:
                    print('Nenhuma tentativa para repetir.'); continue
            elif question.startswith('/'):
                print('Comando desconhecido. Use /nova, /conversas, /abrir ID, /repetir ou /sair.'); continue
            else:
                pending = {'mensagem': question, 'requisicao_id': str(uuid.uuid4())}
                if cid: pending['conversa_id'] = cid
            print('HarpIA esta preparando a resposta...')
            response = client.post('/chat', json=pending)
            if not response.is_success:
                detail = response.json().get('detail', 'Falha na requisicao.')
                if isinstance(detail, dict):
                    cid = detail.get('conversa_id', cid)
                    if cid: pending['conversa_id'] = cid
                    detail = detail.get('mensagem', str(detail))
                print('Aviso:', detail)
                print('Use /repetir para manter a mesma tentativa, ou envie outra pergunta.')
                continue
            data = response.json()
            cid, pending = data['conversa_id'], None
            print('\nHarpIA:', data['resposta'], '\n')
        except httpx.HTTPStatusError as exc:
            print('Nao foi possivel acessar esse recurso. Codigo:', exc.response.status_code)
        except httpx.RequestError:
            print('Falha de conexao. Use /repetir para evitar duplicacao da ultima pergunta.')


def executar():
    preparar_ollama()
    token = credencial_local()
    from main import app
    listener = abrir_socket()
    url = 'http://127.0.0.1:' + str(listener.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
    worker = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if not worker.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError('O backend nao iniciou. Confira as mensagens acima.')
            time.sleep(0.1)
        with httpx.Client(base_url=url, headers={'Authorization': 'Bearer ' + token},
                          timeout=150, trust_env=False) as client:
            response = client.get('/')
            response.raise_for_status()
            print('Backend protegido disponivel em', url)
            conversar_terminal(client)
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
        print('Sessao encerrada. Seu historico foi preservado.')


def main():
    parser = argparse.ArgumentParser(description='Inicia Ollama, modelo, API e chat local.')
    parser.add_argument('--verificar', action='store_true', help='Verifica arquivos sem iniciar servicos nem alterar o banco.')
    args = parser.parse_args()
    try:
        if args.verificar:
            localizar_ollama(); url_ollama()
            for name in ['ModelFile', 'main.py', 'requirements.txt']:
                if not (BACKEND / name).is_file(): raise RuntimeError('Arquivo ausente: ' + name)
            print('Python, dependencias, Ollama e arquivos encontrados. Pronto para iniciar.')
            return 0
        executar()
        return 0
    except KeyboardInterrupt:
        print('\nHarpIA encerrada.'); return 0
    except Exception as exc:
        print('Nao foi possivel iniciar:', str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
