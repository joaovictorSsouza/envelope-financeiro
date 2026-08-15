"""Ferramentas LangChain: a casca fina entre o modelo e as regras.

Cada tool faz sempre a mesma coisa, nessa ordem: lê da API (`sheets`),
chama uma função de `financas.py` e devolve JSON serializável. **Nenhum
cálculo acontece aqui** — se um número precisa ser somado, ele é somado em
`financas.py`, que é testado e offline.

Duas garantias que o resto do sistema depende:

1. DataFrame nunca vaza para o modelo. Tudo passa por `_json`, que converte
   tabela em lista de dicts, Timestamp em ISO e NaN/NaT em None.
2. Tool nunca levanta exceção. Erro vira `{"erro": ..., "sugestao": ...}`
   com uma frase acionável, para o agente poder reagir em vez de quebrar.

Valores monetários saem como float com 2 casas. Formatar "R$ 1.234,56" é
responsabilidade da camada de resposta, nunca desta.

Toda tool que enxerga o futuro (janela de meses ou mês ainda não gerado)
busca as recorrentes e as passa adiante. Sem isso, mês que ainda não passou
por `gerar_mes` entra zerado e a projeção volta a ficar incompleta.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from typing import Any, Callable, Final

import numpy as np
import pandas as pd
from langchain_core.runnables.config import ensure_config
from langchain_core.tools import tool

from . import api, financas, sheets

CASAS: Final[int] = 2
"""Dinheiro sai com 2 casas decimais, sempre como float."""

JANELA_SIMULACAO_S: Final[int] = 30 * 60
"""Prazo de validade de uma simulação. Passou disso, simule de novo."""


# --------------------------------------------------------------------------
# serialização
# --------------------------------------------------------------------------
def _escalar(valor: Any) -> Any:
    """Converte um valor solto no equivalente JSON: NaN/NaT viram None."""
    try:
        if valor is not None and not isinstance(valor, (list, tuple, dict)):
            if pd.isna(valor):
                return None
    except (TypeError, ValueError):
        pass

    if valor is None:
        return None
    if isinstance(valor, (bool, np.bool_)):
        return bool(valor)
    if isinstance(valor, (pd.Timestamp, datetime, date)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return round(float(valor), CASAS)
    if isinstance(valor, (int, np.integer)):
        return int(valor)
    if isinstance(valor, (float, np.floating)):
        return round(float(valor), CASAS)
    if isinstance(valor, str):
        return valor
    return str(valor)


def _json(valor: Any) -> Any:
    """Normaliza qualquer retorno de `financas`/`sheets` em JSON serializável.

    DataFrame vira lista de dicts — é aqui que se garante que nenhuma tabela
    do pandas chegue ao modelo.
    """
    if isinstance(valor, pd.DataFrame):
        return [_json(linha) for linha in valor.to_dict("records")]
    if isinstance(valor, pd.Series):
        return [_json(item) for item in valor.tolist()]
    if isinstance(valor, dict):
        return {str(chave): _json(item) for chave, item in valor.items()}
    if isinstance(valor, (list, tuple, set)):
        return [_json(item) for item in valor]
    return _escalar(valor)


# --------------------------------------------------------------------------
# erros
# --------------------------------------------------------------------------
def _erro_de_ambiguidade(exc: sheets.AmbiguidadeError) -> dict[str, Any]:
    """Devolve os candidatos em vez de escolher um: quem decide é o usuário."""
    return {
        "erro": "ambiguo",
        "candidatos": _json(exc.candidatos),
        "sugestao": (
            "Mais de um lançamento casa com esse filtro. Pergunte ao usuário "
            "qual deles é e chame de novo usando o `id` do escolhido."
        ),
    }


def _protegido(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Transforma qualquer falha em dict de erro. O modelo nunca vê exceção."""

    @wraps(fn)
    def protegida(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except sheets.AmbiguidadeError as exc:
            return _erro_de_ambiguidade(exc)
        except api.ErroDeRede:
            return {
                "erro": "não consegui falar com a planilha, tente de novo",
                "sugestao": "Peça ao usuário para repetir em alguns segundos.",
            }
        except api.RespostaInvalida:
            return {
                "erro": "a planilha respondeu algo que não consegui ler",
                "sugestao": (
                    "Provavelmente o Web App do Apps Script está fora do ar ou "
                    "foi reimplantado. Avise o usuário e tente de novo."
                ),
            }
        except api.ApiError as exc:
            return {
                "erro": str(exc.mensagem),
                "sugestao": "A planilha recusou a operação. Confira os dados e refaça.",
            }
        except ValueError as exc:
            return {
                "erro": str(exc),
                "sugestao": "Corrija o argumento e chame a ferramenta de novo.",
            }

    return protegida


# --------------------------------------------------------------------------
# guard de dois passos: confirmar exige simulação recente e compatível
# --------------------------------------------------------------------------
# O ciclo simular -> mostrar -> confirmar está escrito no system prompt, mas
# prompt é pedido, não garantia: nada impede o modelo de chamar uma tool
# `confirmar_*` direto, e essas escritas não se desfazem. Este registro é a
# trava. Uma `simular_*` bem-sucedida deixa uma marca; a `confirmar_*`
# correspondente só grava se achar essa marca, com os MESMOS argumentos,
# feita há menos de JANELA_SIMULACAO_S na MESMA thread.
#
# A marca é consumida ao ser usada: confirmar duas vezes exige simular duas
# vezes. É isso que protege `confirmar_geracao_mes`, que duplica o mês
# inteiro se rodar duas vezes.
#
# O registro vive no processo, como o MemorySaver do agente: reiniciar
# apaga tudo e o usuário simula de novo. É o custo certo — o erro caro é
# gravar sem confirmação, não pedir uma simulação a mais.
_SIMULACOES: dict[tuple[str, str], float] = {}
_TRAVA_SIMULACOES: Final[threading.Lock] = threading.Lock()


def _thread_atual() -> str:
    """thread_id da conversa, lido do config que o LangGraph põe em contexto.

    Fora de uma conversa (teste chamando a tool direto, script) não há
    config: a thread vira "sem-thread", que é uma thread como outra
    qualquer. O guard continua valendo.
    """
    try:
        configuravel = ensure_config().get("configurable") or {}
    except Exception:  # noqa: BLE001 — sem contexto, seguimos sem thread
        return "sem-thread"
    return str(configuravel.get("thread_id") or "sem-thread")


def _normalizar(valor: Any) -> str:
    """Deixa 300, 300.0 e '300' com a mesma cara, para a assinatura casar."""
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, (int, np.integer)):
        return str(int(valor))
    if isinstance(valor, (float, np.floating, Decimal)):
        return f"{round(float(valor), CASAS):.2f}"
    if valor is None:
        return ""
    return str(valor).strip().lower()


