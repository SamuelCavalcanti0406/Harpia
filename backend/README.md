# HarpIA

Assistente local em português com Ollama, busca documental, análises econômicas locais e conversas privadas por usuário.

## Análise econômica de arquivos e planilhas locais

No chat local, use `/importar "C:\\caminho\\da\\base.xlsx"` (ou `"base.pdf"`) para enviar uma base CSV, XLSX ou PDF. CSV deve ter cabeçalhos na primeira linha; em Excel, a HarpIA lê a primeira aba; em PDF, a HarpIA extrai automaticamente a tabela com cabeçalhos e colunas numéricas. O limite é 25 MB, 200 mil linhas e 100 colunas. `/bases` lista os arquivos daquele usuário; a base enviada mais recentemente é usada nas análises.

Exemplos de perguntas:

- `Analise o faturamento, os custos e o lucro desta base.`
- `Como as vendas variaram por mês?`
- `Quais indicadores têm maior correlação?`

A análise usa Pandas e NumPy para calcular estatísticas descritivas, somas para colunas financeiras reconhecidas, variação mensal quando há uma coluna de data e correlações entre colunas numéricas. Os nomes de coluna são inferidos; use títulos claros, como `data`, `estabelecimento`, `receita`, `custo`, `despesa` e `lucro`. A HarpIA não presume moeda ou unidade. Correlação não demonstra causalidade.

Os arquivos são guardados localmente em `.local/economic_data/`, em uma pasta derivada da identidade autenticada de cada usuário. O `.local/` já é ignorado pelo Git. O arquivo integral não é enviado ao modelo: somente o resumo calculado é incluído no contexto do Ollama local.

## Documentos com PyMuPDF (fitz)

A HarpIA utiliza PyMuPDF (`fitz`) para leitura de PDFs com baixo consumo de memória RAM (gerenciador de contexto `with fitz.open()` iterando página por página e gerando chunks imediatos de 1000 caracteres com 100 de overlap).

Envie o original para `POST /documentos` com o campo multipart `arquivo`. Esse passo apenas guarda o original. Liste os documentos com `GET /documentos` e importe o escolhido com `POST /documentos/{id}/importar`. O conteúdo importado passa a ser recuperável pelo `/chat`; tabelas podem ser baixadas em `GET /documentos/{id}/tabelas/{numero}/csv`.

Os originais, o Markdown extraído, os CSVs de tabelas e os índices de pesquisa ficam separados em `.local/documents/`, por usuário. O sistema deduplica conteúdo idêntico e conserva versões anteriores quando o mesmo nome é atualizado. Macros, scripts e instruções presentes no conteúdo são tratados como dados e não são executados.

## Abertura automática no Windows

Dê dois cliques em `Iniciar-HarpIA.bat`, na pasta principal do projeto. Ele prepara o ambiente, inicia o Ollama e o backend e abre um chat autenticado no terminal. A credencial fica criptografada para seu login do Windows. Consulte `../LEIA-ME.md` para os comandos do chat, primeiro acesso e diagnóstico. O histórico antigo não é atribuído automaticamente.

## Instalação e primeiro acesso (PowerShell)

Execute a partir da pasta `backend`. Use Python 3.11 ou posterior (testado com 3.14).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama create harpia -f ModelFile
.\.venv\Scripts\python.exe admin.py criar-usuario Samuel
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

O comando de criação mostra uma credencial individual apenas uma vez. Guarde-a em local seguro; o banco armazena somente seu hash. Não existe cadastro HTTP público nem credencial padrão. Crie uma credencial diferente para cada pessoa. Quem controla os arquivos/comandos locais é administrador do serviço.

Todas as rotas exigem `Authorization: Bearer <token>`. Clientes antigos precisarão enviar esse cabeçalho. O servidor determina o proprietário a partir da credencial, nunca de campos enviados pelo cliente. Conversas inexistentes e conversas alheias retornam o mesmo 404. Documentação interativa da API fica desativada.

Exemplo em outra janela do PowerShell:

```powershell
$credencialHarpia = Read-Host 'Token individual' -MaskInput
$cabecalhosHarpia = @{ Authorization = "Bearer $credencialHarpia" }
$pedidoHarpia = [guid]::NewGuid().ToString()
$corpoHarpia = @{ mensagem = 'O que é inflação?'; requisicao_id = $pedidoHarpia } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/chat -Method Post -Headers $cabecalhosHarpia -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($corpoHarpia))
Invoke-RestMethod http://127.0.0.1:8000/conversas -Headers $cabecalhosHarpia
```

