# Relatório de implementação — Yberu Writer MCP MVP

**Data da implementação inicial:** 19 de outubro de 2026
**Estado:** integração MCP ↔ Writer gráfico testada em sessão isolada.  
**Código C++ do LibreOffice:** não alterado.

## Resumo

Foi implementado um servidor MCP stdio modular que se liga por PyUNO a uma instância Writer com listener UNO em localhost. O servidor mantém um único worker para as operações UNO, IDs de documento opacos por sessão, revisão local derivada de snapshots, âncoras temporárias e verificações de precondição antes das escritas. Callbacks de eventos apenas colocam notificações numa fila thread-safe; o processamento coalesce eventos e invalida snapshots sem executar UNO. Desde o Upgrade 2, snapshots válidos são reutilizados até uma notificação de modificação, e cada escrita força uma nova captura imediatamente antes da mutação.

O teste de integração iniciou uma instância GUI com perfil temporário privado, fez handshake com o protocolo MCP real, descobriu as ferramentas e leu/escreveu o Writer. Também verificou entrada de teclado X11, estilo, preservação de tabela, conflito, salvamento restrito e reconexão mantendo a mesma sessão MCP. O complemento de 2 de novembro de 2026 ampliou a cobertura para intervalos e células de tabelas.

## Ambiente confirmado

- `/usr/bin/soffice --version`: `LibreOffice 24.2.7.2 420(Build:2)`.
- Python do venv: `3.12.3`.
- MCP SDK instalado: `2.3.0`.
- PyUNO importável no venv e compatível com LibreOffice de sistema.
- API MCP inspecionada antes da implementação: `mcp.server.mcpserver.MCPServer`, `.tool`, `.run("stdio")`, `stdio_client`, `ClientSession`, `initialize`, `list_tools` e `call_tool`. O import legado `mcp.server.fastmcp` não está disponível nesta versão.
- A sessão gráfica ativa usa `DISPLAY=:0`; o teste encontrou a janela privada do Writer por PID/grupo de processos e direcionou o teclado só para essa janela.

## Arquitetura criada

```text
Cliente MCP (Codex ou outro cliente stdio)
        │ JSON-RPC via stdin/stdout
        ▼
MCPServer + registo das ferramentas MCP
        │ validação, permissões, idempotência
        ▼
WriterService ── DocumentRegistry / RevisionState
        │             EventQueue / listeners UNO
        ▼
ThreadPoolExecutor com 1 worker UNO
        │ PyUNO em 127.0.0.1:porta
        ▼
LibreOffice Writer gráfico
```

O stdout do processo MCP é reservado ao protocolo; os logs técnicos usam stderr. A saída do launcher Writer é redirecionada para `yberu-writer.log` no perfil dedicado. Encerrar o MCP não termina o Writer.

## Ficheiros implementados

- `src/yberu_writer_mcp/server.py` e `__main__.py`: inicialização MCP stdio usando a API v2 do SDK.
- `config.py`: loopback, porta, timeouts, limites e allowlists de gravação, imagem e modelo.
- `uno_worker.py`: ligação/reconexão, serialização de chamadas e mapeamento de resultados incertos.
- `documents.py`: descoberta Writer, IDs por sessão, snapshots em cache, âncoras, estrutura de objetos e listeners de modificação, seleção e ciclo de vida.
- `revisions.py`: hash estável, revisão local, âncoras e ledger UUID de idempotência.
- `events.py`: fila limitada, sequência, debounce/coalescing e contagem de notificações descartadas.
- `session.py`: fachada de serviço, leitura, estrutura, seleção, mutações, revisão e gravação segura.
- `errors.py`: erros estruturados usados por todas as ferramentas.
- `tools/registry.py`: ferramentas MCP de alto nível e sem execução UNO arbitrária.
- `scripts/launch_writer.py`: sessão gráfica `/usr/bin/soffice`, porta loopback, perfil isolado e sem headless.
- `tests/unit/test_core.py`: testes unitários de revisão, eventos, idempotência, cache e ferramentas MCP.
- `tests/integration/test_live_writer.py`: ensaio completo com GUI, MCP stdio e Writer de teste.
- `pyproject.toml`, `requirements.txt`, `README.md` e `docs/codex-mcp-config.example.toml`.

