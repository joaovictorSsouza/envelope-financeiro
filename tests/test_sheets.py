"""Testes da camada de tradução, com api.chamar mockado. Nenhuma rede."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from src import api, sheets

RESPOSTA_LANCAMENTOS: dict[str, Any] = {
    "colunas": sheets.COLUNAS_LANCAMENTOS,
    "linhas": [
        [
            "L1",
            "2026-08-01T00:00:00",
            "2026-08",
            "Entrada",
            "Fixa",
            "salario",
            "salário",
            2500,
            "",
            "",
            "",
            "RECEBIDO",
            "manual",
            "2026-08-01T12:30:00.000Z",
        ],
        [
            "L2",
            "2026-08-10T00:00:00",
            "2026-08",
            "saida",
            "fixa",
            "compras",
            "relógio",
            "R$ 133,00",
            4,
            10,
            "g-relogio",
            "previsto",
            "parcela",
            "2026-08-01T12:30:00.000Z",
        ],
    ],
}


def _mock(monkeypatch: pytest.MonkeyPatch, respostas: dict[str, Any]) -> list[tuple]:
    """Substitui api.chamar por um dublê e registra as chamadas feitas."""
    chamadas: list[tuple] = []

    def falso(acao: str, params: dict[str, Any] | None = None) -> Any:
        chamadas.append((acao, params))
        if acao not in respostas:
            raise AssertionError(f"ação inesperada: {acao}")
        return respostas[acao]

    monkeypatch.setattr(sheets.api, "chamar", falso)
    return chamadas


# --------------------------------------------------------------------------
# ler_lancamentos
# --------------------------------------------------------------------------
def test_ler_lancamentos_tipagem(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(monkeypatch, {"ler_lancamentos": RESPOSTA_LANCAMENTOS})
    df = sheets.ler_lancamentos()

    assert list(df.columns) == sheets.COLUNAS_LANCAMENTOS
    assert df["valor"].dtype == "float64"
    assert str(df["data"].dtype).startswith("datetime64")
    assert str(df["criado_em"].dtype).startswith("datetime64")
    assert df["parcela_atual"].dtype == "Int64"
    assert df["parcela_total"].dtype == "Int64"
    assert df["mes_ref"].dtype == "string"


def test_ler_lancamentos_converte_datas_iso(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(monkeypatch, {"ler_lancamentos": RESPOSTA_LANCAMENTOS})
    df = sheets.ler_lancamentos()

    assert df.loc[0, "data"] == pd.Timestamp("2026-08-01 00:00:00")
    assert df.loc[1, "data"] == pd.Timestamp("2026-08-10 00:00:00")
    # O "Z" é normalizado em UTC e o fuso é descartado, sem virar dtype object.
    assert df.loc[0, "criado_em"] == pd.Timestamp("2026-08-01 12:30:00")
    assert df["data"].dt.tz is None


def test_ler_lancamentos_valores_e_parcelas(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(monkeypatch, {"ler_lancamentos": RESPOSTA_LANCAMENTOS})
    df = sheets.ler_lancamentos()

    assert df.loc[0, "valor"] == 2500.0
    assert df.loc[1, "valor"] == 133.0  # "R$ 133,00" vira float
    assert pd.isna(df.loc[0, "parcela_atual"])  # célula vazia vira <NA>, não 0
    assert df.loc[1, "parcela_atual"] == 4
    assert df.loc[1, "parcela_total"] == 10
    # Enums chegam normalizados em minúsculas para as regras compararem direto.
    assert df.loc[0, "tipo"] == "entrada"
    assert df.loc[0, "natureza"] == "fixa"
    assert df.loc[0, "status"] == "recebido"


def test_ler_lancamentos_descarta_grid_em_branco(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O Apps Script devolve a aba inteira: ~1000 linhas vazias no fim."""
    branco = ["" for _ in sheets.COLUNAS_LANCAMENTOS]
    _mock(
        monkeypatch,
        {
            "ler_lancamentos": {
                "colunas": sheets.COLUNAS_LANCAMENTOS,
                "linhas": RESPOSTA_LANCAMENTOS["linhas"] + [list(branco) for _ in range(998)],
            }
        },
    )
    df = sheets.ler_lancamentos()

    assert len(df) == 2
    assert list(df.index) == [0, 1]


