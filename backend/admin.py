"""Local administration only; never exposed through the HTTP API."""
import argparse
import secrets
from auth import criar_usuario, token_hash
from storage import get_conn, init_db

def main():
    parser = argparse.ArgumentParser(description='Administração local da HarpIA')
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('criar-usuario'); create.add_argument('nome')
    rotate = sub.add_parser('renovar-token'); rotate.add_argument('nome')
    revoke = sub.add_parser('revogar-tokens'); revoke.add_argument('nome')
    claim = sub.add_parser('atribuir-legado'); claim.add_argument('nome')
    claim.add_argument('--confirmar', action='store_true', required=True)
    args = parser.parse_args()
    init_db()
    if args.command == 'criar-usuario':
        uid, token = criar_usuario(args.nome)
        print('Usuário:', args.nome, '\nID:', uid, '\nToken (guarde em local seguro):', token)
        return
    with get_conn() as conn:
        user = conn.execute('SELECT id FROM usuarios WHERE nome=?', (args.nome,)).fetchone()
        if not user:
            parser.error('Usuário não encontrado.')
        if args.command == 'atribuir-legado':
            count = conn.execute('UPDATE conversas SET owner_id=? WHERE owner_id IS NULL', (user['id'],)).rowcount
            print('Conversas antigas atribuídas:', count)
        else:
            conn.execute('UPDATE credenciais SET revogada=1 WHERE usuario_id=?', (user['id'],))
            if args.command == 'renovar-token':
                token = secrets.token_urlsafe(32)
                conn.execute('INSERT INTO credenciais(token_hash,usuario_id) VALUES (?,?)', (token_hash(token),user['id']))
                print('Novo token (o anterior foi revogado):', token)
            else:
                print('Credenciais revogadas.')

if __name__ == '__main__':
    main()