Os documentos originais de `docs/audit/` e as PoCs foram preservados.

## Ferramentas MCP implementadas e verificadas

| Ferramenta | Resultado verificado |
|---|---|
| `writer_get_state` | Conectado, versão `24.2…`, geração da sessão, estado ativo, capacidades e contador de eventos. |
| `writer_list_documents` | Distinguiu dois documentos de teste; retornou IDs diferentes, título, modificado, read-only e revisão. |
| `writer_read_document` | Devolveu texto em fatias por segmentos de parágrafo/célula, com revisão, hash quando disponível e referência do trecho. |
| `writer_read_selection` | Leu texto selecionado pela view Writer; classifica nenhuma seleção, ranges múltiplos e seleções não textuais. |
| `writer_read_structure` | Devolveu parágrafos, headings/níveis, estilos, tabelas e referências separadas para objetos estruturais. |
| `writer_preview_change` | Validou o alvo e mostrou preview antes/depois sem alterar o documento; emitiu token vinculado ao payload. |
| `writer_insert_text` | Só inseriu após confirmar prévia, conferir revisão/conteúdo e verificar o resultado. |
| `writer_replace_range` | Substituiu parágrafo; após o Upgrade 1 também substitui intervalo ancorado com verificação de conteúdo. |
| `writer_read_text_range` | Lê intervalo limitado por âncoras de parágrafo e offsets Unicode. |
| `writer_read_table` | Lê a matriz de células de tabela de primeiro nível com hashes. |
| `writer_replace_table_cell` | Substitui célula após validar revisão e texto/hash esperado. |
| `writer_apply_style` | Validou `Heading 1`, aplicou ao parágrafo ancorado e releu estilo e texto. |
| `writer_save_document` | Guardou um documento modificado já existente dentro da raiz temporária explicitamente autorizada no teste. |
| `writer_undo_last_change` | Desfez a mutação MCP identificada após confirmar que ela continuava no topo do undo nativo. |
| `writer_preview_create_document` | Validou a especificação do documento e emitiu token sem abrir um documento. |
| `writer_create_document` | Criou, pelo protocolo MCP, novo documento não guardado com dois parágrafos iniciais; devolveu ID, revisão e hash. |
| `writer_insert_table` | Inseriu uma tabela Writer nativa 2×2 depois de conferir âncora, revisão e confirmação. |
| `writer_insert_image` | Incorporou PNG temporário autorizado, validou o hash da prévia e confirmou texto alternativo e tamanho no documento. |
| `writer_save_as` | Gravou documento novo em ODT dentro da allowlist; recusou destino existente por padrão e só permitiu sobrescrita depois de hash e confirmação explícita. |
| `writer_preview_compose_document` / `writer_compose_document` | Montou documento novo com título, parágrafo, tabela e imagem a partir de blocos tipados, via uma única confirmação. |
| `writer_apply_formatting` | Aplicou negrito, itálico, tamanho, cor e alinhamento a um parágrafo ancorado; verificou as propriedades UNO e o texto preservado. |
| `writer_edit_table_structure` | Inseriu/removeu linha e coluna em tabela nativa com prévia dos valores removidos, hash da matriz e verificação de preservação. |
| `writer_format_table_cell` | Aplicou formatação à célula `B2` e releu texto, peso e cor sem alterar outras células. |
| `writer_update_image` | Atualizou texto alternativo, tamanho e alinhamento usando `object_ref` atual, revisão e fingerprint dos metadados. |
| `writer_preview_create_from_template` / `writer_create_from_template` | Criou documento editável com conteúdo de ODT autorizado, confirmou hash do modelo e verificou que o arquivo fonte não mudou. |

## Testes executados

### Unitários

