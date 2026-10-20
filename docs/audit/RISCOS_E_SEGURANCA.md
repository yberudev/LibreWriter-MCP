# Riscos, segurança, desempenho e fiabilidade

| Risco | Cenário | Controles recomendados |
|---|---|---|
| Corrupção/perda de dados | Escrita em range errado, falha no meio de lote, crash LO, conversão de formato | Re-resolver anchor, precondition, revision/hash, undo context, save snapshot quando autorizado, export para novo arquivo, reler depois de gravar, distinguir `UNKNOWN_OUTCOME`. |
| Conflito com utilizador | O utilizador edita/seleciona enquanto modelo propõe alteração | Expected revision e expected text, conflito visível, nunca retarget automático, operações MCP pequenas, não travar UI. |
| Referência UNO obsoleta | Documento fechado ou processo reiniciado; proxy antigo | Registry de lifecycle; apanhar `DisposedException`; invalidar proxies; reconnect com backoff; redescobrir documento. |
| Deadlock/UI congelada | Operações síncronas lentas ou callback UNO reentrante | Worker serial, callbacks leves, timeouts de aplicação, chunk/paginação, evitar locks cruzados e chamadas UNO dentro de listener. |
| Exposição de socket UNO | Bridge TCP acessível pela rede local/internet | Bind explícito `127.0.0.1`, portas dinâmicas e firewall; não usar `0.0.0.0`. UNO não fornece autenticação adequada como gateway multiuser. |
| Acesso filesystem arbitrário | Agente abre/guarda/exporta caminhos sensíveis | Allowlist de diretórios, validação de scheme/realpath, negar `http`, macro/document links externos por padrão, export path aprovado. |
| Dispatch/macro não autorizados | Tool aceita `.uno:` ou macro string genérica | Ações enumeradas + allowlist por versão; nunca expor `RunMacro` nem nome arbitrário; validar argumentos/contexto/efeito. |
| Prompt/autocontent malicioso | Documento externo contém conteúdo que tenta instruir agente ou macro | Conteúdo do documento é dado não confiável; política de macros em nível LO; revisão humana para ação externa; sem seguir instruções embutidas como código de tool. |
| Alteração destrutiva de revisões | Aceitar/rejeitar redlines em lote ou remover comentários | Ferramentas individuais com snapshot de redline, confirmação de lote, backup/revisão e undo; nunca aceitar global sem autorização explícita. |
| Conversão e formato | DOCX/PDF import/export perde fields, estilos, comments, objetos | Filtros explícitos; guardar em novo arquivo; warning de conversão; corpus versionado de round-trip por LO release. |
| Alpha e incompatibilidade | Wrapper 26.8 alpha difere de PyUNO 24.2 e checkout | Fixar release binário e binding da mesma distribuição; capability detection; CI por versões suportadas; não misturar bibliotecas. |
| Privacidade | Retorno de texto integral/seleção ou logs de documento | Tools paginadas e scopes mínimos; não logar conteúdo por padrão; mascarar URL; consentimento para enviar texto a modelo remoto. |
| Pressão de memória/desempenho | Releitura do documento inteiro a cada XModify | Coalesce, cache/hash, chunks, outline/selection reads localizados, limite de conteúdo, operação de snapshot sob demanda e medição com monografia extensa. |
| Layout demorado | Paginação, índices ou imagens recalculam layout | Timeout consciente, não executar no callback, UI ainda responsiva; status operation progress e cancelamento pré-execução. |
| Concorrência de sessões | Dois processos partilham mesmo profile/porta | Profile único por instância; reservar porta; launcher ownership; não terminar processo alheio. |
| Undo compartilhado | MCP desfaz a última ação do utilizador | Requer document/revision check e label do topo quando disponível; operações MCP em contexts; não chamar undo global se a ação topo não for MCP. |

## Desempenho esperado por padrão

Uma leitura integral por solicitação custa aproximadamente proporcional ao número de caracteres/objetos e pode gerar cópias PyUNO. Para primeiro MVP, leitura integral com `max_chars` é aceitável em documentos pequenos; para monografias adotar paginação por chunks, outline cacheado e leitura localizada por âncora. XModify deve invalidar um cache e ser coalescido, não desencadear dump integral em cada keystroke. O event loop UNO não tem garantia de fornecer delta, logo o MCP mede/deriva snapshot.

## Limites operacionais recomendados

- Limite inicial de tamanho por retorno e batch (configurável, documentado).
- Um documento por operação; múltiplos documentos em batch exigem ordem e rollback explícitos.
- Timeout separado de conexão, leitura e mutação; declarar resultado desconhecido após perda de bridge.
- Uma única operação de escrita em voo por documento; tool cancelada não interrompe chamada nativa em curso.
- Auto-save/backup e export path são políticas do host; não criar cópia silenciosa em diretórios pessoais.
- O listener pertence à sessão configurada e não é mecanismo de autenticação: proteger processo, porta e host.
