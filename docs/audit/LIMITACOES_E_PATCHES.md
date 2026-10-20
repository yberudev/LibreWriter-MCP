# Limitações, alternativas e possíveis patches

## Limitações confirmadas e hipóteses separadas

| Necessidade | Limite/evidência | Alternativa antes de C++ | Patch possível, apenas se necessário | Risco/critério |
|---|---|---|---|---|
| Identificar cada mudança do utilizador com range, old/new e causa | `XModifyListener` só fornece `EventObject` (`offapi/com/sun/star/util/XModifyListener.idl:25-37`). | Snapshot debounce + hash/outline + selection; ferramentas MCP usam revisão optimista. | Novo broadcaster UNO em `offapi/com/sun/star/document/` e integração com notificações de mutação do `SwDoc`/document managers em `sw`; cobrir `undo`, fields, tables, drawing e import. `sfx2` pode suportar contrato de lifecycle. | Mudanças silenciosas ou notificações duplas. Criar teste CppUnit/API por operação/undo/import e stress de documento longo. |
| Mudanças de propriedades de parágrafo como evento | `SwXParagraph::addPropertyChangeListener`/remove têm `OSL_FAIL("not implemented")` (`sw/source/core/unocore/unoparagraph.cxx:837-850`). | Consultar propriedades sob demanda; aplicar operações por lote com snapshot. | Primeiro tentar implementar listener na classe wrapper se requisito se limita a propriedades expostas; não resolve mutação textual/estruturas não-parágrafo. | Contrato property listener deve obedecer XPropertyChangeListener, lifecycle e thread. Cobrir dispose e todas as propriedades. |
| ID imutável por parágrafo persistente em save/load | Nenhum ID público persistente encontrado; `SwNodes` e offsets são estrutura interna mutável. | Bookmark nomeado ou marcação de conteúdo controlada pelo MCP, resolver por nome/âncora/contexto e hash; manifest lateral opcional. | Novo UUID associado a node/text paragraph em `sw/inc/ndtxt.hxx`/gerência de IDs do `SwDoc`; definir cópia, split/merge, delete, paste, save/load e conversão. Possível API UNO no wrapper paragraph. | Identidade é ambígua em split/merge/copy e formatos de export. Definir semântica antes de patch; testar ODT/DOCX e undo. |
| Anexar a GUI normal que já está aberta sem `--accept` | UNO não oferece socket retroativo ao processo por defeito; esta auditoria não encontrou/experimentou listener ativo em sessão preexistente. | Launcher próprio ou perfil de arranque; user escolhe conectar, listener localhost. | Extensão UNO/serviço de sessão que inicia ponte com consentimento e shutdown correto. C++ não é primeira solução. | Compatibilidade multi-versão, UX de segurança e perfil existente. |
| Painel/toolbar/ação Yberu e lifecycle | API de documento não instala automaticamente UI. | Extensão Writer via APIs públicas de add-on/UI config e component registration. | C++ apenas se extensão não puder usar hook/document lifecycle necessário. | Deve sobreviver a atualização sem recompilar core; testar instalador e remoção. |
| Conteúdo de objeto OLE de outro aplicativo | Writer expõe objetos incorporados, mas modelo e capabilities pertencem ao componente. | Resolver `XModel`/API específica do componente ou abrir ferramenta do componente; unsupported se no provider. | Patch no componente proprietário, não necessariamente Writer. | Mudança pode não estar em `sw/`; não alegar controlo universal. |
| Respostas encadeadas a comentários Writer | A auditoria provou `TextField.Annotation` simples, não o modelo de thread/replies. | Investigar propriedades/services da build alvo, round-trip ODT/DOCX e UI; manter comments “single annotation” no primeiro release. | Se o núcleo não expuser semântica necessária: extensão pública ou nova interface anotação com parent/replies. | Evitar perder autor/data/discussão em conversão. Estado atual na matriz é E/desconhecido, não impossibilidade comprovada. |
| Diálogo/estado interno arbitrário | Dispatch executa comando; não garante que toda UI widget/control esteja modelada por command. | UNO acessibilidade/API pública do componente; UI automation externa só como fallback assistivo. | Extensão que exponha operação explícita; não criar interface C++ para imitar todos os widgets. | GUI automation frágil, dependente de locale/resolução/foco. |
| Garantia de atomicidade multi-step | UNO calls são sequência; undo context agrupa a história mas não é transação ACID. | Operações curtas, preconditions, snapshot, rollback por undo onde ownership é seguro. | API transacional nova no core apenas com requisito de produto e modelo de rollback. | Edição manual concorrente pode ocorrer entre chamadas. Testar falhas parciais e undo. |

