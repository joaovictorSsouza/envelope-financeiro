# Agente Financeiro — Fases 1 a 4 (dados, regras, ferramentas, agente e bot)

Camada de dados e regras de negócio de um agente financeiro pessoal, as
ferramentas que expõem essas regras a um modelo, o agente que conversa e o bot
do Telegram que transporta mensagens. Os números continuam sendo calculados em
Python puro: nenhum valor pode sair de um LLM.

A planilha não é acessada por gspread nem pela Google Sheets API: ela é
exposta por um Web App do Google Apps Script que responde a POST com JSON.

## Instalação

```powershell
pip install -r requirements.txt
copy .env.example .env   # e preencha WEBAPP_URL, API_TOKEN e as chaves usadas
python scripts/testar_conexao.py
```

O `.env` está no `.gitignore` e nunca deve ser versionado.

## Arquitetura

| Camada | Arquivo | Responsabilidade |
|---|---|---|
| I/O | `src/api.py` | Único ponto que fala com a rede: monta o POST, valida o envelope, faz retry e cache |
| Tradução | `src/sheets.py` | Transforma as respostas em DataFrames e dicts tipados |
| Regras | `src/financas.py` | Cálculo puro, offline, testável sem rede nem token |
| Ferramentas | `src/tools.py` | Casca fina: lê da API, chama `financas.py`, devolve JSON |
| Conversa | `src/agente.py` + `src/prompts/sistema.md` | Modelo, prompt, contexto do turno e memória por thread |
| Telegram | `src/bot.py` | Transporte: recebe texto, chama o agente e devolve texto |

A dependência é de mão única: `financas.py` **não** importa `sheets.py` e não
faz I/O — recebe o DataFrame e a config como argumentos.

### `api.py`

`chamar(acao, params=None)` devolve o campo `data` da resposta.

- Timeout de 30s (o Apps Script hiberna e o primeiro acesso é lento).
- `allow_redirects=True`: o POST responde 302 para `script.googleusercontent.com`.
- Header `Content-Type: text/plain;charset=utf-8` — com `application/json` o
  Apps Script dispara preflight e a chamada falha.
- 3 tentativas com backoff (1s, 2s) para falha de transporte. `ok=false` é
  erro de negócio: vira `ApiError` na hora, sem repetir.
- Cache em memória de 60s para `ler_lancamentos`, `ler_recorrentes` e
  `ler_config`. Qualquer escrita limpa o cache. `ping` nunca é cacheado.

Exceções: `ApiError` (base), `ErroDeRede` e `RespostaInvalida`.

### `sheets.py`

| Função | Risco | Observação |
|---|---|---|
| `ler_lancamentos()` | leitura | DataFrame tipado (`valor` float, datas datetime, parcelas `Int64` nulável) |
| `ler_config()` | leitura | dict com float/int/bool convertidos |
| `ler_recorrentes()` | leitura | só as linhas com `ativo = True` |
| `adicionar_lancamento(...)` | **BAIXO** | uma linha, sem efeito em meses futuros |
| `atualizar_status(status, id/descricao, mes_ref)` | **BAIXO** | muda o status de uma linha (previsto → pago) |
| `atualizar_lancamento(...)` | **BAIXO** | edita valor, categoria, natureza ou descrição de uma linha |
| `adicionar_parcelamento(..., dry_run=True)` | **ALTO** | escreve N meses de uma vez |
| `gerar_mes(mes_ref, dry_run=True)` | **ALTO** | materializa o mês inteiro; rodar duas vezes duplica |

As duas funções de alto risco têm `dry_run=True` por padrão, igual ao servidor:
só gravam com `dry_run=False` explícito, e devolvem a prévia caso contrário.

As duas de atualização identificam a linha por `id` ou por `descricao`
(opcionalmente com `mes_ref`). Quando o filtro casa com mais de um lançamento,
elas levantam `AmbiguidadeError` — subclasse de `ApiError` — com a lista em
`.candidatos`, para quem chama poder perguntar ao usuário qual é.