def _assinatura(operacao: str, **campos: Any) -> str:
    """Identidade do que foi simulado. Argumento diferente, assinatura diferente."""
    partes = [f"{nome}={_normalizar(campos[nome])}" for nome in sorted(campos)]
    return f"{operacao}|" + "|".join(partes)


def _registrar_simulacao(assinatura: str, agora: float | None = None) -> None:
    """Marca que esta simulação aconteceu agora, nesta thread."""
    momento = time.monotonic() if agora is None else agora
    with _TRAVA_SIMULACOES:
        _SIMULACOES[(_thread_atual(), assinatura)] = momento


def _consumir_simulacao(assinatura: str, agora: float | None = None) -> bool:
    """Existe simulação válida para isto? Se existir, consome e devolve True."""
    momento = time.monotonic() if agora is None else agora
    chave = (_thread_atual(), assinatura)
    with _TRAVA_SIMULACOES:
        for antiga, feita_em in list(_SIMULACOES.items()):
            if momento - feita_em > JANELA_SIMULACAO_S:
                del _SIMULACOES[antiga]
        feita_em = _SIMULACOES.pop(chave, None)
    return feita_em is not None


def limpar_simulacoes() -> None:
    """Zera o registro. Existe para os testes não vazarem um no outro."""
    with _TRAVA_SIMULACOES:
        _SIMULACOES.clear()


def _erro_sem_simulacao(tool_de_simulacao: str) -> dict[str, Any]:
    """Recusa da escrita. Nada foi gravado, e a saída diz o que fazer."""
    return {
        "gravado": False,
        "erro": "sem_simulacao",
        "sugestao": (
            f"Nada foi gravado: não há simulação recente com estes mesmos "
            f"argumentos nesta conversa. Chame `{tool_de_simulacao}`, MOSTRE a "
            f"prévia ao usuário, espere ele confirmar e só então confirme. Se "
            f"algum valor mudou depois da prévia, simule de novo com os "
            f"valores novos."
        ),
    }


# --------------------------------------------------------------------------
# leitura dos dados (a mesma abertura para toda tool)
# --------------------------------------------------------------------------
def _mes_planejado(mes_ref: str | None) -> str:
    """Sem mês explícito, planeja-se o mês SEGUINTE — nunca o corrente."""
    return mes_ref or financas.mes_de_planejamento()


