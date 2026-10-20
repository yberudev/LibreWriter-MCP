# Auditoria técnica do LibreOffice Writer para o Yberu Writer MCP

**Data da inspeção:** 19 de outubro de 2026  
**Checkout principal:** `CodigoFonte/core-git`  
**Commit:** `2431868f2abd904d682010175ea966e9b78bc2d6` (`master`, limpo durante a auditoria)  
**Escopo:** investigar a implementação e as interfaces do Writer, validar operações em instâncias próprias e temporárias, e propor a arquitetura. Não foi implementado o servidor MCP nem alterado o código-fonte do LibreOffice.

## Conclusão executiva

O caminho tecnicamente indicado é começar com **MCP Python + PyUNO diretamente no ambiente virtual + LibreOffice já executado com um listener UNO local configurado**. O ambiente Python 3.12.3 consegue importar tanto o SDK MCP 2.3.0 como `uno`; a PoC ligou-se ao LibreOffice do sistema 24.2.7, criou e editou Writer, aplicou propriedades, executou `.uno:Bold`, inseriu tabela e anotação, usou undo/redo e fez round-trip ODT. Uma segunda PoC restabeleceu a ligação depois de reiniciar exclusivamente a instância criada pelo próprio teste.

O Writer expõe uma API UNO ampla para texto, cursores e intervalos, estilos, tabelas, objetos, referências, índices, notas, anotações, alterações registadas, armazenamento e controlo da view. O núcleo não precisa de ser alterado para a primeira versão robusta do MCP. O principal limite público confirmado é a observabilidade: `XModifyListener` informa que algo mudou, sem intervalo nem delta; os callbacks de seleção também fornecem somente `EventObject`. A implementação Writer de `SwXParagraph::addPropertyChangeListener` está explicitamente não implementada. Se for requisito entregar deltas semânticos de cada edição do utilizador, há justificação para desenhar uma extensão da API no núcleo C++, mas ainda não para iniciar um fork.

“Controlo máximo” aqui significa que o MCP consegue consultar e alterar de modo validado o modelo do documento e, com uma sessão habilitada, atuar sobre seleção/view e despachar comandos. Isso não significa que qualquer diálogo, componente OLE, macro, estado gráfico ou edição venha com um stream de eventos detalhado. Tais capacidades dependem do componente, de uma extensão ou de uma investigação específica.

## Ambiente conferido

| Item | Resultado | Consequência |
|---|---|---|
| Python do venv | 3.12.3, executável em `MCPProj/.venv/bin/python` | Alinha com o Python de sistema indicado em `pyvenv.cfg`. |
| SDK MCP | 2.3.0; requisito declarado `mcp[cli]>=2.0,<3.0` | Importado no venv; nenhum servidor foi criado nesta etapa. |
| PyUNO | `uno.py` em `/usr/lib/python3/dist-packages/uno.py`; pacote do sistema `python3-uno` 24.2.7 | Importado pelo venv porque este usa `include-system-site-packages=true`. Ligação testada ao LibreOffice 24.2 do sistema. |
| LibreOffice de sistema | `/usr/bin/soffice`, 24.2.7.2 Build 420 | Foi o runtime escolhido para as PoCs novas, com perfis privados. |
| `soffice` no PATH | Wrapper em `~/.cache/codex-runtimes/.../override/soffice`, runtime LibreOfficeDev 26.8.0.0.alpha0, hash `2c87e51e…` | Não é o binário de sistema. A PoC anterior precisou de `SAL_USE_VCLPLUGIN=gen` nesse runtime headless; não aplicar essa variável por regra ao modo gráfico. |
| Código-fonte | HEAD `2431868f…`; checkout e submódulos limpos; branch `master` | O hash do runtime 26.8 não está presente no clone. Não se pode afirmar que o binário testado corresponda ao checkout. |
| Build | `autoconf`, `automake`, `aclocal`, `libtoolize` não encontrados; não foi executado `autogen.sh` nem compilação | Build completo não é necessário para provar o caminho UNO atual. |
| GUI | `DISPLAY=:0`; a PoC GUI criou uma janela Writer própria com perfil temporário | Não foi tocada uma janela/documento pessoal. A sessão genérica sem listener não foi assumida como acessível. |