## Quais itens realmente justificam C++?

Nenhum patch deve ser feito antes do protótipo UNO. A matriz assinala dois casos com exigência não coberta por API identificada:

1. ChangeSet nativo de alterações semânticas, se o produto necessita deltas exatos (código `D`, EVT-03).
2. Identificador nativo persistente por parágrafo, se bookmarks/manifest MCP forem insuficientes (código `D`, TXT-08).

As duas propostas são **condicionais**, ainda sem desenho final ou patch preparado. A cobertura parcial da notificação paragraph property poderia ser um pequeno patch isolado, mas não satisfaz o primeiro item. Adicionar um serviço de arranque/painel é normalmente caso de extensão `C`, não patch do núcleo.

## Proposta de desenho de ChangeSet, se aprovado tecnicamente

Interface a discutir, não existente nesta árvore:

```text
DocumentChange {
  documentId, revision, origin, kind,
  anchorBefore, anchorAfter,
  oldValue?, newValue?, affectedObjectId?, timestamp
}
```

Semântica que o projeto precisa fechar antes de alterar C++: offsets em UTF-16 vs posição UNO; comportamento em `split/merge`; origem do input; alterações agrupadas; redline versus texto aceite; formato importado; views múltiplas; ordering; backpressure; listener disposal. Uma fila limitada pode condensar eventos em `document_invalidated` quando excede capacidade, para não atrasar o Writer.

### Candidatos de ficheiros/módulos para investigação (não é lista de patch garantida)

- IDL pública e documentação: `offapi/com/sun/star/document/` e possível build/API test.
- Wrapper/modelo: `sw/inc/unotxdoc.hxx`, `sw/source/uibase/uno/unotxdoc.cxx`, `include/sfx2/sfxbasemodel.hxx`, `sfx2/source/doc/sfxbasemodel.cxx`.
- Caminho de texto/posição: `sw/inc/pam.hxx`, `ndtxt.hxx`, `doc.hxx`, `sw/source/core/crsr/`, `sw/source/core/doc/`, `sw/source/core/unocore/`.
- QA: `sw/qa/api/`, `sw/qa/python/`, `sw/qa/uibase/` e CppUnit de evento/lifecycle. Confirmar alvo exato após tracing de uma mutação específica.

Não se afirma que um único hook em `SwDoc::SetModified` baste: alterações e origem podem passar por managers e formatos diferentes; tracing e testes são requisito anterior ao patch.

## Comparação das estratégias

| Estratégia | Alcance | Custo e compatibilidade | Decisão |
|---|---|---|---|
| A — MCP externo + UNO | Grande cobertura documental, seleção e comandos públicos | Menor manutenção; depende de binários compatíveis e listener configurado | **Começar aqui.** |
| B — MCP + extensão | Bootstrap, painel, commands próprios, integração com API de componentes | Mantém core intacto; teste por versão e packaging de extensão | **Adicionar se UX/ligação exigir.** |
| C — Patch C++ delimitado | Eventos/IDs que a API pública não oferece | Custo de build/QA/portabilidade; upstreaming ou manutenção de patch | **Só após gap medido e requisito estável.** |
| D — Fork do LibreOffice | Controle de qualquer módulo e instalação própria | Custo contínuo de rebase, QA, acessibilidade, segurança e formatos; usuário tem de usar distribuição própria | **Não recomendado nesta fase.** |

## Compilação e compatibilidade

O ambiente ainda não possui ferramentas autotools encontradas na inspeção, não há árvore `instdir` construída e não há configure concluído. Não foi executado build integral. Depois de um desenho C++ concreto, instalar/configurar toolchain seguindo as instruções do projeto, fazer build incremental de componente e suite Writer relevante, e medir se há dependência de rebuild maior. Não assumir de antemão que um patch compila isoladamente ou que uma biblioteca produzível pode substituir o binário do sistema.