Datas ISO (`2026-08-01T00:00:00`, com ou sem `Z`) são normalizadas em UTC e
entregues sem fuso, para a coluna não virar `object`.

### `financas.py`

Vocabulário das regras:

- **sobra** = entradas − saídas do mês
- **reserva** = `pct_investimento` sobre a sobra (zero se a sobra for negativa)
- **livre por semana** = (sobra − reserva) / `semanas_no_mes`
- **comprometido** = soma das *parcelas* do mês (linhas com `parcela_total`)
- **teto de comprometimento** = `limite_comprometimento` sobre as entradas

| Função | Devolve |
|---|---|
| `orcamento_mes(df, config, mes_ref)` | lente de planejamento: renda, fixas, reserva e envelope do mês |
| `resumo_mes(df, config, mes_ref)` | totais do mês + `realizado`/`previsto` separados |
| `acompanhamento_mes(df, config, mes_ref)` | o envelope do variável durante o mês (ver abaixo) |
| `gastos_por_categoria(df, mes_ref)` | saídas por categoria, com percentual |
| `compromissos_futuros(df, mes_ref, n_meses=12)` | mês a mês do que já está lançado (janela inclui `mes_ref`), com `sintetizado` por mês |
| `tipos_do_alvo(df, id, descricao, mes_ref)` | tipos dos lançamentos que casam com o filtro (entrada/saida) |
| `parcelas_em_aberto(df, mes_ref)` | parcelamentos por `grupo_id`, com quanto falta |
| `projetar_janela(df, recorrentes, mes_inicial, n_meses=12)` | df ampliado com os meses ainda não gerados |
| `capacidade_de_compra(df, config, valor, n_parcelas, mes_ref=None, recorrentes=None)` | veredito em 12 meses |
| `planejamento_status(df, recorrentes, mes_ref, config=None)` | o que já existe e o que falta no mês |
| `comparar_meses(df, mes_a, mes_b)` | variação absoluta e percentual entre dois meses |

`orcamento_mes` é a fonte única do envelope: `acompanhamento_mes` o consome em
vez de recalcular, então planejar setembro e acompanhar agosto usam o mesmo
número. `orcamento_mes` não olha status nem depende de `hoje`.

Helpers de contexto temporal, todos com `hoje` injetável para teste:
`mes_de_planejamento(hoje)` (mês seguinte), `mes_de_acompanhamento(hoje)`
(mês corrente) e `fase_do_mes(hoje)` → `"inicio"`/`"meio"`/`"fim"`, por terço
do tamanho real do mês.

Todo dinheiro é somado em `Decimal` e arredondado para 2 casas com
`ROUND_HALF_UP` antes de virar `float`.

## O envelope do variável (sem contagem dupla)

Gastos variáveis são planejados com antecedência. Uma variada com
`status = "previsto"` é **reserva dentro do envelope**, não gasto realizado —
contá-la como gasto e como plano ao mesmo tempo é contar o dinheiro duas vezes.
Por isso `acompanhamento_mes` devolve três números em vez de um:

| Campo | Significado |
|---|---|
| `envelope_variavel` | entradas − fixas − reserva (o teto do variável no mês) |
| `gasto_realizado` | variadas com status `pago` |
| `comprometido` | variadas com status `previsto`, ainda por gastar |
| `livre` | `envelope − gasto_realizado − comprometido` — o que dá para gastar sem furar nenhum plano |
| `restante` | `envelope − gasto_realizado` — o que ainda vai passar pelo caixa |

`situacao` (`tranquilo` / `atencao` a 70% / `no_limite` a 90% / `estourado`) e
`invade_reserva` olham **`gasto_realizado + comprometido`**, nunca só o que já
foi pago.

Marcar um previsto como pago não muda o `livre`: o valor só migra de
`comprometido` para `gasto_realizado`.