def _mes_corrente(mes_ref: str | None) -> str:
    """Sem mês explícito, acompanha-se o mês CORRENTE."""
    return mes_ref or financas.mes_de_acompanhamento()


def _materializado(
    df: pd.DataFrame, recorrentes: pd.DataFrame, mes_ref: str
) -> bool:
    """`gerar_mes` já rodou nesse mês? Quem responde é `financas`."""
    return bool(
        financas.planejamento_status(df, recorrentes, mes_ref)["materializado"]
    )


def _dia_padrao(mes_inicial: str, hoje: date | None = None) -> int:
    """Dia de vencimento quando o usuário não disse qual.

    Parcelamento que começa no mês corrente costuma vencer no dia da compra,
    então o padrão é hoje. Para um mês futuro não há dia melhor que o dia 1.
    """
    referencia = hoje or date.today()
    if mes_inicial == financas.mes_ref_de_hoje(referencia):
        return referencia.day
    return 1


_ROTULO_TIPO: Final[dict[str, str]] = {"entrada": "entrada", "saida": "saída"}


def _conflito_de_tipo(
    esperado: str, tipos: list[str], tool_correta: str
) -> dict[str, Any] | None:
    """Barra pago↔recebido trocados: devolve o erro, ou None se estiver certo.

    Só bloqueia quando TODOS os lançamentos encontrados são do tipo errado —
    não achar nada não é motivo para recusar, a planilha é a fonte da verdade.
    """
    if not tipos or esperado in tipos:
        return None
    encontrado = _ROTULO_TIPO.get(tipos[0], tipos[0])
    return {
        "erro": (
            f"nada foi gravado: esse lançamento é uma {encontrado}, "
            f"não uma {_ROTULO_TIPO[esperado]}"
        ),
        "sugestao": f"Use `{tool_correta}`, que é a ferramenta de {encontrado}.",
        "tipos_encontrados": tipos,
    }


def _marcar(
    status: str,
    tipo_esperado: str,
    tool_correta: str,
    descricao: str | None,
    id: str | None,
    mes_ref: str | None,
) -> dict[str, Any]:
    """Confere o tipo do alvo e só então grava o status.

    A conferência é local, sobre os lançamentos já lidos: marcar uma entrada
    como "paga" é erro de escolha de ferramenta, e sai mais barato devolver
    isso como orientação do que sujar a planilha.
    """
    tipos: list[str] = []
    if id or descricao:  # sem alvo não há o que conferir — nem por que ler.
        tipos = financas.tipos_do_alvo(
            sheets.ler_lancamentos(), id=id, descricao=descricao, mes_ref=mes_ref
        )

    conflito = _conflito_de_tipo(tipo_esperado, tipos, tool_correta)
    if conflito is not None:
        return _json({"gravado": False, **conflito})

    resposta = sheets.atualizar_status(
        status, id=id, descricao=descricao, mes_ref=mes_ref
    )
    return _json({"gravado": True, "status": status, "resposta": resposta})


# --------------------------------------------------------------------------
# tools de leitura
# --------------------------------------------------------------------------
@tool
@_protegido
def ver_orcamento(mes_ref: str | None = None) -> dict[str, Any]:
    """Mostra o PLANO de um mês: renda, fixas, reserva e envelope do variável.

    Use para saber como um mês VAI ficar, antes de ele começar — "quanto eu
    posso gastar em setembro?", "como fica o mês que vem?". Sem `mes_ref`,
    responde sobre o mês SEGUINTE, que é o mês que se planeja.

    NÃO use para saber como o mês em curso está indo: para isso use
    `ver_acompanhamento`, que compara o envelope com o que já foi gasto. Para
    o fechamento de um mês passado, use `ver_resumo`.

    Se o mês ainda não foi gerado, as recorrentes ativas são projetadas em
    memória (nada é gravado) e `materializado` volta False — nesse caso vale
    lembrar o usuário de rodar a geração do mês.
    """
    mes = _mes_planejado(mes_ref)
    df = sheets.ler_lancamentos()
    config = sheets.ler_config()
    recorrentes = sheets.ler_recorrentes()

    projetado = financas.projetar_janela(df, recorrentes, mes, 1)
    orcamento = financas.orcamento_mes(projetado, config, mes)
    return _json(
        {**orcamento, "materializado": _materializado(df, recorrentes, mes)}
    )