Comando: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -v`  
**Resultado:** 5 testes aprovados.

- revisão só avança com mudança do fingerprint;
- repetição idempotente não reexecuta a mutação e reutilização de UUID com argumentos diferentes é recusada;
- resultado incerto não é repetido;
- fila coalesce eventos e atribui sequência;
- o SDK regista as ferramentas MCP.

### Integração MCP + Writer gráfico

Comando: `YBERU_RUN_LIVE_TESTS=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/integration -v`  
**Resultado:** 1 ensaio end-to-end aprovado. A última execução terminou com `OK`.

| Caso pedido | Resultado | Evidência do ensaio |
|---|---|---|
| A — Conexão | Aprovado | Launcher abriu o Writer GUI e `writer_get_state` devolveu conectado e versão 24.2. |
| B — Descoberta | Aprovado | Dois documentos de teste foram lidos e receberam IDs distintos; título e read-only foram verificados. |
| C — Leitura | Aprovado | O conteúdo inserido na fixture foi lido pelo cliente MCP stdio. |
| D — Seleção | Aprovado | A seleção textual da view do Writer foi devolvida por `writer_read_selection`. |
| E — Edição | Aprovado | Inserção e substituição foram observadas no documento Writer aberto. |
| F — Conflito | Aprovado | Alteração externa após a leitura foi recusada com `CONFLICT`. |
| G — Formatação | Aprovado | `Heading 1` foi aplicado e relido com o texto preservado. |
| H — Eventos | Aprovado | XTest digitou `eventprobe`; a sequência interna avançou e uma subscrição MCP real recebeu `ResourceUpdated`. |
| I — Reconexão | Aprovado | Writer foi reiniciado pelo teste enquanto a mesma sessão MCP continuava ativa; geração avançou e ID antigo retornou `DOCUMENT_NOT_FOUND`. |
| J — MCP real | Aprovado | `ClientSession` fez initialize/handshake, listou ferramentas e chamou leituras e escritas via stdio. |
| K — Preservação | Aprovado | A substituição manteve a tabela e células, o texto do parágrafo vizinho e o estilo do alvo. |

Também passaram `compileall` para `src`, `scripts` e `tests`, e a leitura do exemplo TOML do Codex através do parser padrão `tomllib`.

## Segurança e comportamento confirmado

- Listener UNO limitado a `127.0.0.1`; host configurável aceita apenas loopback.
- Launcher recusa porta já ocupada antes de iniciar; o perfil dedicado evita encaminhamento silencioso para a sessão normal.
- Chamadas UNO são serializadas; operações mutáveis não são repetidas automaticamente após timeout/desconexão/resultado incerto.
- IDs de documentos ficam inválidos quando a geração UNO muda.
- Leituras e textos de entrada têm limites. Snapshots acima do limite permitem leitura parcial, mas não edição segura.
- Estilos são selecionados da família de estilos de parágrafo já existente.
- Gravação exige caminho dentro de `YBERU_WRITER_ALLOWED_SAVE_ROOTS`. A allowlist padrão é vazia.
- O servidor não expõe macro, shell, dispatch, Python ou métodos UNO arbitrários.

## Limitações restantes

- Edição de intervalos está limitada ao texto do corpo ancorado; intervalos que atravessem estruturas Writer não suportadas são recusados. Operações estruturais e formatação de células cobrem tabelas de primeiro nível; tabelas aninhadas, cabeçalhos/rodapés e frames não são alvos de edição.
- Revisão é fingerprint local e não transação LibreOffice. Não há garantia ACID nem deteção de mudança externa que seja revertida antes da próxima leitura/verificação.
- Snapshots cobrem parágrafos do corpo, células de tabelas de primeiro nível e dados visíveis de frames, bookmarks, secções, cabeçalhos/rodapés, notas e campos quando as interfaces UNO os expõem. Objetos aninhados ou não suportados ainda não compõem uma revisão integral.
- Eventos MCP são notificações coalescidas de recurso, sem histórico e sem deltas de texto. Clientes sem `subscriptions/listen` dependem de polling.
- `writer_save_document` usa `store()` em ficheiro existente. `writer_save_as` aceita ODT/DOCX/RTF/TXT em raízes autorizadas; URLs remotas e garantias ACID não são suportadas. Save não é reversível pelo undo do Writer.
- Formatação cobre propriedades controladas de parágrafo inteiro e célula; não aplica formatação parcial dentro de um parágrafo. Imagens permitem atualizar descrição, dimensões e alinhamento; não há ferramenta de recorte ou legenda dedicada.
- Criação por template aceita pacotes ODT/OTT válidos em raízes autorizadas. A allowlist padrão é vazia; a abertura usa modo de template, desativa macros e não atualiza links externos.
- Tokens de confirmação duram 120 segundos, são de uso único e ficam em memória; não sobrevivem à reinicialização do MCP.
- Undo limita-se à última mutação MCP verificada, caso ela permaneça no topo da pilha nativa. Ações do utilizador no topo são preservadas e causam recusa.
- A ligação por PyUNO foi comprovada em sessão Writer local com perfil de teste. Instância externa já aberta só pode ser usada se tiver sido iniciada previamente com listener UNO.
- O cliente MCP de teste confirmou protocolo, ferramentas e chamadas reais. A configuração de projeto `.codex/config.toml` foi reconhecida por `codex mcp list --json` em um `CODEX_HOME` temporário onde o projeto estava marcado como confiável. Depois, a pedido do utilizador, o projeto foi marcado como confiável no `~/.codex/config.toml` deste ambiente; o CLI passou a listar `yberu_writer` como habilitado.

## Execução

1. Inicia o Writer: `.venv/bin/python scripts/launch_writer.py --port 2002 --profile "$HOME/.local/share/yberu-writer-mcp/profile" --wait 25 --json`.
2. O projeto está marcado como confiável no Codex e a configuração stdio define raízes separadas para imagens, gravações e templates.
3. Recarrega a sessão depois das alterações do servidor e confirma `codex mcp list`, o catálogo de ferramentas e `writer_get_state`.
4. Para testar edição, usa primeiro um ODT temporário e uma âncora/revisão acabadas de ler.

Não foi necessário editar o checkout C++ do LibreOffice nem tocar em documentos pessoais durante os ensaios. A pedido do utilizador, a configuração global do Codex recebeu a entrada de confiança do projeto e o registro stdio do `yberu_writer`; os demais valores foram preservados.

## Upgrade 5 — Compatibilidade e integração Codex

**Executado em:** 2 de novembro de 2026
**Resultado:** configuração de projeto implementada e descoberta verificada pelo CLI em ambiente isolado.

O arquivo `.codex/config.toml` regista o executável absoluto do venv, o módulo servidor, `cwd`, `PYTHONPATH`, host/porta locais, timeouts e aprovação `writes`. `ToolAnnotations` classifica ferramentas de leitura/prévia como read-only e mutações como não read-only; substituição, célula, save e undo também estão marcadas como destrutivas. O README contém fluxo de descoberta e utilização.

O `codex mcp list --json` inicialmente não listava servidores definidos no projeto porque este diretório ainda não estava confiável. Para verificar o formato sem tocar na configuração real, foi criado um `CODEX_HOME` temporário contendo confiança apenas para este caminho; nesse perfil, o CLI listou `yberu_writer`. Depois, a pedido do utilizador, o caminho foi marcado como confiável no `~/.codex/config.toml`. O teste após reload mostrou que o workspace aberto pelo Codex é a pasta pai `Writer`, onde o arquivo local de `MCPProj` não é carregado. Por isso, o servidor stdio também foi registado globalmente, com os mesmos caminhos e política `writes`. TOML validado; `codex mcp list --json` agora mostra `yberu_writer` habilitado a partir da pasta `Writer`.

O handshake MCP, catálogo de ferramentas, leitura e mutações foram verificados pelo cliente `ClientSession` contra o processo stdio, incluindo Writer GUI de teste. A configuração global também foi reconhecida pelo Codex CLI a partir da pasta pai `Writer`; a sessão aberta precisa recarregar depois da alteração para atualizar o catálogo de ferramentas.

**Resultado:** servidor descoberto e habilitado no CLI a partir da pasta de workspace atual; política de aprovação/anotações verificadas no SDK. Recarregar a sessão atual depois da inclusão global para atualizar o catálogo de ferramentas.

## Upgrade 1 — Intervalos e células de tabela

**Executado em:** 2 de novembro de 2026
**Resultado:** concluído e testado no Writer gráfico de teste e pelo cliente MCP stdio real.

Novas ferramentas: `writer_read_text_range`, `writer_read_table` e `writer_replace_table_cell`. `writer_replace_range` aceita agora uma âncora de parágrafo ou a âncora efémera devolvida por `writer_read_text_range`. As âncoras continuam vinculadas a documento e revisão. A substituição de uma célula exige revisão e texto/hash esperado, verifica que as outras células e parágrafos do snapshot permaneceram iguais e retorna `UNKNOWN_OUTCOME` se a verificação posterior falhar.

O teste atualizado verificou leitura e substituição parcial de “First” por “Opening”, leitura de uma matriz de tabela e troca de `B1` de “keep-value” para “updated-value”; confirmou também preservação da célula `A1`, do parágrafo vizinho e do conteúdo da tabela.

Comandos executados:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -v
YBERU_RUN_LIVE_TESTS=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/integration -v
```

