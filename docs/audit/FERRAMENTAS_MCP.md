# Catálogo proposto de ferramentas MCP

Os nomes e schemas são proposta, ainda não implementados. São tools de alto nível e restringem o comportamento do agente. Nenhuma ferramenta recebe nome de método UNO, macro ou URL dispatch arbitrária.

## Contratos comuns

- `document_id`: handle opaco emitido pelo servidor MCP.
- `expected_revision`: contador/hash de snapshot emitido pelo MCP; obrigatório em mutações dirigidas a texto/estrutura.
- `anchor`: bookmark MCP ou referência estrutural com contexto/hash; não usar offset como ID persistente.
- `max_chars`/`page_token`: limites de leitura e paginação.
- Mutação devolve `revision`, `changed`, resumo e, quando aplicável, `undo_available`.
- Ferramentas que guardam/exportam pedem destino autorizado; tools que fecham/aceitam mudanças em lote pedem confirmação conforme política do host.

## Sessão e descoberta

### `writer_get_state`

Entrada: `{}`  
Saída: estado da ligação, versão do runtime, número de documentos Writer, documento ativo e capabilities. Não retorna caminho completo de documentos sem necessidade.

### `writer_list_documents`

Entrada: `{ "include_read_only": true }`  
Saída: lista de `{document_id,title,url_display,read_only,dirty,revision,active}`. URLs locais podem ser mascaradas.

### `writer_select_document`

Entrada: `{"document_id":"doc_…","activate_window":false}`  
Saída: documento selecionado no contexto MCP; `activate_window` altera foco visual e é separado.

### `writer_open_document`

Entrada: `{ "path_or_url": "file:///…", "read_only": true }`  
Saída: `document_id` e metadata. Path allowlist, scheme check e macro policy antes do load.

### `writer_close_document`

Entrada: `{ "document_id":"doc_…", "discard_changes":false }`  
Saída: fechado ou `CONFIRMATION_REQUIRED`. Não fecha sessão/proc e não descarta sem consentimento explícito.

## Leitura, estrutura e seleção

### `writer_read_document`

Entrada: `{ "document_id":"doc_…", "scope":"body", "max_chars":20000, "page_token":null }`  
Saída: texto/chunks, revision, hash, indicação de truncamento e metadados dos objetos que o texto plano não representa.

### `writer_read_structure`

Entrada: `{ "document_id":"doc_…", "include_tables":true, "include_fields":true }`  
Saída: árvore por heading/paragraph/table/frame/bookmark, com referências de alvo MCP e contexto.

### `writer_read_selection`

Entrada: `{ "document_id":"doc_…", "include_text":true }`  
Saída: `kind=text|table|frame|graphic|shape|none`, texto/células selecionadas, anchor e revision. Sempre lida no estado atual.

### `writer_get_paragraph`

Entrada: `{ "document_id":"doc_…", "paragraph_ref":"p_…", "expected_revision":42 }`  
Saída: texto, estilo, propriedades permitidas, posição estrutural, hash, referências antes/depois. Se ambiguamente resolvido, erro.

### `writer_search`

Entrada: `{ "document_id":"doc_…", "query":"…", "scope":"body", "case_sensitive":false, "limit":100 }`  
Saída: matches com anchors e contexto, não ranges persistidos cegamente.

## Escrita e formatação

### `writer_insert_text`

Entrada: `{ "document_id":"doc_…", "anchor":"…", "text":"…", "expected_revision":42 }`  
Saída: nova revision e range criado. Escapa/valida control characters e tamanho.

### `writer_replace_range`

Entrada: `{ "document_id":"doc_…", "anchor":"…", "expected_text":"…", "replacement":"…", "expected_revision":42 }`  
Saída: `{changed:true,revision:43,undo_available:true}`; divergência devolve `CONFLICT` e nunca substitui silenciosamente.

### `writer_delete_range`

Entrada: mesmo modelo de replace, sem replacement. Trata como destrutiva e pode exigir confirmação.

### `writer_apply_format`

Entrada: `{ "document_id":"doc_…", "anchor":"…", "expected_revision":42, "properties":{"CharWeight":150,"CharPosture":"NONE"} }`  
Saída: propriedades aplicadas/relidas. Allowlist com tipos e unidade; nunca aceitar property names irrestritos.

### `writer_apply_style`