`Read-Host -MaskInput` exige PowerShell 7.1+. Nunca coloque tokens em URLs ou no repositório. Para acesso por rede, use HTTPS e configure as origens exatas da interface; mantenha o padrão localhost enquanto isso. A autenticação protege a API, não os arquivos do computador: restrinja o acesso ao banco e aos backups no sistema operacional.

## Conversas anteriores e migração

A inicialização da aplicação (ou um comando administrativo) migra o esquema sem apagar mensagens. Antes de adicionar proprietário a um banco antigo, cria uma cópia SQLite consistente `harpia.db.pre_auth_<id>.bak`. Reiniciar não cria novas cópias quando o esquema já foi atualizado.

Conversas antigas ficam sem proprietário e invisíveis pela API. **Somente se todas forem suas**, atribua-as explicitamente ao seu usuário:

```powershell
.\.venv\Scripts\python.exe admin.py atribuir-legado Samuel --confirmar
```

Esse comando atribui todas as conversas ainda sem proprietário; não transfere conversas que já têm dono. Se o banco antigo contiver conversas de pessoas diferentes, não execute essa atribuição em lote: mantenha-as isoladas até uma atribuição administrativa individual. Não há reivindicação automática por primeiro login.

Rotacionar ou revogar credenciais mantém a identidade e as conversas:

```powershell
.\.venv\Scripts\python.exe admin.py renovar-token Samuel
.\.venv\Scripts\python.exe admin.py revogar-tokens Samuel
```

## API e repetição segura

- `GET /`: exige credencial e confirma que a API respondeu; não verifica o Ollama.
- `GET /conversas?limite=50&offset=0`: somente conversas do usuário (limite máximo 100).
- `GET /conversas/{id}`: metadados, mensagens concluídas e tentativas com pergunta original, estado e erro.
- `POST /chat`: `mensagem`, `conversa_id` opcional e `requisicao_id` UUID opcional. Campos extras são recusados.

A resposta preserva `conversa_id` e `resposta`, adicionando `requisicao_id`, `estado` e `fontes` (origem, trecho, conteúdo e pontuação). Para continuar, envie `conversa_id` e um **novo** `requisicao_id`. Para repetir após falha ou perda da conexão, mantenha o mesmo UUID, mensagem e conversa. O cliente deve gerar o UUID antes da primeira chamada; se o servidor o gerar e a conexão cair, não haverá chave conhecida para deduplicação.

A mesma requisição concluída devolve o resultado salvo, sem gerar novamente. Reutilizar o UUID com conteúdo diferente retorna 409. Chaves são isoladas por usuário. Falhas de recuperação/modelo retornam 502; timeout retorna 504; o corpo `detail` identifica a tentativa. Histórico recebe pergunta e resposta juntas, somente no sucesso. Tentativas falhas permanecem registradas para consulta/repetição.

Somente uma geração pode ficar pendente por conversa. Outra recebe 409. Uma reserva (geração máxima + 60 s) impede bloqueio permanente após encerramento do processo; resultados atrasados não podem gravar sobre uma reserva substituída. Repetir uma tentativa antiga após novos turnos usa o histórico atual. A geração usa streaming: o limite de 180 s vale entre pedaços da resposta, e o total é limitado a 480 s (`HARPIA_GENERATION_SECONDS`). A reserva não cancela fisicamente a geração no servidor Ollama.

## Motor de busca local

A HarpIA não usa APIs externas de busca. A recuperação é feita por quatro módulos:

| Módulo | Papel |
|---|---|
| `roteador.py` | Classifica a pergunta por regras determinísticas: `identidade`, `casual`, `atualidade` ou `conhecimento`. Identidade e casual **não consultam o índice**. |
| `conectores.py` | Interface `Conector` (fontes + trechos sob demanda). `ConectorTexto` lê `.txt`/`.md`; `ConectorPDF` lê PDFs página por página com PyMuPDF. Ambos dividem o texto em janelas fixas de 1000 caracteres com sobreposição de 100 antes de gravar. |
| `indice.py` | Índice persistente SQLite FTS5 em `.local/conhecimento.db` (arquivo separado do banco de conversas). Guarda a origem de cada trecho (arquivo, ordem, página). Fontes com a mesma assinatura (mtime+tamanho; SHA-256 nos PDFs) não são reindexadas; fontes removidas saem do índice. |
| `busca.py` | Normaliza (minúsculas, sem acentos), remove stopwords, busca por prefixo do radical (`inflação`/`inflações`) e ranqueia **só pelo BM25** do FTS5. A cobertura mínima dos termos funciona como filtro, nunca é somada ao BM25. Deduplica trechos e limita a 1 por arquivo da base (2 por PDF). |

