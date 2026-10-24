import time
import uuid
import sqlite3
import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import httpx
import auth
import context
import knowledge
import main
import storage
import web_search

class HarpiaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.old_db=storage.DB_PATH;storage.DB_PATH=Path(self.tmp.name)/'test.db';storage.init_db()
        self.a,self.ta=auth.criar_usuario('Alice');self.b,self.tb=auth.criar_usuario('Bob')
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test')
        self.model=patch('main.gerar',return_value='Resposta de teste.').start()
    async def asyncTearDown(self):
        patch.stopall();await self.client.aclose();storage.DB_PATH=self.old_db;self.tmp.cleanup()
    def headers(self,t):return {'Authorization':'Bearer '+t}
    async def ask(self,t,**body):return await self.client.post('/chat',headers=self.headers(t),json={'mensagem':'O que é inflação?',**body})
    async def detail(self,t,cid):return await self.client.get('/conversas/'+cid,headers=self.headers(t))
    async def test_unauthenticated_all_routes(self):
        for path in ['/','/conversas','/conversas/missing']:
            for headers in [{},self.headers('invalid')]:
                self.assertEqual((await self.client.get(path,headers=headers)).status_code,401)
        self.assertEqual((await self.client.post('/chat',json={'mensagem':'oi'})).status_code,401)
        self.assertEqual((await self.ask('invalid')).status_code,401)
    async def test_ownership_and_legitimate_continuation(self):
        r=await self.ask(self.ta);self.assertEqual(r.status_code,200,r.text);cid=r.json()['conversa_id']
        self.assertEqual(len((await self.client.get('/conversas',headers=self.headers(self.ta))).json()),1)
        self.assertEqual((await self.client.get('/conversas',headers=self.headers(self.tb))).json(),[])
        for foreign in [cid,'missing',cid.upper()]:
            self.assertEqual((await self.detail(self.tb,foreign)).status_code,404)
            self.assertEqual((await self.ask(self.tb,conversa_id=foreign)).status_code,404)
        self.assertEqual(self.model.call_count,1)
        self.assertEqual((await self.ask(self.ta,conversa_id=cid,mensagem='E quais são suas causas?')).status_code,200)
        self.assertEqual(len((await self.detail(self.ta,cid)).json()['mensagens']),4)
    async def test_owner_forgery(self):
        self.assertEqual((await self.ask(self.tb,owner_id=self.a)).status_code,422)
    async def test_revocation_rotation(self):
        cid=(await self.ask(self.ta)).json()['conversa_id']
        with storage.get_conn() as c:
            c.execute('UPDATE credenciais SET revogada=1 WHERE usuario_id=?',(self.a,))
            c.execute('INSERT INTO credenciais(token_hash,usuario_id) VALUES (?,?)',(auth.token_hash('replacement'),self.a))
        self.assertEqual((await self.detail(self.ta,cid)).status_code,401)
        self.assertEqual((await self.detail('replacement',cid)).status_code,200)
    async def test_retry_idempotency(self):
        rid=str(uuid.uuid4());self.model.side_effect=RuntimeError('private detail')
        r=await self.ask(self.ta,requisicao_id=rid);self.assertEqual(r.status_code,502);self.assertNotIn('private detail',r.text)
        cid=r.json()['detail']['conversa_id'];data=(await self.detail(self.ta,cid)).json()
        self.assertEqual(data['mensagens'],[]);self.assertEqual(data['tentativas'][0]['estado'],'falhou')
        self.assertEqual(data['tentativas'][0]['pergunta'],'O que é inflação?')
        self.assertEqual(data['tentativas'][0]['id'],rid)
        self.model.side_effect=None
        r=await self.ask(self.ta,requisicao_id=rid);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual((await self.ask(self.ta,requisicao_id=rid)).json(),r.json())
        self.assertEqual(self.model.call_count,2)
        self.assertEqual((await self.ask(self.ta,requisicao_id=rid,mensagem='outra')).status_code,409)
        self.assertEqual(len((await self.detail(self.ta,cid)).json()['mensagens']),2)
    async def test_search_failure_timeout_empty(self):
        with patch('main.pesquisar',side_effect=OSError('read failure')):
            self.assertEqual((await self.ask(self.ta)).status_code,502)
        self.model.side_effect=httpx.ReadTimeout('timeout');self.assertEqual((await self.ask(self.ta)).status_code,504)
        self.model.side_effect=None;self.model.return_value=' ';self.assertEqual((await self.ask(self.ta)).status_code,502)
        with storage.get_conn() as c:self.assertEqual(c.execute("SELECT count(*) FROM tentativas WHERE estado='pendente'").fetchone()[0],0)
    async def test_input_limit_before_write(self):
        for msg in ['  ','x'*20000,'á'*5000]:self.assertEqual((await self.ask(self.ta,mensagem=msg)).status_code,422)
        with storage.get_conn() as c:self.assertEqual(c.execute('SELECT count(*) FROM conversas').fetchone()[0],0)
    async def test_long_prompt_rejected_without_truncation_or_database_write(self):
        response=await self.ask(self.ta,mensagem='pergunta '*400)
        self.assertEqual(response.status_code,422,response.text)
        self.assertIn('Divida a pergunta',response.text)
        with storage.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM conversas').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM mensagens').fetchone()[0],0)
    async def test_context_trims_prompt_but_preserves_full_sqlite_history(self):
        first=await self.ask(self.ta)
        cid=first.json()['conversa_id']
        stamp=main.agora()
        with storage.get_conn() as conn:
            for index in range(35):
                conn.executemany('INSERT INTO mensagens(conversa_id,papel,conteudo,criada_em) VALUES (?,?,?,?)',[
                    (cid,'user',f'Pergunta histórica {index}: '+('contexto antigo '*20),stamp),
                    (cid,'assistant',f'Resposta histórica {index}: '+('detalhe anterior '*20),stamp),
                ])
        self.model.reset_mock()
        response=await self.ask(self.ta,conversa_id=cid)
        self.assertEqual(response.status_code,200,response.text)
        messages=self.model.call_args.args[0]
        self.assertEqual(sum(m['role']=='user' and m['content']=='O que é inflação?' for m in messages),1)
        self.assertLessEqual(context.custo(messages)+context.RESPONSE_TOKENS+context.SAFETY_MARGIN,
                             context.CONTEXT_MAX_TOKENS)
        detail=(await self.detail(self.ta,cid)).json()
        self.assertEqual(len(detail['mensagens']),2+70+2)
        self.assertIn('Pergunta histórica 0',detail['mensagens'][2]['conteudo'])
    async def test_legacy_migration(self):
        storage.DB_PATH=Path(self.tmp.name)/'legacy.db'
        with sqlite3.connect(storage.DB_PATH) as c:
            c.executescript("CREATE TABLE conversas(id TEXT PRIMARY KEY,titulo TEXT,criada_em TEXT,atualizada_em TEXT); CREATE TABLE mensagens(id INTEGER PRIMARY KEY,conversa_id TEXT,papel TEXT,conteudo TEXT,criada_em TEXT); INSERT INTO conversas VALUES ('legacy','segredo','a','a'); INSERT INTO mensagens VALUES (1,'legacy','user','privado','a');")
        c.close();storage.init_db();storage.init_db();uid,t=auth.criar_usuario('New')
        self.assertEqual((await self.client.get('/conversas',headers=self.headers(t))).json(),[])
        self.assertEqual((await self.detail(t,'legacy')).status_code,404)
        self.assertEqual((await self.ask(t,conversa_id='legacy')).status_code,404)
        self.assertEqual(len(list(Path(self.tmp.name).glob('legacy.db.pre_auth_*.bak'))),1)
        with storage.get_conn() as c:
            self.assertEqual(c.execute('SELECT conteudo FROM mensagens').fetchone()[0],'privado')
            self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0],1)
            c.execute('UPDATE conversas SET owner_id=? WHERE id=?',(uid,'legacy'))
        self.assertEqual((await self.detail(t,'legacy')).status_code,200)
    async def test_pending_blocks_second_generation(self):
        cid=(await self.ask(self.ta)).json()['conversa_id']
        with storage.get_conn() as c:
            c.execute('INSERT INTO tentativas(id,usuario_id,conversa_id,pergunta,estado,expira_em,criada_em) VALUES (?,?,?,?,?,?,?)',
                      (str(uuid.uuid4()),self.a,cid,'wait','pendente',time.time()+60,main.agora()))
        self.assertEqual((await self.ask(self.ta,conversa_id=cid)).status_code,409);self.assertEqual(self.model.call_count,1)
    async def test_late_result_cannot_append(self):
        def expire(messages):
            with storage.get_conn() as c:c.execute("UPDATE tentativas SET estado='falhou' WHERE estado='pendente'")
            return 'late'
        self.model.side_effect=expire
        self.assertEqual((await self.ask(self.ta)).status_code,409)
        with storage.get_conn() as c:self.assertEqual(c.execute('SELECT count(*) FROM mensagens').fetchone()[0],0)

    async def test_real_concurrent_generation_is_serialized(self):
        cid=(await self.ask(self.ta)).json()['conversa_id']
        started=threading.Event(); release=threading.Event()
        def slow(messages):
            started.set()
            if not release.wait(5): raise RuntimeError('Test synchronization timed out')
            return 'Resposta simultânea'
        self.model.side_effect=slow
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(main.conversar,main.EntradaChat(mensagem='O que é inflação?',conversa_id=cid),self.a)
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual((await self.ask(self.ta,conversa_id=cid)).status_code,409)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5)['estado'],'concluida')
        self.assertEqual(len((await self.detail(self.ta,cid)).json()['mensagens']),4)

    async def test_request_ids_are_scoped_to_user(self):
        rid=str(uuid.uuid4())
        first=await self.ask(self.ta,requisicao_id=rid)
        second=await self.ask(self.tb,requisicao_id=rid)
        self.assertEqual(first.status_code,200);self.assertEqual(second.status_code,200)
        self.assertNotEqual(first.json()['conversa_id'],second.json()['conversa_id'])
        self.assertEqual((await self.ask(self.tb,requisicao_id=rid,conversa_id=first.json()['conversa_id'])).status_code,404)