@tool
@_protegido
def ver_acompanhamento(mes_ref: str | None = None) -> dict[str, Any]:
    """Mostra como o mês EM CURSO está indo contra o envelope planejado.

    Use para "como estou este mês?", "posso gastar?", "quanto ainda tenho?".
    Sem `mes_ref`, responde sobre o mês CORRENTE.

    Devolve três números que não se somam: `gasto_realizado` (variadas já
    pagas), `comprometido` (variadas previstas, ainda por gastar) e `livre`
    (o que dá para gastar sem furar nenhum plano). `situacao` é o semáforo:
    tranquilo / atencao / no_limite / estourado.

    NÃO use para um mês que ainda não começou — aí o número que importa é o
    plano, e a tool é `ver_orcamento`. Para decidir uma compra específica,
    use `simular_compra`, que olha 12 meses e não só este.
    """
    mes = _mes_corrente(mes_ref)
    df = sheets.ler_lancamentos()
    config = sheets.ler_config()
    recorrentes = sheets.ler_recorrentes()

    acompanhamento = financas.acompanhamento_mes(df, config, mes)
    return _json(
        {**acompanhamento, "materializado": _materializado(df, recorrentes, mes)}
    )


@tool
@_protegido
def ver_resumo(mes_ref: str | None = None) -> dict[str, Any]:
    """Fecha as contas de um mês: entradas, saídas, sobra e reserva.

    Lente retrospectiva, para "como foi julho?" ou "quanto sobrou no mês
    passado?". Separa o que já foi realizado (pago/recebido) do que ficou
    apenas previsto. Sem `mes_ref`, usa o mês corrente.

    NÃO use durante o mês para responder "posso gastar?" — este número trata
    toda variada lançada como saída, então não enxerga o envelope. Para isso
    use `ver_acompanhamento`; para o mês que vem, `ver_orcamento`.
    """
    mes = _mes_corrente(mes_ref)
    df = sheets.ler_lancamentos()
    config = sheets.ler_config()
    return _json(financas.resumo_mes(df, config, mes))


@tool
@_protegido
def ver_gastos_por_categoria(mes_ref: str | None = None) -> dict[str, Any]:
    """Lista as saídas do mês agrupadas por categoria, da maior para a menor.

    Use para "onde meu dinheiro foi?", "no que gastei mais?". Cada item traz
    `valor` e `percentual` sobre o total de saídas do mês. Sem `mes_ref`, usa
    o mês corrente.

    NÃO use para saber quanto ainda pode gastar (`ver_acompanhamento`) nem
    para comparar dois meses (`comparar`).
    """
    mes = _mes_corrente(mes_ref)
    df = sheets.ler_lancamentos()
    return _json(
        {
            "mes_ref": mes,
            "categorias": financas.gastos_por_categoria(df, mes),
        }
    )


@tool
@_protegido
def ver_compromissos_futuros(
    mes_ref: str | None = None, n_meses: int = 12
) -> dict[str, Any]:
    """Fotografia mês a mês do que já está comprometido à frente.

    Use para "como ficam meus próximos meses?", "quando isso alivia?". A
    janela começa NO `mes_ref` (inclusive) e cada linha traz entradas, fixas,
    variadas, parcelas e sobra do mês. Sem `mes_ref`, começa no mês corrente.

    Os meses que ainda não foram gerados entram projetados a partir das
    recorrentes ativas (nada é gravado), senão apareceriam com sobra zero.
    Esses meses vêm com `sintetizado: true` — são estimativa, não fato
    lançado, e vale dizer isso ao usuário ao citá-los.

    NÃO use para decidir se uma compra cabe — isso é `simular_compra`, que
    soma a parcela nova em cada mês e dá um veredito. Para ver os
    parcelamentos individuais, use `ver_parcelas_em_aberto`.
    """
    mes = _mes_corrente(mes_ref)
    df = sheets.ler_lancamentos()
    recorrentes = sheets.ler_recorrentes()

    projetado = financas.projetar_janela(df, recorrentes, mes, n_meses)
    return _json(
        {
            "mes_inicial": mes,
            "n_meses": int(n_meses),
            "meses": financas.compromissos_futuros(projetado, mes, n_meses),
        }
    )


@tool
@_protegido
def ver_parcelas_em_aberto(mes_ref: str | None = None) -> dict[str, Any]:
    """Lista os parcelamentos com parcelas em aberto a partir de um mês.

    Use para "o que ainda estou pagando?", "quantas parcelas faltam do
    celular?". Cada item traz quantas parcelas restam, quanto falta pagar no
    total e em que mês termina. Sem `mes_ref`, conta a partir do mês corrente.

    Só lê o que já está lançado — nada é projetado, porque parcela futura já
    existe como linha na planilha.

    NÃO use para ver o total de cada mês (`ver_compromissos_futuros`) nem
    para criar um parcelamento novo (`simular_parcelamento`).
    """
    mes = _mes_corrente(mes_ref)
    df = sheets.ler_lancamentos()
    return _json(
        {
            "mes_ref": mes,
            "parcelamentos": financas.parcelas_em_aberto(df, mes),
        }
    )


