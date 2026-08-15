"""Testes das regras de negócio. Puros: sem rede, sem token, sem sheets.py."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from src import financas

CONFIG: dict[str, Any] = {
    "pct_investimento": 0.20,
    "semanas_no_mes": 3,
    "reserva_intocavel": True,
    "meta_reserva": 10000.0,
    "limite_comprometimento": 0.35,
}

COLUNAS = [
    "id",
    "data",
    "mes_ref",
    "tipo",
    "natureza",
    "categoria",
    "descricao",
    "valor",
    "parcela_atual",
    "parcela_total",
    "grupo_id",
    "status",
    "origem",
    "criado_em",
]


def _linha(**campos: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": campos.get("id", ""),
        "data": f"{campos['mes_ref']}-05T00:00:00",
        "mes_ref": campos["mes_ref"],
        "tipo": "saida",
        "natureza": "fixa",
        "categoria": "outros",
        "descricao": "",
        "valor": 0.0,
        "parcela_atual": None,
        "parcela_total": None,
        "grupo_id": None,
        "status": "previsto",
        "origem": "manual",
        "criado_em": f"{campos['mes_ref']}-05T00:00:00",
    }
    base.update(campos)
    return base


def _df(linhas: list[dict[str, Any]]) -> pd.DataFrame:
    """Monta o DataFrame com os mesmos dtypes que sheets.ler_lancamentos entrega."""
    df = pd.DataFrame(linhas, columns=COLUNAS)
    df["valor"] = df["valor"].astype("float64")
    for coluna in ("data", "criado_em"):
        df[coluna] = pd.to_datetime(df[coluna], errors="coerce")
    for coluna in ("parcela_atual", "parcela_total"):
        df[coluna] = pd.to_numeric(df[coluna], errors="coerce").astype("Int64")
    for coluna in ("id", "mes_ref", "categoria", "descricao", "grupo_id"):
        df[coluna] = df[coluna].astype("string")
    for coluna in ("tipo", "natureza", "status", "origem"):
        df[coluna] = df[coluna].astype("string").str.lower()
    return df


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def df_agosto() -> pd.DataFrame:
    """O mês da especificação: sobra 1547,00 / reserva 309,40 / semana 412,53."""
    return _df(
        [
            _linha(
                id="1",
                mes_ref="2026-08",
                tipo="entrada",
                natureza="fixa",
                categoria="salario",
                descricao="salário",
                valor=2500.00,
                status="recebido",
            ),
            _linha(
                id="2",
                mes_ref="2026-08",
                categoria="compras",
                descricao="relógio",
                valor=133.00,
                parcela_atual=4,
                parcela_total=10,
                grupo_id="g-relogio",
                origem="parcela",
            ),
            _linha(
                id="3",
                mes_ref="2026-08",
                categoria="saude",
                descricao="gympass",
                valor=150.00,
                origem="recorrencia",
            ),
            _linha(
                id="4",
                mes_ref="2026-08",
                categoria="saude",
                descricao="hyrox",
                valor=160.00,
                parcela_atual=1,
                parcela_total=4,
                grupo_id="g-hyrox",
                origem="parcela",
            ),
            _linha(
                id="5",
                mes_ref="2026-08",
                categoria="educacao",
                descricao="faculdade",
                valor=350.00,
                origem="recorrencia",
            ),
            _linha(
                id="6",
                mes_ref="2026-08",
                natureza="variada",
                categoria="mercado",
                descricao="mercado",
                valor=160.00,
            ),
        ]
    )


MESES_12 = [f"2026-{m:02d}" for m in range(8, 13)] + [
    f"2027-{m:02d}" for m in range(1, 8)
]


@pytest.fixture
def df_12_meses() -> pd.DataFrame:
    """Mesmo padrão do mês base repetido por 12 meses, com dois pontos de tensão:

    - relógio 4/10..10/10 (2026-08 a 2027-02) e hyrox 1/4..4/4 (até 2026-11);
    - um seguro anual de R$ 1.400,00 em 2026-11, que derruba a sobra para 147,00.
    """
    linhas: list[dict[str, Any]] = []
    for indice, mes in enumerate(MESES_12):
        linhas += [
            _linha(
                id=f"e{indice}",
                mes_ref=mes,
                tipo="entrada",
                natureza="fixa",
                categoria="salario",
                descricao="salário",
                valor=2500.00,
                status="recebido",
            ),
            _linha(
                id=f"g{indice}",
                mes_ref=mes,
                categoria="saude",
                descricao="gympass",
                valor=150.00,
            ),
            _linha(
                id=f"f{indice}",
                mes_ref=mes,
                categoria="educacao",
                descricao="faculdade",
                valor=350.00,
            ),
            _linha(
                id=f"m{indice}",
                mes_ref=mes,
                natureza="variada",
                categoria="mercado",
                descricao="mercado",
                valor=160.00,
            ),
        ]

    for indice in range(7):  # relógio: parcelas 4/10 .. 10/10
        linhas.append(
            _linha(
                id=f"r{indice}",
                mes_ref=MESES_12[indice],
                categoria="compras",
                descricao="relógio",
                valor=133.00,
                parcela_atual=4 + indice,
                parcela_total=10,
                grupo_id="g-relogio",
                origem="parcela",
            )
        )

    for indice in range(4):  # hyrox: parcelas 1/4 .. 4/4
        linhas.append(
            _linha(
                id=f"h{indice}",
                mes_ref=MESES_12[indice],
                categoria="saude",
                descricao="hyrox",
                valor=160.00,
                parcela_atual=1 + indice,
                parcela_total=4,
                grupo_id="g-hyrox",
                origem="parcela",
            )
        )

    linhas.append(
        _linha(
            id="s0",
            mes_ref="2026-11",
            categoria="seguro",
            descricao="seguro anual do carro",
            valor=1400.00,
        )
    )
    return _df(linhas)


# --------------------------------------------------------------------------
# resumo_mes
# --------------------------------------------------------------------------
def test_resumo_mes_numeros_da_especificacao(df_agosto: pd.DataFrame) -> None:
    resumo = financas.resumo_mes(df_agosto, CONFIG, "2026-08")

    assert resumo["entradas"] == 2500.00
    assert resumo["saida_fixa"] == 793.00
    assert resumo["saida_variada"] == 160.00
    assert resumo["sobra"] == 1547.00
    assert resumo["reserva"] == 309.40
    assert resumo["livre_por_semana"] == 412.53


def test_resumo_mes_separa_realizado_de_previsto(df_agosto: pd.DataFrame) -> None:
    resumo = financas.resumo_mes(df_agosto, CONFIG, "2026-08")

    # Só o salário está recebido; todas as saídas ainda são previstas.
    assert resumo["realizado"]["entradas"] == 2500.00
    assert resumo["realizado"]["saidas"] == 0.00
    assert resumo["previsto"]["entradas"] == 0.00
    assert resumo["previsto"]["saidas"] == 953.00
    assert (
        resumo["realizado"]["sobra"] + resumo["previsto"]["sobra"] == resumo["sobra"]
    )


def test_resumo_mes_sem_lancamentos_nao_divide_por_zero(
    df_agosto: pd.DataFrame,
) -> None:
    resumo = financas.resumo_mes(df_agosto, CONFIG, "2026-01")

    assert resumo["entradas"] == 0.0
    assert resumo["sobra"] == 0.0
    assert resumo["reserva"] == 0.0
    assert resumo["livre_por_semana"] == 0.0


def test_resumo_mes_dataframe_vazio() -> None:
    resumo = financas.resumo_mes(_df([]), CONFIG, "2026-08")
    assert resumo["sobra"] == 0.0
    assert resumo["livre_por_semana"] == 0.0


def test_semanas_no_mes_invalida_levanta_erro(df_agosto: pd.DataFrame) -> None:
    with pytest.raises(ValueError):
        financas.resumo_mes(df_agosto, {**CONFIG, "semanas_no_mes": 0}, "2026-08")


def test_sobra_negativa_nao_gera_reserva() -> None:
    df = _df(
        [
            _linha(id="1", mes_ref="2026-08", tipo="entrada", valor=1000.00),
            _linha(id="2", mes_ref="2026-08", valor=1500.00),
        ]
    )
    resumo = financas.resumo_mes(df, CONFIG, "2026-08")

    assert resumo["sobra"] == -500.00
    assert resumo["reserva"] == 0.0
    assert resumo["livre_por_semana"] == round(-500.00 / 3, 2)


# --------------------------------------------------------------------------
# acompanhamento_mes (envelope do variável)
# --------------------------------------------------------------------------
def _df_envelope(status_da_viagem: str = "previsto") -> pd.DataFrame:
    """Entradas 2.500,00 e fixas 783,00 -> envelope de 1.373,60.

    (2500 - 783) = 1717,00; reserva de 20% = 343,40; envelope = 1373,60.
    """
    return _df(
        [
            _linha(
                id="1",
                mes_ref="2026-08",
                tipo="entrada",
                natureza="fixa",
                categoria="salario",
                descricao="salário",
                valor=2500.00,
                status="recebido",
            ),
            _linha(id="2", mes_ref="2026-08", categoria="saude", descricao="gympass", valor=150.00),
            _linha(
                id="3",
                mes_ref="2026-08",
                categoria="educacao",
                descricao="faculdade",
                valor=350.00,
            ),
            _linha(
                id="4",
                mes_ref="2026-08",
                categoria="compras",
                descricao="relógio",
                valor=133.00,
                parcela_atual=4,
                parcela_total=10,
                grupo_id="g-relogio",
            ),
            _linha(
                id="5",
                mes_ref="2026-08",
                categoria="saude",
                descricao="hyrox",
                valor=150.00,
                parcela_atual=1,
                parcela_total=4,
                grupo_id="g-hyrox",
            ),
            _linha(
                id="6",
                mes_ref="2026-08",
                natureza="variada",
                categoria="viagem",
                descricao="viagem planejada",
                valor=400.00,
                status=status_da_viagem,
            ),
            _linha(
                id="7",
                mes_ref="2026-08",
                natureza="variada",
                categoria="mercado",
                descricao="mercado",
                valor=150.00,
                status="pago",
            ),
        ]
    )


def _df_recorrentes(linhas: list[dict[str, Any]]) -> pd.DataFrame:
    """Mesmo formato que sheets.ler_recorrentes entrega."""
    colunas = [
        "descricao",
        "valor",
        "tipo",
        "natureza",
        "categoria",
        "dia_do_mes",
        "ativo",
    ]
    df = pd.DataFrame(linhas, columns=colunas)
    df["valor"] = df["valor"].astype("float64")
    df["dia_do_mes"] = pd.to_numeric(df["dia_do_mes"], errors="coerce").astype("Int64")
    df["ativo"] = df["ativo"].astype("bool")
    for coluna in ("descricao", "categoria"):
        df[coluna] = df[coluna].astype("string")
    for coluna in ("tipo", "natureza"):
        df[coluna] = df[coluna].astype("string").str.lower()
    return df


RECORRENTES = [
    {
        "descricao": "FIXA",
        "valor": 2500.00,
        "tipo": "entrada",
        "natureza": "fixa",
        "categoria": "salario",
        "dia_do_mes": 4,
        "ativo": True,
    },
    {
        "descricao": "GYMPASS",
        "valor": 150.00,
        "tipo": "saida",
        "natureza": "fixa",
        "categoria": "saude",
        "dia_do_mes": 9,
        "ativo": True,
    },
    {
        "descricao": "FACULDADE",
        "valor": 350.00,
        "tipo": "saida",
        "natureza": "fixa",
        "categoria": "educacao",
        "dia_do_mes": 9,
        "ativo": True,
    },
]


@pytest.fixture
def recorrentes() -> pd.DataFrame:
    return _df_recorrentes(RECORRENTES)


# --------------------------------------------------------------------------
# orcamento_mes
# --------------------------------------------------------------------------
def test_orcamento_mes() -> None:
    orcamento = financas.orcamento_mes(_df_envelope(), CONFIG, "2026-08")

    assert orcamento["renda"] == 2500.00
    assert orcamento["fixas"] == 783.00
    assert orcamento["sobra_planejada"] == 1717.00
    assert orcamento["reserva_planejada"] == 343.40
    assert orcamento["envelope_variavel"] == 1373.60
    assert orcamento["envelope_semanal"] == 457.87


def test_orcamento_mes_ignora_status_e_variadas() -> None:
    """Planejamento não olha status: previsto e pago dão o mesmo envelope."""
    previsto = financas.orcamento_mes(_df_envelope("previsto"), CONFIG, "2026-08")
    pago = financas.orcamento_mes(_df_envelope("pago"), CONFIG, "2026-08")

    assert previsto == pago


def test_acompanhamento_usa_o_mesmo_envelope_do_orcamento() -> None:
    df = _df_envelope()
    orcamento = financas.orcamento_mes(df, CONFIG, "2026-08")
    acompanhamento = financas.acompanhamento_mes(df, CONFIG, "2026-08")

    assert acompanhamento["envelope_variavel"] == orcamento["envelope_variavel"]
    assert acompanhamento["reserva"] == orcamento["reserva_planejada"]


def test_orcamento_mes_sem_lancamentos(df_agosto: pd.DataFrame) -> None:
    orcamento = financas.orcamento_mes(df_agosto, CONFIG, "2026-01")

    assert orcamento["sobra_planejada"] == 0.0
    assert orcamento["envelope_variavel"] == 0.0
    assert orcamento["envelope_semanal"] == 0.0


def test_acompanhamento_separa_gasto_de_comprometido() -> None:
    """Variada prevista é reserva dentro do envelope, não gasto realizado."""
    acompanhamento = financas.acompanhamento_mes(_df_envelope(), CONFIG, "2026-08")

    assert acompanhamento["envelope_variavel"] == 1373.60
    assert acompanhamento["gasto_realizado"] == 150.00
    assert acompanhamento["comprometido"] == 400.00
    assert acompanhamento["livre"] == 823.60
    assert acompanhamento["restante"] == 1223.60  # envelope - gasto_realizado
    assert acompanhamento["invade_reserva"] is False
    assert acompanhamento["situacao"] == "tranquilo"


def test_marcar_previsto_como_pago_nao_muda_o_livre() -> None:
    """Pagar o que já estava planejado só move o dinheiro de coluna."""
    antes = financas.acompanhamento_mes(_df_envelope("previsto"), CONFIG, "2026-08")
    depois = financas.acompanhamento_mes(_df_envelope("pago"), CONFIG, "2026-08")

    assert antes["livre"] == depois["livre"] == 823.60
    assert depois["gasto_realizado"] == 550.00  # 150 + 400
    assert depois["comprometido"] == 0.00
    assert depois["restante"] == 823.60  # este sim muda: o dinheiro saiu


def test_acompanhamento_estoura_considerando_o_comprometido() -> None:
    """Um previsto grande já acende o alerta, mesmo sem nada ter sido pago."""
    df = _df_envelope()
    df.loc[df["id"] == "6", "valor"] = 1300.00  # viagem prevista

    acompanhamento = financas.acompanhamento_mes(df, CONFIG, "2026-08")

    assert acompanhamento["gasto_realizado"] == 150.00
    assert acompanhamento["comprometido"] == 1300.00
    assert acompanhamento["livre"] == -76.40
    assert acompanhamento["invade_reserva"] is True
    assert acompanhamento["situacao"] == "estourado"


def test_acompanhamento_mes_sem_lancamentos(df_agosto: pd.DataFrame) -> None:
    acompanhamento = financas.acompanhamento_mes(df_agosto, CONFIG, "2026-01")

    assert acompanhamento["envelope_variavel"] == 0.0
    assert acompanhamento["livre"] == 0.0
    assert acompanhamento["percentual_usado"] == 0.0
    assert acompanhamento["situacao"] == "sem_envelope"


def test_acompanhamento_ignora_fixas_no_gasto(df_agosto: pd.DataFrame) -> None:
    """Só variadas entram no envelope; as fixas já foram descontadas dele."""
    acompanhamento = financas.acompanhamento_mes(df_agosto, CONFIG, "2026-08")

    assert acompanhamento["saida_fixa"] == 793.00
    assert acompanhamento["envelope_variavel"] == 1365.60  # (2500 - 793) * 0,8
    assert acompanhamento["gasto_realizado"] == 0.00  # a variada está prevista
    assert acompanhamento["comprometido"] == 160.00


# --------------------------------------------------------------------------
# contexto temporal
# --------------------------------------------------------------------------
def test_mes_de_planejamento_e_de_acompanhamento() -> None:
    hoje = date(2026, 8, 13)

    assert financas.mes_de_planejamento(hoje) == "2026-09"
    assert financas.mes_de_acompanhamento(hoje) == "2026-08"
    assert financas.mes_de_planejamento(date(2026, 12, 31)) == "2027-01"


def test_fase_do_mes_por_terco() -> None:
    assert financas.fase_do_mes(date(2026, 8, 1)) == "inicio"
    assert financas.fase_do_mes(date(2026, 8, 10)) == "inicio"
    assert financas.fase_do_mes(date(2026, 8, 13)) == "meio"
    assert financas.fase_do_mes(date(2026, 8, 20)) == "meio"
    assert financas.fase_do_mes(date(2026, 8, 21)) == "fim"
    assert financas.fase_do_mes(date(2026, 8, 31)) == "fim"
    # Fevereiro tem 28 dias: o terço encolhe junto.
    assert financas.fase_do_mes(date(2026, 2, 10)) == "meio"
    assert financas.fase_do_mes(date(2026, 2, 19)) == "fim"


# --------------------------------------------------------------------------
# gastos_por_categoria
# --------------------------------------------------------------------------
def test_gastos_por_categoria(df_agosto: pd.DataFrame) -> None:
    tabela = financas.gastos_por_categoria(df_agosto, "2026-08")

    assert list(tabela["categoria"]) == ["educacao", "saude", "mercado", "compras"]
    assert list(tabela["valor"]) == [350.00, 310.00, 160.00, 133.00]
    assert tabela.loc[0, "percentual"] == 36.73  # 350 / 953
    assert financas._somar(tabela["valor"]) == 953  # entradas ficam de fora


def test_gastos_por_categoria_mes_vazio(df_agosto: pd.DataFrame) -> None:
    tabela = financas.gastos_por_categoria(df_agosto, "2026-01")
    assert tabela.empty
    assert list(tabela.columns) == ["categoria", "valor", "percentual"]


# --------------------------------------------------------------------------
# listar_lancamentos
# --------------------------------------------------------------------------
def test_listar_lancamentos_devolve_as_linhas_e_os_subtotais(
    df_agosto: pd.DataFrame,
) -> None:
    """Todas as linhas do mês, fixas antes das variadas e por valor decrescente."""
    resultado = financas.listar_lancamentos(df_agosto, "2026-08")
    linhas = resultado["lancamentos"]

    assert list(linhas["descricao"]) == [
        "salário",  # entrada também é natureza fixa
        "faculdade",
        "hyrox",
        "gympass",
        "relógio",
        "mercado",  # única variada, vai para o fim
    ]
    assert list(linhas["valor"]) == [2500.00, 350.00, 160.00, 150.00, 133.00, 160.00]
    assert list(linhas.columns) == financas.COLUNAS_LISTAGEM

    assert resultado["mes_ref"] == "2026-08"
    assert resultado["total_entradas"] == 2500.00
    assert resultado["total_fixas"] == 793.00  # 350 + 160 + 150 + 133
    assert resultado["total_parcelas"] == 293.00  # hyrox 160 + relógio 133
    assert resultado["total_variadas"] == 160.00


def test_listar_lancamentos_traz_a_numeracao_so_de_quem_e_parcela(
    df_agosto: pd.DataFrame,
) -> None:
    linhas = financas.listar_lancamentos(df_agosto, "2026-08")["lancamentos"]
    por_descricao = linhas.set_index("descricao")

    assert por_descricao.loc["relógio", "parcela_atual"] == 4
    assert por_descricao.loc["relógio", "parcela_total"] == 10
    assert pd.isna(por_descricao.loc["gympass", "parcela_atual"])
    assert pd.isna(por_descricao.loc["gympass", "parcela_total"])


def test_total_fixas_ja_inclui_as_parcelas(df_agosto: pd.DataFrame) -> None:
    """Parcela é recorte DENTRO da fixa: 793 com 293 dentro, não 793 + 293."""
    resultado = financas.listar_lancamentos(df_agosto, "2026-08")

    fixas = financas._d(resultado["total_fixas"])
    parcelas = financas._d(resultado["total_parcelas"])
    variadas = financas._d(resultado["total_variadas"])

    assert parcelas < fixas
    # As saídas do mês são fixas + variadas. Somar as parcelas conta duas vezes.
    assert financas._r2(fixas + variadas) == 953.00
    assert financas._r2(fixas + parcelas + variadas) != 953.00

    somente_parcelas = financas.listar_lancamentos(
        df_agosto, "2026-08", apenas_parcelas=True
    )
    assert somente_parcelas["total_fixas"] == resultado["total_parcelas"]


def test_listar_lancamentos_com_filtros_combinados(df_agosto: pd.DataFrame) -> None:
    resultado = financas.listar_lancamentos(
        df_agosto, "2026-08", tipo="saida", natureza="fixa"
    )
    linhas = resultado["lancamentos"]

    assert list(linhas["descricao"]) == ["faculdade", "hyrox", "gympass", "relógio"]
    assert set(linhas["tipo"]) == {"saida"}
    assert set(linhas["natureza"]) == {"fixa"}

    # Subtotais são do recorte filtrado: entrada e variada saíram da conta.
    assert resultado["total_entradas"] == 0.0
    assert resultado["total_variadas"] == 0.0
    assert resultado["total_fixas"] == 793.00
    assert resultado["total_parcelas"] == 293.00


def test_listar_lancamentos_apenas_parcelas(df_agosto: pd.DataFrame) -> None:
    resultado = financas.listar_lancamentos(df_agosto, "2026-08", apenas_parcelas=True)
    linhas = resultado["lancamentos"]

    assert list(linhas["descricao"]) == ["hyrox", "relógio"]
    assert linhas["parcela_total"].notna().all()
    assert resultado["total_parcelas"] == 293.00


def test_listar_lancamentos_filtra_por_status(df_agosto: pd.DataFrame) -> None:
    resultado = financas.listar_lancamentos(df_agosto, "2026-08", status="recebido")

    assert list(resultado["lancamentos"]["descricao"]) == ["salário"]
    assert resultado["total_entradas"] == 2500.00


def test_listar_lancamentos_mes_vazio(df_agosto: pd.DataFrame) -> None:
    resultado = financas.listar_lancamentos(df_agosto, "2026-01")

    assert resultado["lancamentos"].empty
    assert list(resultado["lancamentos"].columns) == financas.COLUNAS_LISTAGEM
    assert resultado["total_entradas"] == 0.0
    assert resultado["total_fixas"] == 0.0
    assert resultado["total_parcelas"] == 0.0
    assert resultado["total_variadas"] == 0.0


def test_listar_lancamentos_aceita_o_plural_com_acento(
    df_agosto: pd.DataFrame,
) -> None:
    """'saídas' tem que achar as saídas — filtro escrito assim é comum."""
    com_acento = financas.listar_lancamentos(df_agosto, "2026-08", tipo="Saídas")
    normalizado = financas.listar_lancamentos(df_agosto, "2026-08", tipo="saida")

    assert list(com_acento["lancamentos"]["descricao"]) == list(
        normalizado["lancamentos"]["descricao"]
    )


def test_listar_lancamentos_recusa_filtro_inexistente(
    df_agosto: pd.DataFrame,
) -> None:
    """Filtro errado não pode virar lista vazia em silêncio."""
    with pytest.raises(ValueError, match="tipo inválido"):
        financas.listar_lancamentos(df_agosto, "2026-08", tipo="despesa")

    with pytest.raises(ValueError, match="natureza inválida"):
        financas.listar_lancamentos(df_agosto, "2026-08", natureza="recorrente")


# --------------------------------------------------------------------------
# compromissos_futuros e parcelas_em_aberto
# --------------------------------------------------------------------------
def test_compromissos_futuros_enxerga_parcela_5_de_10(
    df_12_meses: pd.DataFrame,
) -> None:
    tabela = financas.compromissos_futuros(df_12_meses, "2026-08", n_meses=12)

    assert list(tabela["mes_ref"]) == MESES_12
    setembro = tabela[tabela["mes_ref"] == "2026-09"].iloc[0]
    assert setembro["parcelas"] == 293.00  # relógio 5/10 (133) + hyrox 2/4 (160)

    novembro = tabela[tabela["mes_ref"] == "2026-11"].iloc[0]
    assert novembro["sobra"] == 147.00  # seguro anual come o mês


def test_compromissos_futuros_mes_sem_dados(df_agosto: pd.DataFrame) -> None:
    tabela = financas.compromissos_futuros(df_agosto, "2026-08", n_meses=3)
    assert list(tabela["sobra"]) == [1547.00, 0.0, 0.0]
    # Mês vazio não é mês projetado: aqui ninguém preencheu nada.
    assert list(tabela["sintetizado"]) == [False, False, False]


def test_compromissos_futuros_marca_o_mes_projetado(
    df_so_mes_inicial: pd.DataFrame, recorrentes: pd.DataFrame
) -> None:
    """Só 2026-08 é fato lançado; setembro e outubro vêm das recorrentes."""
    ampliado = financas.projetar_janela(
        df_so_mes_inicial, recorrentes, "2026-08", n_meses=3
    )
    tabela = financas.compromissos_futuros(ampliado, "2026-08", n_meses=3)

    assert list(tabela["sintetizado"]) == [False, True, True]
    # A coluna é aditiva: os números continuam onde estavam.
    assert tabela.loc[0, "entradas"] == 2500.00
    assert tabela.loc[1, "entradas"] == 2500.00


def test_parcelas_em_aberto(df_12_meses: pd.DataFrame) -> None:
    tabela = financas.parcelas_em_aberto(df_12_meses, "2026-08").set_index("grupo_id")

    relogio = tabela.loc["g-relogio"]
    assert relogio["parcelas_restantes"] == 7  # 4/10 até 10/10
    assert relogio["proxima_parcela"] == 4
    assert relogio["valor_restante"] == 931.00
    assert relogio["ultimo_mes"] == "2027-02"

    hyrox = tabela.loc["g-hyrox"]
    assert hyrox["parcelas_restantes"] == 4
    assert hyrox["valor_restante"] == 640.00


def test_parcelas_em_aberto_ignora_passado(df_12_meses: pd.DataFrame) -> None:
    tabela = financas.parcelas_em_aberto(df_12_meses, "2026-12").set_index("grupo_id")

    assert "g-hyrox" not in tabela.index  # terminou em 2026-11
    assert tabela.loc["g-relogio", "parcelas_restantes"] == 3  # 8/10, 9/10 e 10/10
    assert tabela.loc["g-relogio", "proxima_parcela"] == 8


# --------------------------------------------------------------------------
# capacidade_de_compra
# --------------------------------------------------------------------------
def test_capacidade_compra_que_cabe(df_agosto: pd.DataFrame) -> None:
    resultado = financas.capacidade_de_compra(
        df_agosto, CONFIG, valor=600.00, n_parcelas=1, mes_ref="2026-08"
    )

    assert resultado["cabe"] is True
    assert resultado["sobra_depois"] == 947.00
    assert resultado["nova_reserva"] == 309.40  # intocável: não encolhe
    assert resultado["novo_livre_semanal"] == 212.53
    assert resultado["reducao_percentual_semanal"] == 48.48
    assert resultado["meses_inviaveis"] == []
    assert resultado["pior_mes"]["mes_ref"] in resultado["motivo"]


def test_capacidade_compra_que_estoura_a_reserva(df_agosto: pd.DataFrame) -> None:
    resultado = financas.capacidade_de_compra(
        df_agosto, CONFIG, valor=1300.00, n_parcelas=1, mes_ref="2026-08"
    )

    # Sobrariam 247,00, abaixo da reserva intocável de 309,40.
    assert resultado["cabe"] is False
    assert resultado["sobra_depois"] == 247.00
    assert resultado["meses_inviaveis"] == ["2026-08"]
    assert resultado["pior_mes"]["mes_ref"] == "2026-08"
    assert "2026-08" in resultado["motivo"]


def test_capacidade_a_vista_afeta_um_mes_so(df_agosto: pd.DataFrame) -> None:
    resultado = financas.capacidade_de_compra(
        df_agosto, CONFIG, valor=600.00, n_parcelas=1, mes_ref="2026-08"
    )

    assert resultado["meses_afetados"] == ["2026-08"]
    assert resultado["valor_parcela"] == 600.00
    assert len(resultado["projecao"]) == 12
    assert all(mes["cabe"] for mes in resultado["projecao"][1:])


def test_capacidade_parcelada_que_cabe_nos_12_meses(
    df_12_meses: pd.DataFrame,
) -> None:
    resultado = financas.capacidade_de_compra(
        df_12_meses, CONFIG, valor=600.00, n_parcelas=6, mes_ref="2026-08"
    )

    assert resultado["cabe"] is True
    assert resultado["valor_parcela"] == 100.00
    assert resultado["meses_afetados"] == MESES_12[:6]
    assert resultado["pior_mes"] == {"mes_ref": "2026-11", "livre_por_semana": 5.87}


def test_capacidade_cabe_hoje_mas_estoura_no_futuro(df_12_meses: pd.DataFrame) -> None:
    """O teste que justifica a janela de 12 meses.

    R$ 1.800,00 em 6x cabe folgado em 2026-08 (sobrariam 1.247,00), mas em
    2026-11 o seguro anual já derrubou a sobra para 147,00 — a parcela de
    300,00 arrebenta a reserva daquele mês.
    """
    resultado = financas.capacidade_de_compra(
        df_12_meses, CONFIG, valor=1800.00, n_parcelas=6, mes_ref="2026-08"
    )

    agosto = resultado["projecao"][0]
    assert agosto["mes_ref"] == "2026-08"
    assert agosto["cabe"] is True
    assert agosto["sobra"] == 1247.00

    assert resultado["cabe"] is False
    assert resultado["meses_inviaveis"] == ["2026-11"]
    assert resultado["pior_mes"]["mes_ref"] == "2026-11"
    assert resultado["pior_mes"]["livre_por_semana"] == -60.80
    assert "2026-11" in resultado["motivo"]


def test_capacidade_barra_por_limite_de_comprometimento(
    df_12_meses: pd.DataFrame,
) -> None:
    """Parcelas do mês não podem passar de 35% da renda, mesmo com sobra."""
    resultado = financas.capacidade_de_compra(
        df_12_meses, CONFIG, valor=3600.00, n_parcelas=6, mes_ref="2026-08"
    )

    agosto = resultado["projecao"][0]
    assert agosto["comprometido"] == 893.00  # 133 + 160 + 600, teto é 875,00
    assert agosto["sobra"] == 947.00  # a reserva de 309,40 até caberia
    assert agosto["cabe"] is False
    assert "teto" in resultado["motivo"]


def test_capacidade_n_parcelas_invalido(df_agosto: pd.DataFrame) -> None:
    with pytest.raises(ValueError):
        financas.capacidade_de_compra(
            df_agosto, CONFIG, valor=100.0, n_parcelas=0, mes_ref="2026-08"
        )


def test_capacidade_projecao_tem_os_campos_pedidos(df_12_meses: pd.DataFrame) -> None:
    resultado = financas.capacidade_de_compra(
        df_12_meses, CONFIG, valor=600.00, n_parcelas=6, mes_ref="2026-08"
    )
    esperado = {
        "mes_ref",
        "comprometido",
        "sobra",
        "livre_por_semana",
        "cabe",
        "sintetizado",
    }
    assert all(set(mes) == esperado for mes in resultado["projecao"])


def test_pior_mes_so_considera_meses_afetados(df_12_meses: pd.DataFrame) -> None:
    """2026-11 é o mês mais apertado, mas uma compra à vista não o alcança."""
    resultado = financas.capacidade_de_compra(
        df_12_meses, CONFIG, valor=300.00, n_parcelas=1, mes_ref="2026-08"
    )

    assert resultado["pior_mes"]["mes_ref"] == "2026-08"
    assert "2026-11" not in resultado["motivo"]


# --------------------------------------------------------------------------
# projetar_janela
# --------------------------------------------------------------------------
@pytest.fixture
def df_so_mes_inicial() -> pd.DataFrame:
    """Só 2026-08 foi gerado; de setembro em diante a planilha está vazia."""
    return _df(
        [
            _linha(
                id="1",
                mes_ref="2026-08",
                tipo="entrada",
                natureza="fixa",
                categoria="salario",
                descricao="FIXA",
                valor=2500.00,
                status="recebido",
                origem="recorrencia",
            ),
            _linha(
                id="2",
                mes_ref="2026-08",
                categoria="saude",
                descricao="GYMPASS",
                valor=150.00,
                origem="recorrencia",
            ),
            _linha(
                id="3",
                mes_ref="2026-08",
                categoria="educacao",
                descricao="FACULDADE",
                valor=350.00,
                origem="recorrencia",
            ),
        ]
    )


def test_projetar_janela_preenche_mes_vazio(
    df_so_mes_inicial: pd.DataFrame, recorrentes: pd.DataFrame
) -> None:
    ampliado = financas.projetar_janela(
        df_so_mes_inicial, recorrentes, "2026-08", n_meses=3
    )
    setembro = ampliado[ampliado["mes_ref"] == "2026-09"]

    assert len(setembro) == 3
    assert set(setembro["origem"]) == {"projetado"}
    assert set(setembro["status"]) == {"previsto"}
    assert financas.resumo_mes(ampliado, CONFIG, "2026-09")["entradas"] == 2500.00
    # Tipos preservados: as regras continuam funcionando sobre o df ampliado.
    assert ampliado["valor"].dtype == "float64"
    assert ampliado["parcela_total"].dtype == "Int64"
    assert str(ampliado["data"].dtype).startswith("datetime64")


def test_projetar_janela_nao_toca_mes_ja_materializado(
    df_so_mes_inicial: pd.DataFrame, recorrentes: pd.DataFrame
) -> None:
    ampliado = financas.projetar_janela(
        df_so_mes_inicial, recorrentes, "2026-08", n_meses=3
    )
    agosto = ampliado[ampliado["mes_ref"] == "2026-08"]

    assert len(agosto) == 3  # nada duplicado
    assert set(agosto["origem"]) == {"recorrencia"}


def test_projetar_janela_sem_recorrentes_devolve_igual(
    df_so_mes_inicial: pd.DataFrame,
) -> None:
    assert financas.projetar_janela(df_so_mes_inicial, None, "2026-08") is (
        df_so_mes_inicial
    )
    vazio = _df_recorrentes([])
    assert financas.projetar_janela(df_so_mes_inicial, vazio, "2026-08") is (
        df_so_mes_inicial
    )


def test_projetar_janela_ignora_recorrente_inativa(
    df_so_mes_inicial: pd.DataFrame,
) -> None:
    inativas = _df_recorrentes([{**RECORRENTES[1], "ativo": False}])
    ampliado = financas.projetar_janela(df_so_mes_inicial, inativas, "2026-08", 3)

    assert ampliado.equals(df_so_mes_inicial)


def test_projetar_janela_ajusta_dia_ao_tamanho_do_mes(
    df_so_mes_inicial: pd.DataFrame,
) -> None:
    dia_31 = _df_recorrentes([{**RECORRENTES[1], "dia_do_mes": 31}])
    ampliado = financas.projetar_janela(df_so_mes_inicial, dia_31, "2026-09", 1)
    data = ampliado[ampliado["mes_ref"] == "2026-09"].iloc[0]["data"]

    assert data == pd.Timestamp("2026-09-30")  # setembro não tem dia 31


# --------------------------------------------------------------------------
# capacidade_de_compra com projeção — a regressão que motivou tudo
# --------------------------------------------------------------------------
def test_compra_parcelada_cabe_com_meses_projetados(
    df_so_mes_inicial: pd.DataFrame, recorrentes: pd.DataFrame
) -> None:
    """REGRESSÃO: sem projetar, os 5 meses vazios recusavam qualquer parcelamento."""
    resultado = financas.capacidade_de_compra(
        df_so_mes_inicial,
        CONFIG,
        valor=1800.00,
        n_parcelas=6,
        mes_ref="2026-08",
        recorrentes=recorrentes,
    )

    assert resultado["cabe"] is True
    assert resultado["meses_inviaveis"] == []
    assert resultado["projecao_incompleta"] is False
    assert "incompleta" not in resultado["motivo"]

    setembro = resultado["projecao"][1]
    assert setembro["mes_ref"] == "2026-09"
    assert setembro["sintetizado"] is True
    assert resultado["projecao"][0]["sintetizado"] is False  # 2026-08 é real


def test_compra_parcelada_sem_recorrentes_avisa_projecao_incompleta(
    df_so_mes_inicial: pd.DataFrame,
) -> None:
    """Sem recorrentes o veredito é conservador, mas nunca silencioso."""
    resultado = financas.capacidade_de_compra(
        df_so_mes_inicial, CONFIG, valor=1800.00, n_parcelas=6, mes_ref="2026-08"
    )

    assert resultado["cabe"] is False
    assert resultado["projecao_incompleta"] is True
    assert "projeção incompleta" in resultado["motivo"]
    assert resultado["motivo"].rstrip().endswith("passe `recorrentes`.")
    assert "2026-09" in resultado["motivo"]


def test_projecao_marca_meses_sintetizados(
    df_so_mes_inicial: pd.DataFrame, recorrentes: pd.DataFrame
) -> None:
    resultado = financas.capacidade_de_compra(
        df_so_mes_inicial,
        CONFIG,
        valor=600.00,
        n_parcelas=1,
        mes_ref="2026-08",
        recorrentes=recorrentes,
    )

    assert resultado["meses_sintetizados"] == financas.janela_de_meses("2026-09", 11)


# --------------------------------------------------------------------------
# planejamento_status
# --------------------------------------------------------------------------
@pytest.fixture
def df_setembro_so_parcela(df_so_mes_inicial: pd.DataFrame) -> pd.DataFrame:
    """Setembro só tem a parcela do relógio: gerar_mes ainda não rodou lá."""
    parcelas = _df(
        [
            _linha(
                id="4",
                mes_ref="2026-08",
                categoria="compras",
                descricao="relógio",
                valor=133.00,
                parcela_atual=4,
                parcela_total=10,
                grupo_id="g-relogio",
                origem="parcela",
            ),
            _linha(
                id="5",
                mes_ref="2026-09",
                categoria="compras",
                descricao="relógio",
                valor=133.00,
                parcela_atual=5,
                parcela_total=10,
                grupo_id="g-relogio",
                origem="parcela",
            ),
        ]
    )
    return pd.concat([df_so_mes_inicial, parcelas], ignore_index=True)


def test_planejamento_status_mes_nao_materializado(
    df_setembro_so_parcela: pd.DataFrame, recorrentes: pd.DataFrame
) -> None:
    status = financas.planejamento_status(
        df_setembro_so_parcela, recorrentes, "2026-09", CONFIG
    )

    assert status["materializado"] is False
    assert status["faltando"] == ["FIXA", "GYMPASS", "FACULDADE"]

    parcelas = status["parcelas_que_caem"]
    assert len(parcelas) == 1
    assert parcelas.iloc[0]["descricao"] == "relógio"
    assert parcelas.iloc[0]["parcela_atual"] == 5

    # A prévia responde "como fica setembro" antes de gerar_mes rodar:
    # 2500 de renda menos 150 + 350 + 133 de fixas.
    previa = status["previa_orcamento"]
    assert previa["renda"] == 2500.00
    assert previa["fixas"] == 633.00
    assert previa["envelope_variavel"] == 1493.60


def test_planejamento_status_mes_ja_materializado(
    df_setembro_so_parcela: pd.DataFrame, recorrentes: pd.DataFrame
) -> None:
    status = financas.planejamento_status(
        df_setembro_so_parcela, recorrentes, "2026-08", CONFIG
    )

    assert status["materializado"] is True
    assert status["faltando"] == []
    assert status["previa_orcamento"]["fixas"] == 633.00  # nada foi sintetizado


def test_planejamento_status_sem_config_nao_inventa_previa(
    df_setembro_so_parcela: pd.DataFrame, recorrentes: pd.DataFrame
) -> None:
    status = financas.planejamento_status(
        df_setembro_so_parcela, recorrentes, "2026-09"
    )
    assert status["previa_orcamento"] is None


def test_planejamento_status_mes_sem_nada(
    df_agosto: pd.DataFrame, recorrentes: pd.DataFrame
) -> None:
    status = financas.planejamento_status(df_agosto, recorrentes, "2027-05", CONFIG)

    assert status["materializado"] is False
    assert len(status["faltando"]) == 3
    assert status["parcelas_que_caem"].empty


# --------------------------------------------------------------------------
# comparar_meses
# --------------------------------------------------------------------------
def test_comparar_meses(df_12_meses: pd.DataFrame) -> None:
    comparacao = financas.comparar_meses(df_12_meses, "2026-08", "2026-11")

    assert comparacao["totais_a"]["sobra"] == 1547.00
    assert comparacao["totais_b"]["sobra"] == 147.00
    assert comparacao["variacao"]["sobra"]["delta"] == -1400.00
    assert comparacao["variacao"]["sobra"]["percentual"] == -90.50
    assert comparacao["variacao"]["entradas"]["delta"] == 0.0

    categorias = comparacao["categorias"].set_index("categoria")
    assert categorias.loc["seguro", "valor_b"] == 1400.00
    # Base zero: variação percentual não existe (NaN na tabela, None no dict).
    assert pd.isna(categorias.loc["seguro", "percentual"])


def test_comparar_meses_com_mes_vazio(df_agosto: pd.DataFrame) -> None:
    comparacao = financas.comparar_meses(df_agosto, "2026-01", "2026-08")

    assert comparacao["totais_a"]["sobra"] == 0.0
    assert comparacao["variacao"]["sobra"]["percentual"] is None
    assert comparacao["variacao"]["sobra"]["delta"] == 1547.00


# --------------------------------------------------------------------------
# calendário
# --------------------------------------------------------------------------
def test_aritmetica_de_meses() -> None:
    assert financas.somar_meses("2026-11", 3) == "2027-02"
    assert financas.somar_meses("2026-01", -1) == "2025-12"
    assert financas.janela_de_meses("2026-08", 12) == MESES_12


def test_mes_ref_invalido() -> None:
    with pytest.raises(ValueError):
        financas.somar_meses("2026-13", 1)
    with pytest.raises(ValueError):
        financas.janela_de_meses("agosto", 3)


# --------------------------------------------------------------------------
# tipos_do_alvo
# --------------------------------------------------------------------------
def test_tipos_do_alvo_por_descricao(df_agosto: pd.DataFrame) -> None:
    assert financas.tipos_do_alvo(df_agosto, descricao="salário") == ["entrada"]
    assert financas.tipos_do_alvo(df_agosto, descricao="gympass") == ["saida"]
    # A busca ignora caixa: o usuário não digita como a planilha guarda.
    assert financas.tipos_do_alvo(df_agosto, descricao="  SALÁRIO ") == ["entrada"]


def test_tipos_do_alvo_por_id_e_mes(df_agosto: pd.DataFrame) -> None:
    assert financas.tipos_do_alvo(df_agosto, id="1") == ["entrada"]
    assert financas.tipos_do_alvo(df_agosto, descricao="salário", mes_ref="2026-08") == [
        "entrada"
    ]
    assert financas.tipos_do_alvo(df_agosto, descricao="salário", mes_ref="2026-01") == []


def test_tipos_do_alvo_sem_achar_devolve_lista_vazia(df_agosto: pd.DataFrame) -> None:
    """Lista vazia é "não achei aqui", não "não existe" — quem decide é a planilha."""
    assert financas.tipos_do_alvo(df_agosto, descricao="internet") == []
    assert financas.tipos_do_alvo(df_agosto) == []


# --------------------------------------------------------------------------
# dividir_parcelas e numeros_do_parcelamento
# --------------------------------------------------------------------------
def _soma(parcelas: list[Decimal]) -> Decimal:
    return sum(parcelas, Decimal("0"))


def test_dividir_parcelas_redondo() -> None:
    """O caso comum: 1.200 em 6x são seis de 200,00 e nenhum resto."""
    parcelas = financas.dividir_parcelas(1200, 6)

    assert parcelas == [Decimal("200.00")] * 6
    assert _soma(parcelas) == Decimal("1200.00")


def test_dividir_parcelas_com_resto_vai_para_a_ultima() -> None:
    """1.200 em 7x: seis de 171,43 e uma de 171,42, somando exatamente 1.200."""
    parcelas = financas.dividir_parcelas(1200, 7)

    assert parcelas[:6] == [Decimal("171.43")] * 6
    assert parcelas[-1] == Decimal("171.42")
    assert _soma(parcelas) == Decimal("1200.00")


def test_dividir_parcelas_soma_exata_em_divisao_infinita() -> None:
    parcelas = financas.dividir_parcelas(1799, 7)

    assert len(parcelas) == 7
    assert _soma(parcelas) == Decimal("1799.00")


def test_dividir_parcelas_centavo_a_mais_fica_na_ultima() -> None:
    """100 em 3x: o centavo que sobra vai no FIM, não no começo."""
    parcelas = financas.dividir_parcelas(100, 3)

    assert parcelas == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]
    assert _soma(parcelas) == Decimal("100.00")


@pytest.mark.parametrize(
    ("valor_total", "n_parcelas"),
    [(1200, 6), (1200, 7), (1799, 7), (100, 3), (0.05, 5), (99.99, 4), (2500, 12)],
)
def test_dividir_parcelas_sempre_fecha_a_soma(
    valor_total: float, n_parcelas: int
) -> None:
    """A garantia que interessa: a soma das parcelas é o total, sempre."""
    parcelas = financas.dividir_parcelas(valor_total, n_parcelas)

    assert len(parcelas) == n_parcelas
    assert _soma(parcelas) == Decimal(str(valor_total)).quantize(Decimal("0.01"))


def test_dividir_parcelas_a_vista() -> None:
    assert financas.dividir_parcelas(1200, 1) == [Decimal("1200.00")]


def test_dividir_parcelas_argumento_invalido() -> None:
    with pytest.raises(ValueError):
        financas.dividir_parcelas(1200, 0)
    with pytest.raises(ValueError):
        financas.dividir_parcelas(0, 6)
    with pytest.raises(ValueError):
        financas.dividir_parcelas(-1200, 6)
    # Pequeno demais para caber um centavo em cada parcela.
    with pytest.raises(ValueError):
        financas.dividir_parcelas(0.02, 3)


def test_numeros_do_parcelamento_pelo_total() -> None:
    numeros = financas.numeros_do_parcelamento(n_parcelas=6, valor_total=1200)

    assert numeros == {
        "n_parcelas": 6,
        "valor_total": 1200.0,
        "valor_parcela": 200.0,
        "valor_ultima_parcela": 200.0,
        "ajuste_de_centavos": False,
    }


def test_numeros_do_parcelamento_pelo_total_com_ajuste() -> None:
    numeros = financas.numeros_do_parcelamento(n_parcelas=7, valor_total=1200)

    assert numeros["valor_total"] == 1200.0
    assert numeros["valor_parcela"] == 171.43
    assert numeros["valor_ultima_parcela"] == 171.42
    assert numeros["ajuste_de_centavos"] is True


def test_numeros_do_parcelamento_pela_parcela() -> None:
    """O outro lado do pedido: o total é a multiplicação, feita aqui."""
    numeros = financas.numeros_do_parcelamento(n_parcelas=6, valor_parcela=200)

    assert numeros == {
        "n_parcelas": 6,
        "valor_total": 1200.0,
        "valor_parcela": 200.0,
        "valor_ultima_parcela": 200.0,
        "ajuste_de_centavos": False,
    }


def test_numeros_do_parcelamento_exige_exatamente_um_lado() -> None:
    with pytest.raises(ValueError, match="valor_total OU valor_parcela"):
        financas.numeros_do_parcelamento(
            n_parcelas=6, valor_total=1200, valor_parcela=200
        )
    with pytest.raises(ValueError, match="valor_total OU valor_parcela"):
        financas.numeros_do_parcelamento(n_parcelas=6)


def test_numeros_do_parcelamento_recusa_parcela_invalida() -> None:
    with pytest.raises(ValueError):
        financas.numeros_do_parcelamento(n_parcelas=6, valor_parcela=0)
    with pytest.raises(ValueError):
        financas.numeros_do_parcelamento(n_parcelas=0, valor_parcela=200)