O envelope é o modelo "pague-se primeiro" — das entradas saem as fixas e depois
a reserva. É diferente de `resumo_mes`, que fecha o mês com as variadas já
lançadas como saída. São duas lentes do mesmo mês: `acompanhamento_mes` é a que
se usa durante o mês.

## A janela de 12 meses

Uma compra em 6x já existe como 6 linhas na planilha, uma por `mes_ref`, com o
mesmo `grupo_id`. **O futuro já está na tabela — nada é projetado
matematicamente aqui, só filtrado.**

`capacidade_de_compra` avalia os 12 meses seguintes, somando a parcela nova às
parcelas já comprometidas de cada mês. Um mês fica **inviável** quando:

1. com `reserva_intocavel = TRUE`, o que sobra depois da compra não cobre mais
   a reserva daquele mês (a reserva não cede para pagar a parcela); ou
2. as parcelas do mês passam do teto de comprometimento sobre a renda.

`cabe` global é `False` se **qualquer** mês afetado ficar inviável, e `motivo`
cita o pior mês pelo nome. O retorno traz ainda `projecao` (12 meses),
`pior_mes` e `meses_inviaveis`.

`pior_mes` é o menor livre por semana **entre os meses que a compra alcança**
(`meses_afetados`) — não adianta culpar um mês que a parcela sequer atinge. Com
`n_parcelas = 1`, o pior mês é o próprio `mes_ref`.

Uma compra à vista (`n_parcelas = 1`) consome a sobra do mês mas não entra no
comprometimento — ele mede dívida parcelada contra renda.

### Meses que ainda não existem

A planilha só tem linhas futuras onde existem parcelas: salário e recorrentes de
setembro em diante só nascem quando `gerar_mes` roda. Sem tratamento, esses
meses entram na janela com sobra zero e **qualquer** compra parcelada é
recusada.

`projetar_janela(df, recorrentes, mes_inicial, n_meses)` resolve isso em
memória: todo mês da janela sem nenhuma linha `origem="recorrencia"` recebe as
recorrentes ativas como `status="previsto"` e `origem="projetado"`. Mês já
materializado não recebe nada — dado real manda —, e parcelas e lançamentos
reais são sempre preservados. Nada é gravado na planilha.

`compromissos_futuros` traz a mesma marca: a coluna `sintetizado` diz, mês a
mês, se aquela linha veio da planilha ou de `projetar_janela` — estimativa não
pode ser citada ao usuário como fato lançado.

Passe `recorrentes` para `capacidade_de_compra` e cada item de `projecao` dirá
se foi `sintetizado`. Sem `recorrentes`, havendo mês vazio na janela, o veredito
sai conservador e o `motivo` **termina** com o aviso de projeção incompleta —
nunca em silêncio.

`planejamento_status(df, recorrentes, mes_ref, config)` fecha o ciclo: diz se o
mês foi materializado, quais recorrentes ativas ainda faltam, quais parcelas
caem lá e — passando `config` — a `previa_orcamento` daquele mês antes de
`gerar_mes` rodar.

## `tools.py` — as ferramentas do modelo

Cada tool faz sempre o mesmo: lê da API, chama uma função de `financas.py` e
devolve JSON serializável. **Nenhum cálculo acontece na tool** e nenhum
DataFrame chega ao modelo — tudo passa por `_json`, que converte tabela em
lista de dicts, Timestamp em ISO e NaN/NaT em `None`. Dinheiro sai como float
com 2 casas; escrever "R$ 1.234,56" é trabalho da camada de resposta.

