# Inventário de comandos Writer

## Fontes e cobertura

Há duas fontes distintas:

1. `sw/sdi/swriter.sdi`: declara slots Sfx, tipos de item/argumentos, command names, handlers e estados/contextos para o dispatcher interno.
2. `sw/uiconfig/swriter/` (e outras configurações Writer): menus, toolbars e layouts referenciam URLs `.uno:`.

Uma extração estática da UI encontrou **653 URLs `.uno:` distintas em `xlink:href`**. É uma amostra mínima de comandos referenciados pela interface, não a contagem total de slots, comandos extension-only, `action-name`, macros ou estados. Não foi gerado um catálogo completo de todos os slots/handlers nesta auditoria. O CSV classifica os comandos mais relevantes para as ferramentas MCP.

## Comandos confirmados no checkout

| Nome UNO/slot | Definição observada | Uso potencial | Contexto/argumentos/risco |
|---|---|---|---|
| `.uno:Bold` | comando de formatação padrão via dispatch | Alternar peso de seleção | PoC executada em frame Writer; range mudou `CharWeight` de 150 para 100. Toggle requer estado prévio explícito. |
| `.uno:TrackChanges` | `SfxBoolItem TrackChanges FN_REDLINE_ON` em `sw/sdi/swriter.sdi:56` | Ativar/desativar registo de alterações | Estado depende de documento e view; consultar estado e depois alterar com operação idempotente MCP. |
| `.uno:AcceptTrackedChanges` | `SfxBoolItem AcceptTrackedChanges FN_REDLINE_ACCEPT` em `sw/sdi/swriter.sdi:37` | Ação de aceitação/global conforme contexto | Alto impacto; separar aceitação em lote de alteração individual e exigir precondição. |
| `.uno:AcceptTrackedChange` | slot `FN_REDLINE_ACCEPT_DIRECT` em `:8043-8044`, inclui variante com `SfxUInt32Item` | Aceitar revisão específica | Validar argumento/index e estado da lista; evitar assumir índice estável. |
| `.uno:RejectTrackedChange` | `FN_REDLINE_REJECT_DIRECT` em `:8118-8119`, com variante `SfxUInt32Item` | Rejeitar revisão específica | Operação destrutiva/semântica; indexar snapshot e verificar autor/texto/contexto. |
| `.uno:AcceptTrackedChangeToNext`, `.uno:RejectTrackedChangeToNext` | slots em `:8210,8228` | Rever uma a uma com cursor em revisão | Cursor/seleção afetam o resultado. |
| `.uno:TrackChangesInThisView`, `.uno:TrackChangesInAllViews` | slots em `:8668,8685` | Controlar visualização das revisões | Estado de view; não confundir com habilitar gravação de redlines. |
| `.uno:InsertFootnote` | `FN_INSERT_FOOTNOTE` em `:3104` | Inserir nota de rodapé | Cursor e configuração de numeração são parte do contrato. |
| `.uno:InsertTable` | `FN_INSERT_TABLE` em `:3749`, item `SfxUInt16Item` | UI de inserção de tabela | Para ferramenta programática, `XTextTable` costuma ser mais preciso e evita diálogo. |
| `.uno:UpdateAllIndexes` | `FN_UPDATE_TOX` em `:6794` | Atualizar índices/TOCs | Pode alterar paginação e conteúdo derivado; prever latência e revisão. |

Os nomes de URL devem ser confirmados pelo `XDispatchProvider` da frame/modelo e pelo ambiente ativo antes de chamar. A tabela de cima reúne os slots de alto interesse identificados por `rg`; não substitui ler toda a `.sdi` e validar cada handler/argumento.

## Como invocar com segurança

Cliente externo deve pedir dispatch ao `XDispatchProvider` associado à frame/controlador atual, ou usar `com.sun.star.frame.DispatchHelper` apenas com comandos previamente permitidos. Não chamar `SwUnoModule` como se fosse roteador genérico: `sw/source/uibase/uno/unomodule.cxx:46-85` usa `GetSlot(aURL.Complete)` e executa sincronamente o slot contra o frame atual. `addStatusListener` e `removeStatusListener` desse módulo estão vazios em `:93-103`.

O teste concreto usou `DispatchHelper.executeDispatch(frame, ".uno:Bold", "", 0, tuple())` após selecionar dez caracteres com `XController.select`. `CharWeight` do range passou de `150.0` para `100.0`. Isso verifica execução e efeito de Bold na build de sistema, mas não autoriza dispatch de URLs arbitrárias.

## Modelo MCP para dispatch

- Tool de alto nível com enumeração de ações (`toggle_bold`, `accept_change`, `update_indexes`), nunca `dispatch(url, args)` irrestrito para o modelo.
- Cada ação declara documento alvo, estado esperado, argumentos tipados, impacto e se opera sobre seleção ou documento inteiro.
- Resolve a dispatch na frame correta; valida que foi encontrada; consulta estado quando disponível; executa; relê o documento para confirmar efeito.
- Para ações que não devolvem resultado confiável, confirmar observando estado do modelo. Não interpretar ausência de exceção como efeito confirmado.
- `XStatusListener` não garante notification por especificação: `offapi/com/sun/star/frame/XStatusListener.idl:24-28`. Preferir `XNotifyingDispatch` para resultado de uma ação e ler novamente o estado.
- Lista de allowlist versionada por versão do LibreOffice; unknown command retorna `UNSUPPORTED_COMMAND`, com URL original não executada.

## Próximo trabalho no inventário

Na implementação, extrair todos os nomes/assinaturas da `.sdi`, mapear `FN_*` para métodos `Exec...`/handlers, gerar lista estática dos XML de configuração e validar um subconjunto por runtime com `queryDispatch`. Gravar `slot`, command URL, item schemas, shell/context, `QueryState`, argumento, resultado e teste. Incluir `action-name` e `.uno:` em valores/arquivos que não são `xlink:href`. O número 653 não deve aparecer como cobertura integral.