Ordem de recuperação no `/chat`: análise econômica das planilhas do usuário → PDFs importados (ativos) → base de conhecimento. As camadas não são misturadas por pontuação. Cada resposta recebe no máximo 3 trechos (até 5 com `HARPIA_MAX_TRECHOS`), e o orçamento de contexto ainda descarta o que não couber. Sem fontes, o modelo é instruído a dizer que não encontrou nada localmente; perguntas de atualidade sem fonte recebem um aviso de modo offline, sem chamar o modelo.

As fontes enviadas ao modelo são numeradas (`[Fonte 1 — arquivo, trecho]`). O prompt pede que ele cite os números, e o backend anexa `Fontes consultadas` com a mesma numeração. A resposta JSON traz `fontes` com `id`, `fonte`, `trecho`, `conteudo` e `relevancia`.

Memória: os trechos são gerados um a um e gravados em lotes de 200; nenhuma coleção é carregada inteira na RAM. Documentos importados por versões anteriores (catálogo JSON) são reindexados automaticamente na primeira consulta.

Isso é busca lexical: sinônimos e paráfrases sem palavras em comum ainda podem falhar.

## Contexto

O orçamento usa uma estimativa conservadora de tokens por bytes UTF-8, não o tokenizer exato do modelo; a margem reduz, mas não elimina, o risco de divergência. O padrão é 4.096 tokens de contexto, 512 reservados à resposta e 128 de margem. O sistema e a pergunta atual têm prioridade; turnos recentes completos (até 40% do espaço) e os trechos entram apenas se couberem. Trechos são limitados a 900 caracteres. Perguntas que não cabem recebem 422 sem criar conversa. Mensagens omitidas continuam no SQLite. Logs registram estimativas e quantidades, nunca o conteúdo das mensagens.

## Configuração

- A HarpIA funciona offline; nenhuma chave de API é necessária. `.env.example` lista as opções.
- `HARPIA_INDEX_PATH`: índice FTS5; padrão `.local/conhecimento.db`.
- `HARPIA_MAX_TRECHOS`: trechos por resposta, de 3 a 5; padrão 3.
- `HARPIA_DB_PATH`: caminho do SQLite; padrão ao lado de `storage.py`.
- `HARPIA_MODEL`: padrão `harpia`.
- `OLLAMA_HOST`: padrão `http://127.0.0.1:11434`. Defina apenas servidores confiáveis.
- `HARPIA_NUM_CTX`: de 2048 a 32768; padrão 2048; depende da capacidade do modelo/hardware.
- `HARPIA_RESPONSE_TOKENS`: reserva de saída; padrão 512.
- `HARPIA_CONTEXT_MARGIN`: margem para formatação e erro da estimativa; padrão 128.
- `HARPIA_CORS_ORIGINS`: origens exatas separadas por vírgula; vazio por padrão; `*` recusado.

Os textos das fontes são tratados como dados não confiáveis. A documentação interativa Swagger/OpenAPI permanece desativada nesta instalação; faça as chamadas com autenticação Bearer conforme o exemplo da seção API ou use um cliente HTTP compatível.

Não há limitação global de taxa por usuário nesta versão. Para implantação compartilhada, acrescente limitação de requisições no proxy e monitore capacidade. Não publique backups ou a pasta do projeto como arquivos estáticos.

## Verificação

```powershell
.\.venv\Scripts\python.exe -m compileall -q main.py auth.py admin.py storage.py context.py knowledge.py busca.py indice.py conectores.py roteador.py tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Testes usam bancos temporários e simulam o Ollama. Cobrem isolamento, revogação, migração, falhas, repetição, entrada, busca, limite de contexto, histórico persistente, prioridade de turnos recentes e corte de documentos. A estimativa não verifica o tokenizer real do modelo. Não execute os antigos scripts de auditoria em `tmp/pdfs` como suíte desta versão.