**Resultado:** 5 testes unitários aprovados e 1 ensaio gráfico end-to-end aprovado em 2 de novembro de 2026. A integração usou um perfil e documentos temporários e encerrou apenas o processo Writer criado pelo próprio ensaio.

## Upgrade 2 — Snapshots incrementais e estrutura

**Executado em:** 2 de novembro de 2026
**Resultado:** concluído e verificado em testes unitários e integração GUI/MCP.

`DocumentRegistry` mantém o último snapshot e uma versão imediata dos eventos de modificação por documento. A fila invalida o cache no momento em que recebe uma alteração, antes do debounce; eventos de seleção não invalidam conteúdo. `writer_list_documents` e `writer_read_structure` podem reutilizar snapshots válidos. Cada escrita força uma captura nova dentro do worker serializado, mantendo a verificação de conflito. O snapshot agora inclui nível de outline e metadados separados de frames, bookmarks, secções, cabeçalhos/rodapés, notas e campos, sem achatar esses objetos no texto do corpo.

`writer_read_document` recorta segmentos de parágrafos e células e preserva os separadores do texto Writer sem montar uma segunda string integral do documento. Se o documento ultrapassa o limite de snapshot, a leitura paginada continua por segmentos UNO, com `snapshot_hash` indisponível e escrita segura recusada. A leitura estrutural continua sujeita ao limite configurado.