Entrada: `{ "document_id":"doc_…", "anchor":"…", "family":"ParagraphStyles", "style_name":"Heading 1", "expected_revision":42 }`  
Saída: estilo final e revision. Nome pode variar por template/locale; pode listar estilos primeiro.

### `writer_create_heading`

Entrada: `{ "document_id":"doc_…", "text":"Metodologia", "level":1, "after":"…", "expected_revision":42 }`  
Saída: referência de parágrafo criada, estilo e índice/outline recalculado quando requerido. Verifica template antes de usar título.

### `writer_edit_batch`

Entrada: `{ "document_id":"doc_…", "expected_revision":42, "operations":[...], "batch_id":"…" }`  
Saída: resultados por operação, revision final e status `complete|partial`. Prevalidar tudo e agrupar undo; não anunciar atomicidade.

## Tabelas, notas e referências

### `writer_create_table`

Entrada: `{ "document_id":"doc_…", "anchor":"…", "rows":3, "columns":2, "data":[[...]], "expected_revision":42 }`  
Saída: `table_ref`, dimensões, nomes de célula e revision.

### `writer_update_table`

Entrada: `{ "table_ref":"t_…", "expected_revision":42, "cells":[{"cell":"A1","text":"…"}] }`  
Saída: alterações relidas. Células mescladas/estrutura complexa têm retorno tipado.

### `writer_insert_footnote`

Entrada: `{ "document_id":"doc_…", "anchor":"…", "text":"…", "kind":"footnote"|"endnote", "expected_revision":42 }`.

### `writer_add_annotation`

Entrada: `{ "document_id":"doc_…", "anchor":"…", "author":"…", "content":"…", "expected_revision":42 }`  
Saída: annotation ref. No MVP, declarar “anotação” e não prometer replies/threading.

### `writer_refresh_indexes`

Entrada: `{ "document_id":"doc_…", "expected_revision":42, "index_refs":[...] }`  
Saída: índices atualizados e nova revision. Pode repaginar o documento.

## Revisões, undo e exportação

### `writer_list_tracked_changes`

Entrada: `{ "document_id":"doc_…", "page_token":null }`  
Saída: redlines com autor, data, tipo, texto/contexto e snapshot revision, quando disponíveis.

### `writer_apply_revision`

Entrada: `{ "document_id":"doc_…", "redline_ref":"r_…", "decision":"accept"|"reject", "expected_revision":42 }`  
Saída: estado após reread. Confirmar alvo e decisão individual; global accept/reject é outra tool de maior privilégio.

### `writer_undo` / `writer_redo`

Entrada: `{ "document_id":"doc_…", "expected_revision":42 }`  
Saída: novo snapshot e label de undo/redo. Undo partilhado com ações do utilizador, portanto consulta estado e avisa exatamente qual ação será afetada.

### `writer_save_document`

Entrada: `{ "document_id":"doc_…", "expected_revision":42, "overwrite":false }`  
Saída: URL mascarada, filter e estado modified. Separar “Save” do “Save As”.

### `writer_export_document`

Entrada: `{ "document_id":"doc_…", "format":"pdf"|"odt"|"docx", "destination":"…", "overwrite":false }`  
Saída: path autorizado, formato/filter e warnings. Sem macros arbitrárias nem acesso ao filesystem irrestrito.

## Eventos e interface

### `writer_subscribe_events`

Entrada: `{ "document_ids":["doc_…"], "events":["document_dirty","selection_changed"], "include_selection_text":false }`  
Saída: subscription handle e event sequence. `document_dirty` indica reread; não representa uma única alteração.

### `writer_get_event_changes`

Entrada: `{ "document_id":"doc_…", "after_sequence":123 }`  
Saída: eventos MCP coalescidos e snapshots. Não usar nome “changes” para alegar delta nativo quando só existe invalidação; ou renomear para `writer_get_event_snapshot` no release.

### `writer_focus_range`

Entrada: `{ "document_id":"doc_…", "anchor":"…", "select":true, "scroll_into_view":true }`  
Saída: seleção e view aplicadas; inclui `focus_changed` pois rouba foco.

## Erros MCP

Formato: `{"error":{"code":"CONFLICT","message":"O trecho mudou desde a leitura.","retryable":true,"current_revision":43,"details":{"current_text":"…"}}}`.

Mapear exceções UNO para códigos estáveis; omitir tracebacks do cliente normal. Para crash/timeout durante mutação responder `UNKNOWN_OUTCOME`, reler antes de retry e usar `batch_id` para evitar duplicação.
