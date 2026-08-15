"""Testes das tools, com `sheets` mockado. Nenhuma rede, nenhuma escrita real.

Duas coisas são verificadas em quase todo teste, porque são o contrato da
camada: o retorno passa por `json.dumps` sem estourar (nada de DataFrame,
Timestamp ou NaN vazando para o modelo) e nenhuma exceção sobe.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd
import pytest

from src import api, financas, sheets, tools

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

MES = "2026-08"
MES_SEGUINTE = "2026-09"


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
    """Mesmos dtypes que `sheets.ler_lancamentos` entrega."""
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
def lancamentos() -> pd.DataFrame:
    """2026-08 materializado (tem origem=recorrencia); 2026-09 ainda não existe."""
    return _df(
        [
            _linha(
                id="1",
                mes_ref=MES,
                tipo="entrada",
                natureza="fixa",
                categoria="salario",
                descricao="salário",
                valor=2500.00,
                status="recebido",
                origem="recorrencia",
            ),
            _linha(
                id="2",
                mes_ref=MES,
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
                mes_ref=MES,
                categoria="saude",
                descricao="gympass",
                valor=150.00,
                origem="recorrencia",
            ),
            _linha(
                id="4",
                mes_ref=MES,
                natureza="variada",
                categoria="mercado",
                descricao="mercado",
                valor=160.00,
                status="pago",
            ),
            _linha(
                id="5",
                mes_ref="2026-07",
                tipo="entrada",
                natureza="fixa",
                categoria="salario",
                descricao="salário",
                valor=2500.00,
                status="recebido",
                origem="recorrencia",
            ),
            _linha(
                id="6",
                mes_ref="2026-07",
                natureza="variada",
                categoria="lazer",
                descricao="cinema",
                valor=90.00,
                status="pago",
            ),
        ]
    )


@pytest.fixture
def recorrentes() -> pd.DataFrame:
    df = pd.DataFrame(
        [
            {
                "descricao": "salário",
                "valor": 2500.00,
                "tipo": "entrada",
                "natureza": "fixa",
                "categoria": "salario",
                "dia_do_mes": 5,
                "ativo": True,
            },
            {
                "descricao": "gympass",
                "valor": 150.00,
                "tipo": "saida",
                "natureza": "fixa",
                "categoria": "saude",
                "dia_do_mes": 10,
                "ativo": True,
            },
        ],
        columns=sheets.COLUNAS_RECORRENTES,
    )
    df["valor"] = df["valor"].astype("float64")
    df["dia_do_mes"] = df["dia_do_mes"].astype("Int64")
    df["ativo"] = df["ativo"].astype("bool")
    return df


@pytest.fixture
def sheets_mockado(
    monkeypatch: pytest.MonkeyPatch,
    lancamentos: pd.DataFrame,
    recorrentes: pd.DataFrame,
) -> dict[str, list[Any]]:
    """Troca as leituras de `sheets` por dublês e registra as escritas.

    Qualquer escrita real que escapar aparece aqui — é assim que os testes de
    "não pode gravar" ficam honestos.
    """
    escritas: dict[str, list[Any]] = {
        "adicionar_lancamento": [],
        "atualizar_status": [],
        "atualizar_lancamento": [],
        "adicionar_parcelamento": [],
        "gerar_mes": [],
    }

    monkeypatch.setattr(tools.sheets, "ler_lancamentos", lambda: lancamentos)
    monkeypatch.setattr(tools.sheets, "ler_config", lambda: dict(CONFIG))
    monkeypatch.setattr(tools.sheets, "ler_recorrentes", lambda: recorrentes)

    def _adicionar_lancamento(**kwargs: Any) -> str:
        escritas["adicionar_lancamento"].append(kwargs)
        return "L99"

    def _atualizar_status(status: str, **kwargs: Any) -> dict[str, Any]:
        escritas["atualizar_status"].append({"status": status, **kwargs})
        return {"id": "L1", "status": status}

    def _atualizar_lancamento(**kwargs: Any) -> dict[str, Any]:
        escritas["atualizar_lancamento"].append(kwargs)
        return {"id": "L1"}

    def _adicionar_parcelamento(**kwargs: Any) -> list[dict[str, Any]]:
        escritas["adicionar_parcelamento"].append(kwargs)
        return [
            {"mes_ref": "2026-09", "valor": 300.0, "parcela_atual": 1},
            {"mes_ref": "2026-10", "valor": 300.0, "parcela_atual": 2},
        ]

    def _gerar_mes(mes_ref: str, dry_run: bool = True) -> list[dict[str, Any]]:
        escritas["gerar_mes"].append({"mes_ref": mes_ref, "dry_run": dry_run})
        return [{"mes_ref": mes_ref, "descricao": "salário", "valor": 2500.0}]

    monkeypatch.setattr(tools.sheets, "adicionar_lancamento", _adicionar_lancamento)
    monkeypatch.setattr(tools.sheets, "atualizar_status", _atualizar_status)
    monkeypatch.setattr(tools.sheets, "atualizar_lancamento", _atualizar_lancamento)
    monkeypatch.setattr(tools.sheets, "adicionar_parcelamento", _adicionar_parcelamento)
    monkeypatch.setattr(tools.sheets, "gerar_mes", _gerar_mes)
    return escritas


def _serializavel(valor: Any) -> str:
    """json.dumps sem `default`: se algo não for JSON puro, o teste quebra aqui."""
    return json.dumps(valor, ensure_ascii=False)


# --------------------------------------------------------------------------
# contrato geral
# --------------------------------------------------------------------------
CHAMADAS_DE_LEITURA: list[tuple[Any, dict[str, Any]]] = [
    (tools.ver_orcamento, {}),
    (tools.ver_orcamento, {"mes_ref": MES}),
    (tools.ver_acompanhamento, {}),
    (tools.ver_acompanhamento, {"mes_ref": MES}),
    (tools.ver_resumo, {"mes_ref": MES}),
    (tools.ver_gastos_por_categoria, {"mes_ref": MES}),
    (tools.listar_lancamentos, {}),
    (tools.listar_lancamentos, {"mes_ref": MES}),
    (tools.listar_lancamentos, {"mes_ref": MES, "tipo": "saida", "natureza": "fixa"}),
    (tools.ver_compromissos_futuros, {"mes_ref": MES, "n_meses": 3}),
    (tools.ver_parcelas_em_aberto, {"mes_ref": MES}),
    (tools.ver_planejamento, {"mes_ref": MES_SEGUINTE}),
    (tools.comparar, {"mes_a": "2026-07", "mes_b": MES}),
    (tools.simular_compra, {"valor": 1800.0, "n_parcelas": 6, "mes_ref": MES}),
]


@pytest.mark.parametrize(
    "ferramenta, argumentos",
    CHAMADAS_DE_LEITURA,
    ids=[f"{f.name}-{i}" for i, (f, _) in enumerate(CHAMADAS_DE_LEITURA)],
)
def test_leitura_devolve_json_serializavel(
    ferramenta: Any,
    argumentos: dict[str, Any],
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = ferramenta.invoke(argumentos)

    assert isinstance(resultado, dict)
    assert "erro" not in resultado
    _serializavel(resultado)


def test_escrita_devolve_json_serializavel(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    chamadas = [
        (tools.registrar_gasto, {"descricao": "café", "valor": 12.5, "categoria": "lazer"}),
        (tools.marcar_como_pago, {"descricao": "gympass", "mes_ref": MES}),
        (tools.marcar_como_recebido, {"descricao": "salário", "mes_ref": MES}),
        (tools.corrigir_lancamento, {"id": "2", "valor": 140.0}),
        (
            tools.simular_parcelamento,
            {
                "descricao": "geladeira",
                "valor_parcela": 300.0,
                "n_parcelas": 6,
                "mes_inicial": MES_SEGUINTE,
                "categoria": "casa",
            },
        ),
        (
            tools.confirmar_parcelamento,
            {
                "descricao": "geladeira",
                "valor_parcela": 300.0,
                "n_parcelas": 6,
                "mes_inicial": MES_SEGUINTE,
                "categoria": "casa",
            },
        ),
        (tools.simular_geracao_mes, {"mes_ref": MES_SEGUINTE}),
        (tools.confirmar_geracao_mes, {"mes_ref": MES_SEGUINTE}),
    ]
    for ferramenta, argumentos in chamadas:
        resultado = ferramenta.invoke(argumentos)
        assert isinstance(resultado, dict), ferramenta.name
        assert "erro" not in resultado, ferramenta.name
        _serializavel(resultado)


def test_nenhum_dataframe_vaza(sheets_mockado: dict[str, list[Any]]) -> None:
    """Tabela sempre vira lista de dicts antes de chegar ao modelo."""
    planejamento = tools.ver_planejamento.invoke({"mes_ref": MES})
    comparacao = tools.comparar.invoke({"mes_a": "2026-07", "mes_b": MES})
    categorias = tools.ver_gastos_por_categoria.invoke({"mes_ref": MES})

    assert isinstance(planejamento["parcelas_que_caem"], list)
    assert isinstance(comparacao["categorias"], list)
    assert isinstance(categorias["categorias"], list)
    assert all(isinstance(item, dict) for item in comparacao["categorias"])


def test_valores_monetarios_sao_float_com_2_casas(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """Nunca string formatada: quem escreve "R$" é a camada de resposta."""
    orcamento = tools.ver_orcamento.invoke({"mes_ref": MES})

    for campo in ("renda", "fixas", "reserva_planejada", "envelope_variavel"):
        valor = orcamento[campo]
        assert isinstance(valor, float), campo
        assert round(valor, 2) == valor, campo
    assert orcamento["renda"] == 2500.00


def test_toda_tool_tem_descricao_para_o_modelo() -> None:
    """O docstring é o que o modelo lê para escolher — não pode faltar."""
    assert len(tools.TOOLS) == 18
    for ferramenta in tools.TOOLS:
        assert ferramenta.description and len(ferramenta.description) > 80
        assert ferramenta.name == ferramenta.func.__name__


# --------------------------------------------------------------------------
# mês padrão: planejamento x acompanhamento
# --------------------------------------------------------------------------
def test_ver_orcamento_sem_argumento_usa_o_mes_seguinte(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    hoje = date(2026, 8, 13)
    monkeypatch.setattr(
        tools.financas, "mes_de_planejamento", lambda *a, **k: financas.somar_meses(
            financas.mes_ref_de_hoje(hoje), 1
        )
    )
    monkeypatch.setattr(
        tools.financas, "mes_de_acompanhamento", lambda *a, **k: "NAO-DEVE-USAR"
    )

    resultado = tools.ver_orcamento.invoke({})

    assert resultado["mes_ref"] == MES_SEGUINTE


def test_ver_acompanhamento_sem_argumento_usa_o_mes_corrente(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    hoje = date(2026, 8, 13)
    monkeypatch.setattr(
        tools.financas, "mes_de_acompanhamento", lambda *a, **k: financas.mes_ref_de_hoje(hoje)
    )
    monkeypatch.setattr(
        tools.financas, "mes_de_planejamento", lambda *a, **k: "NAO-DEVE-USAR"
    )

    resultado = tools.ver_acompanhamento.invoke({})

    assert resultado["mes_ref"] == MES


def test_os_dois_meses_padrao_sao_diferentes() -> None:
    """A distinção planejar/acompanhar não pode colapsar no mesmo mês."""
    hoje = date(2026, 8, 13)
    assert financas.mes_de_acompanhamento(hoje) == MES
    assert financas.mes_de_planejamento(hoje) == MES_SEGUINTE


def test_ver_planejamento_sem_argumento_usa_o_mes_seguinte(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    monkeypatch.setattr(
        tools.financas, "mes_de_planejamento", lambda *a, **k: MES_SEGUINTE
    )

    assert tools.ver_planejamento.invoke({})["mes_ref"] == MES_SEGUINTE


# --------------------------------------------------------------------------
# a janela futura sempre recebe as recorrentes
# --------------------------------------------------------------------------
def test_simular_compra_sempre_passa_recorrentes(
    monkeypatch: pytest.MonkeyPatch,
    sheets_mockado: dict[str, list[Any]],
    recorrentes: pd.DataFrame,
) -> None:
    """Sem recorrentes a projeção fica incompleta e a compra é recusada à toa."""
    original = financas.capacidade_de_compra
    capturado: dict[str, Any] = {}

    def espiao(*args: Any, **kwargs: Any) -> dict[str, Any]:
        capturado.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(tools.financas, "capacidade_de_compra", espiao)
    tools.simular_compra.invoke({"valor": 1800.0, "n_parcelas": 6, "mes_ref": MES})

    assert capturado["recorrentes"] is not None
    pd.testing.assert_frame_equal(capturado["recorrentes"], recorrentes)


def test_simular_compra_projeta_meses_ainda_nao_gerados(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """Só 2026-08 existe na planilha; os meses seguintes vêm sintetizados."""
    resultado = tools.simular_compra.invoke(
        {"valor": 1800.0, "n_parcelas": 6, "mes_ref": MES}
    )

    assert resultado["projecao_incompleta"] is False
    assert any(mes["sintetizado"] for mes in resultado["projecao"])
    assert set(resultado["projecao"][0]) == {
        "mes_ref",
        "livre_por_semana",
        "cabe",
        "sintetizado",
    }


def test_ver_orcamento_de_mes_nao_gerado_projeta_as_recorrentes(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """2026-09 não tem nenhuma linha: sem projeção, a renda apareceria como 0."""
    resultado = tools.ver_orcamento.invoke({"mes_ref": MES_SEGUINTE})

    assert resultado["materializado"] is False
    assert resultado["renda"] == 2500.00
    assert resultado["fixas"] == 150.00


def test_ver_compromissos_futuros_projeta_a_janela(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.ver_compromissos_futuros.invoke({"mes_ref": MES, "n_meses": 3})

    assert [mes["mes_ref"] for mes in resultado["meses"]] == [
        MES,
        MES_SEGUINTE,
        "2026-10",
    ]
    # Sem projeção, setembro entraria zerado na janela.
    assert resultado["meses"][1]["entradas"] == 2500.00


def test_ver_compromissos_futuros_marca_o_que_e_estimativa(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """2026-08 é fato lançado; 2026-09 em diante é projeção das recorrentes."""
    meses = tools.ver_compromissos_futuros.invoke({"mes_ref": MES, "n_meses": 3})[
        "meses"
    ]

    assert [mes["sintetizado"] for mes in meses] == [False, True, True]


def test_ver_planejamento_passa_recorrentes_e_config(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.ver_planejamento.invoke({"mes_ref": MES_SEGUINTE})

    assert resultado["materializado"] is False
    assert set(resultado["faltando"]) == {"salário", "gympass"}
    # previa_orcamento só existe porque a config foi passada adiante.
    assert resultado["previa_orcamento"]["renda"] == 2500.00


# --------------------------------------------------------------------------
# alto risco: simular nunca grava
# --------------------------------------------------------------------------
def test_simular_parcelamento_nao_grava(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.simular_parcelamento.invoke(
        {
            "descricao": "geladeira",
            "valor_parcela": 300.0,
            "n_parcelas": 6,
            "mes_inicial": MES_SEGUINTE,
            "categoria": "casa",
        }
    )

    assert resultado["gravado"] is False
    assert resultado["previa"]
    assert [c["dry_run"] for c in sheets_mockado["adicionar_parcelamento"]] == [True]
    # Nenhuma outra escrita foi disparada de tabela.
    assert sheets_mockado["adicionar_lancamento"] == []
    assert sheets_mockado["gerar_mes"] == []


def test_dia_do_mes_padrao_no_mes_corrente_e_hoje() -> None:
    """Parcelamento que começa neste mês vence no dia da compra."""
    hoje = date(2026, 8, 13)

    assert tools._dia_padrao(MES, hoje) == 13


def test_dia_do_mes_padrao_em_mes_futuro_e_dia_1() -> None:
    """Para um mês que ainda não chegou não existe "hoje" que faça sentido."""
    hoje = date(2026, 8, 13)

    assert tools._dia_padrao(MES_SEGUINTE, hoje) == 1
    assert tools._dia_padrao("2027-03", hoje) == 1


def test_simular_parcelamento_usa_o_dia_padrao(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    monkeypatch.setattr(tools, "_dia_padrao", lambda *a, **k: 13)

    resultado = tools.simular_parcelamento.invoke(
        {
            "descricao": "geladeira",
            "valor_parcela": 300.0,
            "n_parcelas": 6,
            "mes_inicial": MES,
            "categoria": "casa",
        }
    )

    assert sheets_mockado["adicionar_parcelamento"][0]["dia_do_mes"] == 13
    # O dia volta na prévia para o confirmar gravar exatamente o que foi visto.
    assert resultado["dia_do_mes"] == 13


def test_dia_do_mes_explicito_manda(sheets_mockado: dict[str, list[Any]]) -> None:
    tools.simular_parcelamento.invoke(
        {
            "descricao": "geladeira",
            "valor_parcela": 300.0,
            "n_parcelas": 6,
            "mes_inicial": MES,
            "categoria": "casa",
            "dia_do_mes": 20,
        }
    )

    assert sheets_mockado["adicionar_parcelamento"][0]["dia_do_mes"] == 20


def test_simular_parcelamento_nao_expoe_dry_run() -> None:
    """Se `dry_run` fosse argumento, o modelo poderia gravar sem confirmar."""
    campos = tools.simular_parcelamento.args_schema.model_fields
    assert "dry_run" not in campos
    assert "dry_run" not in tools.simular_geracao_mes.args_schema.model_fields


def test_confirmar_parcelamento_grava(sheets_mockado: dict[str, list[Any]]) -> None:
    """O ciclo completo: simular libera a confirmação idêntica."""
    argumentos = {
        "descricao": "geladeira",
        "valor_parcela": 300.0,
        "n_parcelas": 6,
        "mes_inicial": MES_SEGUINTE,
        "categoria": "casa",
    }
    tools.simular_parcelamento.invoke(dict(argumentos))
    resultado = tools.confirmar_parcelamento.invoke(dict(argumentos))

    assert resultado["gravado"] is True
    assert [c["dry_run"] for c in sheets_mockado["adicionar_parcelamento"]] == [
        True,
        False,
    ]


def test_confirmar_parcelamento_exige_confirmacao_no_docstring() -> None:
    """O contrato de alto risco vive na descrição — é ela que o modelo lê."""
    descricao = tools.confirmar_parcelamento.description.lower()
    assert "simular_parcelamento" in descricao
    assert "confirm" in descricao

    descricao_mes = tools.confirmar_geracao_mes.description.lower()
    assert "simular_geracao_mes" in descricao_mes
    assert "confirm" in descricao_mes


def test_simular_geracao_mes_nao_grava(sheets_mockado: dict[str, list[Any]]) -> None:
    resultado = tools.simular_geracao_mes.invoke({"mes_ref": MES_SEGUINTE})

    assert resultado["gravado"] is False
    assert sheets_mockado["gerar_mes"] == [
        {"mes_ref": MES_SEGUINTE, "dry_run": True}
    ]


def test_confirmar_geracao_mes_grava(sheets_mockado: dict[str, list[Any]]) -> None:
    """O ciclo completo: simular libera a confirmação do mesmo mês."""
    tools.simular_geracao_mes.invoke({"mes_ref": MES_SEGUINTE})
    resultado = tools.confirmar_geracao_mes.invoke({"mes_ref": MES_SEGUINTE})

    assert resultado["gravado"] is True
    assert sheets_mockado["gerar_mes"] == [
        {"mes_ref": MES_SEGUINTE, "dry_run": True},
        {"mes_ref": MES_SEGUINTE, "dry_run": False},
    ]


# --------------------------------------------------------------------------
# o guard de dois passos
# --------------------------------------------------------------------------
PARCELAMENTO: dict[str, Any] = {
    "descricao": "geladeira",
    "valor_parcela": 300.0,
    "n_parcelas": 6,
    "mes_inicial": MES_SEGUINTE,
    "categoria": "casa",
}


def test_confirmar_parcelamento_recusa_sem_simulacao(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """O teste que importa: confirmar direto não escreve nada."""
    resultado = tools.confirmar_parcelamento.invoke(dict(PARCELAMENTO))

    assert resultado["gravado"] is False
    assert resultado["erro"] == "sem_simulacao"
    assert "simular_parcelamento" in resultado["sugestao"]
    assert sheets_mockado["adicionar_parcelamento"] == []


def test_confirmar_geracao_mes_recusa_sem_simulacao(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.confirmar_geracao_mes.invoke({"mes_ref": MES_SEGUINTE})

    assert resultado["gravado"] is False
    assert resultado["erro"] == "sem_simulacao"
    assert sheets_mockado["gerar_mes"] == []


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("n_parcelas", 3),
        ("valor_parcela", 250.0),
        ("mes_inicial", "2026-10"),
        ("descricao", "geladeira nova"),
        ("categoria", "eletro"),
    ],
)
def test_confirmar_com_argumento_diferente_do_simulado_recusa(
    sheets_mockado: dict[str, list[Any]], campo: str, valor: Any
) -> None:
    """Mudou o valor depois da prévia? Então o usuário não confirmou ISSO."""
    tools.simular_parcelamento.invoke(dict(PARCELAMENTO))
    resultado = tools.confirmar_parcelamento.invoke({**PARCELAMENTO, campo: valor})

    assert resultado["gravado"] is False
    assert resultado["erro"] == "sem_simulacao"
    assert [c["dry_run"] for c in sheets_mockado["adicionar_parcelamento"]] == [True]


def test_simulacao_vale_por_uma_confirmacao_so(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """É o que impede `gerar_mes` de duplicar o mês inteiro."""
    tools.simular_geracao_mes.invoke({"mes_ref": MES_SEGUINTE})

    primeira = tools.confirmar_geracao_mes.invoke({"mes_ref": MES_SEGUINTE})
    segunda = tools.confirmar_geracao_mes.invoke({"mes_ref": MES_SEGUINTE})

    assert primeira["gravado"] is True
    assert segunda["gravado"] is False
    assert segunda["erro"] == "sem_simulacao"
    gravacoes = [c for c in sheets_mockado["gerar_mes"] if not c["dry_run"]]
    assert len(gravacoes) == 1


def test_simulacao_expira_depois_de_30_minutos(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    """Prévia de meia hora atrás não vale como confirmação de agora."""
    relogio = {"agora": 1000.0}
    monkeypatch.setattr(tools.time, "monotonic", lambda: relogio["agora"])

    tools.simular_parcelamento.invoke(dict(PARCELAMENTO))
    relogio["agora"] += tools.JANELA_SIMULACAO_S + 1
    resultado = tools.confirmar_parcelamento.invoke(dict(PARCELAMENTO))

    assert resultado["gravado"] is False
    assert resultado["erro"] == "sem_simulacao"
    assert [c["dry_run"] for c in sheets_mockado["adicionar_parcelamento"]] == [True]


def test_simulacao_dentro_da_janela_ainda_vale(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    relogio = {"agora": 1000.0}
    monkeypatch.setattr(tools.time, "monotonic", lambda: relogio["agora"])

    tools.simular_parcelamento.invoke(dict(PARCELAMENTO))
    relogio["agora"] += tools.JANELA_SIMULACAO_S - 60
    resultado = tools.confirmar_parcelamento.invoke(dict(PARCELAMENTO))

    assert resultado["gravado"] is True


def test_simulacao_de_uma_thread_nao_libera_outra(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """A conversa do Telegram de amanhã não herda a prévia de hoje."""
    config_a = {"configurable": {"thread_id": "conversa-a"}}
    config_b = {"configurable": {"thread_id": "conversa-b"}}

    tools.simular_parcelamento.invoke(dict(PARCELAMENTO), config=config_a)

    de_outra = tools.confirmar_parcelamento.invoke(dict(PARCELAMENTO), config=config_b)
    assert de_outra["gravado"] is False
    assert de_outra["erro"] == "sem_simulacao"

    da_mesma = tools.confirmar_parcelamento.invoke(dict(PARCELAMENTO), config=config_a)
    assert da_mesma["gravado"] is True


def test_simulacao_que_falhou_nao_libera_confirmacao(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    """Prévia que estourou não é prévia: não há o que o usuário tenha visto."""

    def explode(**kwargs: Any) -> Any:
        raise api.ErroDeRede("sem rede")

    monkeypatch.setattr(tools.sheets, "adicionar_parcelamento", explode)
    previa = tools.simular_parcelamento.invoke(dict(PARCELAMENTO))
    assert "erro" in previa

    monkeypatch.undo()
    resultado = tools.confirmar_parcelamento.invoke(dict(PARCELAMENTO))

    assert resultado["gravado"] is False
    assert resultado["erro"] == "sem_simulacao"


def test_guard_nao_mexe_no_schema_exposto_ao_modelo() -> None:
    """A trava é interna: nenhum argumento novo apareceu para o modelo."""
    campos = set(tools.confirmar_parcelamento.args_schema.model_fields)
    assert campos == {
        "descricao",
        "valor_parcela",
        "n_parcelas",
        "mes_inicial",
        "categoria",
        "dia_do_mes",
    }
    assert set(tools.confirmar_geracao_mes.args_schema.model_fields) == {"mes_ref"}


# --------------------------------------------------------------------------
# escrita de baixo risco
# --------------------------------------------------------------------------
def test_registrar_gasto_grava_uma_linha(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.registrar_gasto.invoke(
        {"descricao": "mercado", "valor": 80.0, "categoria": "mercado", "mes_ref": MES}
    )

    assert resultado["gravado"] is True
    assert resultado["id"] == "L99"
    (chamada,) = sheets_mockado["adicionar_lancamento"]
    assert chamada["tipo"] == "saida"
    assert chamada["natureza"] == "variada"
    assert chamada["mes_ref"] == MES
    assert chamada["origem"] == "agente"
    assert chamada["data"]  # sem data explícita, entra a de hoje


def test_registrar_gasto_ja_gastei_grava_pago(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """"gastei 80 no mercado": o dinheiro já saiu, é gasto realizado."""
    resultado = tools.registrar_gasto.invoke(
        {
            "descricao": "mercado",
            "valor": 80.0,
            "categoria": "mercado",
            "mes_ref": MES,
            "ja_gastei": True,
        }
    )

    assert resultado["status"] == "pago"
    assert sheets_mockado["adicionar_lancamento"][0]["status"] == "pago"


def test_registrar_gasto_planejado_grava_previsto(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """"vou gastar uns 400 de mercado": reserva dentro do envelope."""
    resultado = tools.registrar_gasto.invoke(
        {
            "descricao": "mercado",
            "valor": 400.0,
            "categoria": "mercado",
            "mes_ref": MES_SEGUINTE,
            "ja_gastei": False,
        }
    )

    assert resultado["status"] == "previsto"
    assert sheets_mockado["adicionar_lancamento"][0]["status"] == "previsto"


def test_registrar_gasto_por_padrao_ja_aconteceu(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """O caso comum é o usuário contando um gasto que já fez."""
    resultado = tools.registrar_gasto.invoke(
        {"descricao": "café", "valor": 12.0, "categoria": "lazer", "mes_ref": MES}
    )

    assert resultado["status"] == "pago"
    assert sheets_mockado["adicionar_lancamento"][0]["status"] == "pago"


def test_registrar_gasto_sem_mes_usa_o_corrente(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    monkeypatch.setattr(tools.financas, "mes_de_acompanhamento", lambda *a, **k: MES)

    tools.registrar_gasto.invoke(
        {"descricao": "café", "valor": 12.0, "categoria": "lazer"}
    )

    assert sheets_mockado["adicionar_lancamento"][0]["mes_ref"] == MES


def test_marcar_como_pago_repassa_o_filtro(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    tools.marcar_como_pago.invoke({"descricao": "gympass", "mes_ref": MES})

    assert sheets_mockado["atualizar_status"] == [
        {"status": "pago", "id": None, "descricao": "gympass", "mes_ref": MES}
    ]


def test_marcar_como_recebido_grava_recebido(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.marcar_como_recebido.invoke(
        {"descricao": "salário", "mes_ref": MES}
    )

    assert resultado["gravado"] is True
    assert sheets_mockado["atualizar_status"] == [
        {"status": "recebido", "id": None, "descricao": "salário", "mes_ref": MES}
    ]


# --------------------------------------------------------------------------
# pago x recebido: a tool errada não grava
# --------------------------------------------------------------------------
def test_marcar_como_recebido_em_saida_nao_grava(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """"gympass" é saída: recebido não é status dela."""
    resultado = tools.marcar_como_recebido.invoke(
        {"descricao": "gympass", "mes_ref": MES}
    )

    assert resultado["gravado"] is False
    assert "marcar_como_pago" in resultado["sugestao"]
    assert resultado["tipos_encontrados"] == ["saida"]
    assert sheets_mockado["atualizar_status"] == []
    _serializavel(resultado)


def test_marcar_como_pago_em_entrada_nao_grava(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """"salário" é entrada: pago não é status dela."""
    resultado = tools.marcar_como_pago.invoke({"descricao": "salário", "mes_ref": MES})

    assert resultado["gravado"] is False
    assert "marcar_como_recebido" in resultado["sugestao"]
    assert resultado["tipos_encontrados"] == ["entrada"]
    assert sheets_mockado["atualizar_status"] == []


def test_alvo_desconhecido_nao_bloqueia_a_escrita(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """Não achar a linha aqui não prova que ela não existe: a planilha decide."""
    resultado = tools.marcar_como_pago.invoke({"descricao": "internet"})

    assert resultado["gravado"] is True
    assert len(sheets_mockado["atualizar_status"]) == 1


# --------------------------------------------------------------------------
# ambiguidade: a tool não escolhe
# --------------------------------------------------------------------------
CANDIDATOS = [
    {"id": "L7", "mes_ref": "2026-08", "descricao": "faculdade", "valor": 350.0},
    {"id": "L9", "mes_ref": "2026-09", "descricao": "faculdade", "valor": 350.0},
]


def test_marcar_como_pago_com_ambiguidade_devolve_candidatos(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise sheets.AmbiguidadeError("Mais de um lançamento.", CANDIDATOS)

    monkeypatch.setattr(tools.sheets, "atualizar_status", explode)
    resultado = tools.marcar_como_pago.invoke({"descricao": "faculdade"})

    assert resultado["erro"] == "ambiguo"
    assert [c["id"] for c in resultado["candidatos"]] == ["L7", "L9"]
    assert resultado["sugestao"]
    _serializavel(resultado)


def test_corrigir_lancamento_com_ambiguidade_devolve_candidatos(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise sheets.AmbiguidadeError("Mais de um lançamento.", CANDIDATOS)

    monkeypatch.setattr(tools.sheets, "atualizar_lancamento", explode)
    resultado = tools.corrigir_lancamento.invoke(
        {"descricao": "faculdade", "valor": 360.0}
    )

    assert resultado["erro"] == "ambiguo"
    assert len(resultado["candidatos"]) == 2
    assert "gravado" not in resultado


# --------------------------------------------------------------------------
# listar_lancamentos
# --------------------------------------------------------------------------
def test_listar_lancamentos_devolve_as_linhas_como_lista_de_dicts(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.listar_lancamentos.invoke({"mes_ref": MES})

    assert isinstance(resultado["lancamentos"], list)
    assert all(isinstance(linha, dict) for linha in resultado["lancamentos"])
    assert [linha["descricao"] for linha in resultado["lancamentos"]] == [
        "salário",
        "gympass",
        "relógio",
        "mercado",
    ]
    _serializavel(resultado)


def test_listar_lancamentos_traz_os_quatro_subtotais(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """A fixture tem uma parcela dentro das fixas: 283 com 133 dentro."""
    resultado = tools.listar_lancamentos.invoke({"mes_ref": MES})

    assert resultado["total_entradas"] == 2500.00
    assert resultado["total_fixas"] == 283.00  # gympass 150 + relógio 133
    assert resultado["total_parcelas"] == 133.00  # o relógio, que já está nas fixas
    assert resultado["total_variadas"] == 160.00


def test_listar_lancamentos_marca_a_parcela_e_deixa_o_resto_nulo(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """NaN do pandas nunca chega ao modelo: vira None e sobrevive ao json."""
    linhas = tools.listar_lancamentos.invoke({"mes_ref": MES})["lancamentos"]
    por_descricao = {linha["descricao"]: linha for linha in linhas}

    assert por_descricao["relógio"]["parcela_atual"] == 4
    assert por_descricao["relógio"]["parcela_total"] == 10
    assert por_descricao["gympass"]["parcela_atual"] is None
    assert por_descricao["gympass"]["parcela_total"] is None
    _serializavel(linhas)


def test_listar_lancamentos_sem_argumento_usa_o_mes_de_acompanhamento(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    hoje = date(2026, 8, 13)
    monkeypatch.setattr(
        tools.financas,
        "mes_de_acompanhamento",
        lambda *a, **k: financas.mes_ref_de_hoje(hoje),
    )
    monkeypatch.setattr(
        tools.financas, "mes_de_planejamento", lambda *a, **k: "NAO-DEVE-USAR"
    )

    resultado = tools.listar_lancamentos.invoke({})

    assert resultado["mes_ref"] == MES
    assert resultado["total_entradas"] == 2500.00


def test_listar_lancamentos_com_filtros_combinados(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.listar_lancamentos.invoke(
        {"mes_ref": MES, "tipo": "saida", "natureza": "fixa"}
    )

    assert [linha["descricao"] for linha in resultado["lancamentos"]] == [
        "gympass",
        "relógio",
    ]
    assert resultado["total_entradas"] == 0.0
    assert resultado["total_variadas"] == 0.0
    assert resultado["total_fixas"] == 283.00


def test_listar_lancamentos_apenas_parcelas(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.listar_lancamentos.invoke(
        {"mes_ref": MES, "apenas_parcelas": True}
    )

    assert [linha["descricao"] for linha in resultado["lancamentos"]] == ["relógio"]
    assert all(
        linha["parcela_total"] is not None for linha in resultado["lancamentos"]
    )
    assert resultado["total_parcelas"] == 133.00


def test_listar_lancamentos_de_mes_sem_dado_volta_vazio_sem_projetar(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    """Esta tool não projeta: mês não gerado é lista vazia, não estimativa."""
    resultado = tools.listar_lancamentos.invoke({"mes_ref": MES_SEGUINTE})

    assert resultado["lancamentos"] == []
    assert resultado["total_fixas"] == 0.0
    assert resultado["total_entradas"] == 0.0


def test_listar_lancamentos_com_filtro_invalido_vira_dict_de_erro(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.listar_lancamentos.invoke({"mes_ref": MES, "tipo": "despesa"})

    assert "tipo inválido" in resultado["erro"]
    assert resultado["sugestao"]
    _serializavel(resultado)


# --------------------------------------------------------------------------
# erros nunca sobem como exceção
# --------------------------------------------------------------------------
def test_api_error_vira_dict_de_erro(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    def explode() -> Any:
        raise api.ApiError("token inválido")

    monkeypatch.setattr(tools.sheets, "ler_lancamentos", explode)
    resultado = tools.ver_acompanhamento.invoke({"mes_ref": MES})

    assert resultado["erro"] == "token inválido"
    assert resultado["sugestao"]
    _serializavel(resultado)


def test_erro_de_rede_tem_frase_acionavel(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    def explode() -> Any:
        raise api.ErroDeRede("timeout após 3 tentativas")

    monkeypatch.setattr(tools.sheets, "ler_lancamentos", explode)
    resultado = tools.ver_resumo.invoke({"mes_ref": MES})

    assert resultado["erro"] == "não consegui falar com a planilha, tente de novo"
    assert resultado["sugestao"]


def test_resposta_invalida_vira_dict_de_erro(
    monkeypatch: pytest.MonkeyPatch, sheets_mockado: dict[str, list[Any]]
) -> None:
    def explode() -> Any:
        raise api.RespostaInvalida("<html>erro do Google</html>")

    monkeypatch.setattr(tools.sheets, "ler_lancamentos", explode)
    resultado = tools.ver_gastos_por_categoria.invoke({"mes_ref": MES})

    assert "erro" in resultado
    assert resultado["sugestao"]


def test_mes_ref_invalido_vira_dict_de_erro(
    sheets_mockado: dict[str, list[Any]],
) -> None:
    resultado = tools.ver_resumo.invoke({"mes_ref": "agosto"})

    assert "mes_ref" in resultado["erro"]
    assert resultado["sugestao"]


def test_escrita_sem_alvo_vira_dict_de_erro() -> None:
    """Sem id nem descrição não dá para saber qual linha mudar.

    Sem mock: a validação do `sheets` é síncrona e falha antes de qualquer
    chamada de rede — o que importa é que o erro chegue como dict.
    """
    resultado = tools.marcar_como_pago.invoke({})

    assert "Informe id ou descricao" in resultado["erro"]
    assert resultado["sugestao"]


def test_nenhuma_tool_levanta_excecao_sem_dados(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planilha vazia é caso real (mês zerado), não motivo para quebrar."""
    vazio = _df([])
    monkeypatch.setattr(tools.sheets, "ler_lancamentos", lambda: vazio)
    monkeypatch.setattr(tools.sheets, "ler_config", lambda: dict(CONFIG))
    monkeypatch.setattr(
        tools.sheets,
        "ler_recorrentes",
        lambda: pd.DataFrame(columns=sheets.COLUNAS_RECORRENTES),
    )

    for ferramenta, argumentos in CHAMADAS_DE_LEITURA:
        resultado = ferramenta.invoke(argumentos)
        assert isinstance(resultado, dict), ferramenta.name
        _serializavel(resultado)


