# Relatório de implementação — Yberu Writer MCP MVP

**Data da execução:** 19 de outubro de 2026  
**Estado:** integração MCP ↔ Writer gráfico testada em sessão isolada.  
**Código C++ do LibreOffice:** não alterado.

## Resumo

Foi implementado um servidor MCP stdio modular que se liga por PyUNO a uma instância Writer com listener UNO em localhost. O servidor mantém um único worker para as operações UNO, IDs de documento opacos por sessão, revisão local derivada de snapshots, âncoras temporárias e verificações de precondição antes das escritas. Callbacks de eventos apenas colocam notificações numa fila thread-safe; o processamento coalesce eventos e invalida snapshots sem executar UNO.

O teste de integração iniciou uma instância GUI com perfil temporário privado, fez handshake com o protocolo MCP real, descobriu as nove ferramentas e leu/escreveu o Writer. Também verificou entrada de teclado X11, estilo, preservação de tabela, conflito, salvamento restrito e reconexão mantendo a mesma sessão MCP.

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
MCPServer + registo de 9 ferramentas
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
- `config.py`: loopback, porta, timeouts, limites e raízes de gravação autorizadas.
- `uno_worker.py`: ligação/reconexão, serialização de chamadas e mapeamento de resultados incertos.
- `documents.py`: descoberta Writer, IDs por sessão, snapshots, âncoras e listeners de modificação, seleção e ciclo de vida.
- `revisions.py`: hash estável, revisão local, âncoras e ledger UUID de idempotência.
- `events.py`: fila limitada, sequência, debounce/coalescing e contagem de notificações descartadas.
- `session.py`: fachada de serviço, leitura, estrutura, seleção, mutações, revisão e gravação segura.
- `errors.py`: erros estruturados usados por todas as ferramentas.
- `tools/registry.py`: as nove ferramentas MCP de alto nível e sem execução UNO arbitrária.
- `scripts/launch_writer.py`: sessão gráfica `/usr/bin/soffice`, porta loopback, perfil isolado e sem headless.
- `tests/unit/test_core.py`: cinco testes unitários.
- `tests/integration/test_live_writer.py`: ensaio completo com GUI, MCP stdio e Writer de teste.
- `pyproject.toml`, `requirements.txt`, `README.md` e `docs/codex-mcp-config.example.toml`.

Os documentos originais de `docs/audit/` e as PoCs foram preservados.

## Ferramentas MCP implementadas e verificadas

| Ferramenta | Resultado verificado |
|---|---|
| `writer_get_state` | Conectado, versão `24.2…`, geração da sessão, estado ativo, capacidades e contador de eventos. |
| `writer_list_documents` | Distinguiu dois documentos de teste; retornou IDs diferentes, título, modificado, read-only e revisão. |
| `writer_read_document` | Devolveu texto existente com limite, revisão, hash quando disponível e referência do trecho. |
| `writer_read_selection` | Leu texto selecionado pela view Writer; classifica nenhuma seleção, ranges múltiplos e seleções não textuais. |
| `writer_read_structure` | Devolveu parágrafos, headings/níveis, estilos, referências de tabela e âncoras efémeras. |
| `writer_insert_text` | Inseriu texto numa âncora, conferiu revisão/conteúdo e confirmou o resultado. |
| `writer_replace_range` | Substituiu o parágrafo exato com `expected_text` ou `expected_hash`; confirmou o texto e a preservação do estilo. |
| `writer_apply_style` | Validou `Heading 1`, aplicou ao parágrafo ancorado e releu estilo e texto. |
| `writer_save_document` | Guardou um documento modificado já existente dentro da raiz temporária explicitamente autorizada no teste. |

## Testes executados

### Unitários

Comando: `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -v`  
**Resultado:** 5 testes aprovados.

- revisão só avança com mudança do fingerprint;
- repetição idempotente não reexecuta a mutação e reutilização de UUID com argumentos diferentes é recusada;
- resultado incerto não é repetido;
- fila coalesce eventos e atribui sequência;
- o SDK regista as nove ferramentas.

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
| H — Eventos | Aprovado | XTest digitou `eventprobe` na janela Writer do teste; conteúdo e sequência de eventos avançaram. |
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
- Gravação não recebe caminho do cliente; exige documento local existente e caminho dentro de `YBERU_WRITER_ALLOWED_SAVE_ROOTS`. A configuração padrão não autoriza gravação.
- O servidor não expõe macro, shell, dispatch, Python ou métodos UNO arbitrários.

## Limitações restantes

- As mutações MVP estão limitadas a parágrafos do corpo. Apesar do nome, `writer_replace_range` não substitui ranges arbitrários nem conteúdo de células, cabeçalhos/rodapés e frames.
- Revisão é fingerprint local e não transação LibreOffice. Não há garantia ACID nem deteção de mudança externa que seja revertida antes da próxima leitura/verificação.
- Snapshots representam parágrafos do corpo e células de tabelas de primeiro nível; objetos aninhados e algumas regiões especiais não compõem ainda uma revisão integral.
- Eventos são infraestrutura interna de invalidação; ainda não há subscrição MCP nem stream de deltas.
- Gravação suporta `store()` em ficheiro existente sob allowlist; não há Save As ou URL remota.
- A ligação por PyUNO foi comprovada em sessão Writer local com perfil de teste. Instância externa já aberta só pode ser usada se tiver sido iniciada previamente com listener UNO.
- O cliente MCP de teste confirmou protocolo, ferramentas e chamadas reais. O `~/.codex/config.toml` não foi modificado; a descoberta na interface Codex depende de acrescentar o bloco documentado e recarregar o Codex.

## Execução

1. Inicia o Writer: `.venv/bin/python scripts/launch_writer.py --port 2002 --profile "$HOME/.local/share/yberu-writer-mcp/profile" --wait 25 --json`.
2. Regista o servidor stdio no Codex conforme a secção “Registar no Codex” do README ou `docs/codex-mcp-config.example.toml`.
3. Recarrega o Codex e confirma `writer_get_state`, `writer_list_documents` e uma leitura de documento de teste.
4. Para testar edição, usa primeiro um ODT temporário e uma âncora/revisão acabadas de ler.

Não foi necessário editar o checkout C++ do LibreOffice, alterar configurações globais do Codex, nem tocar em documentos pessoais durante os ensaios.
