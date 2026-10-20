# Plano de implementação do Yberu Writer MCP

As fases seguem a ordem solicitada. A complexidade pressupõe primeira versão de produto e os limites descobertos nesta auditoria.

## Etapa 1 — Bridge UNO persistente e sessões

- **Objetivo:** conectar/recuperar uma sessão LO configurada; identificar documentos Writer; gerir lifecycle e reconexão.
- **Dependências:** decisão de runtime compatível; definição de como iniciar a GUI com listener (launcher primeiro; extensão opcional).
- **Ferramentas:** UNO resolver, PyUNO no venv, worker serial, process/profile registry, state tools.
- **Complexidade:** média.
- **Riscos:** listener sem autenticação, sessão já aberta sem listener, proxy obsoleto, GUI focus.
- **Critérios:** connect/list/disconnect sem afetar processo externo; reinício controlado e recuperação; teste GUI visível; capability report por runtime.
- **QA:** repetir `reconnect_probe.py`, testar session shutdown limpo, listener timeout, porta ocupada, documentos múltiplos.

## Etapa 2 — Operações documentais acessíveis por UNO

- **Objetivo:** read/write text, structure, styles, table, metadata, search, notes, save/export.
- **Dependências:** Etapa 1; schemas estáveis, `document_id`, revisão/preconditions e allowlists.
- **Ferramentas:** leitura chunked, anchors/bookmarks MCP, text/style/table/reference adapters, save/export.
- **Complexidade:** alta.
- **Riscos:** stale ranges, estruturas complexas, unidade e locale, ODT/DOCX diferenças.
- **Critérios:** cada mutation confirma a precondition, devolve nova revision, undo agrupado quando possível; reabertura confere texto/estrutura.
- **QA:** PoCs e casos de `sw/qa/python`; documentos aninhados, Unicode, tabelas e head/footer; export ODT/DOCX/PDF.

## Etapa 3 — Dispatch tipado

- **Objetivo:** expor ações de UI que complementam API documental sem permitir comando arbitrário.
- **Dependências:** inventário de slots/argumentos por runtime; Etapas 1–2.
- **Ferramentas:** registry allowlisted; wrappers para atualização de índices, track changes, navegação, format toggles.
- **Complexidade:** média.
- **Riscos:** comando depende de frame/seleção, estados invertidos, ação destrutiva.
- **Critérios:** tool consulta o estado, chama comando validado, relê resultado; URL desconhecida não executa.
- **QA:** command query/dispatch por documento, args faltantes, view errada, sem seleção, documento read-only.

## Etapa 4 — Eventos, seleção, revisão e sincronização

- **Objetivo:** mudanças do utilizador geram estado novo e seleção atual para IA; conflito otimista.
- **Dependências:** serial worker e schemas revision/anchor.
- **Ferramentas:** XModify/selection listener, event queue, debounce, snapshots, track change APIs.
- **Complexidade:** alta.
- **Riscos:** callbacks em threads diferentes, flood, eventos genéricos, conflito não atómico.
- **Critérios:** UI nunca bloqueia por callback; nenhum evento presume delta; stale writes retornam CONFLICT; reconnect invalida revision e proxies.
- **QA:** digitação, paste, undo, troca de view, múltiplos docs, encerramento, stress event queue, concorrência GUI real.

## Etapa 5 — Funcionalidades académicas e monografias

- **Objetivo:** template, outline, títulos académicos, TOC, notas/referências, validação de estrutura.
- **Dependências:** operações confiáveis de estilo/índice/fields e política de provenance.
- **Ferramentas:** template inspection, create heading, reference record, bibliography, index refresh, validation report.
- **Complexidade:** alta.
- **Riscos:** norma académica variável, campos externos, referências inventadas, template locale.
- **Critérios:** perfil de instituição configurável; cada referência inclui provenance; regras são testáveis e não confundidas com capacidades nativas do Writer.
- **QA:** templates de amostra autorizados, bibliografia, captions, cross-references, paginação e export.

## Etapa 6 — Extensão LibreOffice opcional

- **Objetivo:** iniciar/configurar listener com consentimento, instalar painel/menu e ações Yberu.
- **Dependências:** Etapa 1 funcional; UX/segurança aprovadas; preferência entre launcher e extensão.
- **Ferramentas:** component registration, XJob/extension API, UI config/add-on, bootstrap handshake local.
- **Complexidade:** média/alta.
- **Riscos:** signing/install/update multi-platform, lifecycle de shutdown, compatibilidade UI.
- **Critérios:** opt-in claro, listener localhost, uninstall limpo, não abre acesso remoto adicional.
- **QA:** instal/update/remove em releases suportadas, perfil novo/existente, sessão headless/gui.

## Etapa 7 — Patches C++ apenas para lacunas demonstradas

- **Objetivo:** ChangeSet detalhado e/ou ID persistente se snapshots e bookmarks forem insuficientes.
- **Dependências:** medição de custo/latência e requisito formal; trace até ponto real de mutação; build configurado.
- **Ferramentas:** nova interface UNO versionada, implementação pontual `sw`/`offapi`/`sfx2`, CppUnit e integração.
- **Complexidade:** muito alta.
- **Riscos:** coverage incompleta de import/undo/drawing/fields, lock/thread, rebase de core, ABI.
- **Critérios:** contrato de eventos documentado; teste de cada categoria; fallback a invalidation; compatibilidade de saved docs; patch revisável sem fork se possível.
- **QA:** CppUnit/API/UIWriter, undo/redo, import/export ODT/DOCX, multi-view, stress, listener disposal e performance.

## Critério de início recomendado

Implementar já uma spike da Etapa 1 e poucas tools de Etapa 2 numa branch/projeto de MCP. Usar a distribuição 24.2 compatível com PyUNO para reproduzir as PoCs, até haver decisão de empacotamento para a versão source/master. Não iniciar build C++ antes do requisito de ChangeSet/ID ser aceite e haver um protótipo UNO medido.