**Recomendação sobre processos:** não são necessários dois processos Python só para “compatibilizar” o UNO neste ambiente: o mesmo interpretador venv importa MCP e PyUNO e conectou-se à instalação 24.2. Para MCP assíncrono, convém executar chamadas UNO síncronas num worker serializado do mesmo processo, com callbacks UNO reduzidos a mensagens numa fila thread-safe. O LibreOffice continua a ser um processo separado. Manter um bridge Python adicional só se justifica por isolamento/crash containment ou se o runtime final não puder carregar o PyUNO compatível.

**Limite importante de sessão:** uma instância normal aberta sem `--accept`/listener não passa automaticamente a aceitar conexões UNO. Para o caso “o utilizador abre o Writer normalmente”, o produto deverá integrar o arranque por um launcher/perfil dedicado ou uma extensão que estabeleça o listener. Ligar-se a uma sessão GUI já aberta e sem listener não foi testado, e não se deve tentar anexar-se a processos pessoais sem configuração explícita.

## Descobertas sobre o Writer

1. `SwDoc` é o modelo C++ do documento (estrutura de nós, estado e acesso ao undo). `SwDocShell` integra-o no sistema de documentos do Sfx; `SwView`/`SwViewShell` e `SwWrtShell` ligam view, layout, cursor e interação. A API externa é principalmente o objeto `SwXTextDocument`/`SfxBaseModel`, cursores, suppliers e o controller/view UNO.
2. As implementações UNO do Writer estão espalhadas por `sw/source/core/unocore/` (texto, parágrafos, tabelas, estilos, campos, marcas, frames, redlines) e `sw/source/uibase/uno/` (documento e view). Os serviços UNO encapsulam o modelo C++ em vez de expor diretamente os objetos internos.
3. O modelo interno `SwNodes`/`SwTextNode` oferece endereços e offsets estruturais, mas a auditoria não encontrou um identificador de parágrafo público persistente. Um offset em `SwNodes` pode mudar com inserções/remoções. O MCP deve usar bookmarks nomeados ou marcas próprias e re-resolver cada alvo, validando versão/hash e contexto.
4. A seleção não tem um único tipo de retorno. `SwXTextView::getSelection()` entrega uma coleção de ranges para seleção textual, cursor de tabela para seleção em tabela, objetos para frames/imagens e uma coleção de shapes para desenho. O consumidor deve inspecionar o tipo/interface recebido.
5. `.uno:` dispatch é um caminho real para comandos da UI. O ensaio selecionou o texto `paragraph.` numa instância headless, executou `.uno:Bold` via `DispatchHelper` e observou `CharWeight` baixar de 150 para 100. A execução e o efeito foram verificados. O slot, argumentos e estado dependem do comando e do contexto; não se deve aceitar uma URL arbitrária fornecida pelo modelo.
6. Há 653 URLs `.uno:` distintas extraídas de `xlink:href` nas configurações Writer inspecionadas. Esse número é uma contagem estática mínima das ligações UI verificadas, não um inventário de todos os slots: `sw/sdi/swriter.sdi` tem slots adicionais e algumas ações da UI usam outras formas de declarar comandos.
7. Os filtros `writer8` e `writer_pdf_Export` estão declarados na árvore `filter/`; a PoC executou o round-trip ODT com `writer8`. Importação/exportação DOCX é declarada por filtros OOXML. Fidelidade de conversão não foi avaliada.
8. Comentários/anotações são representáveis como `com.sun.star.text.TextField.Annotation`; a PoC definiu autor e conteúdo, inseriu e enumerou a anotação. Isso não prova suporte a respostas encadeadas, nem a equivalência total com os comentários recentes em todos os formatos.

## Provas de conceito concluídas

Os scripts estão em [`poc/`](poc/). Todos lançam processo com perfil isolado e só usam documentos temporários.