@tool
@_protegido
def ver_planejamento(mes_ref: str | None = None) -> dict[str, Any]:
    """Diz o que ainda falta materializar num mês futuro e a prévia dele.

    Use antes de fechar o planejamento de um mês: responde se `gerar_mes` já
    rodou (`materializado`), quais recorrentes ativas ainda não têm linha
    (`faltando`), quais parcelas já caem lá e a `previa_orcamento` do mês.
    Sem `mes_ref`, olha o mês SEGUINTE.

    NÃO use para os números do plano em si — para renda, envelope e envelope
    semanal use `ver_orcamento`. Se `materializado` for False e o usuário
    quiser gerar o mês, o caminho é `simular_geracao_mes` e só depois
    `confirmar_geracao_mes`.
    """
    mes = _mes_planejado(mes_ref)
    df = sheets.ler_lancamentos()
    config = sheets.ler_config()
    recorrentes = sheets.ler_recorrentes()

    return _json(financas.planejamento_status(df, recorrentes, mes, config))


@tool
@_protegido
def comparar(mes_a: str, mes_b: str) -> dict[str, Any]:
    """Compara dois meses em valores absolutos e percentuais (`mes_b` sobre `mes_a`).

    Use para "gastei mais que no mês passado?", "o que mudou de julho para
    agosto?". Devolve os totais dos dois meses, a variação de cada um deles e
    a comparação por categoria. Percentual sobre base zero volta None — não
    existe variação percentual a partir de zero.

    Ambos os meses são obrigatórios, no formato AAAA-MM. NÃO use para olhar
    um mês só (`ver_resumo`) nem para o futuro (`ver_compromissos_futuros`).
    """
    df = sheets.ler_lancamentos()
    return _json(financas.comparar_meses(df, mes_a, mes_b))


@tool
@_protegido
def simular_compra(
    valor: float, n_parcelas: int = 1, mes_ref: str | None = None
) -> dict[str, Any]:
    """Diz se uma compra cabe, olhando os 12 meses seguintes e não só este.

    Use sempre que o usuário perguntar "posso comprar X?", "dá para pagar em
    Nx?". `valor` é o valor TOTAL da compra, não o da parcela — a divisão é
    feita pelas regras. Sem `mes_ref`, a compra começa no mês corrente.

    Não grava nada: é só um cálculo. A parcela nova é somada às parcelas já
    lançadas de cada mês, e basta UM mês afetado ficar inviável para `cabe`
    ser False; `motivo` já vem como frase pronta citando o pior mês.

    Se `projecao_incompleta` for True, existe mês sem nenhum dado na janela e
    o veredito pode estar errado por falta de informação — diga isso ao
    usuário em vez de afirmar o resultado com segurança.

    NÃO use para registrar a compra depois de decidida: à vista use
    `registrar_gasto`, parcelada use `simular_parcelamento` seguido de
    `confirmar_parcelamento`.
    """
    mes = _mes_corrente(mes_ref)
    df = sheets.ler_lancamentos()
    config = sheets.ler_config()
    recorrentes = sheets.ler_recorrentes()

    resultado = financas.capacidade_de_compra(
        df,
        config,
        valor=valor,
        n_parcelas=n_parcelas,
        mes_ref=mes,
        recorrentes=recorrentes,
    )
    return _json(
        {
            "cabe": resultado["cabe"],
            "mes_ref": resultado["mes_ref"],
            "valor": resultado["valor"],
            "n_parcelas": resultado["n_parcelas"],
            "valor_parcela": resultado["valor_parcela"],
            "pior_mes": resultado["pior_mes"],
            "meses_inviaveis": resultado["meses_inviaveis"],
            "motivo": resultado["motivo"],
            "projecao_incompleta": resultado["projecao_incompleta"],
            "projecao": [
                {
                    "mes_ref": mes_projetado["mes_ref"],
                    "livre_por_semana": mes_projetado["livre_por_semana"],
                    "cabe": mes_projetado["cabe"],
                    "sintetizado": mes_projetado["sintetizado"],
                }
                for mes_projetado in resultado["projecao"]
            ],
        }
    )


