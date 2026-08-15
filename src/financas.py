"""Regras de negócio do agente financeiro. Funções puras, offline.

Não importa `sheets.py`, não fala com a rede e não lê variáveis de
ambiente: recebe o DataFrame de lançamentos e o dict de config como
argumentos. Todo dinheiro é somado em Decimal e arredondado para 2 casas
(ROUND_HALF_UP) antes de virar float.

Vocabulário das regras:
- sobra          = entradas - saídas do mês
- reserva        = pct_investimento aplicado sobre a sobra (o que se guarda)
- livre/semana   = (sobra - reserva) / semanas_no_mes
- comprometido   = soma das PARCELAS do mês (linhas com parcela_total)
- comprometimento máximo = limite_comprometimento sobre as entradas
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Iterable

import pandas as pd

STATUS_REALIZADO: Final[frozenset[str]] = frozenset({"pago", "recebido"})
CENTAVO: Final[Decimal] = Decimal("0.01")
ORIGEM_RECORRENCIA: Final[str] = "recorrencia"
ORIGEM_PROJETADO: Final[str] = "projetado"

Config = dict[str, Any]


# --------------------------------------------------------------------------
# dinheiro e calendário
# --------------------------------------------------------------------------
def _d(valor: Any) -> Decimal:
    """Converte para Decimal sem herdar o ruído binário do float."""
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return Decimal("0")
    return Decimal(str(valor))


def _r2(valor: Decimal | float) -> float:
    """Arredonda para centavos com ROUND_HALF_UP (não o banker's rounding)."""
    return float(_d(valor).quantize(CENTAVO, rounding=ROUND_HALF_UP))


def _somar(valores: Iterable[Any]) -> Decimal:
    total = Decimal("0")
    for valor in valores:
        if valor is None or (isinstance(valor, float) and pd.isna(valor)):
            continue
        total += _d(valor)
    return total


def _partes_mes(mes_ref: str) -> tuple[int, int]:
    texto = str(mes_ref).strip()
    ano, _, mes = texto.partition("-")
    if len(ano) != 4 or len(mes) != 2 or not (ano.isdigit() and mes.isdigit()):
        raise ValueError(f"mes_ref inválido: {mes_ref!r}. Formato esperado 'AAAA-MM'")
    numero = int(mes)
    if not 1 <= numero <= 12:
        raise ValueError(f"mes_ref inválido: {mes_ref!r}. Mês fora de 01..12")
    return int(ano), numero


def somar_meses(mes_ref: str, n: int) -> str:
    """'2026-11' + 3 -> '2027-02'. Aritmética de calendário sem depender de data real."""
    ano, mes = _partes_mes(mes_ref)
    total = (ano * 12 + (mes - 1)) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def janela_de_meses(mes_ref: str, n_meses: int) -> list[str]:
    """Lista de n meses começando NO mes_ref (inclusive)."""
    if n_meses < 1:
        raise ValueError("n_meses deve ser >= 1")
    return [somar_meses(mes_ref, i) for i in range(n_meses)]


def mes_ref_de_hoje(hoje: date | None = None) -> str:
    """mes_ref do mês corrente. Recebe `hoje` para manter os testes determinísticos."""
    referencia = hoje or date.today()
    return f"{referencia.year:04d}-{referencia.month:02d}"


def mes_de_acompanhamento(hoje: date | None = None) -> str:
    """Mês que se acompanha: o corrente. É nele que se olha o envelope."""
    return mes_ref_de_hoje(hoje)


def mes_de_planejamento(hoje: date | None = None) -> str:
    """Mês que se planeja: o seguinte. Planeja-se o mês que ainda não começou."""
    return somar_meses(mes_ref_de_hoje(hoje), 1)


def fase_do_mes(hoje: date | None = None) -> str:
    """'inicio', 'meio' ou 'fim' — por terço do mês, não por dia fixo.

    O terço é calculado sobre o tamanho real do mês: dia 10 é início em
    março (31 dias) e também em fevereiro (28).
    """
    referencia = hoje or date.today()
    dias = monthrange(referencia.year, referencia.month)[1]
    terco = Decimal(dias) / 3
    if referencia.day <= terco:
        return "inicio"
    if referencia.day <= terco * 2:
        return "meio"
    return "fim"


# --------------------------------------------------------------------------
# recortes do DataFrame
# --------------------------------------------------------------------------
def _do_mes(df: pd.DataFrame, mes_ref: str) -> pd.DataFrame:
    _partes_mes(mes_ref)
    if df.empty:
        return df
    return df[df["mes_ref"].astype("string").str.strip() == mes_ref]


def _totais(df_mes: pd.DataFrame) -> dict[str, float]:
    """Entradas, saídas fixa/variada e sobra de um recorte já filtrado."""
    if df_mes.empty:
        return {
            "entradas": 0.0,
            "saida_fixa": 0.0,
            "saida_variada": 0.0,
            "saidas": 0.0,
            "sobra": 0.0,
        }

    tipo = df_mes["tipo"].astype("string").str.lower()
    natureza = df_mes["natureza"].astype("string").str.lower()

    entradas = _somar(df_mes.loc[tipo == "entrada", "valor"])
    saidas = tipo == "saida"
    fixa = _somar(df_mes.loc[saidas & (natureza == "fixa"), "valor"])
    variada = _somar(df_mes.loc[saidas & (natureza == "variada"), "valor"])

    return {
        "entradas": _r2(entradas),
        "saida_fixa": _r2(fixa),
        "saida_variada": _r2(variada),
        "saidas": _r2(fixa + variada),
        "sobra": _r2(entradas - fixa - variada),
    }


def _parcelas_do_mes(df_mes: pd.DataFrame) -> float:
    """Soma das saídas que são parcela de algo (linhas com parcela_total)."""
    if df_mes.empty:
        return 0.0
    tipo = df_mes["tipo"].astype("string").str.lower()
    parcelado = df_mes["parcela_total"].notna()
    return _r2(_somar(df_mes.loc[(tipo == "saida") & parcelado, "valor"]))


def _coluna(df: pd.DataFrame, nome: str) -> pd.Series:
    """Coluna de texto normalizada em minúsculas, tolerante a coluna ausente."""
    if nome not in df.columns:
        return pd.Series("", index=df.index, dtype="string")
    return df[nome].astype("string").str.strip().str.lower()


def _tem_recorrencia(df_mes: pd.DataFrame) -> bool:
    """O mês já foi materializado por gerar_mes?"""
    if df_mes.empty:
        return False
    return bool((_coluna(df_mes, "origem") == ORIGEM_RECORRENCIA).any())


def _dia_valido(mes_ref: str, dia: Any) -> int:
    """Dia 31 em mês de 30 cai no último dia, em vez de virar data inválida."""
    ano, mes = _partes_mes(mes_ref)
    ultimo = monthrange(ano, mes)[1]
    try:
        numero = int(dia)
    except (TypeError, ValueError):
        numero = 1
    return max(1, min(numero, ultimo))


def _normalizar_tipos(df: pd.DataFrame) -> pd.DataFrame:
    """Devolve os dtypes de sheets.ler_lancamentos depois de um concat."""
    for coluna in ("parcela_atual", "parcela_total"):
        if coluna in df.columns:
            df[coluna] = pd.to_numeric(df[coluna], errors="coerce").astype("Int64")
    if "valor" in df.columns:
        df["valor"] = pd.to_numeric(df["valor"], errors="coerce").astype("float64")
    for coluna in ("data", "criado_em"):
        if coluna in df.columns:
            df[coluna] = pd.to_datetime(df[coluna], errors="coerce")
    for coluna in ("id", "mes_ref", "categoria", "descricao", "grupo_id"):
        if coluna in df.columns:
            df[coluna] = df[coluna].astype("string")
    for coluna in ("tipo", "natureza", "status", "origem"):
        if coluna in df.columns:
            df[coluna] = df[coluna].astype("string").str.lower()
    return df


def projetar_janela(
    df: pd.DataFrame,
    recorrentes: pd.DataFrame | None,
    mes_inicial: str,
    n_meses: int = 12,
) -> pd.DataFrame:
    """Preenche em memória os meses da janela que ainda não foram gerados.

    A planilha só tem linhas futuras onde existem parcelas: salário e
    recorrentes de um mês só nascem quando `gerar_mes` roda. Sem isso, todo
    mês futuro parece ter sobra zero.

    Regra: mês que já tem alguma linha com origem="recorrencia" é dado real
    e não recebe nada. Os demais recebem as recorrentes ativas como
    status="previsto" e origem="projetado". Parcelas e lançamentos reais
    são sempre preservados; nada é gravado na planilha.
    """
    if recorrentes is None or len(recorrentes) == 0:
        return df

    ativos = recorrentes
    if "ativo" in ativos.columns:
        ativos = ativos[ativos["ativo"].astype("boolean").fillna(False)]
    if ativos.empty:
        return df

    colunas = list(df.columns)
    sinteticas: list[dict[str, Any]] = []
    for mes in janela_de_meses(mes_inicial, n_meses):
        if _tem_recorrencia(_do_mes(df, mes)):
            continue
        for _, recorrente in ativos.iterrows():
            dia = _dia_valido(mes, recorrente.get("dia_do_mes"))
            base = {
                "id": "",
                "data": f"{mes}-{dia:02d}",
                "mes_ref": mes,
                "tipo": str(recorrente.get("tipo", "saida")).strip().lower(),
                "natureza": str(recorrente.get("natureza", "fixa")).strip().lower(),
                "categoria": recorrente.get("categoria", ""),
                "descricao": recorrente.get("descricao", ""),
                "valor": recorrente.get("valor", 0.0),
                "parcela_atual": None,
                "parcela_total": None,
                "grupo_id": None,
                "status": "previsto",
                "origem": ORIGEM_PROJETADO,
                "criado_em": None,
            }
            sinteticas.append({coluna: base.get(coluna) for coluna in colunas})

    if not sinteticas:
        return df

    projetadas = pd.DataFrame(sinteticas, columns=colunas)
    ampliado = pd.concat([df, projetadas], ignore_index=True)
    return _normalizar_tipos(ampliado)


def _config(config: Config) -> tuple[Decimal, int, bool, Decimal]:
    """Extrai e valida os parâmetros usados nas contas."""
    pct = _d(config.get("pct_investimento", 0))
    semanas = int(config.get("semanas_no_mes", 4) or 0)
    if semanas <= 0:
        raise ValueError("semanas_no_mes deve ser >= 1 na CONFIG")
    intocavel = bool(config.get("reserva_intocavel", True))
    limite = _d(config.get("limite_comprometimento", 1))
    return pct, semanas, intocavel, limite


# --------------------------------------------------------------------------
# 1. resumo do mês
# --------------------------------------------------------------------------
def resumo_mes(df: pd.DataFrame, config: Config, mes_ref: str) -> dict[str, Any]:
    """Fecha o mês: sobra, reserva e quanto sobra por semana.

    Reserva = pct_investimento sobre a sobra (zero se a sobra for negativa).
    Livre por semana = (sobra - reserva) / semanas_no_mes.
    Realizado (status pago/recebido) e previsto vêm separados, e a soma dos
    dois é o total.
    """
    pct, semanas, _, _ = _config(config)
    do_mes = _do_mes(df, mes_ref)

    total = _totais(do_mes)
    if do_mes.empty:
        realizado, previsto = _totais(do_mes), _totais(do_mes)
    else:
        status = do_mes["status"].astype("string").str.lower()
        marca = status.isin(STATUS_REALIZADO)
        realizado = _totais(do_mes[marca])
        previsto = _totais(do_mes[~marca])

    sobra = _d(total["sobra"])
    reserva = _r2(sobra * pct) if sobra > 0 else 0.0
    livre_por_semana = _r2((sobra - _d(reserva)) / semanas)

    return {
        "mes_ref": mes_ref,
        **total,
        "reserva": reserva,
        "livre_por_semana": livre_por_semana,
        "realizado": realizado,
        "previsto": previsto,
    }


# --------------------------------------------------------------------------
# 1b. acompanhamento do mês (modelo de envelope)
# --------------------------------------------------------------------------
LIMITE_ATENCAO: Final[Decimal] = Decimal("0.70")
LIMITE_NO_LIMITE: Final[Decimal] = Decimal("0.90")


def orcamento_mes(df: pd.DataFrame, config: Config, mes_ref: str) -> dict[str, Any]:
    """Lente de PLANEJAMENTO: quanto o mês comporta, antes de qualquer gasto.

    Não olha status e não depende de `hoje` — responde "como fica setembro".
    Da renda saem as fixas; do que resta congela-se a reserva; o restante é
    o envelope do variável, que é o teto de gasto do mês.
    """
    pct, semanas, _, _ = _config(config)
    totais = _totais(_do_mes(df, mes_ref))

    renda = _d(totais["entradas"])
    fixas = _d(totais["saida_fixa"])
    sobra_planejada = renda - fixas
    # A reserva é congelada sobre o planejado: gasto variável não a reduz.
    reserva = sobra_planejada * pct if sobra_planejada > 0 else Decimal("0")
    envelope = sobra_planejada - reserva

    return {
        "mes_ref": mes_ref,
        "renda": _r2(renda),
        "fixas": _r2(fixas),
        "sobra_planejada": _r2(sobra_planejada),
        "reserva_planejada": _r2(reserva),
        "envelope_variavel": _r2(envelope),
        "envelope_semanal": _r2(envelope / semanas),
    }


def acompanhamento_mes(
    df: pd.DataFrame, config: Config, mes_ref: str
) -> dict[str, Any]:
    """Acompanha o envelope do variável, sem contar o mesmo dinheiro duas vezes.

    Uma variada com status "previsto" é RESERVA DENTRO DO ENVELOPE — gasto
    planejado, ainda não realizado —, não gasto feito. Por isso o mês tem
    três números e não um:

    - gasto_realizado: variadas já pagas
    - comprometido:    variadas previstas, ainda por gastar
    - livre:           envelope - gasto_realizado - comprometido

    `restante` é envelope - gasto_realizado (o que ainda passa pelo caixa);
    `livre` é o que dá para gastar sem furar nenhum plano já feito.

    O envelope vem de `orcamento_mes` — o teto é sempre o mesmo número, seja
    olhando o mês antes de começar ou no meio dele. Isso difere de
    `resumo_mes`, que fecha o mês com as variadas já lançadas como saída —
    são duas lentes do mesmo mês, e é `acompanhamento_mes` que se usa
    durante o mês.
    """
    _, semanas, _, _ = _config(config)
    do_mes = _do_mes(df, mes_ref)

    # Uma fonte só para o envelope: o orçamento planejado do mês.
    orcamento = orcamento_mes(df, config, mes_ref)
    envelope = _d(orcamento["envelope_variavel"])

    if do_mes.empty:
        gasto_realizado = comprometido = Decimal("0")
    else:
        tipo = do_mes["tipo"].astype("string").str.lower()
        natureza = do_mes["natureza"].astype("string").str.lower()
        status = do_mes["status"].astype("string").str.lower()
        variadas = do_mes[(tipo == "saida") & (natureza == "variada")]
        realizadas = status.reindex(variadas.index).isin(STATUS_REALIZADO)
        gasto_realizado = _somar(variadas.loc[realizadas, "valor"])
        comprometido = _somar(variadas.loc[~realizadas, "valor"])

    usado = gasto_realizado + comprometido
    livre = envelope - usado
    restante = envelope - gasto_realizado

    return {
        "mes_ref": mes_ref,
        "entradas": orcamento["renda"],
        "saida_fixa": orcamento["fixas"],
        "reserva": orcamento["reserva_planejada"],
        "envelope_variavel": orcamento["envelope_variavel"],
        "envelope_semanal": orcamento["envelope_semanal"],
        "gasto_realizado": _r2(gasto_realizado),
        "comprometido": _r2(comprometido),
        "livre": _r2(livre),
        "restante": _r2(restante),
        "livre_por_semana": _r2(livre / semanas),
        "percentual_usado": _r2(usado / envelope * 100) if envelope > 0 else 0.0,
        # O que já foi gasto MAIS o que já está prometido decide a situação.
        "invade_reserva": bool(usado > envelope),
        "situacao": _situacao(usado, envelope),
    }


def _situacao(usado: Decimal, envelope: Decimal) -> str:
    """Semáforo do envelope, sempre sobre gasto_realizado + comprometido."""
    if envelope <= 0:
        return "estourado" if usado > 0 else "sem_envelope"
    if usado > envelope:
        return "estourado"
    if usado >= envelope * LIMITE_NO_LIMITE:
        return "no_limite"
    if usado >= envelope * LIMITE_ATENCAO:
        return "atencao"
    return "tranquilo"


# --------------------------------------------------------------------------
# 2. gastos por categoria
# --------------------------------------------------------------------------
def gastos_por_categoria(df: pd.DataFrame, mes_ref: str) -> pd.DataFrame:
    """Saídas do mês agrupadas por categoria, da maior para a menor.

    Percentual é sobre o total de saídas do mês; mês sem saídas devolve
    tabela vazia em vez de dividir por zero.
    """
    colunas = ["categoria", "valor", "percentual"]
    do_mes = _do_mes(df, mes_ref)
    if do_mes.empty:
        return pd.DataFrame(columns=colunas)

    saidas = do_mes[do_mes["tipo"].astype("string").str.lower() == "saida"]
    if saidas.empty:
        return pd.DataFrame(columns=colunas)

    agrupado = (
        saidas.assign(categoria=saidas["categoria"].astype("string").fillna(""))
        .groupby("categoria", dropna=False)["valor"]
        .apply(lambda serie: _r2(_somar(serie)))
        .reset_index()
    )
    total = _somar(agrupado["valor"])
    agrupado["percentual"] = [
        _r2(_d(valor) / total * 100) if total > 0 else 0.0
        for valor in agrupado["valor"]
    ]
    return (
        agrupado.sort_values(["valor", "categoria"], ascending=[False, True])
        .reset_index(drop=True)[colunas]
    )


# --------------------------------------------------------------------------
# 2b. lançamentos individuais do mês
# --------------------------------------------------------------------------
COLUNAS_LISTAGEM: Final[list[str]] = [
    "descricao",
    "valor",
    "tipo",
    "natureza",
    "categoria",
    "status",
    "parcela_atual",
    "parcela_total",
]

TIPOS: Final[frozenset[str]] = frozenset({"entrada", "saida"})
NATUREZAS: Final[frozenset[str]] = frozenset({"fixa", "variada"})

ORDEM_NATUREZA: Final[dict[str, int]] = {"fixa": 0, "variada": 1}
"""Fixa antes de variada. Natureza desconhecida vai para o fim."""

_APELIDOS: Final[dict[str, str]] = {
    "saída": "saida",
    "saídas": "saida",
    "saidas": "saida",
    "entradas": "entrada",
}
"""O filtro vem de texto livre: 'saídas' tem que achar as saídas, não zero."""


def _filtro(valor: str, permitidos: frozenset[str], rotulo: str) -> str:
    """Normaliza o filtro e recusa o que não existe.

    Recusar é melhor que devolver lista vazia: filtro escrito errado some
    com todas as linhas em silêncio, e quem lê acha que o mês está vazio.
    """
    texto = str(valor).strip().lower()
    texto = _APELIDOS.get(texto, texto)
    if texto not in permitidos:
        opcoes = ", ".join(sorted(permitidos))
        raise ValueError(f"{rotulo}: {valor!r}. Use um de: {opcoes}")
    return texto


def _tabela_de_lancamentos(df_mes: pd.DataFrame) -> pd.DataFrame:
    """Fixas primeiro, e dentro de cada natureza do maior valor para o menor."""
    if df_mes.empty:
        return pd.DataFrame(columns=COLUNAS_LISTAGEM)

    ordem = pd.to_numeric(
        _coluna(df_mes, "natureza").map(ORDEM_NATUREZA), errors="coerce"
    ).fillna(len(ORDEM_NATUREZA))
    return (
        df_mes.assign(_ordem=ordem)
        .sort_values(
            ["_ordem", "valor", "descricao"], ascending=[True, False, True]
        )
        .reset_index(drop=True)[COLUNAS_LISTAGEM]
    )


def listar_lancamentos(
    df: pd.DataFrame,
    mes_ref: str,
    tipo: str | None = None,
    natureza: str | None = None,
    status: str | None = None,
    apenas_parcelas: bool = False,
) -> dict[str, Any]:
    """Os lançamentos de um mês, um a um, mais os subtotais desse recorte.

    Existe para responder "quais são as saídas de setembro?": as outras
    funções devolvem total ou agrupamento, e nenhuma mostra as linhas que
    compõem o número.

    Ordem: fixas antes das variadas e, dentro de cada natureza, do maior
    valor para o menor. `parcela_atual` e `parcela_total` vêm preenchidos só
    nas linhas que são parcela de algo; nas demais, None.

    Os quatro subtotais são do recorte JÁ FILTRADO — filtrar muda a lista e
    os totais juntos. O encaixe entre eles não é plano:

        total_entradas
        total_fixas      ← já INCLUI total_parcelas
          total_parcelas   (recorte DENTRO das fixas, não categoria irmã)
        total_variadas

    Saídas do mês = total_fixas + total_variadas. Somar `total_parcelas` aí
    conta o mesmo dinheiro duas vezes.

    `tipo` e `natureza` só aceitam os valores que existem na planilha
    ("entrada"/"saida", "fixa"/"variada"); qualquer outra coisa levanta
    ValueError em vez de devolver lista vazia. `status` é livre (pago,
    recebido, previsto). `apenas_parcelas` deixa só as linhas parceladas.
    """
    filtro_tipo = _filtro(tipo, TIPOS, "tipo inválido") if tipo is not None else None
    filtro_natureza = (
        _filtro(natureza, NATUREZAS, "natureza inválida")
        if natureza is not None
        else None
    )
    filtro_status = str(status).strip().lower() if status is not None else None

    do_mes = _do_mes(df, mes_ref)
    if not do_mes.empty:
        marca = pd.Series(True, index=do_mes.index)
        if filtro_tipo is not None:
            marca &= _coluna(do_mes, "tipo") == filtro_tipo
        if filtro_natureza is not None:
            marca &= _coluna(do_mes, "natureza") == filtro_natureza
        if filtro_status is not None:
            marca &= _coluna(do_mes, "status") == filtro_status
        if apenas_parcelas:
            marca &= do_mes["parcela_total"].notna()
        do_mes = do_mes[marca.fillna(False).astype(bool)]

    totais = _totais(do_mes)
    return {
        "mes_ref": mes_ref,
        "lancamentos": _tabela_de_lancamentos(do_mes),
        "total_entradas": totais["entradas"],
        "total_fixas": totais["saida_fixa"],
        "total_parcelas": _parcelas_do_mes(do_mes),
        "total_variadas": totais["saida_variada"],
    }


# --------------------------------------------------------------------------
# 3. compromissos futuros
# --------------------------------------------------------------------------
def compromissos_futuros(
    df: pd.DataFrame, mes_ref: str, n_meses: int = 12
) -> pd.DataFrame:
    """Fotografia mês a mês do que já está lançado à frente.

    A janela começa NO mes_ref (inclusive). Esta função não projeta nada: as
    parcelas futuras já existem como linhas, então isto é só agregação.

    `sintetizado` diz se aquele mês foi preenchido por `projetar_janela`
    antes de chegar aqui (linhas com origem="projetado"). Sem esse aviso, um
    mês inteiro vindo das recorrentes seria lido como fato consumado.
    """
    linhas: list[dict[str, Any]] = []
    for mes in janela_de_meses(mes_ref, n_meses):
        do_mes = _do_mes(df, mes)
        total = _totais(do_mes)
        linhas.append(
            {
                "mes_ref": mes,
                "entradas": total["entradas"],
                "saida_fixa": total["saida_fixa"],
                "saida_variada": total["saida_variada"],
                "saidas": total["saidas"],
                "parcelas": _parcelas_do_mes(do_mes),
                "sobra": total["sobra"],
                "sintetizado": _e_sintetizado(do_mes),
            }
        )
    return pd.DataFrame(linhas)


def _e_sintetizado(df_mes: pd.DataFrame) -> bool:
    """O mês veio de `projetar_janela` em vez da planilha?"""
    if df_mes.empty:
        return False
    return bool((_coluna(df_mes, "origem") == ORIGEM_PROJETADO).any())


def tipos_do_alvo(
    df: pd.DataFrame,
    id: str | None = None,
    descricao: str | None = None,
    mes_ref: str | None = None,
) -> list[str]:
    """Tipos ("entrada"/"saida") dos lançamentos que casam com o filtro.

    Existe para a camada de escrita poder recusar "marcar como recebido" em
    uma saída (e vice-versa) ANTES de tocar a planilha. A comparação de
    descrição é exata (sem acento removido, sem parcial) de propósito: é
    melhor não achar nada e deixar a planilha decidir do que bloquear uma
    escrita legítima por um casamento frouxo.

    Lista vazia significa "não encontrei aqui", não "não existe".
    """
    if df.empty or (not id and not descricao):
        return []

    marca = pd.Series(True, index=df.index)
    if id:
        marca &= (_coluna(df, "id") == str(id).strip().lower()).fillna(False)
    if descricao:
        marca &= (
            _coluna(df, "descricao") == str(descricao).strip().lower()
        ).fillna(False)
    if mes_ref:
        marca &= (
            df["mes_ref"].astype("string").str.strip() == mes_ref
        ).fillna(False)

    achados = df[marca.astype(bool)]
    if achados.empty:
        return []
    return sorted({tipo for tipo in _coluna(achados, "tipo").dropna() if tipo})


# --------------------------------------------------------------------------
# 4. parcelas em aberto
# --------------------------------------------------------------------------
def parcelas_em_aberto(df: pd.DataFrame, mes_ref: str) -> pd.DataFrame:
    """Parcelamentos com parcelas em mes_ref ou depois, agrupados por grupo_id.

    Só olha o que já está na tabela: quantas parcelas restam, quanto ainda
    falta pagar e em que mês termina.
    """
    colunas = [
        "grupo_id",
        "descricao",
        "categoria",
        "valor_parcela",
        "proxima_parcela",
        "parcela_total",
        "parcelas_restantes",
        "valor_restante",
        "ultimo_mes",
    ]
    if df.empty:
        return pd.DataFrame(columns=colunas)

    _partes_mes(mes_ref)
    tipo = df["tipo"].astype("string").str.lower()
    futuras = df[
        (tipo == "saida")
        & df["parcela_total"].notna()
        & (df["mes_ref"].astype("string").str.strip() >= mes_ref)
    ]
    if futuras.empty:
        return pd.DataFrame(columns=colunas)

    grupos = futuras["grupo_id"].astype("string").fillna("")
    linhas: list[dict[str, Any]] = []
    for grupo, bloco in futuras.assign(grupo_id=grupos).groupby("grupo_id"):
        bloco = bloco.sort_values("mes_ref")
        primeira = bloco.iloc[0]
        linhas.append(
            {
                "grupo_id": grupo,
                "descricao": primeira["descricao"],
                "categoria": primeira["categoria"],
                "valor_parcela": _r2(_d(primeira["valor"])),
                "proxima_parcela": primeira["parcela_atual"],
                "parcela_total": primeira["parcela_total"],
                "parcelas_restantes": int(len(bloco)),
                "valor_restante": _r2(_somar(bloco["valor"])),
                "ultimo_mes": str(bloco.iloc[-1]["mes_ref"]),
            }
        )
    return (
        pd.DataFrame(linhas, columns=colunas)
        .sort_values(["valor_restante", "descricao"], ascending=[False, True])
        .reset_index(drop=True)
    )


# --------------------------------------------------------------------------
# 5. capacidade de compra (janela de 12 meses)
# --------------------------------------------------------------------------
def _avaliar_mes_com_compra(
    df: pd.DataFrame,
    config: Config,
    mes_ref: str,
    parcela_nova: Decimal,
    conta_no_comprometimento: bool,
) -> dict[str, Any]:
    """Situação de UM mês depois da compra.

    Inviável quando, com reserva_intocavel, o que sobra não cobre mais a
    reserva daquele mês (a reserva não cede para pagar a parcela), ou
    quando as parcelas passam de limite_comprometimento sobre as entradas.
    """
    pct, semanas, intocavel, limite = _config(config)
    do_mes = _do_mes(df, mes_ref)
    total = _totais(do_mes)

    sobra_antes = _d(total["sobra"])
    reserva_antes = sobra_antes * pct if sobra_antes > 0 else Decimal("0")
    sobra_depois = sobra_antes - parcela_nova

    # Com reserva intocável a reserva não encolhe para financiar a compra.
    if intocavel:
        reserva_depois = reserva_antes
    else:
        reserva_depois = sobra_depois * pct if sobra_depois > 0 else Decimal("0")

    livre_depois = (sobra_depois - reserva_depois) / semanas
    entradas = _d(total["entradas"])
    comprometido = _d(_parcelas_do_mes(do_mes)) + (
        parcela_nova if conta_no_comprometimento else Decimal("0")
    )
    teto_comprometimento = entradas * limite

    reserva_ok = sobra_depois >= reserva_depois if intocavel else sobra_depois >= 0
    comprometimento_ok = comprometido <= teto_comprometimento
    afetado = parcela_nova > 0

    return {
        "mes_ref": mes_ref,
        "entradas": total["entradas"],
        "comprometido": _r2(comprometido),
        "teto_comprometimento": _r2(teto_comprometimento),
        "sobra": _r2(sobra_depois),
        "sobra_antes": total["sobra"],
        "reserva": _r2(reserva_depois),
        "livre_por_semana": _r2(livre_depois),
        "livre_por_semana_antes": _r2(
            (sobra_antes - reserva_antes) / semanas
        ),
        "parcela_nova": _r2(parcela_nova),
        "afetado": afetado,
        "sintetizado": _e_sintetizado(do_mes),
        "vazio": bool(do_mes.empty),
        # Um mês que a compra não toca não pode ficar inviável por causa dela.
        "cabe": bool((reserva_ok and comprometimento_ok) or not afetado),
        "motivo_reserva": not reserva_ok,
        "motivo_comprometimento": not comprometimento_ok,
    }


def capacidade_de_compra(
    df: pd.DataFrame,
    config: Config,
    valor: float,
    n_parcelas: int,
    mes_ref: str | None = None,
    n_meses: int = 12,
    recorrentes: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Diz se uma compra cabe olhando os próximos 12 meses, não só o mês atual.

    A parcela nova entra em cada mês da janela junto das parcelas que já
    estão lançadas lá. Basta UM mês afetado ficar inviável para `cabe` ser
    False, e o motivo cita o pior mês (o de menor livre por semana entre os
    meses que a compra realmente toca).

    Compra à vista (n_parcelas = 1) não conta como comprometimento futuro,
    só consome a sobra do mês.

    Passe `recorrentes` para que os meses ainda não gerados sejam projetados
    em memória. Sem isso, mês sem lançamento entra zerado na janela e a
    compra é recusada por falta de dado — nesse caso o `motivo` termina com
    um aviso de projeção incompleta.
    """
    if n_parcelas < 1:
        raise ValueError("n_parcelas deve ser >= 1")
    mes_ref = mes_ref or mes_ref_de_hoje()

    valor_total = _d(valor)
    parcela = _d(_r2(valor_total / n_parcelas))
    meses = janela_de_meses(mes_ref, n_meses)
    afetados = set(janela_de_meses(mes_ref, n_parcelas))
    conta_comprometimento = n_parcelas > 1

    df_janela = projetar_janela(df, recorrentes, mes_ref, n_meses)
    avaliacoes = [
        _avaliar_mes_com_compra(
            df_janela,
            config,
            mes,
            parcela if mes in afetados else Decimal("0"),
            conta_comprometimento,
        )
        for mes in meses
    ]

    projecao = [
        {
            "mes_ref": item["mes_ref"],
            "comprometido": item["comprometido"],
            "sobra": item["sobra"],
            "livre_por_semana": item["livre_por_semana"],
            "cabe": item["cabe"],
            "sintetizado": item["sintetizado"],
        }
        for item in avaliacoes
    ]

    inviaveis = [item["mes_ref"] for item in avaliacoes if not item["cabe"]]
    cabe = not inviaveis

    # O pior mês tem que ser um mês que a compra atinge: não adianta culpar
    # um mês que a parcela sequer alcança.
    pior = min(
        (item for item in avaliacoes if item["mes_ref"] in afetados),
        key=lambda item: item["livre_por_semana"],
    )
    atual = avaliacoes[0]
    vazios = [item["mes_ref"] for item in avaliacoes if item["vazio"]]

    livre_antes = _d(atual["livre_por_semana_antes"])
    livre_depois = _d(atual["livre_por_semana"])
    if livre_antes > 0:
        reducao = _r2((livre_antes - livre_depois) / livre_antes * 100)
    else:
        reducao = 0.0 if livre_antes == livre_depois else 100.0

    return {
        "cabe": cabe,
        "valor": _r2(valor_total),
        "n_parcelas": int(n_parcelas),
        "valor_parcela": _r2(parcela),
        "mes_ref": mes_ref,
        "sobra_depois": atual["sobra"],
        "nova_reserva": atual["reserva"],
        "novo_livre_semanal": atual["livre_por_semana"],
        "livre_semanal_antes": atual["livre_por_semana_antes"],
        "reducao_percentual_semanal": reducao,
        "meses_afetados": [mes for mes in meses if mes in afetados],
        "projecao": projecao,
        "pior_mes": {
            "mes_ref": pior["mes_ref"],
            "livre_por_semana": pior["livre_por_semana"],
        },
        "meses_inviaveis": inviaveis,
        "meses_sintetizados": [
            item["mes_ref"] for item in avaliacoes if item["sintetizado"]
        ],
        "projecao_incompleta": bool(vazios),
        "motivo": _motivo(cabe, pior, avaliacoes, inviaveis, vazios),
    }


def _motivo(
    cabe: bool,
    pior: dict[str, Any],
    avaliacoes: list[dict[str, Any]],
    inviaveis: list[str],
    vazios: list[str],
) -> str:
    """Frase única, sempre citando o pior mês pelo nome.

    Se a janela tem mês sem nenhum lançamento, o aviso de projeção
    incompleta fecha a frase: o veredito pode estar errado por falta de
    dado, e isso não pode passar em silêncio.
    """
    pior_mes = pior["mes_ref"]
    livre = pior["livre_por_semana"]
    aviso = ""
    if vazios:
        aviso = (
            f" Aviso: projeção incompleta — {len(vazios)} mês(es) da janela "
            f"({', '.join(vazios)}) não têm nenhum lançamento e nenhuma lista de "
            "recorrentes foi informada; rode gerar_mes ou passe `recorrentes`."
        )

    if cabe:
        return (
            f"Cabe: mesmo no mês mais apertado ({pior_mes}) ainda sobram "
            f"R$ {livre:.2f} por semana."
        ) + aviso

    por_mes = {item["mes_ref"]: item for item in avaliacoes}
    critico = por_mes.get(inviaveis[0], pior)
    if critico["motivo_reserva"]:
        detalhe = (
            f"a parcela invade a reserva de R$ {critico['reserva']:.2f} "
            f"em {critico['mes_ref']}"
        )
    else:
        detalhe = (
            f"as parcelas de {critico['mes_ref']} chegam a "
            f"R$ {critico['comprometido']:.2f}, acima do teto de "
            f"R$ {critico['teto_comprometimento']:.2f}"
        )
    return (
        f"Não cabe: o pior mês é {pior_mes}, com R$ {livre:.2f} livres por "
        f"semana — {detalhe}. Meses inviáveis: {', '.join(inviaveis)}."
    ) + aviso


# --------------------------------------------------------------------------
# 6. status do planejamento de um mês
# --------------------------------------------------------------------------
def planejamento_status(
    df: pd.DataFrame,
    recorrentes: pd.DataFrame | None,
    mes_ref: str,
    config: Config | None = None,
) -> dict[str, Any]:
    """O que já existe e o que falta para o mês estar planejado.

    `materializado` diz se `gerar_mes` já rodou (há linha com
    origem="recorrencia"); `faltando` lista as recorrentes ativas que ainda
    não têm linha no mês. `previa_orcamento` usa `projetar_janela` para
    responder "como fica setembro" antes de gerar_mes rodar — exige config,
    porque a reserva sai de pct_investimento.
    """
    do_mes = _do_mes(df, mes_ref)
    materializado = _tem_recorrencia(do_mes)

    ativos = recorrentes
    if ativos is not None and "ativo" in ativos.columns:
        ativos = ativos[ativos["ativo"].astype("boolean").fillna(False)]

    faltando: list[str] = []
    if ativos is not None and not ativos.empty:
        ja_no_mes = set(_coluna(do_mes, "descricao").dropna())
        faltando = [
            str(descricao)
            for descricao in ativos["descricao"]
            if str(descricao).strip().lower() not in ja_no_mes
        ]

    previa = None
    if config is not None:
        previa = orcamento_mes(
            projetar_janela(df, recorrentes, mes_ref, 1), config, mes_ref
        )

    return {
        "mes_ref": mes_ref,
        "materializado": materializado,
        "faltando": faltando,
        "parcelas_que_caem": _parcelas_lancadas(do_mes),
        "previa_orcamento": previa,
    }


def _parcelas_lancadas(df_mes: pd.DataFrame) -> pd.DataFrame:
    """Parcelas que já estão lançadas no mês, da maior para a menor."""
    colunas = [
        "descricao",
        "categoria",
        "valor",
        "parcela_atual",
        "parcela_total",
        "grupo_id",
    ]
    if df_mes.empty:
        return pd.DataFrame(columns=colunas)
    parcelas = df_mes[
        (_coluna(df_mes, "tipo") == "saida") & df_mes["parcela_total"].notna()
    ]
    if parcelas.empty:
        return pd.DataFrame(columns=colunas)
    return (
        parcelas[colunas]
        .sort_values(["valor", "descricao"], ascending=[False, True])
        .reset_index(drop=True)
    )


# --------------------------------------------------------------------------
# 7. comparação entre meses
# --------------------------------------------------------------------------
def comparar_meses(df: pd.DataFrame, mes_a: str, mes_b: str) -> dict[str, Any]:
    """Compara dois meses em valores absolutos e percentuais (b em relação a a).

    Só usa números que não dependem da CONFIG. Variação percentual sobre
    base zero não existe: vira None em `variacao` e NaN na tabela de
    categorias, nunca infinito.
    """
    totais_a = _totais(_do_mes(df, mes_a))
    totais_b = _totais(_do_mes(df, mes_b))

    variacao: dict[str, dict[str, float | None]] = {}
    for campo in ("entradas", "saida_fixa", "saida_variada", "saidas", "sobra"):
        a, b = _d(totais_a[campo]), _d(totais_b[campo])
        delta = b - a
        variacao[campo] = {
            "a": _r2(a),
            "b": _r2(b),
            "delta": _r2(delta),
            "percentual": _r2(delta / abs(a) * 100) if a != 0 else None,
        }

    categorias = _comparar_categorias(df, mes_a, mes_b)
    return {
        "mes_a": mes_a,
        "mes_b": mes_b,
        "totais_a": totais_a,
        "totais_b": totais_b,
        "variacao": variacao,
        "categorias": categorias,
    }


def _comparar_categorias(df: pd.DataFrame, mes_a: str, mes_b: str) -> pd.DataFrame:
    colunas = ["categoria", "valor_a", "valor_b", "delta", "percentual"]
    a = gastos_por_categoria(df, mes_a).set_index("categoria")["valor"]
    b = gastos_por_categoria(df, mes_b).set_index("categoria")["valor"]

    linhas: list[dict[str, Any]] = []
    for categoria in sorted(set(a.index) | set(b.index)):
        valor_a = _d(a.get(categoria, 0.0))
        valor_b = _d(b.get(categoria, 0.0))
        delta = valor_b - valor_a
        linhas.append(
            {
                "categoria": categoria,
                "valor_a": _r2(valor_a),
                "valor_b": _r2(valor_b),
                "delta": _r2(delta),
                "percentual": _r2(delta / valor_a * 100) if valor_a != 0 else None,
            }
        )
    if not linhas:
        return pd.DataFrame(columns=colunas)
    return (
        pd.DataFrame(linhas, columns=colunas)
        .sort_values(["delta", "categoria"], ascending=[False, True])
        .reset_index(drop=True)
    )
