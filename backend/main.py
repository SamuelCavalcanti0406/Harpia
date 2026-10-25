from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
import uuid

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
import httpx
from ollama import Client, ResponseError
from pydantic import BaseModel, ConfigDict, Field
from dotenv import load_dotenv

from auth import usuario_atual
from context import montar_contexto, NUM_CTX, REFERENCIAS_MARCADOR, RESPONSE_TOKENS, INPUT_BUDGET
from document_processing import (
    DocumentProcessingError,
    documentos_contexto,
    exportar_tabela_csv,
    importar_documento,
    listar_documentos,
    registrar_upload,
)
from economic_analysis import EconomicDataError, documentos_analise, listar_bases, salvar_upload
from knowledge import pesquisar, assunto_anterior
from storage import get_conn, init_db
from web_search import pesquisar_web, precisa_pesquisa


load_dotenv(Path(__file__).resolve().parent / '.env')

logger=logging.getLogger('harpia')
MODEL_NAME=os.getenv('HARPIA_MODEL','harpia')
LEASE_SECONDS=180

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app=FastAPI(title='HarpIA',version='0.2.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
origins=[x.strip() for x in os.getenv('HARPIA_CORS_ORIGINS','').split(',') if x.strip()]
if '*' in origins:
    raise ValueError('Configure origens explícitas em HARPIA_CORS_ORIGINS, não *.')
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=['GET','POST'],allow_headers=['Authorization','Content-Type'])

class EntradaChat(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    mensagem: str=Field(min_length=1,max_length=12000)
    conversa_id: str | None=Field(default=None,min_length=1,max_length=64)
    requisicao_id: uuid.UUID=Field(default_factory=uuid.uuid4)

def agora():
    return datetime.now(timezone.utc).isoformat()

def conversa_do_usuario(conn,conversa_id,usuario):
    row=conn.execute('SELECT id,titulo,criada_em,atualizada_em FROM conversas WHERE id=? AND owner_id=?',
                     (conversa_id,usuario)).fetchone()
    if row is None: raise HTTPException(404,'Conversa não encontrada.')
    return row

def resultado(row):
    return {'conversa_id':row['conversa_id'],'requisicao_id':row['id'],
            'resposta':row['resposta'],'fontes':json.loads(row['fontes']),'estado':'concluida'}

@app.get('/')
def inicio(usuario: str=Depends(usuario_atual)):
    return {'mensagem':'HarpIA está funcionando!'}

@app.get('/conversas')
def listar_conversas(usuario: str=Depends(usuario_atual),limite: int=Query(50,ge=1,le=100),offset: int=Query(0,ge=0)):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            'SELECT id,titulo,criada_em,atualizada_em FROM conversas WHERE owner_id=? ORDER BY atualizada_em DESC LIMIT ? OFFSET ?',
            (usuario,limite,offset))]

@app.post('/dados')
def importar_dados(arquivo: UploadFile=File(...),usuario: str=Depends(usuario_atual)):
    try:
        return salvar_upload(usuario,arquivo.filename,arquivo.file)
    except EconomicDataError as exc:
        raise HTTPException(400,str(exc)) from exc
    finally:
        arquivo.file.close()

@app.get('/dados')
def listar_dados(usuario: str=Depends(usuario_atual)):
    return listar_bases(usuario)

@app.post('/documentos')
def receber_documento(arquivo: UploadFile=File(...),usuario: str=Depends(usuario_atual)):
    """Guarda o original; a importação para conhecimento é uma ação explícita."""
    try:
        return registrar_upload(usuario,arquivo.filename,arquivo.file)
    except DocumentProcessingError as exc:
        raise HTTPException(400,str(exc)) from exc
    finally:
        arquivo.file.close()

@app.get('/documentos')
def listar_documentos_api(usuario: str=Depends(usuario_atual)):
    return listar_documentos(usuario)

@app.post('/documentos/{documento_id}/importar')
def importar_documento_api(documento_id: str,usuario: str=Depends(usuario_atual)):
    try:
        return importar_documento(usuario,documento_id)
    except DocumentProcessingError as exc:
        raise HTTPException(400,str(exc)) from exc