# --------------------------------------------------------------------------
# tools de escrita — baixo risco
# --------------------------------------------------------------------------
@tool
@_protegido
def registrar_gasto(
    descricao: str,
    valor: float,
    categoria: str,
    mes_ref: str | None = None,
    natureza: str = "variada",
    data: str | None = None,
    ja_gastei: bool = True,
) -> dict[str, Any]:
    """Grava UM gasto na planilha. Escreve direto, sem simulação.

    BAIXO RISCO: uma linha só, sem efeito sobre meses futuros.

    `ja_gastei` decide o status, e a diferença importa:

    - `True` (padrão) → status "pago": o dinheiro JÁ saiu. É o caso de
      "gastei 80 no mercado", "paguei 150 de gasolina". Entra como gasto
      realizado do mês.
    - `False` → status "previsto": o gasto ainda VAI acontecer e está sendo
      reservado dentro do envelope. É o caso de "vou gastar uns 400 de
      mercado em setembro", "separa 200 para presente". Entra como
      comprometido, não como gasto realizado.

    Os dois consomem o envelope do mesmo jeito — o que muda é se o valor já
    passou pelo caixa. Na dúvida entre os dois, pergunte ao usuário.

    `natureza` é "variada" para gasto do dia a dia e "fixa" para conta que se
    repete todo mês. Sem `mes_ref`, o gasto entra no mês corrente; se a data
    informada for de outro mês, passe o `mes_ref` correspondente junto. Sem
    `data`, usa hoje.

    NÃO use para compra parcelada: aí o par é `simular_parcelamento` +
    `confirmar_parcelamento`. NÃO use para corrigir algo já lançado
    (`corrigir_lancamento`) nem para entradas — esta tool só grava saída.
    """
    mes = _mes_corrente(mes_ref)
    status = "pago" if ja_gastei else "previsto"
    id_gerado = sheets.adicionar_lancamento(
        data=data or date.today().isoformat(),
        mes_ref=mes,
        tipo="saida",
        natureza=natureza,
        categoria=categoria,
        descricao=descricao,
        valor=valor,
        status=status,
        origem="agente",
    )
    return _json(
        {
            "gravado": True,
            "id": id_gerado,
            "mes_ref": mes,
            "descricao": descricao,
            "valor": valor,
            "categoria": categoria,
            "natureza": natureza,
            "status": status,
        }
    )


@tool
@_protegido
def marcar_como_pago(
    descricao: str | None = None,
    id: str | None = None,
    mes_ref: str | None = None,
) -> dict[str, Any]:
    """Muda o status de UM lançamento de previsto para pago.

    BAIXO RISCO: altera uma linha e não cria nada. Use para "já paguei a
    faculdade", "a fatura foi paga". Identifique o lançamento pelo `id`
    (preferível) ou pela `descricao`; `mes_ref` estreita a busca.

    Esta tool é para SAÍDAS — conta, parcela, gasto. Para uma ENTRADA que
    caiu (salário, freela, reembolso) o status certo é "recebido" e a tool é
    `marcar_como_recebido`.

    Se mais de um lançamento casar com o filtro, a resposta vem como
    `{"erro": "ambiguo", "candidatos": [...]}` — NÃO escolha por conta
    própria: mostre os candidatos, pergunte ao usuário qual é e chame de novo
    com o `id`.

    Marcar como pago não muda o quanto sobra no mês: o valor só migra de
    comprometido para gasto realizado. NÃO use para lançar um gasto novo
    (`registrar_gasto`) nem para mudar valor ou categoria
    (`corrigir_lancamento`).
    """
    return _marcar("pago", "saida", "marcar_como_recebido", descricao, id, mes_ref)


@tool
@_protegido
def marcar_como_recebido(
    descricao: str | None = None,
    id: str | None = None,
    mes_ref: str | None = None,
) -> dict[str, Any]:
    """Muda o status de UMA entrada de previsto para recebido.

    BAIXO RISCO: altera uma linha e não cria nada. Use para "o salário caiu",
    "recebi o freela", "o reembolso entrou". Identifique o lançamento pelo
    `id` (preferível) ou pela `descricao`; `mes_ref` estreita a busca.

    Esta tool é para ENTRADAS — dinheiro que chegou. Para uma SAÍDA que foi
    quitada (conta, parcela, gasto) o status certo é "pago" e a tool é
    `marcar_como_pago`. Se você errar a tool, nada é gravado e a resposta diz
    qual usar.

    Se mais de um lançamento casar com o filtro, a resposta vem como
    `{"erro": "ambiguo", "candidatos": [...]}` — NÃO escolha por conta
    própria: pergunte ao usuário qual é e chame de novo com o `id`.
    """
    return _marcar("recebido", "entrada", "marcar_como_pago", descricao, id, mes_ref)


