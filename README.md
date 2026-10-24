<div align="center">
  <img src="assets/harpia_logo.png" alt="Logo da HarpIA" width="420">
  <h1>HarpIA</h1>
  <p><strong>Uma assistente local em português para aprender e explorar ideias.</strong></p>
</div>

## Sobre

A HarpIA é uma assistente de inteligência artificial executada localmente com [Ollama](https://ollama.com/) e o modelo [Mistral](https://mistral.ai/). Ela combina conversa, busca em uma base de conhecimento e, opcionalmente, pesquisa na web para perguntas sobre informações atuais.

## Recursos

- Conversa pelo terminal, sem interface web.
- Busca em documentos locais para complementar as respostas.
- Conversas privadas por usuário e API protegida por credencial.
- Pesquisa opcional na web com links das fontes consultadas.
- Análise local de bases econômicas CSV e Excel com Pandas e NumPy.
- Leitura controlada de PDF, DOCX e HTML com Docling quando a dependência opcional está instalada.

## Começar no Windows

Pré-requisitos: Python 3.11 ou posterior e [Ollama](https://ollama.com/download) instalados.

1. Abra `Iniciar-HarpIA.bat` com dois cliques.
2. Na primeira execução, aguarde a preparação do ambiente e do modelo; o download inicial pode demorar.
3. Faça uma pergunta no chat que será aberto no terminal.

Para analisar dados de um estabelecimento, envie uma planilha CSV ou XLSX pelo comando `/importar "C:\\caminho\\da\\base.xlsx"`. Depois, pergunte sobre faturamento, custos, despesas, lucro ou tendências. Use `/bases` para ver os arquivos recebidos.

As bases ficam no computador, separadas por usuário, e não são incluídas no Git. A HarpIA envia somente resumos numéricos ao modelo local para explicar os resultados.

Para ativar a pesquisa na web, copie `backend/.env.example` para `backend/.env` e configure sua chave Tavily. Mantenha essa chave apenas no arquivo `.env`, que não é enviado ao Git.

## Documentação

- [Guia de uso e primeiros passos](LEIA-ME.md)
- [Configuração, API e administração](backend/README.md)
