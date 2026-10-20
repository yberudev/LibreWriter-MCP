# Yberu Writer MCP

Servidor MCP stdio local para ler e modificar documentos abertos no LibreOffice Writer através de PyUNO.

## Requisitos validados

- Python 3.12.3.
- MCP SDK Python 2.3.0, com a API `mcp.server.mcpserver.MCPServer` inspecionada neste ambiente.
- PyUNO compatível com o LibreOffice de sistema.
- LibreOffice Writer 24.2.7.2, lançado por `/usr/bin/soffice`.
- Sessão gráfica X11 para o launcher e os testes GUI. O Writer não é iniciado em modo headless.

Estas são as versões efetivamente validadas. Não mistures PyUNO com outra instalação; o wrapper `soffice` no `PATH` pode apontar para LibreOfficeDev.

## Instalação

O projeto já contém `.venv`. Só executa a primeira linha se o ambiente ainda não existir; `--system-site-packages` é necessário para expor o PyUNO de sistema:

```bash
cd /home/yberudev/Documentos/MCP_Project/LibreOffice/Writer/MCPProj
python3.12 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
```

Para executar também a integração GUI, instala a dependência Python de teste: `.venv/bin/python -m pip install -e '.[test]'`. O `wmctrl` é uma dependência de sistema (por exemplo, `sudo apt install wmctrl` em Debian/Ubuntu).

PyUNO deve corresponder ao LibreOffice instalado. Em Debian/Ubuntu costuma ser fornecido por `python3-uno`; a exposição ao venv varia por distribuição. Verifica:

```bash
/usr/bin/soffice --version
.venv/bin/python -c 'import uno; from mcp.server.mcpserver import MCPServer; print(uno.__file__, MCPServer)'
```

## Iniciar Writer com listener local (Modo B)

O launcher usa `/usr/bin/soffice`, cria uma sessão gráfica com perfil isolado e escuta apenas em `127.0.0.1`. Sem `--profile`, usa `~/.local/share/yberu-writer-mcp/profile-PORTA`.

```bash
cd /home/yberudev/Documentos/MCP_Project/LibreOffice/Writer/MCPProj
.venv/bin/python scripts/launch_writer.py --port 2002 --profile "$HOME/.local/share/yberu-writer-mcp/profile" --wait 25 --json
```

O comando devolve PID, porta e perfil. O Writer continua aberto quando o MCP termina; fecha esta sessão pela interface gráfica. Para uma segunda sessão, configura outra porta e perfil. O launcher recusa porta ocupada. Não usa `--headless` e não força `SAL_USE_VCLPLUGIN=gen`.

Uma janela Writer normal não aceita automaticamente ligações UNO externas.

### Ligar a uma sessão existente (Modo A)

Só funciona se a instância já tiver sido iniciada com listener UNO local. Configura `YBERU_WRITER_PORT` para a porta desse listener. Acrescentar `--accept` depois de uma sessão normal estar aberta pode apenas encaminhar o pedido para a instância sem criar o listener. O servidor não encerra sessões externas.

## Iniciar o servidor MCP

O stdout é reservado às mensagens MCP; os logs técnicos vão para stderr.

```bash
cd /home/yberudev/Documentos/MCP_Project/LibreOffice/Writer/MCPProj
PYTHONPATH=/home/yberudev/Documentos/MCP_Project/LibreOffice/Writer/MCPProj/src \
YBERU_WRITER_HOST=127.0.0.1 YBERU_WRITER_PORT=2002 \
.venv/bin/python -m yberu_writer_mcp.server
```

Normalmente o Codex inicia este processo. Terminar o processo MCP remove listeners quando possível, mas não fecha o Writer.

## Registar no Codex

Acrescenta ao `~/.codex/config.toml` existente; conserva os outros blocos:

```toml
[mcp_servers.yberu_writer]
command = "/home/yberudev/Documentos/MCP_Project/LibreOffice/Writer/MCPProj/.venv/bin/python"
args = ["-m", "yberu_writer_mcp.server"]
cwd = "/home/yberudev/Documentos/MCP_Project/LibreOffice/Writer/MCPProj"
startup_timeout_sec = 20
tool_timeout_sec = 60
env = { PYTHONPATH = "/home/yberudev/Documentos/MCP_Project/LibreOffice/Writer/MCPProj/src", YBERU_WRITER_HOST = "127.0.0.1", YBERU_WRITER_PORT = "2002" }
```

Para permitir `writer_save_document`, acrescenta `YBERU_WRITER_ALLOWED_SAVE_ROOTS` a `env`, apenas com diretórios existentes cujos documentos estás disposto a guardar. Em Linux, separa várias raízes com `:`. Sem allowlist, a ferramenta recusa gravações.

