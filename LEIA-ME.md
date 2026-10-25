# Abrir a HarpIA

Dê **dois cliques em `Iniciar-HarpIA.bat`**, nesta pasta.

O inicializador prepara o ambiente Python quando necessário, instala as dependências ausentes, inicia o Ollama, prepara o modelo HarpIA e abre o backend protegido e um chat no terminal. O primeiro preparo do modelo pode baixar arquivos grandes; mantenha a conexão com a internet até terminar. Python 3.11+ e Ollama precisam estar instalados; se algum estiver ausente, o script informa o que falta.

Não há interface gráfica/web no projeto atual. A janela aberta é o chat: escreva uma pergunta e pressione Enter.

A HarpIA funciona offline. Ela responde com a base de conhecimento em `backend/knowledge_base/` e com os PDFs que você importar, buscando em um índice local (`.local/conhecimento.db`). Quando não encontra fontes, ela avisa.

Experimente no chat:

- `Olá, tudo bem?` — conversa casual, sem busca.
- `Quem é você?` — a HarpIA se apresenta pela própria ficha, sem busca.
- `O que é inflação?` — busca na base local e lista as fontes no fim da resposta.
- `Quais são as notícias de hoje?` — sem internet, ela avisa que não pode confirmar fatos recentes.

Para analisar os dados de um estabelecimento, envie uma planilha CSV, Excel ou arquivo PDF com dados tabulares no chat com `/importar "C:\\caminho\\da\\base.xlsx"` (ou `"base.pdf"`). Depois pergunte sobre faturamento, custos, despesas, lucro ou tendências. Use `/bases` para ver os arquivos enviados. As bases ficam localmente em `.local` e são separadas por usuário.

As fontes usadas aparecem em `Fontes consultadas`, no fim da resposta, com o arquivo e o trecho ou página. A documentação Swagger em `/docs` permanece desativada; a API exige credencial, e o chat local já usa essa autenticação com segurança.

- `/nova`: começa outra conversa.
- `/conversas`: mostra as conversas do seu usuário.
- `/abrir ID`: retoma uma conversa da lista.
- `/repetir`: repete a tentativa anterior sem duplicá-la.
- `/sair`: encerra o chat e o backend desta sessão. O Ollama pode permanecer aberto para outros programas.

No primeiro uso, se não existir nenhum usuário na HarpIA, cria um usuário local com o nome do seu login do Windows. Sua credencial é guardada criptografada pelo Windows na pasta `.local`, vinculada ao seu login e ao banco utilizado. Nas próximas aberturas não é preciso copiá-la. Se já houver usuários e não houver uma credencial salva válida, solicita uma credencial existente; não toma posse de uma conta pelo nome nem renova tokens automaticamente.

As conversas antigas sem proprietário continuam privadas. Para atribuí-las, siga o procedimento explícito em `backend/README.md`. Nunca as atribua em lote se pertencerem a pessoas diferentes.

A API escuta apenas no computador local. Se a porta 8000 estiver ocupada, usa outra porta livre e mostra o endereço na tela. Não encerra serviços já existentes. Fechar com `/sair` ou Ctrl+C encerra o backend da sessão.

Para verificar a instalação sem iniciar serviços, criar credenciais ou alterar o banco:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Iniciar-HarpIA.ps1 -Verificar
```

A opção `ExecutionPolicy Bypass` vale somente para esse processo; não altera a política permanente do Windows. Não são necessários privilégios de administrador.

Em caso de falha ao iniciar o Ollama, consulte `.local/ollama.log`. Não compartilhe a pasta `.local` nem os bancos/backups. Para mais detalhes da API, configuração e administração, consulte `backend/README.md`.