| Experiência | Estado | Evidência observada |
|---|---|---|
| Listener e ligação UNO | Concluída | `uno_document_probe.py` e `reconnect_probe.py` conectaram por `127.0.0.1`. |
| Documento ativo e lista | Parcial | O documento Writer da PoC apareceu como um único componente; não se inferiu a janela ativa de uma sessão externa. |
| Leitura e edição textual | Concluída | Texto criado e lido por `XText`; escrita verificada. |
| Estilo e formatação | Concluída | Estilo `Heading 1` e peso de fonte aplicados a ranges e lidos. |
| Dispatch | Concluída | `.uno:Bold` alterou o peso do range de 150 para 100. |
| Tabela e anotação | Concluída | Uma tabela 2×2 e anotação Writer foram criadas e enumeradas. |
| Eventos | Concluída, com limites | PoC headless recebeu 36 eventos XModify (callback no `MainThread`). A prova GUI anterior recebeu 7 para digitação real e callback no `Dummy-2`. Nenhum contém delta. |
| Seleção/interface gráfica | Concluída na sessão privada | GUI isolada visível; entrada por teclado X11 mudou o texto; seleção UNO e callbacks de seleção foram observados. |
| Concorrência | Parcial | Foi simulada alteração posterior à leitura e a política optimistic-concurrency recusou a escrita obsoleta. Não é teste de duas edições verdadeiramente simultâneas. |
| Undo/redo | Concluída | Uma sentinela agrupada foi removida por undo e restaurada por redo. |
| ODT | Concluída | Guardado e reaberto; texto e tabela persistiram. |
| Reconexão | Concluída | A instância privada foi parada; uma nova instância no mesmo listener/perfil de teste aceitou conexão; um novo documento foi legível. Nenhum processo existente foi terminado. |
| Attach a GUI preexistente | Não testada | Não foi iniciada qualquer sessão pessoal nem se presumiu que tivesse listener. |

Para resultados brutos, comandos e limites, ver [`PROVAS_DE_CONCEITO.md`](PROVAS_DE_CONCEITO.md). As PoCs são ensaios de mecanismo, não uma suíte de qualidade nem prova exaustiva das funcionalidades do Writer.

## Classificação da matriz

A [`MATRIZ_CAPACIDADES.csv`](MATRIZ_CAPACIDADES.csv) contém **66 capacidades** em 13 categorias, com estado de verificação e evidência por linha. A classificação registra o caminho necessário para a capacidade descrita:

| Código | Quantidade | Significado |
|---|---:|---|
| A | 56 | UNO/document model disponível diretamente, sujeito aos parâmetros e contexto. |
| B | 2 | Principalmente comando dispatch existente. |
| C | 5 | Extensão/componente adicional ou integração com outro serviço necessária para a forma descrita. |
| D | 2 | API ou mudança nativa do núcleo necessária para a exigência exata descrita. |
| E | 1 | Ainda sem solução comprovada nesta auditoria. |

Os códigos não significam que todas as funções A foram exercitadas. “PoC”, “Fonte/API”, “Parcial” e “Não testado” distinguem os níveis de evidência. O inventário pode e deve ser expandido por feature quando começarem os testes de regressão do MCP.

## Limitações que importam para a primeira arquitetura

- **Eventos sem deltas:** `XModifyListener.modified(EventObject)` não identifica texto, range ou origem. Em GUI, o callback ocorreu numa thread Python `Dummy-2`; em headless, na thread principal. O callback deve enfileirar evento pequeno e nunca bloquear nem editar UNO reentrante.
- **Granularidade:** `SwXParagraph::addPropertyChangeListener` e o respetivo remove chamam `OSL_FAIL("not implemented")`. Uma extensão UNO pode fornecer bootstrap, painel e comandos, mas não cria automaticamente a semântica de alteração que o núcleo não publica.
- **Referências obsoletas:** cursores/ranges podem viver através de alterações, mas a PoC não prova que sejam semanticamente seguros após mutações arbitrárias. Resolver de novo a âncora e verificar uma precondição antes de escrever.
- **Sessão existente:** listener precisa ser habilitado. Não expor socket UNO a interfaces de rede; usar `127.0.0.1` e perfil/instância própria na primeira versão.
- **Versões:** conexão comprovada é venv/PyUNO 24.2 ↔ LibreOffice 24.2. O wrapper/runtime 26.8 e o checkout do código mais recente não formam uma combinação certificada.
- **Semântica do Writer:** texto plano não inclui toda a estrutura semântica; seleção pode representar texto, tabelas, shapes ou objetos. Respostas MCP devem preservar tipo, posição e contexto.
- **Extensões e C++:** não há motivo comprovado para alterar o núcleo antes de implementar e medir o contrato UNO externo. A primeira necessidade de patch seria um contrato de ChangeSet/deltas granular e IDs nativos se estes forem requisitos firmes.

## Arquitetura recomendada