def test_ler_lancamentos_vazio(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(
        monkeypatch,
        {"ler_lancamentos": {"colunas": sheets.COLUNAS_LANCAMENTOS, "linhas": []}},
    )
    df = sheets.ler_lancamentos()

    assert df.empty
    assert list(df.columns) == sheets.COLUNAS_LANCAMENTOS
    assert df["valor"].dtype == "float64"
    assert df["parcela_total"].dtype == "Int64"


# --------------------------------------------------------------------------
# ler_config
# --------------------------------------------------------------------------
def test_ler_config_converte_tipos(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(
        monkeypatch,
        {
            "ler_config": {
                "pct_investimento": "0,20",
                "semanas_no_mes": "3",
                "reserva_intocavel": "TRUE",
                "meta_reserva": "10000",
                "limite_comprometimento": 0.35,
            }
        },
    )
    config = sheets.ler_config()

    assert config["pct_investimento"] == 0.20
    assert isinstance(config["semanas_no_mes"], int) and config["semanas_no_mes"] == 3
    assert config["reserva_intocavel"] is True
    assert config["meta_reserva"] == 10000.0
    assert config["limite_comprometimento"] == 0.35


def test_ler_config_falso(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(monkeypatch, {"ler_config": {"reserva_intocavel": "FALSE"}})
    assert sheets.ler_config()["reserva_intocavel"] is False


# --------------------------------------------------------------------------
# ler_recorrentes
# --------------------------------------------------------------------------
def test_ler_recorrentes_filtra_inativos(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock(
        monkeypatch,
        {
            "ler_recorrentes": {
                "colunas": sheets.COLUNAS_RECORRENTES,
                "linhas": [
                    ["gympass", 150, "saida", "fixa", "saude", 10, True],
                    ["netflix", "39,90", "saida", "fixa", "lazer", 5, "FALSE"],
                    ["salário", 2500, "entrada", "fixa", "salario", 5, "TRUE"],
                    ["", "", "", "", "", "", True],  # linha em branco do grid
                    ["", "", "", "", "", "", False],
                ],
            }
        },
    )
    df = sheets.ler_recorrentes()

    assert list(df["descricao"]) == ["gympass", "salário"]
    assert df["valor"].dtype == "float64"
    assert df["dia_do_mes"].dtype == "Int64"
    assert bool(df["ativo"].all())


# --------------------------------------------------------------------------
# escritas
# --------------------------------------------------------------------------
def test_adicionar_lancamento_envia_params_e_devolve_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas = _mock(
        monkeypatch, {"adicionar_lancamento": {"id": "L99", "mes_ref": "2026-08"}}
    )
    novo_id = sheets.adicionar_lancamento(
        data="2026-08-12",
        mes_ref="2026-08",
        tipo="Saida",
        natureza="Variada",
        categoria="mercado",
        descricao="feira",
        valor="89,9",
        status="pago",
        origem="agente",
    )

    assert novo_id == "L99"
    acao, params = chamadas[0]
    assert acao == "adicionar_lancamento"
    assert params["tipo"] == "saida" and params["natureza"] == "variada"
    assert params["valor"] == 89.90


def test_adicionar_lancamento_valida_antes_da_rede(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas = _mock(monkeypatch, {})

    with pytest.raises(ValueError):
        sheets.adicionar_lancamento(
            data="2026-08-12",
            mes_ref="2026-08",
            tipo="despesa",  # não existe
            natureza="fixa",
            categoria="x",
            descricao="x",
            valor=10,
        )
    with pytest.raises(ValueError):
        sheets.adicionar_lancamento(
            data="2026-08-12",
            mes_ref="08/2026",  # formato errado
            tipo="saida",
            natureza="fixa",
            categoria="x",
            descricao="x",
            valor=10,
        )
    assert chamadas == []


def test_atualizar_status_envia_filtro_e_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas = _mock(
        monkeypatch, {"atualizar_status": {"id": "L7", "status": "pago"}}
    )
    resposta = sheets.atualizar_status("Pago", descricao="mercado", mes_ref="2026-08")

    assert chamadas[0] == (
        "atualizar_status",
        {"status": "pago", "descricao": "mercado", "mes_ref": "2026-08"},
    )
    assert resposta["id"] == "L7"


def test_atualizar_status_propaga_candidatos_da_ambiguidade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quem chama precisa dos candidatos para perguntar ao usuário qual é."""
    candidatos = [
        {"id": "L7", "mes_ref": "2026-08", "descricao": "mercado", "valor": 150.0},
        {"id": "L9", "mes_ref": "2026-08", "descricao": "mercado", "valor": 89.9},
    ]

    def falso(acao: str, params: dict[str, Any] | None = None) -> Any:
        raise api.ApiError(
            "Mais de um lançamento com essa descrição.",
            payload={"ok": False, "erro": "ambiguidade", "candidatos": candidatos},
        )

    monkeypatch.setattr(sheets.api, "chamar", falso)

    with pytest.raises(sheets.AmbiguidadeError) as erro:
        sheets.atualizar_status("pago", descricao="mercado")

    assert erro.value.candidatos == candidatos
    assert [c["id"] for c in erro.value.candidatos] == ["L7", "L9"]
    assert "L9" in str(erro.value)  # a lista aparece na mensagem também
    assert isinstance(erro.value, api.ApiError)  # dá para tratar como erro de API


def test_atualizar_status_ambiguidade_em_resposta_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alguns handlers devolvem ok=true com os candidatos em vez de aplicar."""
    _mock(
        monkeypatch,
        {"atualizar_status": {"candidatos": [{"id": "L7"}, {"id": "L9"}]}},
    )
    with pytest.raises(sheets.AmbiguidadeError) as erro:
        sheets.atualizar_status("pago", descricao="mercado")
    assert len(erro.value.candidatos) == 2


def test_atualizar_status_exige_alvo_e_status_valido(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas = _mock(monkeypatch, {})

    with pytest.raises(ValueError):
        sheets.atualizar_status("pago")  # sem id nem descrição
    with pytest.raises(ValueError):
        sheets.atualizar_status("quitado", id="L7")  # status inexistente
    assert chamadas == []


def test_atualizar_lancamento_envia_so_o_que_mudou(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas = _mock(monkeypatch, {"atualizar_lancamento": {"id": "L7"}})
    sheets.atualizar_lancamento(
        id="L7", valor="1.234,50", categoria="mercado", descricao_nova="feira do mês"
    )

    acao, params = chamadas[0]
    assert acao == "atualizar_lancamento"
    assert params == {
        "id": "L7",
        "valor": 1234.50,
        "categoria": "mercado",
        "descricao_nova": "feira do mês",
    }


def test_atualizar_lancamento_sem_mudanca_falha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas = _mock(monkeypatch, {})
    with pytest.raises(ValueError):
        sheets.atualizar_lancamento(id="L7")
    assert chamadas == []


def test_atualizar_lancamento_propaga_candidatos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def falso(acao: str, params: dict[str, Any] | None = None) -> Any:
        raise api.ApiError(
            "ambíguo", payload={"ok": False, "data": {"candidatos": [{"id": "L1"}]}}
        )

    monkeypatch.setattr(sheets.api, "chamar", falso)
    with pytest.raises(sheets.AmbiguidadeError) as erro:
        sheets.atualizar_lancamento(descricao="mercado", valor=10)
    assert erro.value.candidatos == [{"id": "L1"}]


def test_adicionar_parcelamento_dry_run_por_padrao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previa = {
        "colunas": ["mes_ref", "valor"],
        "previa": [["2026-08", 200.0], ["2026-09", 200.0], ["2026-10", 200.0]],
    }
    chamadas = _mock(monkeypatch, {"adicionar_parcelamento": previa})

    linhas = sheets.adicionar_parcelamento(
        descricao="fone",
        valor_parcela=200.0,
        n_parcelas=3,
        mes_inicial="2026-08",
        categoria="compras",
        dia_do_mes=10,
    )

    assert chamadas[0][1]["dry_run"] is True  # ALTO RISCO: padrão é prévia
    assert linhas == [
        {"mes_ref": "2026-08", "valor": 200.0},
        {"mes_ref": "2026-09", "valor": 200.0},
        {"mes_ref": "2026-10", "valor": 200.0},
    ]


def test_adicionar_parcelamento_so_grava_com_flag_explicita(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas = _mock(monkeypatch, {"adicionar_parcelamento": []})
    sheets.adicionar_parcelamento(
        descricao="fone",
        valor_parcela=200.0,
        n_parcelas=3,
        mes_inicial="2026-08",
        categoria="compras",
        dia_do_mes=10,
        dry_run=False,
    )
    assert chamadas[0][1]["dry_run"] is False


def test_adicionar_parcelamento_manda_a_ultima_parcela_quando_difere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Divisão que não fecha redonda: a última linha vai com o resto."""
    chamadas = _mock(monkeypatch, {"adicionar_parcelamento": []})
    sheets.adicionar_parcelamento(
        descricao="geladeira",
        valor_parcela=171.43,
        valor_ultima_parcela=171.42,
        n_parcelas=7,
        mes_inicial="2026-08",
        categoria="casa",
        dia_do_mes=10,
    )
    assert chamadas[0][1]["valor_ultima_parcela"] == 171.42


def test_adicionar_parcelamento_omite_a_ultima_parcela_quando_igual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parcelamento redondo vai para a planilha com os parâmetros de sempre."""
    chamadas = _mock(monkeypatch, {"adicionar_parcelamento": []})
    sheets.adicionar_parcelamento(
        descricao="fone",
        valor_parcela=200.0,
        valor_ultima_parcela=200.0,
        n_parcelas=6,
        mes_inicial="2026-08",
        categoria="compras",
        dia_do_mes=10,
    )
    assert "valor_ultima_parcela" not in chamadas[0][1]


def test_adicionar_parcelamento_recusa_ultima_parcela_invalida(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chamadas = _mock(monkeypatch, {"adicionar_parcelamento": []})
    with pytest.raises(ValueError):
        sheets.adicionar_parcelamento(
            descricao="fone",
            valor_parcela=200.0,
            valor_ultima_parcela=0.0,
            n_parcelas=6,
            mes_inicial="2026-08",
            categoria="compras",
            dia_do_mes=10,
        )
    assert chamadas == []


def test_gerar_mes_dry_run_por_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas = _mock(
        monkeypatch, {"gerar_mes": [{"descricao": "gympass", "valor": 150.0}]}
    )
    linhas = sheets.gerar_mes("2026-09")

    assert chamadas[0] == ("gerar_mes", {"mes_ref": "2026-09", "dry_run": True})
    assert linhas == [{"descricao": "gympass", "valor": 150.0}]


def test_gerar_mes_valida_mes_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas = _mock(monkeypatch, {})
    with pytest.raises(ValueError):
        sheets.gerar_mes("2026-13")
    assert chamadas == []


# --------------------------------------------------------------------------
# api.py: envelope, cache e erros (sempre com requests.post dublado)
# --------------------------------------------------------------------------
class _Resposta:
    status_code = 200
    text = ""

    def __init__(self, corpo: Any) -> None:
        self._corpo = corpo

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._corpo


def _post_fake(monkeypatch: pytest.MonkeyPatch, corpo: Any) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []

    def falso(url: str, **kwargs: Any) -> _Resposta:
        posts.append({"url": url, **kwargs})
        return _Resposta(corpo)

    monkeypatch.setenv("WEBAPP_URL", "https://exemplo.test/exec")
    monkeypatch.setenv("API_TOKEN", "token-de-teste")
    monkeypatch.setattr(api.requests, "post", falso)
    return posts


def test_api_monta_post_no_formato_do_apps_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts = _post_fake(monkeypatch, {"ok": True, "data": {"hora": "10:00"}})
    assert api.chamar("ping") == {"hora": "10:00"}

    enviado = posts[0]
    assert enviado["headers"]["Content-Type"].startswith("text/plain")
    assert enviado["allow_redirects"] is True  # o Apps Script responde 302
    assert enviado["timeout"] == 30
    assert b'"acao": "ping"' in enviado["data"]
    assert b'"token": "token-de-teste"' in enviado["data"]


def test_api_erro_do_servidor_vira_excecao(monkeypatch: pytest.MonkeyPatch) -> None:
    _post_fake(monkeypatch, {"ok": False, "erro": "token inválido"})
    with pytest.raises(api.ApiError, match="token inválido"):
        api.chamar("ler_config")


def test_api_cacheia_leitura_e_invalida_na_escrita(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts = _post_fake(monkeypatch, {"ok": True, "data": {"pct_investimento": 0.2}})

    api.chamar("ler_config")
    api.chamar("ler_config")
    assert len(posts) == 1  # segunda leitura veio do cache de 60s

    api.chamar("adicionar_lancamento", {"valor": 1})
    api.chamar("ler_config")
    assert len(posts) == 3  # a escrita derrubou o cache


def test_api_nao_cacheia_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = _post_fake(monkeypatch, {"ok": True, "data": {"hora": "10:00"}})
    api.chamar("ping")
    api.chamar("ping")
    assert len(posts) == 2