@tool
@_protegido
def corrigir_lancamento(
    descricao: str | None = None,
    id: str | None = None,
    mes_ref: str | None = None,
    valor: float | None = None,
    categoria: str | None = None,
    descricao_nova: str | None = None,
    natureza: str | None = None,
) -> dict[str, Any]:
    """Edita os campos de UM lançamento que já existe.

    BAIXO RISCO: altera uma linha, sem efeito em meses futuros. Use para "na
    verdade foram 90, não 80", "isso era mercado, não lazer". `descricao`
    serve para ACHAR a linha; `descricao_nova` é o texto que substitui.
    Informe ao menos um campo para alterar.

    Se mais de um lançamento casar com o filtro, a resposta vem como
    `{"erro": "ambiguo", "candidatos": [...]}` — NÃO escolha por conta
    própria: pergunte ao usuário qual é e chame de novo com o `id`.

    NÃO use para marcar como pago (`marcar_como_pago`), para criar um gasto
    (`registrar_gasto`) nem para mexer num parcelamento inteiro — esta tool
    altera uma parcela só.
    """
    resposta = sheets.atualizar_lancamento(
        id=id,
        descricao=descricao,
        mes_ref=mes_ref,
        valor=valor,
        categoria=categoria,
        descricao_nova=descricao_nova,
        natureza=natureza,
    )
    return _json({"gravado": True, "resposta": resposta})


# --------------------------------------------------------------------------
# tools de escrita — alto risco (sempre em par simular -> confirmar)
# --------------------------------------------------------------------------
@tool
@_protegido
def simular_parcelamento(
    descricao: str,
    valor_parcela: float,
    n_parcelas: int,
    mes_inicial: str,
    categoria: str,
    dia_do_mes: int | None = None,
) -> dict[str, Any]:
    """Mostra a prévia de um parcelamento SEM gravar nada.

    ALTO RISCO controlado: esta tool NUNCA escreve na planilha. Ela devolve
    as linhas que seriam criadas — uma por mês — para você mostrar ao usuário
    e pedir confirmação.

    Use sempre que o usuário for parcelar algo. `valor_parcela` é o valor de
    CADA parcela (em `simular_compra`, ao contrário, o valor é o total).
    `mes_inicial` é AAAA-MM. `dia_do_mes` é o dia do vencimento: informe
    quando o usuário disser; sem ele, um parcelamento que começa neste mês
    vence no dia de hoje e um que começa mais adiante vence no dia 1.

    Depois de mostrar a prévia e o usuário confirmar, e só então, chame
    `confirmar_parcelamento` com exatamente os mesmos argumentos — inclusive
    o `dia_do_mes` que veio nesta resposta. Se ele não confirmar, não chame
    nada.
    """
    dia = dia_do_mes if dia_do_mes is not None else _dia_padrao(mes_inicial)
    previa = sheets.adicionar_parcelamento(
        descricao=descricao,
        valor_parcela=valor_parcela,
        n_parcelas=n_parcelas,
        mes_inicial=mes_inicial,
        categoria=categoria,
        dia_do_mes=dia,
        dry_run=True,
    )
    # Só uma prévia que deu certo libera a confirmação correspondente.
    _registrar_simulacao(
        _assinatura(
            "parcelamento",
            descricao=descricao,
            valor_parcela=valor_parcela,
            n_parcelas=n_parcelas,
            mes_inicial=mes_inicial,
            categoria=categoria,
            dia_do_mes=dia,
        )
    )
    return _json(
        {
            "gravado": False,
            "dry_run": True,
            "descricao": descricao,
            "valor_parcela": valor_parcela,
            "n_parcelas": n_parcelas,
            "mes_inicial": mes_inicial,
            "dia_do_mes": dia,
            "previa": previa,
        }
    )