O ensaio GUI/MCP confirmou conteúdo igual a `XText.getString()` num documento temporário com tabela, além de bookmark, frame e cabeçalho. Testes unitários cobrem reutilização de cache, invalidação imediata, refresh forçado e leitura por fatias.

**Resultado:** 7 testes unitários aprovados, 1 integração end-to-end aprovada e `compileall` aprovado em 2 de novembro de 2026.

## Upgrade 3 — Subscrições de eventos MCP

**Executado em:** 2 de novembro de 2026
**Resultado:** concluído e verificado via stdio com `ClientSession` e `subscriptions/listen`.

O SDK instalado fornece `MCPServer(subscriptions=...)`, `InMemorySubscriptionBus`, `ResourceUpdated` e `ClientSession.listen` para o protocolo moderno. O servidor publica no bus a partir de um bridge que transfere eventos coalescidos da thread de eventos para o event loop MCP; callbacks UNO continuam a enfileirar apenas. `writer_list_documents` devolve `event_uri` por documento. O recurso `yberu-writer://documents/{document_id}/events` oferece sequência, tipos de evento, revisão, hash e estado dirty, sem texto do documento. `writer_get_state` anuncia suporte a subscrições.

O teste MCP real abriu uma subscrição para o URI do documento, fez uma inserção no Writer pela ferramenta MCP, recebeu `ResourceUpdated` e leu o recurso para confirmar ID e tipo de evento. Não existe histórico nem replay; o evento é um sinal para reler o estado. Clientes que não suportam `subscriptions/listen` permanecem funcionais por chamada normal, mas sem push.

## Upgrade 4 — Prévia, confirmação e undo

**Executado em:** 2 de novembro de 2026
**Resultado:** concluído e verificado via cliente MCP real.

