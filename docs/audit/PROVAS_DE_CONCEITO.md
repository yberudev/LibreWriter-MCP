# Provas de conceito

Todas as PoCs usam documentos temporários. As scripts que iniciam LibreOffice utilizam `/usr/bin/soffice`, perfil `UserInstallation` privado, listener em `127.0.0.1` e limpam apenas processos iniciados pelo próprio script. Nenhum documento de trabalho foi aberto ou alterado. Scripts em [`poc/`](poc/).

## Como repetir

Na raiz do projeto Writer:

```bash
MCPProj/.venv/bin/python MCPProj/docs/audit/poc/uno_document_probe.py
MCPProj/.venv/bin/python MCPProj/docs/audit/poc/reconnect_probe.py
MCPProj/.venv/bin/python MCPProj/docs/audit/poc/gui_typing_probe.py
```

`gui_typing_probe.py` cria uma janela GUI isolada e envia teclado para a janela do seu grupo de processo identificado por `wmctrl`; ela toma foco da tela durante o ensaio. Deve ser executada apenas quando se pode permitir essa janela temporária. Nenhum teste usa o wrapper 26.8, pois o PyUNO carregado é 24.2 do sistema.

## Experiência UNO integrada

**Script:** `poc/uno_document_probe.py`  
**Comando executado:** `MCPProj/.venv/bin/python MCPProj/docs/audit/poc/uno_document_probe.py`  
**Runtime:** `/usr/bin/soffice` 24.2.7.2, headless, perfil temporário, socket localhost, Python venv 3.12.3 / python3-uno 24.2.7.

Saída observada:

```text
created_writer_document=OK
document_component_count= 1
document_text_readable= True
paragraph_style= Heading 1
selection_length= 5
dispatch_bold_weight_before_after= 150.0 100.0
undo_redo= True True True
optimistic_conflict_detected= True
stale_write_blocked= True
table_cells= ('A1', 'B1', 'A2', 'B2')
annotation_authors= ['Yberu audit']
odt_saved= True 11906
odt_roundtrip_text= True
odt_roundtrip_table_count= 1
modify_event_count= 36
modify_event_threads= ['MainThread']
writer_uno_type_count= 111
has_XUndoManagerSupplier= True
has_XDocumentEventBroadcaster= True
```

O tamanho do ODT pode variar ligeiramente entre execuções. O script cria texto, aplica `Heading 1` e `CharWeight`, seleciona uma range via controller, executa `.uno:Bold`, verifica o peso antes/depois, agrupa uma inserção num contexto de undo, faz undo/redo, simula uma alteração concorrente e recusa uma escrita com snapshot velho, cria tabela 2×2, insere e enumera `TextField.Annotation`, guarda em ODT, reabre e compara texto/tabelas, e lista tipos do componente.

**Limites:** a concorrência é simulada sequencialmente dentro de uma única sessão: primeiro leitura, depois alteração externa simulada, depois verificação da guarda. Não é teste de duas threads/escritores concorrentes. O listener XModify recebeu 36 notificações, mas o conteúdo do evento não foi interpretado como delta. A PoC não validou `RecordChanges`/redline, export PDF, round-trip DOCX ou thread de respostas a comentário.

## Digitação e seleção em GUI real isolada

**Script:** `poc/gui_typing_probe.py` (executada anteriormente nesta auditoria).  
**Método:** Writer GUI do sistema 24.2 com `SAL_USE_VCLPLUGIN` removida do ambiente, perfil temporário, janela encontrada pelo PID/process group próprio; `XTest` injeta texto ASCII; listener XModify e selection change acompanha o modelo.

Resultado registado:

```text
writer_window_visible=True
keyboard_input_content='livetestaudit'
modify_callbacks=7
callback_threads=['Dummy-2']
```

A sessão demonstrou GUI visível e texto digitado por entrada de teclado refletido no documento e acompanhado por notificação. O callback aconteceu numa thread Python diferente da main. A PoC anterior da seleção demonstrou callbacks e `getSelection()` para seleção textual com coleção `TextRanges` (`XIndexAccess`), sendo necessário ler o primeiro range. Seleções de tabela/shape/frame têm tipos diferentes conforme `SwXTextView::getSelection()`.