| Tool | Risco | Mês padrão |
|---|---|---|
| `ver_orcamento(mes_ref)` | leitura | **seguinte** (planejamento) |
| `ver_acompanhamento(mes_ref)` | leitura | corrente |
| `ver_resumo(mes_ref)` | leitura | corrente |
| `ver_gastos_por_categoria(mes_ref)` | leitura | corrente |
| `ver_compromissos_futuros(mes_ref, n_meses=12)` | leitura | corrente |
| `ver_parcelas_em_aberto(mes_ref)` | leitura | corrente |
| `ver_planejamento(mes_ref)` | leitura | **seguinte** |
| `comparar(mes_a, mes_b)` | leitura | — |
| `simular_compra(valor, n_parcelas, mes_ref)` | leitura | corrente |
| `registrar_gasto(..., ja_gastei=True)` | **BAIXO** | corrente |
| `marcar_como_pago(...)` | **BAIXO** | — |
| `marcar_como_recebido(...)` | **BAIXO** | — |
| `corrigir_lancamento(...)` | **BAIXO** | — |
| `simular_parcelamento(...)` / `confirmar_parcelamento(...)` | **ALTO** | — |
| `simular_geracao_mes(mes)` / `confirmar_geracao_mes(mes)` | **ALTO** | — |

As de alto risco andam em par: a `simular_*` **nunca** grava (não expõe
`dry_run` como argumento) e a `confirmar_*` só grava depois de uma simulação
recente e idêntica — o docstring pede, e o guard de dois passos (abaixo)
garante. Sem `dia_do_mes`, um parcelamento que começa no mês
corrente vence no dia de hoje; nos demais meses, no dia 1. O dia escolhido volta
na prévia, para a confirmação gravar exatamente o que o usuário viu.

`registrar_gasto` decide o status pelo `ja_gastei`: `True` (padrão) grava
`pago` — o dinheiro já saiu, é gasto realizado; `False` grava `previsto` — o
gasto ainda vai acontecer e está reservado dentro do envelope. Os dois consomem
o envelope igual; o que muda é se o valor já passou pelo caixa.

`marcar_como_pago` é para SAÍDAS e `marcar_como_recebido` para ENTRADAS. Trocar
as duas não suja a planilha: o tipo do alvo é conferido antes (via
`financas.tipos_do_alvo`) e a resposta volta com `gravado: false` e o nome da
tool certa. Não achar o lançamento localmente **não** bloqueia a escrita — a
planilha continua sendo a fonte da verdade.

Toda tool que enxerga o futuro (`ver_orcamento`, `ver_compromissos_futuros`,
`ver_planejamento`, `simular_compra`) busca as recorrentes e as passa adiante.
Sem isso, mês que ainda não passou por `gerar_mes` entra zerado e a projeção
volta a ficar incompleta.

Tool nunca levanta exceção: `ApiError` vira `{"erro", "sugestao"}`, falha de
rede vira `"não consegui falar com a planilha, tente de novo"` e ambiguidade
vira `{"erro": "ambiguo", "candidatos": [...]}` — a tool **não escolhe** qual
lançamento é, quem pergunta é o agente.

## `agente.py` — a camada de conversa

`create_agent` (langchain) com `MemorySaver` e o modelo do provedor
configurado no `.env`.