Recarrega o Codex e verifica a configuração com `codex mcp list`. No Codex, chama `writer_get_state`, `writer_list_documents` e `writer_read_document` sobre um documento de teste. Para verificar escrita, lê `writer_read_structure` e usa a revisão e âncora atuais. A configuração global do Codex não foi alterada pelo projeto. Referência oficial: [referência de configuração do Codex](https://developers.openai.com/codex/config-file/config-reference).

## Ferramentas disponíveis

| Ferramenta | Função |
|---|---|
| `writer_get_state` | Ligação, versão LibreOffice, documento ativo, contagem e capacidades. |
| `writer_list_documents` | IDs opacos, título, modificado, leitura/escrita e revisão. |
| `writer_read_document` | Texto em fatias limitadas, revisão, hash e referência de conteúdo. |
| `writer_read_selection` | Seleção textual, múltiplos ranges, não textual ou nenhuma seleção. |
| `writer_read_structure` | Parágrafos, títulos, níveis, estilos, tabelas e âncoras temporárias. |
| `writer_insert_text` | Insere no início/fim/offset de um parágrafo ancorado. |
| `writer_replace_range` | Substitui exatamente o parágrafo ancorado com texto esperado ou hash. |
| `writer_apply_style` | Aplica estilo de parágrafo existente a um parágrafo ancorado. |
| `writer_save_document` | Guarda ficheiro local existente, alterado e sob raiz autorizada. |

### Fluxo de edição

1. Usa `writer_list_documents` para escolher o documento.
2. Usa `writer_read_structure` para obter `revision`, `anchor` e texto.
3. Chama `writer_replace_range` com `document_id`, `anchor`, `expected_revision`, `expected_text` (ou `expected_hash`), `new_content` e um `operation_id` UUID novo.
4. Relê a estrutura para confirmar e obter âncoras novas.

Âncoras são temporárias e não podem ser reutilizadas após edição/revisão. Escritas obsoletas retornam `CONFLICT`; o servidor nunca procura texto aproximado.

## Configuração do servidor

| Variável | Padrão | Descrição |
|---|---:|---|
| `YBERU_WRITER_HOST` | `127.0.0.1` | Loopback apenas (`127.0.0.1`, `localhost`, `::1`). |
| `YBERU_WRITER_PORT` | `2002` | Porta do listener UNO. |
| `YBERU_WRITER_SOFFICE` | `/usr/bin/soffice` | Executável usado pelo launcher. |
| `YBERU_WRITER_ALLOWED_SAVE_ROOTS` | vazio | Raízes permitidas para guardar documentos existentes. |
| `YBERU_WRITER_MAX_READ_CHARS` | `20000` | Máximo por fatia de leitura. |
| `YBERU_WRITER_MAX_SNAPSHOT_CHARS` | `5000000` | Limite de snapshot para revisão/escrita segura. |
| `YBERU_WRITER_MAX_STRUCTURE_ITEMS` | `2000` | Máximo de parágrafos na estrutura. |
| `YBERU_WRITER_MAX_TEXT_CHARS` | `100000` | Máximo para texto de entrada. |
| `YBERU_WRITER_LOG_LEVEL` | `INFO` | Nível dos logs stderr. |

## Segurança e erros

As respostas de erro têm código, mensagem, retryability e detalhes quando aplicável. Incluem `NOT_CONNECTED`, `DOCUMENT_NOT_FOUND`, `INVALID_ANCHOR`, `CONFLICT`, `PERMISSION_DENIED`, `UNKNOWN_OUTCOME`, `INVALID_ARGUMENT` e `SIZE_LIMIT`.

Um worker serializa UNO. Callbacks UNO só enfileiram notificações; eventos coalescidos invalidam snapshots e atualizam a revisão local. `XModifyListener` não representa deltas. IDs ficam inválidos após reconexão. Escritas exigem UUID de idempotência; resultado incerto não é repetido automaticamente.

Não há ferramentas de macros, shell, dispatch arbitrário, código Python ou métodos UNO genéricos. Gravação não recebe destino do cliente, não grava URL remota e não implementa Save As.

## Testes

Testes unitários:

```bash
cd /home/yberudev/Documentos/MCP_Project/LibreOffice/Writer/MCPProj
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -v
```

Integração com Writer gráfico isolado, cliente MCP stdio real, entrada de teclado X11 e reinício da sessão criada pelo teste:

```bash
cd /home/yberudev/Documentos/MCP_Project/LibreOffice/Writer/MCPProj
YBERU_RUN_LIVE_TESTS=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/integration -v
```

O teste GUI requer `DISPLAY`, `wmctrl` e Python Xlib. Usa documentos temporários e termina apenas o grupo de processos que ele iniciou.

## Limitações conhecidas

- Escritas MVP atuam sobre parágrafos do corpo. `writer_replace_range` ainda não cobre intervalos arbitrários, tabelas, cabeçalhos/rodapés ou caixas de texto.
- Tabelas são representadas por referências e células; não são achatadas em texto corrido. Objetos aninhados, campos, notas e frames têm cobertura limitada.
- Revisões são fingerprints locais, não transações nativas. Uma alteração externa seguida de restauração textual idêntica entre verificações pode não ser detetada. Não é prometida atomicidade ACID.
- Eventos são internos; ainda não há subscrição MCP nem histórico de deltas.
- `writer_save_document` só guarda ficheiros locais existentes dentro de allowlist. Save As e URLs remotas não são suportados.
- Modo A depende de listener ativo; Writer aberto normalmente não basta.

## Resolução de problemas

- **`connected=false` / `NOT_CONNECTED`:** confirma listener e que host/porta coincidem.
- **Porta ocupada:** seleciona outra porta no launcher e no Codex.
- **Erro ao importar `uno`:** alinha PyUNO com `/usr/bin/soffice` 24.2; não uses o wrapper LibreOfficeDev.
- **Ferramentas não aparecem:** verifica caminhos absolutos, `PYTHONPATH`, recarrega Codex e executa `codex mcp list`.
- **`CONFLICT` ou `INVALID_ANCHOR`:** relê `writer_read_structure` e usa revisão/âncora atuais.
- **Gravação recusada:** verifica URL local existente e `YBERU_WRITER_ALLOWED_SAVE_ROOTS`.
- **Falha de teste GUI:** verifica X11, `wmctrl`, `DISPLAY` e criação da janela com perfil de teste.

## Relatório de implementação

Resultados, matriz de testes e limitações pendentes: [`docs/IMPLEMENTACAO_MVP.md`](docs/IMPLEMENTACAO_MVP.md). Os relatórios originais continuam em `docs/audit/`.