class RetrievalTests(unittest.TestCase):
    def test_chained_followups_keep_topic(self):
        rows=[{'papel':'user','conteudo':'O que é inflação?'},
              {'papel':'assistant','conteudo':'Aumento dos preços.'},
              {'papel':'user','conteudo':'E quais são suas causas?'}]
        previous=knowledge.assunto_anterior(rows)
        self.assertEqual(previous,'O que é inflação?')
        result=knowledge.pesquisar('E quais são seus efeitos?',anterior=previous)
        self.assertTrue(result)
        self.assertTrue(result[0]['fonte'].endswith('/inflacao.txt'))
    def test_relevance_abstention_followup(self):
        for q in ['O que é inflação?','Por que as coisas ficam mais caras?']:
            self.assertTrue(knowledge.pesquisar(q)[0]['fonte'].endswith('/inflacao.txt'),q)
        for q in ['Qual é a capital da França?','Quem ganhou a eleição de 2026?','Como funciona a fotossíntese?']:
            self.assertEqual(knowledge.pesquisar(q),[],q)
        result=knowledge.pesquisar('E quais são suas causas?',anterior='O que é inflação?')
        self.assertTrue(result);self.assertIn('infla',result[0]['fonte'])
        self.assertEqual(len({x['fonte'] for x in result}),len(result))
    def test_cache_updates(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as td:
            with patch.object(knowledge,'KNOWLEDGE_DIR',Path(td)):
                p=Path(td)/'teste.txt';p.write_text('jabuticaba brasileira',encoding='utf-8');self.assertTrue(knowledge.pesquisar('jabuticaba'))
                p.write_text('manga brasileira tropical',encoding='utf-8');self.assertFalse(knowledge.pesquisar('jabuticaba'));self.assertTrue(knowledge.pesquisar('manga'))
                p.unlink();self.assertEqual(knowledge.pesquisar('manga'),[])
    def test_context_budget(self):
        rows=[{'papel':role,'conteudo':'Texto longo '*15} for _ in range(100) for role in ['user','assistant']]
        messages,sources=context.montar_contexto('E suas causas?',rows,knowledge.pesquisar('O que é inflação?'))
        self.assertLessEqual(context.custo(messages)+context.RESPONSE_TOKENS+context.SAFETY_MARGIN,context.CONTEXT_MAX_TOKENS)
        self.assertLess(len(messages),len(rows));self.assertEqual(messages[-1]['content'],'E suas causas?')
        self.assertNotIn('DOCUMENTOS',messages[0]['content'])
        for source in sources:self.assertIn(source['conteudo'],messages[-2]['content'])
        with self.assertRaises(ValueError):context.montar_contexto('😀'*4000,[],[])

    def test_short_prompt_and_current_question_are_preserved_once(self):
        history=[{'papel':'user','conteudo':'Pergunta repetida'},
                 {'papel':'assistant','conteudo':'Resposta antiga'}]
        messages,_,stats=context.montar_contexto('Pergunta repetida',history,[],incluir_diagnostico=True)
        self.assertEqual(messages[-1],{'role':'user','content':'Pergunta repetida'})
        self.assertEqual(sum(m['content']=='Pergunta repetida' for m in messages),1)
        self.assertLessEqual(stats['estimated_input_tokens'],context.INPUT_BUDGET)
        self.assertEqual(stats['history_messages_discarded'],2)

    def test_many_documents_are_capped_and_prompt_stays_within_budget(self):
        docs=[{'fonte':f'doc-{n}.txt','conteudo':'evidência relevante '+('palavra '*500),
               'pontuacao':100-n} for n in range(20)]
        messages,sources,stats=context.montar_contexto('Pergunta curta',[],docs,incluir_diagnostico=True)
        self.assertLessEqual(len(sources),context.MAX_DOCUMENTS)
        self.assertTrue(all(len(doc['conteudo'])<=context.MAX_DOCUMENT_CHARS for doc in sources))
        self.assertEqual(stats['documents_discarded'],20-len(sources))
        self.assertLessEqual(stats['estimated_input_tokens'],context.INPUT_BUDGET)
        self.assertLessEqual(context.custo(messages)+context.RESPONSE_TOKENS+context.SAFETY_MARGIN,
                             context.CONTEXT_MAX_TOKENS)

    def test_recent_history_keeps_order_and_discards_older_turns_first(self):
        rows=[]
        for index in range(60):
            rows.extend([{'papel':'user','conteudo':f'pergunta {index} '+('antigo '*20)},
                         {'papel':'assistant','conteudo':f'resposta {index} '+('resposta '*20)}])
        messages,_,stats=context.montar_contexto('pergunta nova',rows,[] ,incluir_diagnostico=True)
        contents=[m['content'] for m in messages]
        chosen_numbers=[int(content.split()[1]) for content in contents if content.startswith('pergunta ') and content!='pergunta nova']
        self.assertEqual(chosen_numbers,sorted(chosen_numbers))
        self.assertTrue(chosen_numbers)
        self.assertEqual(chosen_numbers[-1],59)
        self.assertGreater(stats['history_messages_discarded'],0)

class ContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db_patch=patch.object(storage,'DB_PATH',Path(self.tmp.name)/'context.db')
        self.db_patch.start();storage.init_db()
        self.uid,self.token=auth.criar_usuario('Contexto')
        self.cid=str(uuid.uuid4())
        with storage.get_conn() as conn:
            conn.execute('INSERT INTO conversas(id,titulo,criada_em,atualizada_em,owner_id) VALUES (?,?,?,?,?)',
                         (self.cid,'Teste','agora','agora',self.uid))

    def tearDown(self):
        self.db_patch.stop();self.tmp.cleanup()

    def test_chat_budget_omits_old_prompt_content_but_keeps_database_history(self):
        stamp=main.agora()
        with storage.get_conn() as conn:
            for index in range(35):
                conn.executemany('INSERT INTO mensagens(conversa_id,papel,conteudo,criada_em) VALUES (?,?,?,?)',[
                    (self.cid,'user',f'Pergunta histórica {index}: '+('contexto antigo '*20),stamp),
                    (self.cid,'assistant',f'Resposta histórica {index}: '+('detalhe anterior '*20),stamp),
                ])
        docs=[{'fonte':f'doc-{index}.txt','conteudo':'evidência relevante '+('texto '*300),
               'pontuacao':100-index} for index in range(8)]
        pergunta='Pergunta nova e específica?'
        request=main.EntradaChat(mensagem=pergunta,conversa_id=self.cid)
        with patch('main.pesquisar',return_value=docs), patch('main.gerar',return_value='Resposta simulada.') as generate:
            response=main.conversar(request,self.uid)
        messages=generate.call_args.args[0]
        self.assertEqual(response['estado'],'concluida')
        self.assertEqual(sum(m['role']=='user' and m['content']==pergunta for m in messages),1)
        self.assertLessEqual(context.custo(messages)+context.RESPONSE_TOKENS+context.SAFETY_MARGIN,
                             context.CONTEXT_MAX_TOKENS)
        with storage.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM mensagens WHERE conversa_id=?',(self.cid,)).fetchone()[0],72)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM mensagens WHERE conteudo LIKE 'Pergunta histórica 0:%'").fetchone())

    def test_oversized_question_is_rejected_before_persistence(self):
        request=main.EntradaChat(mensagem='pergunta '*400,conversa_id=self.cid)
        with self.assertRaises(main.HTTPException) as raised:
            main.conversar(request,self.uid)
        self.assertEqual(getattr(raised.exception,'status_code',None),422)
        with storage.get_conn() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM mensagens WHERE conversa_id=?',(self.cid,)).fetchone()[0],0)

    def test_ollama_gets_context_and_output_reservations(self):
        captured={}
        class FakeResult:
            class message:
                content='ok'
        class FakeClient:
            def __init__(self,**kwargs): self._client=self
            def chat(self,**kwargs): captured.update(kwargs); return FakeResult()
            def close(self): pass
        with patch('main.Client',FakeClient):
            self.assertEqual(main.gerar([{'role':'user','content':'oi'}]),'ok')
        self.assertEqual(captured['options']['num_ctx'],context.CONTEXT_MAX_TOKENS)
        self.assertEqual(captured['options']['num_predict'],context.RESPONSE_TOKENS)

class WebSearchFreshnessTests(unittest.TestCase):
    class TavilyResponse:
        status_code=200
        def __init__(self,results): self.results=results
        def raise_for_status(self): pass
        def json(self): return {'results':self.results}

    def results(self):
        from datetime import date, timedelta
        return [
            {'title':'Notícia atual','url':'https://example.com/current','content':'Informação recente.',
             'published_date':date.today().isoformat()},
            {'title':'Matéria antiga','url':'https://example.com/old','content':'Arquivo de 2024.',
             'published_date':'2024-05-10'},
            {'title':'Sem data','url':'https://example.com/undated','content':'Data indisponível.',
             'published_date':None},
            {'title':'Semana passada','url':'https://example.com/recent','content':'Ainda recente.',
             'published_date':(date.today()-timedelta(days=3)).isoformat()},
        ]

    def test_today_news_filters_old_and_undated_results(self):
        captured={}
        def request(url,**kwargs):
            captured.update(kwargs['json'])
            return self.TavilyResponse(self.results())
        with patch.dict('os.environ',{'TAVILY_API_KEY':'test-key'}), patch('web_search.httpx.post',side_effect=request):
            sources,error=web_search.pesquisar_web('Quais são as notícias de hoje no Brasil?')
        self.assertIsNone(error)
        self.assertEqual([source['titulo'] for source in sources],['Notícia atual'])
        self.assertEqual(captured['time_range'],'day')
        self.assertTrue(captured['filter_by_published_date'])

    def test_general_news_search_defaults_to_recent_week(self):
        captured={}
        def request(url,**kwargs):
            captured.update(kwargs['json'])
            return self.TavilyResponse(self.results())
        with patch.dict('os.environ',{'TAVILY_API_KEY':'test-key'}), patch('web_search.httpx.post',side_effect=request):
            sources,error=web_search.pesquisar_web('Notícias sobre o Brasil')
        self.assertIsNone(error)
        self.assertEqual(captured['time_range'],'week')
        self.assertTrue(captured['filter_by_published_date'])
        self.assertNotIn('Matéria antiga',[source['titulo'] for source in sources])
        self.assertNotIn('Sem data',[source['titulo'] for source in sources])
        self.assertIn('Semana passada',[source['titulo'] for source in sources])

    def test_unqualified_news_question_triggers_web_search(self):
        self.assertTrue(web_search.precisa_pesquisa('Quais são as notícias do Brasil?'))

    def test_explicit_historical_year_is_not_filtered_as_current_news(self):
        captured={}
        def request(url,**kwargs):
            captured.update(kwargs['json'])
            return self.TavilyResponse(self.results())
        with patch.dict('os.environ',{'TAVILY_API_KEY':'test-key'}), patch('web_search.httpx.post',side_effect=request):
            sources,error=web_search.pesquisar_web('Notícias do Brasil em 2024')
        self.assertIsNone(error)
        self.assertNotIn('time_range',captured)
        self.assertIn('Matéria antiga',[source['titulo'] for source in sources])

if __name__=='__main__':unittest.main()