| Variável | Obrigatória | Padrão | Observação |
|---|---|---|---|
| `PROVEDOR` | não | `google` | `google` ou `anthropic` |
| `GOOGLE_API_KEY` | se `PROVEDOR=google` | — | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — grátis, sem cartão |
| `ANTHROPIC_API_KEY` | se `PROVEDOR=anthropic` | — | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| `MODELO` | não | `gemini-3.5-flash-lite` / `claude-sonnet-5` | o padrão depende do provedor — ver [Qual modelo do Gemini](#qual-modelo-do-gemini) |
| `TEMPERATURA` | não | `0` | um agente que não pode calcular não tem uso para criatividade |

`construir_modelo()` resolve tudo isso e falha na largada, com o nome da
variável que falta e o link de onde tirar a chave — não com um 401 no meio de
uma conversa. Só o SDK do provedor em uso precisa estar instalado: os imports
são locais.

Tools, system prompt e o ciclo de dois passos são agnósticos ao provedor.

```powershell
python scripts/conversar.py           # REPL
python scripts/conversar.py --debug   # + as tools escolhidas e seus argumentos
```

O system prompt mora em `src/prompts/sistema.md` e é **relido do disco a cada
turno**: dá para editar o texto com o REPL aberto e ver o efeito na mensagem
seguinte.

Antes de cada ida ao modelo, `montar_prompt` monta um `SystemMessage` com o
prompt mais um bloco `CONTEXTO ATUAL` — data de hoje, mês de acompanhamento, mês
de planejamento e fase do mês, todos vindos de `financas.py`. O bloco **não**
entra no histórico: é remontado a cada turno, então uma conversa aberta durante
a virada do mês não continua respondendo sobre o mês anterior. Todo
`date.today()` do módulo está em `agente._hoje`, e os testes injetam `hoje`.

Quem faz isso é o middleware `dynamic_prompt`. No `create_react_agent` era um
callable `prompt=state -> [SystemMessage, *mensagens]`; o `create_agent` não
recebe mais callable de prompt — seu `system_prompt` é estático, e quem varia o
texto por turno é o middleware, que devolve só o system prompt e deixa o agente
prepará-lo às mensagens do estado. O que chega ao modelo é idêntico: um único
`SystemMessage` na primeira posição, remontado a cada ida ao LLM, inclusive na
volta depois de uma tool.

`conversar(agente, mensagem, thread_id)` devolve `(resposta, chamadas_de_tool)`.
As chamadas são só as do último turno — é o que o `--debug` imprime.

### O guard de dois passos

O ciclo simular → mostrar → confirmar está no system prompt, mas prompt é
pedido, não garantia. A trava vive em `tools.py`: uma `simular_*` bem-sucedida
registra uma marca `(thread_id, assinatura dos argumentos)`, e a `confirmar_*`
correspondente **só grava se achar essa marca**. Sem ela, devolve
`{"gravado": false, "erro": "sem_simulacao"}` sem tocar na planilha.

| Situação | Resultado |
|---|---|
| `confirmar_*` sem simulação | recusa |
| argumento diferente do simulado (3x → 2x, outro valor, outro mês) | recusa |
| simulação de mais de 30 min (`JANELA_SIMULACAO_S`) | recusa |
| simulação feita em outra thread | recusa |
| simulação que falhou (rede, API) | recusa |
| confirmar duas vezes com uma simulação só | a segunda recusa |

A marca é **consumida** ao ser usada: confirmar duas vezes exige simular duas
vezes. É isso que impede `confirmar_geracao_mes` de duplicar o mês inteiro.

O `thread_id` vem de `ensure_config()`, lido do contexto que o LangGraph já
injeta — as assinaturas das tools não mudaram, e o schema exposto ao modelo
continua idêntico. Isso sobreviveu à troca do `create_react_agent` pelo
`create_agent`: o `config` com o `configurable.thread_id` continua chegando às
tools pelo mesmo caminho, e o isolamento por thread continua valendo (simular na
thread A não libera confirmar na thread B). Fora de uma conversa a thread vira
`"sem-thread"`, e o guard continua valendo. O registro é de processo, como o `MemorySaver`: reiniciar
apaga, e o usuário simula de novo.

## `bot.py` — Telegram

O bot é a última camada e não contém regra de negócio: ele valida o chat,
encaminha o texto para `agente.conversar(...)` e devolve a resposta em texto
simples. O `thread_id` vem do `chat_id`, então a conversa continua entre
mensagens. O comando `/novo` troca a thread e começa um histórico limpo.

Variáveis no `.env`:

| Variável | Obrigatória | Observação |
|---|---|---|
| `TELEGRAM_TOKEN` | sim | Token do bot criado no BotFather |
| `TELEGRAM_CHAT_ID` | sim | Único chat autorizado; outros chats recebem "este bot é privado" |

Comandos:

| Comando | O que faz |
|---|---|
| `/start` | Mostra uma mensagem curta sobre o bot |
| `/novo` | Limpa o histórico e inicia uma nova thread para o chat |
| `/ajuda` | Mostra exemplos reais do que pedir |

Para rodar localmente com polling:

```powershell
python -m src.bot
```

Não há webhook nem deploy nesta fase. Durante um turno, o bot envia
`typing...`; respostas acima do limite do Telegram são divididas, e erros do
agente viram mensagens amigáveis em português.

### Retry do modelo

`ModeloComRetry` envolve o chat model e repete **só** falhas transitórias —
503/429/500/502/504, timeouts e mensagens de sobrecarga. Duas re-tentativas,
esperando 2s e 5s. Erro de schema, de chave ou de tool sobe na hora: repetir só
demoraria para dar o mesmo erro.

O retry está no **modelo**, não no agente. Repetir o agente inteiro
re-executaria as tools já executadas no turno, e entre elas pode haver escrita.
`Runnable.with_retry` não serve: devolve um `RunnableRetry`, que não tem
`bind_tools`.

## Testes

```powershell
python -m pytest -q
```

189 testes, todos offline: `tests/conftest.py` substitui `requests.post` por uma
função que falha, então qualquer chamada de rede não mockada quebra o teste.
Em `tests/test_tools.py` o `sheets` inteiro é dublado, e o dublê registra as
escritas — é assim que "simular não grava" vira uma verificação de verdade.

O mês de referência dos testes (`2026-08`): entrada de 2.500,00; fixas de
133,00 (relógio 4/10), 150,00 (gympass), 160,00 (hyrox 1/4) e 350,00
(faculdade); variada de 160,00.

```
sobra = 1.547,00   reserva = 309,40   livre por semana = 412,53
```

A regressão que motivou `projetar_janela` está em
`test_compra_parcelada_cabe_com_meses_projetados`: R$ 1.800 em 6x com só o mês
inicial materializado e os cinco seguintes vazios. Com `recorrentes`, `cabe =
True`; sem eles, `cabe = False` e o motivo traz o aviso de projeção incompleta.

O teste central é `test_capacidade_cabe_hoje_mas_estoura_no_futuro`:
R$ 1.800,00 em 6x cabe folgado em 2026-08 (sobrariam 1.247,00), mas em 2026-11
um seguro anual já derrubou a sobra para 147,00 — a parcela de 300,00 arrebenta
a reserva daquele mês. Resultado: `cabe = False` e `pior_mes = 2026-11`.

### O schema das tools no Gemini

O Gemini é mais rígido que o Claude com o JSON Schema das ferramentas, e
`bind_tools` **não** verifica isso: ele guarda as tools no formato OpenAI e a
conversão só acontece no request. Quem converte é
`convert_to_genai_function_declarations`, e é ela que
`test_todas_as_tools_convertem_para_o_schema_do_gemini` chama — as 17 tools,
campo a campo, sem rede.

As assinaturas atuais passam sem mudança: `str | None = None` vira `STRING
nullable` fora de `required`, nenhum campo fica sem tipo e nenhum vira `anyOf`
não resolvido. Não foi preciso `Optional`/`Field` em lugar nenhum.

Em `tests/test_agente.py` o modelo é um dublê com fila de respostas programadas
e as tools são funções que registram a chamada. Isso testa o **encanamento** —
a tool roda, o retorno volta, o contexto chega, o histórico fica na thread
certa. A *escolha* da tool é do modelo real e nenhum teste offline a cobre.

## Escopo destas fases

Dentro: `api.py`, `sheets.py`, `financas.py`, `tools.py`, `agente.py`, o prompt,
`bot.py` e os testes. A camada de conversa consome as ferramentas e nunca
recalcula nada por conta própria; o bot só transporta mensagens.

Ainda não existe ferramenta para registrar uma **entrada** nova, para **apagar**
um lançamento nem para **listar as categorias** válidas. O prompt declara os
três como limites, em vez de deixar o modelo improvisar.