**Limites:** o teste não se anexou a um Writer já aberto pelo utilizador; criou a própria janela e tomou foco temporariamente. Não testou todos os backends gráficos nem acentos/IME/atalhos.

## Reconexão após restart

**Script:** `poc/reconnect_probe.py`  
**Comando executado:** `MCPProj/.venv/bin/python MCPProj/docs/audit/poc/reconnect_probe.py`

Saída observada:

```text
first_process_connection=OK
first_process_pid= 1606629
reconnected_after_restart=OK
second_process_pid= 1606892
new_document_readable= True
```

O script iniciou duas instâncias privadas sequencialmente com o mesmo perfil temporário e porta; terminou só o primeiro process group, esperou a nova instância ficar pronta e voltou a resolver o service manager. A PoC demonstra criar conexão nova depois de um encerramento do servidor UNO. Não demonstra restauração automática de documento não salvo: ao morrer processo, documento em memória foi perdido. Produção precisa de shutdown limpo e salvaguardas, e nunca encerrar processo que não iniciou.

## Testes observados antes da PoC integrada

- Importar `mcp.server.MCPServer` e `uno` no Python do venv; a dependência UNO vem do site packages do sistema via `include-system-site-packages=true`.
- Conectar o venv ao runtime do sistema 24.2 e criar/fechar documento Writer.
- Iniciar listener headless 24.2 sem `SAL_USE_VCLPLUGIN=gen`: permanece ativo. O problema de código 139 foi no runtime headless 26.8 via wrapper Codex. Com `SAL_USE_VCLPLUGIN=gen`, o wrapper 26.8 chegou a aceitar conexão e a criar/fechar Writer em PoC prévia, mas não certifica compatibilidade de todos os tipos UNO.
- GUI 24.2 funcionou sem forçar VCL genérico. Não herdar `SAL_USE_VCLPLUGIN=gen` para sessão gráfica por padrão.

## Mapa das 12 experiências solicitadas

| # | Experiência | Resultado |
|---:|---|---|
| 1 | Ligação e componentes | Ligação própria concluída; componente enumerado. |
| 2 | Estado/active document | Documento da PoC e tipos UNO consultados; active document externo não testado. |
| 3 | Texto, estilos e estrutura | Texto e estilos básicos; estrutura profunda/outlines não coberta. |
| 4 | Edição de parágrafo | Concluída em doc temporário. |
| 5 | Seleção | Concluída em GUI isolada; API retorna diferentes formas. |
| 6 | Formatação | Concluída para estilo e peso; não para todas propriedades. |
| 7 | Eventos | XModify headless e GUI verificados; payload é genérico. |
| 8 | Concorrência | Guarda optimista simulada; corrida real pendente. |
| 9 | Undo/redo | Concluída para sentinela isolada. |
| 10 | Persistência ODT | Concluída; texto e tabela voltaram. |
| 11 | Interface gráfica | Janela e digitação real isolada verificadas. |
| 12 | Reconexão | Concluída para restart de processo de teste próprio. |

## Resultados negativos/limites

- `uno.getTypeByName` não existe nesta instalação Python; introspecção correta usada foi `document.getTypes()`. Isso foi ajuste do harness, não falha do Writer.
- Uma tentativa anterior de construir `URLTransformer.parseStrict` com shape incorreto falhou por argumento da PoC. A chamada foi substituída por `DispatchHelper` e o efeito `.uno:Bold` foi verificado.
- Um primeiro acesso a células de uma tabela antes de inseri-la no documento gerou `Lost connection to core objects`; a ordem correta (criar, inicializar, inserir, depois aceder às células) veio confirmada por `sw/qa/python/check_table.py`. A script final passa.

## Provas que faltam antes de afirmar suporte de produção

Aplicar redlines e aceitar/rejeitar uma revisão; headers/footers e page styles; footnote/endnote; imagem com ancoragem; search/replace; PDF e DOCX round-trip; documento com centenas de páginas; mutações simultâneas reais; lifecycle de listener durante fechar documento; comportamento do runtime de distribuição final e de sessão GUI bootstrapada por extensão.
