# Eventos e sincronização em tempo real

## O que existe

| Interface/fonte | Notifica | Payload | Implementação/evidência | Adequação |
|---|---|---|---|---|
| `util.XModifyListener` no modelo | Uma alteração genérica no objeto | `EventObject` com source | `SfxBaseModel::addModifyListener` em `sfx2/source/doc/sfxbasemodel.cxx:1469`; PoC recebeu 36 chamadas em mutações temporárias | Bom para invalidar cache; insuficiente como ChangeSet. |
| `view.XSelectionChangeListener` | Alteração da seleção da view | `EventObject`; cliente consulta `XSelectionSupplier` | Registo em `sw/source/uibase/uno/unotxvw.cxx:431-443`, notify em `:737-744`; views chamam em `view.cxx:538,622` e `wview.cxx:280` | Útil para UI; seleção pode ser range, tabela, objeto ou shapes. |
| `document.XDocumentEventBroadcaster` | Eventos de lifecycle/nomeados de documento | `DocumentEvent` | Interface herdada pelo modelo Sfx; implementação em `sfxbasemodel.cxx:2598+` | Bom para eventos de documento, não substitui eventos de cada tecla/edição. |
| `document.XUndoManagerListener` | Eventos do histórico/undo | Ações de undo context/estado | `XUndoManagerSupplier` está presente no documento; API em offapi | Bom para estado de revisão; evento granular de conteúdo não presumido. |
| `frame.XStatusListener` | Suposto estado de comando | `FeatureStateEvent` | IDL diz que não há garantia de notificação (`offapi/com/sun/star/frame/XStatusListener.idl:24-28`); `SwUnoModule::addStatusListener` é vazio (`unomodule.cxx:93-103`) | Não usar como fonte de verdade/stream. |
| Listener de propriedade em `SwXParagraph` | Mudanças de propriedade do parágrafo | PropertyChangeEvent esperado | `unoparagraph.cxx:837-850` chama `OSL_FAIL("...not implemented")` | Limite específico e confirmado da implementação observada. |

`XModifyListener.idl:25-37` só especifica que o modelo mudou; não inclui range, texto antigo/novo, tipo de operação ou autor. `XSelectionChangeListener.idl:25-38` também aponta para consultar a nova seleção via fonte do evento. Esses são avisos de invalidar e reler, não log de mudanças reexecutável.

## Resultados de runtime

1. A PoC UNO headless teve **36 callbacks XModify** durante criação, formatação, edição, tabela, anotação e save round-trip. Os callbacks observados foram entregues na thread `MainThread`.
2. A PoC anterior de GUI isolada teve **7 callbacks** enquanto o script enviava texto por X11 ao Writer visível. Python reportou callback em `Dummy-2`. A GUI também gerou callbacks de seleção ao selecionar texto e navegar.
3. A diferença de thread entre modos e contextos confirma que o callback precisa ser tratado como vindo de thread de UNO. Não chamar tool MCP, bloquear, nem executar uma mutação UNO complexa dentro do listener.
4. O evento não identificou se a causa foi digitação, comando, formatação, auto-save, importação, undo ou alteração feita pelo MCP. O volume depende da operação.

Os resultados aplicam-se à instalação de sistema LO/PyUNO 24.2.7. Não foram medidos payloads de cada implementação de XDocumentEvent/XUndoManagerListener nem a latência em documento extenso.

## Estratégia recomendada

### Fase sem patch

1. Registar listener por documento; ao chegar `modified`, definir `dirty=true`, guardar timestamp/contador monotónico local e notificar um `queue.Queue` thread-safe.
2. Coalescer eventos repetidos durante pequeno intervalo para reduzir chamadas de leitura.
3. O worker serializado verifica se proxy/documento ainda está válido, lê `isModified`, selection atual e conteúdo/estrutura necessária; cria nova revisão local.
4. Fornecer MCP event `document_dirty` ou `selection_changed` como aviso; operações de leitura devolvem snapshot e `revision` calculada pelo MCP.
5. Não alegar que cada evento corresponde a uma alteração única. Snapshot é a fonte de verdade.
6. Em reconexão, eliminar todos proxies UNO e refazer enumeração; invalidar versões locais e emitir evento `session_reconnected`.

### Contrato de revisão para escrita

O cliente envia `document_id`, `expected_revision`, `anchor` e `expected_text` (ou hash/contexto). O worker re-resolve âncora e compara snapshot. Se divergir, responde `CONFLICT` com trecho atual, não sobrescreve. O ensaio concorrente simulado demonstrou a regra de guarda do lado do MCP; não demonstra uma transação atómica UNO nem conflito de duas threads reais.

### Quando propor mudança no núcleo

Se a especificação exigir transmissão exata de alterações e não apenas estado atualizado, propor `XDocumentChangeBroadcaster` ou equivalente com `revision`, `ChangeKind`, offsets/range âncora, conteúdo antigo/novo, source (`user`, `uno`, `undo`, `import`) e lifecycle de IDs. Deve cobrir edição textual, formatação, tabelas, desenho, fields, undo/redo, import e remoção. A fonte principal do estado são mudanças no `SwDoc` e os seus document managers; `SwPaM`/`SwTextNode` são candidatos para mapping de ranges, mas são detalhes internos frágeis. Criar listener apenas no wrapper de parágrafo não captura todas as categorias.

Antes do patch, implementar snapshots e medir custos. Para monografias, um evento UNO genérico pode invalidar só o outline ou a seleção; leitura integral para cada evento pode ser cara. Cachear text/structure hash e buscar trecho apenas quando há evento de seleção, edição MCP ou idle debounce.

## Threading e deadlocks

- PyUNO chamadas síncronas podem bloquear até resposta do LibreOffice. Definir timeout/health check ao redor do cliente e cancelar requisição pendente sem deixar lock MCP preso.
- Usar uma única fila serial para chamadas que alteram documento e operações de leitura dependentes do estado, para impedir interleaving criado pelo próprio servidor.
- Callback listener não faz chamada para o worker de forma síncrona; apenas enqueue sem bloquear.
- Não manter lock da sessão enquanto aguarda resposta de ferramenta externa ou aprovação humana.
- Evitar chamar métodos UNO dentro de callback que o Writer disparou sob SolarMutex; essa reentrada pode travar ou criar deadlock.
- Na saída, remover listeners quando possível, fechar só recursos pertencentes à sessão MCP e tratar `DisposedException`/perda de bridge como reconexão.