@tool
@_protegido
def confirmar_parcelamento(
    descricao: str,
    valor_parcela: float,
    n_parcelas: int,
    mes_inicial: str,
    categoria: str,
    dia_do_mes: int | None = None,
) -> dict[str, Any]:
    """GRAVA o parcelamento: cria uma linha em cada um dos meses futuros.

    ALTO RISCO: escreve em vários meses de uma vez e não se desfaz sozinho.

    Só pode ser chamada depois que `simular_parcelamento` foi executada, a
    prévia foi MOSTRADA ao usuário e ele CONFIRMOU explicitamente. Nunca
    chame esta tool direto, nunca na mesma resposta em que a prévia foi
    exibida, e nunca com argumentos diferentes dos que foram simulados — se
    algum valor mudou, simule de novo antes. Repasse o `dia_do_mes` que veio
    na prévia, para gravar exatamente o que o usuário confirmou.

    Isto não é só orientação: sem uma simulação recente e idêntica nesta
    conversa, a tool RECUSA e devolve `{"gravado": false, "erro":
    "sem_simulacao"}` sem escrever nada.
    """
    dia = dia_do_mes if dia_do_mes is not None else _dia_padrao(mes_inicial)
    if not _consumir_simulacao(
        _assinatura(
            "parcelamento",
            descricao=descricao,
            valor_parcela=valor_parcela,
            n_parcelas=n_parcelas,
            mes_inicial=mes_inicial,
            categoria=categoria,
            dia_do_mes=dia,
        )
    ):
        return _json(_erro_sem_simulacao("simular_parcelamento"))

    linhas = sheets.adicionar_parcelamento(
        descricao=descricao,
        valor_parcela=valor_parcela,
        n_parcelas=n_parcelas,
        mes_inicial=mes_inicial,
        categoria=categoria,
        dia_do_mes=dia,
        dry_run=False,
    )
    return _json(
        {
            "gravado": True,
            "dry_run": False,
            "descricao": descricao,
            "valor_parcela": valor_parcela,
            "n_parcelas": n_parcelas,
            "mes_inicial": mes_inicial,
            "dia_do_mes": dia,
            "linhas": linhas,
        }
    )


@tool
@_protegido
def simular_geracao_mes(mes_ref: str) -> dict[str, Any]:
    """Mostra a prévia dos lançamentos recorrentes de um mês SEM gravar nada.

    ALTO RISCO controlado: esta tool NUNCA escreve na planilha. Devolve as
    linhas que `confirmar_geracao_mes` criaria — salário, contas fixas e
    demais recorrentes ativas do mês.

    Use quando `ver_planejamento` disser que o mês ainda não foi
    materializado. Mostre a prévia ao usuário e peça confirmação; só depois
    chame `confirmar_geracao_mes` com o mesmo `mes_ref`.
    """
    previa = sheets.gerar_mes(mes_ref, dry_run=True)
    _registrar_simulacao(_assinatura("geracao_mes", mes_ref=mes_ref))
    return _json(
        {"gravado": False, "dry_run": True, "mes_ref": mes_ref, "previa": previa}
    )


@tool
@_protegido
def confirmar_geracao_mes(mes_ref: str) -> dict[str, Any]:
    """GRAVA os lançamentos recorrentes do mês na planilha.

    ALTO RISCO: cria várias linhas de uma vez e, rodada duas vezes no mesmo
    mês, DUPLICA tudo.

    Só pode ser chamada depois que `simular_geracao_mes` foi executada, a
    prévia foi MOSTRADA ao usuário e ele CONFIRMOU explicitamente. Nunca
    chame direto e nunca na mesma resposta em que a prévia foi exibida. Na
    dúvida sobre o mês já ter sido gerado, confira antes com
    `ver_planejamento`.

    Isto não é só orientação: sem uma simulação recente deste mesmo mês
    nesta conversa, a tool RECUSA e devolve `{"gravado": false, "erro":
    "sem_simulacao"}` sem escrever nada. Cada simulação vale por UMA
    confirmação — é o que impede o mês de ser duplicado.
    """
    if not _consumir_simulacao(_assinatura("geracao_mes", mes_ref=mes_ref)):
        return _json(_erro_sem_simulacao("simular_geracao_mes"))

    linhas = sheets.gerar_mes(mes_ref, dry_run=False)
    return _json(
        {"gravado": True, "dry_run": False, "mes_ref": mes_ref, "linhas": linhas}
    )


# --------------------------------------------------------------------------
# registro
# --------------------------------------------------------------------------
TOOLS_LEITURA: Final[list[Any]] = [
    ver_orcamento,
    ver_acompanhamento,
    ver_resumo,
    ver_gastos_por_categoria,
    ver_compromissos_futuros,
    ver_parcelas_em_aberto,
    ver_planejamento,
    comparar,
    simular_compra,
]

TOOLS_ESCRITA_BAIXO_RISCO: Final[list[Any]] = [
    registrar_gasto,
    marcar_como_pago,
    marcar_como_recebido,
    corrigir_lancamento,
]

TOOLS_ESCRITA_ALTO_RISCO: Final[list[Any]] = [
    simular_parcelamento,
    confirmar_parcelamento,
    simular_geracao_mes,
    confirmar_geracao_mes,
]

TOOLS: Final[list[Any]] = [
    *TOOLS_LEITURA,
    *TOOLS_ESCRITA_BAIXO_RISCO,
    *TOOLS_ESCRITA_ALTO_RISCO,
]
"""Todas as ferramentas, na ordem em que serão apresentadas ao modelo."""