`writer_preview_change` suporta inserção, substituição de parágrafo/intervalo, estilo, célula, save e undo. A prévia verifica documento, revisão, âncora, conteúdo esperado, estilo/célula ou caminho de gravação e devolve antes/depois limitado, hashes e token aleatório de uso único com validade de 120 segundos. Todas as ferramentas mutáveis agora exigem esse token e os mesmos argumentos; ele é consumido dentro do ledger idempotente, de modo que uma repetição do mesmo `operation_id` devolve o resultado já confirmado sem reexecutar ou consumir novamente o token.

Inserção, substituição, célula e estilo usam `XUndoManager` com um rótulo MCP que contém o UUID da operação. `writer_undo_last_change` também exige prévia e só chama `undo()` quando a revisão/hash conferem e o título da próxima ação da pilha é exatamente o rótulo dessa mutação. Assim, uma edição posterior do utilizador não é desfeita. O undo é confirmado ao comparar o snapshot resultante com o snapshot anterior. `writer_save_document` exige prévia com o caminho atual, que é revalidado antes de guardar; o save não gera um undo reversível.

O teste de integração confirmou que chamada sem token foi recusada sem mutação; a prévia não alterou o texto; inserção foi confirmada; repetição idempotente não executou de novo; a subscrição MCP notificou a mudança; e o undo do valor de célula restaurou o snapshot anterior sem alterar o restante documento.

**Resultado:** 8 testes unitários aprovados, 1 integração end-to-end aprovada e `compileall` aprovado em 2 de novembro de 2026.

## Upgrade 6 — Criação de documentos e composição estrutural

**Executado em:** 3 de novembro de 2026
**Resultado:** aprovado por cliente MCP stdio real contra uma instância Writer GUI isolada.

Foram adicionadas quatro ferramentas: `writer_preview_create_document`, `writer_create_document`, `writer_insert_table` e `writer_insert_image`. Criar documento requer prévia, token de confirmação de uso único e UUID de idempotência; o resultado informa o ID opaco, revisão e hash do documento. O documento novo fica aberto e não guardado, sem caminho de gravação escolhido pelo servidor.

Tabela e imagem são inseridas depois de parágrafo com âncora e revisão atuais, usando `writer_preview_change` e a ferramenta de mutação correspondente. A tabela é nativa, valida linhas/colunas/valores e confirma o snapshot sem perder o conteúdo anterior. Imagens exigem caminho local dentro de `YBERU_WRITER_ALLOWED_IMAGE_ROOTS`, limite de tamanho e formato reconhecido (PNG, JPEG, GIF ou BMP); a prévia devolve hash, que é revalidado antes da incorporação. O teste confirmou metadados de texto alternativo, uma tabela 2×2, um objeto gráfico, o texto inicial e os valores de célula.

Configuração do ambiente atual: a pasta autorizada é `/home/yberudev/.local/share/yberu-writer-mcp/images`. A allowlist fica vazia por padrão noutros ambientes; sem ela, inserção de imagens é recusada. As tabelas têm no máximo 100 linhas/colunas e respeitam `YBERU_WRITER_MAX_TABLE_CELLS` (500 por padrão).

O ensaio foi executado em documento e perfil temporários com o handshake MCP stdio e conferência independente via PyUNO da contagem de tabelas e imagens. Não usou ficheiros pessoais e encerrou apenas o processo iniciado pelo teste. A validação final executou 9 testes unitários, 1 integração end-to-end, `compileall` e `git diff --check`.