@app.get('/documentos/{documento_id}/tabelas/{numero}/csv')
def exportar_tabela_api(documento_id: str,numero: int,usuario: str=Depends(usuario_atual)):
    try:
        content,filename=exportar_tabela_csv(usuario,documento_id,numero)
    except DocumentProcessingError as exc:
        raise HTTPException(404,str(exc)) from exc
    return Response(content=content,media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{filename}"'})

@app.get('/conversas/{conversa_id}')
def obter_conversa(conversa_id: str,usuario: str=Depends(usuario_atual)):
    with get_conn() as conn:
        row=conversa_do_usuario(conn,conversa_id,usuario)
        messages=[dict(r) for r in conn.execute('SELECT papel,conteudo,criada_em FROM mensagens WHERE conversa_id=? ORDER BY id',(conversa_id,))]
        attempts=[dict(r) for r in conn.execute('SELECT id,pergunta,estado,erro,criada_em,expira_em FROM tentativas WHERE conversa_id=? AND usuario_id=? ORDER BY criada_em',(conversa_id,usuario))]
        for attempt in attempts:
            if attempt['estado']=='pendente' and attempt['expira_em']<=time.time():
                attempt['estado']='falhou'; attempt['erro']='Tempo de processamento excedido; repita a requisição.'
            del attempt['expira_em']
    return {'conversa':dict(row),'mensagens':messages,'tentativas':attempts}

def gerar(messages):
    # Explicit connection/read/write timeouts. A DB lease also fences late results.
    client=Client(host=os.getenv('OLLAMA_HOST','http://127.0.0.1:11434'),timeout=httpx.Timeout(120.0,connect=5.0))
    try:
        return client.chat(model=MODEL_NAME,messages=messages,stream=False,
                           options={'num_ctx':NUM_CTX,'num_predict':RESPONSE_TOKENS}).message.content
    finally:
        client._client.close()

def registrar_contexto(requisicao_id, dados):
    logger.info(
        'Contexto estimado requisicao=%s sistema=%d pergunta=%d historico=%d documentos=%d '
        'entrada=%d/%d reserva_saida=%d margem=%d mensagens_historico_descartadas=%d '
        'documentos_incluidos=%d documentos_descartados=%d',
        requisicao_id, dados['system_tokens'], dados['current_message_tokens'],
        dados['history_tokens'], dados['documents_tokens'], dados['estimated_input_tokens'],
        INPUT_BUDGET, dados['response_reserve_tokens'], dados['safety_margin_tokens'],
        dados['history_messages_discarded'], dados['documents_included'],
        dados['documents_discarded'])

@app.post('/chat')
def conversar(entrada: EntradaChat,usuario: str=Depends(usuario_atual)):
    # Validate the question budget before persisting anything.
    try: montar_contexto(entrada.mensagem,[],[])
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    rid=str(entrada.requisicao_id)
    lease=time.time()+LEASE_SECONDS
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if entrada.conversa_id:
            conversa_do_usuario(conn,entrada.conversa_id,usuario)
        old=conn.execute('SELECT * FROM tentativas WHERE usuario_id=? AND id=?',(usuario,rid)).fetchone()
        if old:
            conversa_do_usuario(conn,old['conversa_id'],usuario)
            if old['pergunta']!=entrada.mensagem or (entrada.conversa_id and old['conversa_id']!=entrada.conversa_id):
                raise HTTPException(409,'requisicao_id já utilizado com outro conteúdo.')
            if old['estado']=='concluida': return resultado(old)
            cid=old['conversa_id']
        else:
            cid=entrada.conversa_id or str(uuid.uuid4())
            if not entrada.conversa_id:
                stamp=agora()
                conn.execute('INSERT INTO conversas(id,titulo,criada_em,atualizada_em,owner_id) VALUES (?,?,?,?,?)',
                             (cid,entrada.mensagem[:80],stamp,stamp,usuario))
        # Expired work cannot write later because completion checks its exact lease.
        conn.execute("UPDATE tentativas SET estado='falhou',erro='Processamento expirado.' WHERE conversa_id=? AND estado='pendente' AND expira_em<=?",(cid,time.time()))
        if conn.execute("SELECT 1 FROM tentativas WHERE conversa_id=? AND estado='pendente'",(cid,)).fetchone():
            raise HTTPException(409,{'mensagem':'Conversa já está sendo processada.','conversa_id':cid,'requisicao_id':rid})
        if old:
            conn.execute("UPDATE tentativas SET estado='pendente',erro=NULL,expira_em=? WHERE usuario_id=? AND id=?",(lease,usuario,rid))
        else:
            conn.execute('INSERT INTO tentativas(id,usuario_id,conversa_id,pergunta,estado,expira_em,criada_em) VALUES (?,?,?,?,?,?,?)',
                         (rid,usuario,cid,entrada.mensagem,'pendente',lease,agora()))
        history_total=conn.execute('SELECT count(*) FROM mensagens WHERE conversa_id=?',(cid,)).fetchone()[0]
        rows=list(reversed(conn.execute('SELECT papel,conteudo FROM mensagens WHERE conversa_id=? ORDER BY id DESC LIMIT 100',(cid,)).fetchall()))
    try:
        previous=assunto_anterior(rows)
        busca_atual=precisa_pesquisa(entrada.mensagem)
        web_docs=[]
        if busca_atual:
            query_web=entrada.mensagem
            if previous and any(t in entrada.mensagem.lower() for t in ('isso','isto','suas','seus','deles','delas','disso','nesse','nessa')):
                query_web=previous+' '+entrada.mensagem
            web_docs,web_error=pesquisar_web(query_web,limite=3)
            if not web_docs:
                text=('Não consegui verificar essa informação atual. '+web_error+
                      ' Configure ou tente novamente a pesquisa na internet antes de confiar em um valor ou acontecimento recente.')
                sources=[]
            else:
                # Perguntas atuais usam somente evidências recentes da web. Documentos
                # locais ficam de fora; do histórico entram só os últimos turnos.
                docs=web_docs
                messages,sources,usage=montar_contexto(entrada.mensagem,rows,docs,
                                                       pesquisa_atual=True,incluir_diagnostico=True,
                                                       historico_total=history_total)
                registrar_contexto(rid,usage)
                if not any(fonte.get('tipo')=='web' for fonte in sources):
                    text='Não consegui incluir fontes atuais no contexto da resposta. Tente novamente com uma pergunta mais curta.'
                    sources=[]
                else:
                    text=gerar(messages)
        else:
            docs=documentos_analise(usuario,entrada.mensagem)
            if not docs:
                docs=documentos_contexto(usuario,entrada.mensagem,limite=3)
            if not docs:
                docs=pesquisar(entrada.mensagem,limite=3,anterior=previous)
            messages,sources,usage=montar_contexto(entrada.mensagem,rows,docs,
                                                   incluir_diagnostico=True,
                                                   historico_total=history_total)
            registrar_contexto(rid,usage)
            text=gerar(messages)
        if not isinstance(text,str) or not text.strip(): raise ValueError('Resposta vazia')
        # Renderiza links apenas das fontes web que realmente couberam no contexto.
        usados=[fonte for fonte in sources if fonte.get('tipo')=='web']
        if usados:
            referencias='\n'.join('- ['+fonte['titulo']+']('+fonte['url']+')'+
                                   (f" (publicado em {fonte['data_publicacao']})" if fonte.get('data_publicacao') else '')
                                   for fonte in usados)
            text=text.rstrip()+REFERENCIAS_MARCADOR+referencias
    except Exception as exc:
        logger.warning('Geração falhou: tipo=%s requisicao=%s',type(exc).__name__,rid)
        timeout=isinstance(exc,httpx.TimeoutException)
        error='Tempo limite do modelo excedido.' if timeout else 'Não foi possível gerar a resposta. Repita com o mesmo requisicao_id.'
        with get_conn() as conn:
            conn.execute("UPDATE tentativas SET estado='falhou',erro=? WHERE usuario_id=? AND id=? AND estado='pendente' AND expira_em=?",(error,usuario,rid,lease))
        raise HTTPException(504 if timeout else 502,{'mensagem':error,'conversa_id':cid,'requisicao_id':rid,'estado':'falhou'}) from exc
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conversa_do_usuario(conn,cid,usuario)
        updated=conn.execute("UPDATE tentativas SET estado='concluida',resposta=?,fontes=? WHERE usuario_id=? AND id=? AND estado='pendente' AND expira_em=? AND expira_em>?",
                             (text,json.dumps(sources,ensure_ascii=False),usuario,rid,lease,time.time())).rowcount
        if updated!=1:
            raise HTTPException(409,{'mensagem':'Tentativa expirada. Consulte a conversa e repita.','conversa_id':cid,'requisicao_id':rid})
        stamp=agora()
        conn.executemany('INSERT INTO mensagens(conversa_id,papel,conteudo,criada_em) VALUES (?,?,?,?)',
                         [(cid,'user',entrada.mensagem,stamp),(cid,'assistant',text,stamp)])
        conn.execute('UPDATE conversas SET atualizada_em=? WHERE id=? AND owner_id=?',(stamp,cid,usuario))
    return {'conversa_id':cid,'requisicao_id':rid,'resposta':text,'fontes':sources,'estado':'concluida'}