1. Processo MCP Python em `MCPProj`, com PyUNO carregado no próprio venv e cliente UNO síncrono numa fila serializada.
2. Conexão local com instância habilitada por configuração explícita; gerir sessão/perfil separado para sessões controladas pelo MCP e não terminar processos externos.
3. Camada de sessão que consulta documentos/controladores em cada operação; invalidar proxies UNO após desconexão e refazer descoberta/reconexão.
4. Ferramentas tipadas de leitura, estrutura, edição, estilo, tabela, referência, revisão e exportação. Cada escrita recebe ID do documento, `expected_revision` e precondição de texto/âncora.
5. Revisões MCP incrementais: listener XModify marca documento como “dirty”; worker coalesce eventos e refaz snapshot de partes afetadas (ou texto/modelo inteiro para sincronização inicial). Isto é polling orientado por evento, não stream de deltas garantido.
6. `XSelectionChangeListener` separado para seleção em GUI; callback põe `selection_changed` na fila e o worker consulta o estado atual da seleção.
7. Undo context para lote de alterações do MCP quando suportado, log auditável, confirmação explícita do utilizador por operação destrutiva e exportação para caminhos autorizados.
8. Extensão opcional para iniciar listener de modo integrado, instalar painel e fornecer ações visíveis. Só depois da medição considerar interface C++ para deltas nativos.

## Recomendação sobre C++ e fork

Não fazer fork nesta etapa. Implementar primeiro Estratégia A (MCP + UNO) e, se a inicialização/UX da sessão exigir, Estratégia B (extensão). Só abrir uma proposta C++ delimitada quando houver requisito demonstrável de: (a) ChangeSet de edição semântica com range/old/new/causa/revisão; ou (b) identidade estável de nós do documento garantida ao longo de edição, save/load e conversão. Para cada proposta, criar primeiro um protótipo de contrato IDL e mapear alterações em `sw`, `offapi` e `sfx2`; compilar apenas alvos incrementais depois de configurar o build. Um fork aumenta custo de atualização e não corrige por si só threading, conflito, segurança nem compatibilidade de filtros.

## Índice dos entregáveis

- [`RESUMO_EXECUTIVO.md`](RESUMO_EXECUTIVO.md) — respostas rápidas às dez perguntas da missão.
- [`MAPA_CODIGO_FONTE.md`](MAPA_CODIGO_FONTE.md) — módulos, classes, fluxos e pontos de extensão.
- [`MATRIZ_CAPACIDADES.csv`](MATRIZ_CAPACIDADES.csv) — inventário classificável e pesquisável.
- [`INVENTARIO_COMANDOS.md`](INVENTARIO_COMANDOS.md) — rotas de comandos, slots e limites.
- [`EVENTOS_TEMPO_REAL.md`](EVENTOS_TEMPO_REAL.md) — eventos UNO, threading e sincronização.
- [`LIMITACOES_E_PATCHES.md`](LIMITACOES_E_PATCHES.md) — restrições, alternativas e propostas condicionais C++.
- [`ARQUITETURA_YBERU_WRITER_MCP.md`](ARQUITETURA_YBERU_WRITER_MCP.md) — arquitetura e contratos MCP.
- [`FERRAMENTAS_MCP.md`](FERRAMENTAS_MCP.md) — catálogo proposto de tools.
- [`PROVAS_DE_CONCEITO.md`](PROVAS_DE_CONCEITO.md) — método, comandos e resultados.
- [`RISCOS_E_SEGURANCA.md`](RISCOS_E_SEGURANCA.md) — limites operacionais e medidas.
- [`PLANO_IMPLEMENTACAO.md`](PLANO_IMPLEMENTACAO.md) — etapas e critérios de aceitação.

## Itens pendentes para a implementação, não para bloquear o início

- Testar Attach a instância GUI iniciada pelo launcher/extensão final, sem listener headless.
- Implementar uma PoC dedicada de `RecordChanges`, enumeração/aceitação de redlines e autores.
- Testar inserção de imagem, notas, estilos de página, page cursor, pesquisa e export PDF em runtime 24.2 e na versão selecionada para distribuição.
- Fazer round-trip DOCX com documentos de amostra controlados e verificar tabelas/campos/estilos.
- Testar ranges/âncoras sob edições simultâneas reais em GUI; decidir a política de conflito e a versão por documento.
- Determinar o contrato de comentário/resposta encadeada na versão alvo do Writer.
- Atualizar o inventário de comandos a partir de `sw/sdi/swriter.sdi` e de todos os esquemas `.ui`, sem considerar as 653 referências visuais uma enumeração integral.

## Estado do checkout após auditoria

O checkout `CodigoFonte/core-git` permaneceu sem alterações de fonte (verificação inicial limpa; a auditoria criou somente documentos e scripts fora do checkout). O build completo do LibreOffice não foi iniciado.