Comandos de validação:

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests scripts
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -v
YBERU_RUN_LIVE_TESTS=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/integration -v
git diff --check
```

Depois de atualizar o checkout, reinicia/recarrega a sessão Codex para o processo stdio anunciar as quatro ferramentas desse upgrade.

## Upgrade 7 — Save As com proteção de destino

**Executado em:** 3 de novembro de 2026
**Resultado:** testado via cliente MCP stdio e Writer GUI isolado.

`writer_save_as` aceita apenas diretórios dentro de `YBERU_WRITER_ALLOWED_SAVE_ROOTS`, exige extensão correspondente a ODT, DOCX, RTF ou TXT e verifica o destino na prévia. Se o destino existe, a prévia normal recusa a operação; para substituição é preciso pedir `overwrite=true`, confirmar o hash atual do ficheiro e consumir o token de confirmação. Antes da mutação, revisão, caminho, formato e hash são revalidados. Erros depois de `storeAsURL` retornam `UNKNOWN_OUTCOME` para evitar repetição automática.

O ensaio gravou um documento novo como ODT, confirmou URL e estado Writer e verificou que um destino existente permaneceu intacto quando a sobrescrita não foi autorizada.

## Upgrade 8 — Composição a partir de blocos

**Executado em:** 3 de novembro de 2026
**Resultado:** testado com cliente MCP stdio real e Writer gráfico.

`writer_preview_compose_document` e `writer_compose_document` aceitam uma lista validada de blocos `paragraph`, `heading`, `table` e `image`. O preview inclui valores estruturais e hashes dos ficheiros de imagem; uma única confirmação cria o documento, aplica níveis de título e insere tabela e imagem nativas. Se a montagem falhar, o servidor tenta descartar apenas o documento parcial que criou. O teste releu o novo documento e verificou título, texto, nível do heading, tabela e texto alternativo da imagem.

## Upgrade 9 — Formatação de parágrafo

**Executado em:** 3 de novembro de 2026
**Resultado:** testado e verificado por propriedades UNO.

`writer_apply_formatting` aplica apenas propriedades permitidas: negrito, itálico, sublinhado, fonte, tamanho entre 6 e 96 pt, cor hexadecimal e alinhamento. A operação requer âncora, revisão, texto esperado, preview e confirmação. Snapshots agora incluem as propriedades diretas de parágrafos e células relevantes para detectar alteração visual e conflito. A integração verificou texto preservado, negrito, itálico, 14 pt, cor e alinhamento.

## Upgrade 10 — Edição estrutural de tabelas e imagens

**Executado em:** 3 de novembro de 2026
**Resultado:** testado em documento temporário.

`writer_edit_table_structure` insere/remove linhas e colunas em tabela de primeiro nível, mostra células removidas, inclui fingerprint da matriz e verifica dimensões e conteúdo preservado. `writer_format_table_cell` reutiliza a allowlist de formatação e mantém conteúdo das demais células. `writer_update_image` resolve um `object_ref` apenas dentro da revisão atual e permite atualizar texto alternativo, dimensões e alinhamento do parágrafo de ancoragem. O ensaio executou os quatro tipos de mudança estrutural, formatou `B2` sem alterar o texto e atualizou as propriedades de uma imagem incorporada.

## Upgrade 11 — Documentos a partir de modelos ODT/OTT

**Executado em:** 3 de novembro de 2026
**Resultado:** testado com ODT temporário e allowlist do próprio teste.

`writer_preview_create_from_template` valida caminho, raiz permitida, extensão, MIME OpenDocument, tamanho compactado/descompactado e hash. `writer_create_from_template` revalida o hash e abre uma cópia editável com `AsTemplate`; execução de macros e atualização de links ficam desativadas durante a abertura. O teste confirmou que o documento criado contém o conteúdo do modelo, recebeu título próprio e deixou o ficheiro-fonte byte a byte inalterado.

O Codex deste ambiente foi configurado para usar as pastas dedicadas `/home/yberudev/.local/share/yberu-writer-mcp/saves`, `/home/yberudev/.local/share/yberu-writer-mcp/images` e `/home/yberudev/.local/share/yberu-writer-mcp/templates`. As duas configurações TOML foram validadas; a sessão Codex precisa recarregar para o processo MCP ler as novas allowlists.

## Validação cumulativa dos cinco upgrades

Comandos executados em documentos e perfis Writer temporários:

```bash
PYTHONPATH=src .venv/bin/python -m compileall -q src tests scripts
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -v
YBERU_RUN_LIVE_TESTS=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/integration -v
git diff --check
```

**Resultado:** 11 testes unitários aprovados, 1 integração MCP/Writer GUI aprovada e verificação de diff limpa. A integração percorreu gravação segura, composição, formatação, edição de tabelas/imagens e criação baseada em modelo. Os upgrades deste relatório ainda são alterações locais posteriores ao commit publicado `d9eaeb7`.
