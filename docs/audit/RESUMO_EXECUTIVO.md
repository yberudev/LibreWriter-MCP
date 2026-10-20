# Resumo executivo — Yberu Writer MCP

## Respostas diretas

1. **Até onde controlamos sem patch?** A maior parte do modelo documental Writer: texto, ranges, estilos, tabelas, campos, notas, bookmarks, frames, objetos, índices, metadados, undo/redo, store/export, controller, seleção e comandos dispatch. A PoC cobriu o caminho base com Writer 24.2.7. Não é controle literal de cada diálogo, objeto externo ou evento interno.
2. **O que já existe em UNO?** `XTextDocument`, `XText`/`XTextRange`/`XTextCursor`, `XPropertySet`, suppliers de styles/tables/frames/fields/bookmarks/notes/indexes/redlines, `XStorable`, `XCloseable`, `XUndoManagerSupplier`, `XSelectionSupplier`, `XTextViewCursorSupplier` e eventos básicos. O objeto Writer real reportou 111 tipos UNO na PoC.
3. **O que depende de dispatch?** Comandos que espelham ações da UI como aceitar/rejeitar mudanças registadas, atualizar índices e certas operações de view. `.uno:Bold` foi executado e o efeito medido. Cada comando requer contexto, argumentos e lista permitida; dispatch não é API arbitrária segura.
4. **O que exige extensão?** Integração da sessão (launcher/bootstrapping), painel e comandos próprios, ligação com serviços como Zotero, e ferramentas UI que não têm API UNO direta. A extensão não substitui um evento nativo de cada mutação do modelo.
5. **O que exige C++?** Para a exigência de **deltas semânticos garantidos** (range, conteúdo antigo/novo, causa e versão) não foi encontrada interface pública equivalente; `XModifyListener` só traz `EventObject`, e listeners de alteração de propriedades do parágrafo não estão implementados. Um ID nativo estável para cada parágrafo também não foi encontrado. Isto torna-se patch apenas se bookmarks/âncoras e snapshots não cumprirem os requisitos de produto.
6. **Precisamos de fork?** Não para começar. Um fork só se justifica depois de provar que um contrato novo do núcleo é requisito, e que não pode ser mantido como extensão upstream ou patch estreito.
7. **Como fazer tempo real?** `XModifyListener` para invalidar/coalescer snapshots; `XSelectionChangeListener` para saber quando reler seleção; worker serializado para todas as chamadas UNO. Eventos não são stream de deltas. Callbacks são leves e põem dados numa fila.
8. **Melhor arquitetura inicial?** MCP Python + PyUNO diretamente no venv, conectado por localhost a uma instância explícita com listener, com sessão UNO e fila serializada num worker. Sem bridge Python extra no ambiente testado.
9. **Primeiros componentes?** Gestor de sessão/reconexão, catálogo de documentos, leitura/estrutura, edição com precondições/versionamento, estilos/tabelas, undo, eventos básicos e logs de auditoria.
10. **Caminho eficiente?** Entregar uma versão sem patch usando UNO; acrescentar dispatch tipado para lacunas de ação; adicionar extensão para integração de arranque/UI; medir os gaps; só então desenhar ChangeSet/ID nativo e patch C++ se necessário.

## Contagem da auditoria

Foram classificados **66 itens** na matriz CSV: **A 56**, **B 2**, **C 5**, **D 2**, **E 1**. Esta contagem representa capacidades inventariadas, não testes executados; a coluna `Estado` diferencia PoC, evidência de fonte/API, parcial e desconhecido.

## Estado do ambiente

- Venv Python 3.12.3: MCP SDK 2.3.0 e PyUNO importam no mesmo processo.
- Runtime testado em PoCs recentes: LibreOffice de sistema 24.2.7.2 e `python3-uno` 24.2.7.
- O `soffice` do PATH é wrapper para outro runtime headless, LibreOfficeDev 26.8 alpha. A versão não corresponde ao pacote PyUNO do sistema nem ao commit do checkout; requer validação de compatibilidade antes de escolher runtime final.
- Checkout `core-git`: `2431868f2abd904d682010175ea966e9b78bc2d6`, branch master, limpo.

## Principais riscos

- Eventos são genéricos e podem chegar noutra thread.
- A sessão precisa estar configurada para aceitar UNO; listener de rede deve ficar restrito a localhost.
- Mudanças do utilizador entre leitura e escrita pedem precondição e re-resolução da âncora.
- PyUNO e LibreOffice devem ser distribuídos em versões compatíveis.
- Dispatch pode alterar a janela ativa; limitar comandos e validar argumentos.
- Exportação DOCX/PDF e objetos OLE requerem verificação por formato/componente.

## Relatórios

Todos os ficheiros ficam em `MCPProj/docs/audit/`. O relatório principal é [`AUDITORIA_COMPLETA.md`](AUDITORIA_COMPLETA.md); matriz em [`MATRIZ_CAPACIDADES.csv`](MATRIZ_CAPACIDADES.csv); PoCs e resultados em [`PROVAS_DE_CONCEITO.md`](PROVAS_DE_CONCEITO.md).

## Próximo passo recomendado

Implementar a **Etapa 1** do plano: um protótipo MCP pequeno com `writer_get_state`, `writer_list_documents`, `writer_read_selection`, `writer_read_structure`, `writer_insert_text` e `writer_replace_range` sob precondição `expected_revision`. Antes de uma sessão GUI habitual, decidir o mecanismo de bootstrap do listener. Ver [`PLANO_IMPLEMENTACAO.md`](PLANO_IMPLEMENTACAO.md).