# --------------------------------------------------------------------------
# serialização
# --------------------------------------------------------------------------
def test_json_converte_nan_nat_e_timestamp() -> None:
    df = pd.DataFrame(
        {
            "data": [pd.Timestamp("2026-08-05"), pd.NaT],
            "valor": [133.456, float("nan")],
            "parcela": pd.array([4, None], dtype="Int64"),
            "descricao": pd.array(["relógio", None], dtype="string"),
        }
    )

    convertido = tools._json({"linhas": df, "quando": date(2026, 8, 13)})

    assert convertido["linhas"][0] == {
        "data": "2026-08-05T00:00:00",
        "valor": 133.46,
        "parcela": 4,
        "descricao": "relógio",
    }
    assert convertido["linhas"][1] == {
        "data": None,
        "valor": None,
        "parcela": None,
        "descricao": None,
    }
    assert convertido["quando"] == "2026-08-13"
    _serializavel(convertido)


def test_json_preserva_booleanos_do_numpy() -> None:
    """`cabe` e `invade_reserva` precisam virar true/false, não 0/1."""
    import numpy as np

    convertido = tools._json({"cabe": np.bool_(True), "n": np.int64(6)})

    assert convertido["cabe"] is True
    assert convertido["n"] == 6
    assert json.loads(_serializavel(convertido))["cabe"] is True
