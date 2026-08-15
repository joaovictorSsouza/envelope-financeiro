"""Tradução das respostas da API em DataFrames e dicts tipados.

Esta camada não decide nada: só busca (via `api.chamar`) e converte tipos.
Toda regra de negócio vive em `financas.py`, que recebe estes DataFrames
prontos e não faz I/O.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final

import pandas as pd

from . import api

COLUNAS_LANCAMENTOS: Final[list[str]] = [
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

COLUNAS_RECORRENTES: Final[list[str]] = [
    "descricao",
    "valor",
    "tipo",
    "natureza",
    "categoria",
    "dia_do_mes",
    "ativo",
]

_COLUNAS_DATA: Final[tuple[str, ...]] = ("data", "criado_em")
_COLUNAS_INT: Final[tuple[str, ...]] = ("parcela_atual", "parcela_total")
_COLUNAS_ENUM: Final[tuple[str, ...]] = ("tipo", "natureza", "status", "origem")

_CONFIG_INT: Final[frozenset[str]] = frozenset({"semanas_no_mes"})
_CONFIG_BOOL: Final[frozenset[str]] = frozenset({"reserva_intocavel"})
_CONFIG_FLOAT: Final[frozenset[str]] = frozenset(
    {"pct_investimento", "meta_reserva", "limite_comprometimento"}
)

TIPOS: Final[frozenset[str]] = frozenset({"entrada", "saida"})
NATUREZAS: Final[frozenset[str]] = frozenset({"fixa", "variada"})
STATUS: Final[frozenset[str]] = frozenset({"previsto", "pago", "recebido"})
ORIGENS: Final[frozenset[str]] = frozenset(
    {"manual", "recorrencia", "parcela", "agente", "migracao"}
)

_VERDADEIROS: Final[frozenset[str]] = frozenset({"true", "verdadeiro", "sim", "1", "x"})


# --------------------------------------------------------------------------
# conversores
# --------------------------------------------------------------------------
def _para_numero(valor: Any) -> float | None:
    """Aceita número puro ou texto do Sheets ("R$ 1.234,56") e devolve float."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool):
        return float(valor)
    if isinstance(valor, (int, float)):
        return float(valor)

    texto = str(valor).strip().replace("R$", "").replace("\xa0", "").replace(" ", "")
    if not texto:
        return None
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return None


def _para_bool(valor: Any) -> bool:
    """TRUE do Sheets chega como bool, texto ou número — tudo vira bool."""
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return valor != 0
    return str(valor).strip().lower() in _VERDADEIROS


def _para_data(serie: pd.Series) -> pd.Series:
    """ISO ("2026-08-01T00:00:00" ou com Z) -> datetime64 sem timezone.

    Normaliza tudo em UTC antes de descartar o fuso, para que datas com e
    sem "Z" na mesma coluna não virem dtype object.
    """
    convertida = pd.to_datetime(serie, errors="coerce", utc=True, format="ISO8601")
    return convertida.dt.tz_localize(None)


def _texto(serie: pd.Series) -> pd.Series:
    return serie.astype("string").str.strip()


def _para_iso(valor: Any) -> str:
    """Normaliza data/datetime/texto para a string ISO que o Apps Script espera."""
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    return str(valor).strip()


def _tabela_para_df(data: Any, colunas_padrao: list[str]) -> pd.DataFrame:
    """Converte {colunas, linhas} em DataFrame, tolerando resposta vazia."""
    if not isinstance(data, dict):
        raise api.RespostaInvalida(f"Esperava objeto com colunas/linhas, veio {data!r}")
    colunas = [str(c).strip() for c in (data.get("colunas") or colunas_padrao)]
    linhas = data.get("linhas") or []
    return pd.DataFrame(linhas, columns=colunas)


def _sem_linhas_vazias(df: pd.DataFrame, colunas_chave: tuple[str, ...]) -> pd.DataFrame:
    """Descarta as linhas em branco do grid.

    O Apps Script devolve a planilha inteira, não só a região preenchida:
    uma aba com 3 registros chega com mais de mil linhas vazias.
    """
    if df.empty:
        return df
    vazia = pd.Series(True, index=df.index)
    for coluna in colunas_chave:
        serie = df[coluna]
        if pd.api.types.is_numeric_dtype(serie) or pd.api.types.is_datetime64_any_dtype(
            serie
        ):
            preenchida = serie.notna()
        else:
            preenchida = serie.notna() & (serie.astype("string").str.strip() != "")
        vazia &= ~preenchida
    return df[~vazia]


# --------------------------------------------------------------------------
# leituras
# --------------------------------------------------------------------------
def ler_lancamentos() -> pd.DataFrame:
    """Lê a aba LANCAMENTOS já tipada.

    O futuro já está na tabela: uma compra em 6x são 6 linhas, uma por
    mes_ref, com o mesmo grupo_id. Nada aqui projeta parcelas.
    """
    df = _tabela_para_df(api.chamar("ler_lancamentos"), COLUNAS_LANCAMENTOS)

    for coluna in COLUNAS_LANCAMENTOS:
        if coluna not in df.columns:
            df[coluna] = None
    df = df[COLUNAS_LANCAMENTOS]

    df["valor"] = df["valor"].map(_para_numero).astype("float64")
    for coluna in _COLUNAS_DATA:
        df[coluna] = _para_data(df[coluna])
    for coluna in _COLUNAS_INT:
        df[coluna] = pd.to_numeric(df[coluna], errors="coerce").astype("Int64")
    for coluna in ("id", "mes_ref", "categoria", "descricao", "grupo_id"):
        df[coluna] = _texto(df[coluna])
    for coluna in _COLUNAS_ENUM:
        df[coluna] = _texto(df[coluna]).str.lower()

    df = _sem_linhas_vazias(df, ("id", "mes_ref", "descricao", "valor"))
    return df.reset_index(drop=True)


def ler_config() -> dict[str, Any]:
    """Lê CONFIG como objeto plano, com float/int/bool já convertidos."""
    data = api.chamar("ler_config")
    if not isinstance(data, dict):
        raise api.RespostaInvalida(f"CONFIG deveria ser um objeto, veio {data!r}")

    config: dict[str, Any] = {}
    for chave_bruta, valor in data.items():
        chave = str(chave_bruta).strip()
        if chave in _CONFIG_BOOL:
            config[chave] = _para_bool(valor)
        elif chave in _CONFIG_INT:
            numero = _para_numero(valor)
            config[chave] = int(numero) if numero is not None else None
        elif chave in _CONFIG_FLOAT:
            config[chave] = _para_numero(valor)
        else:
            numero = _para_numero(valor)
            config[chave] = numero if numero is not None else valor
    return config


def ler_recorrentes() -> pd.DataFrame:
    """Lê RECORRENTES devolvendo apenas as linhas com ativo = True."""
    df = _tabela_para_df(api.chamar("ler_recorrentes"), COLUNAS_RECORRENTES)

    for coluna in COLUNAS_RECORRENTES:
        if coluna not in df.columns:
            df[coluna] = None
    df = df[COLUNAS_RECORRENTES]

    df["valor"] = df["valor"].map(_para_numero).astype("float64")
    df["dia_do_mes"] = pd.to_numeric(df["dia_do_mes"], errors="coerce").astype("Int64")
    df["ativo"] = df["ativo"].map(_para_bool).astype("bool")
    for coluna in ("descricao", "categoria"):
        df[coluna] = _texto(df[coluna])
    for coluna in ("tipo", "natureza"):
        df[coluna] = _texto(df[coluna]).str.lower()

    df = _sem_linhas_vazias(df, ("descricao", "valor"))
    return df[df["ativo"]].reset_index(drop=True)


# --------------------------------------------------------------------------
# escritas
# --------------------------------------------------------------------------
def adicionar_lancamento(
    *,
    data: str | date | datetime,
    mes_ref: str,
    tipo: str,
    natureza: str,
    categoria: str,
    descricao: str,
    valor: float,
    status: str = "previsto",
    origem: str = "agente",
) -> str:
    """Grava UMA linha em LANCAMENTOS e devolve o id gerado.

    BAIXO RISCO: escreve uma única linha, sem efeito sobre meses futuros.
    Os enums são validados aqui para o erro aparecer antes da rede.
    """
    _validar_enum("tipo", tipo, TIPOS)
    _validar_enum("natureza", natureza, NATUREZAS)
    _validar_enum("status", status, STATUS)
    _validar_enum("origem", origem, ORIGENS)
    _validar_mes_ref(mes_ref)

    numero = _para_numero(valor)
    if numero is None:
        raise ValueError(f"valor inválido: {valor!r}")

    resposta = api.chamar(
        "adicionar_lancamento",
        {
            "data": _para_iso(data),
            "mes_ref": mes_ref,
            "tipo": tipo.lower(),
            "natureza": natureza.lower(),
            "categoria": categoria,
            "descricao": descricao,
            "valor": round(numero, 2),
            "status": status.lower(),
            "origem": origem.lower(),
        },
    )
    if not isinstance(resposta, dict) or "id" not in resposta:
        raise api.RespostaInvalida(f"Esperava {{id, mes_ref}}, veio {resposta!r}")
    return str(resposta["id"])


class AmbiguidadeError(api.ApiError):
    """Mais de um lançamento casa com o filtro; `candidatos` traz as opções.

    Quem chama precisa poder perguntar ao usuário qual deles é — por isso a
    lista sobe junto com o erro em vez de virar só uma mensagem.
    """

    def __init__(self, mensagem: str, candidatos: list[dict[str, Any]]) -> None:
        super().__init__(mensagem)
        self.candidatos = candidatos

    def __str__(self) -> str:
        resumo = "; ".join(_rotulo_candidato(c) for c in self.candidatos)
        return f"{self.mensagem} Candidatos: {resumo}"


def _rotulo_candidato(candidato: dict[str, Any]) -> str:
    partes = [
        str(candidato[chave])
        for chave in ("id", "mes_ref", "descricao", "valor")
        if isinstance(candidato, dict) and candidato.get(chave) not in (None, "")
    ]
    return " | ".join(partes) if partes else str(candidato)


def _candidatos(fonte: Any) -> list[dict[str, Any]]:
    """Procura a lista de candidatos nas chaves que o Apps Script pode usar."""
    if not isinstance(fonte, dict):
        return []
    for chave in ("candidatos", "opcoes", "matches", "lancamentos"):
        valor = fonte.get(chave)
        if isinstance(valor, list) and valor:
            return [item if isinstance(item, dict) else {"linha": item} for item in valor]
    for interno in ("data", "detalhes", "erro"):
        achados = _candidatos(fonte.get(interno))
        if achados:
            return achados
    return []


def _chamar_com_ambiguidade(acao: str, params: dict[str, Any]) -> Any:
    """Converte o erro de ambiguidade da API em AmbiguidadeError com candidatos."""
    try:
        resposta = api.chamar(acao, params)
    except api.ApiError as exc:
        candidatos = _candidatos(exc.payload)
        if candidatos:
            raise AmbiguidadeError(exc.mensagem, candidatos) from exc
        raise

    # Alguns handlers devolvem ok=true com a lista em vez de aplicar a mudança.
    if isinstance(resposta, dict) and "id" not in resposta:
        candidatos = _candidatos(resposta)
        if candidatos:
            raise AmbiguidadeError("Mais de um lançamento casa com o filtro.", candidatos)
    return resposta


def _filtro_de_alvo(
    id: str | None, descricao: str | None, mes_ref: str | None
) -> dict[str, Any]:
    """Monta o filtro que identifica a linha: id, ou descrição (+ mes_ref)."""
    if not id and not descricao:
        raise ValueError("Informe id ou descricao para identificar o lançamento")
    if mes_ref is not None:
        _validar_mes_ref(mes_ref)

    filtro: dict[str, Any] = {}
    if id:
        filtro["id"] = str(id)
    if descricao:
        filtro["descricao"] = descricao
    if mes_ref:
        filtro["mes_ref"] = mes_ref
    return filtro


def atualizar_status(
    status: str,
    id: str | None = None,
    descricao: str | None = None,
    mes_ref: str | None = None,
) -> dict[str, Any]:
    """Muda o status de UM lançamento (ex.: previsto -> pago).

    BAIXO RISCO: mexe em uma linha e não cria nada. Se o filtro casar com
    mais de um lançamento, levanta AmbiguidadeError com os candidatos para
    quem chama perguntar ao usuário qual é.
    """
    _validar_enum("status", status, STATUS)
    params = {"status": status.lower(), **_filtro_de_alvo(id, descricao, mes_ref)}
    resposta = _chamar_com_ambiguidade("atualizar_status", params)
    return resposta if isinstance(resposta, dict) else {"resultado": resposta}


def atualizar_lancamento(
    id: str | None = None,
    descricao: str | None = None,
    mes_ref: str | None = None,
    valor: float | None = None,
    categoria: str | None = None,
    descricao_nova: str | None = None,
    natureza: str | None = None,
) -> dict[str, Any]:
    """Edita campos de UM lançamento já existente.

    BAIXO RISCO: altera uma linha, sem efeito em meses futuros. `descricao`
    serve para achar a linha; `descricao_nova` é o texto que substitui.
    Ambiguidade vira AmbiguidadeError com os candidatos.
    """
    mudancas: dict[str, Any] = {}
    if valor is not None:
        numero = _para_numero(valor)
        if numero is None:
            raise ValueError(f"valor inválido: {valor!r}")
        mudancas["valor"] = round(numero, 2)
    if categoria is not None:
        mudancas["categoria"] = categoria
    if descricao_nova is not None:
        mudancas["descricao_nova"] = descricao_nova
    if natureza is not None:
        _validar_enum("natureza", natureza, NATUREZAS)
        mudancas["natureza"] = natureza.lower()

    if not mudancas:
        raise ValueError("Informe ao menos um campo para alterar")

    params = {**_filtro_de_alvo(id, descricao, mes_ref), **mudancas}
    resposta = _chamar_com_ambiguidade("atualizar_lancamento", params)
    return resposta if isinstance(resposta, dict) else {"resultado": resposta}


def adicionar_parcelamento(
    *,
    descricao: str,
    valor_parcela: float,
    n_parcelas: int,
    mes_inicial: str,
    categoria: str,
    dia_do_mes: int,
    valor_ultima_parcela: float | None = None,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """Cria N linhas futuras (uma por mes_ref) e devolve a prévia.

    ALTO RISCO: grava em vários meses de uma vez. `dry_run=True` é o padrão
    e devolve apenas a prévia; só grava com dry_run=False explícito.

    `valor_ultima_parcela` existe para a divisão fechar em centavos: quando
    o total não divide redondo, a última linha sai com o resto. Ele só é
    enviado quando difere de `valor_parcela` — parcelamento redondo
    continua com exatamente os mesmos parâmetros de sempre.
    """
    _validar_mes_ref(mes_inicial)
    if n_parcelas < 1:
        raise ValueError("n_parcelas deve ser >= 1")

    numero = _para_numero(valor_parcela)
    if numero is None or numero <= 0:
        raise ValueError(f"valor_parcela inválido: {valor_parcela!r}")

    params: dict[str, Any] = {
        "descricao": descricao,
        "valor_parcela": round(numero, 2),
        "n_parcelas": int(n_parcelas),
        "mes_inicial": mes_inicial,
        "categoria": categoria,
        "dia_do_mes": int(dia_do_mes),
        "dry_run": bool(dry_run),
    }

    if valor_ultima_parcela is not None:
        ultima = _para_numero(valor_ultima_parcela)
        if ultima is None or ultima <= 0:
            raise ValueError(
                f"valor_ultima_parcela inválido: {valor_ultima_parcela!r}"
            )
        if round(ultima, 2) != round(numero, 2):
            params["valor_ultima_parcela"] = round(ultima, 2)

    return _linhas_de_previa(api.chamar("adicionar_parcelamento", params))


def gerar_mes(mes_ref: str, dry_run: bool = True) -> list[dict[str, Any]]:
    """Materializa os lançamentos recorrentes do mês e devolve a prévia.

    ALTO RISCO: cria várias linhas de uma vez e pode duplicar o mês se
    rodado duas vezes. `dry_run=True` é o padrão; só grava com
    dry_run=False explícito.
    """
    _validar_mes_ref(mes_ref)
    return _linhas_de_previa(
        api.chamar("gerar_mes", {"mes_ref": mes_ref, "dry_run": bool(dry_run)})
    )


# --------------------------------------------------------------------------
# auxiliares
# --------------------------------------------------------------------------
def _validar_enum(campo: str, valor: str, permitidos: frozenset[str]) -> None:
    if str(valor).strip().lower() not in permitidos:
        raise ValueError(f"{campo} inválido: {valor!r}. Use um de {sorted(permitidos)}")


def _validar_mes_ref(mes_ref: str) -> None:
    texto = str(mes_ref).strip()
    partes = texto.split("-")
    valido = (
        len(partes) == 2
        and len(partes[0]) == 4
        and len(partes[1]) == 2
        and partes[0].isdigit()
        and partes[1].isdigit()
        and 1 <= int(partes[1]) <= 12
    )
    if not valido:
        raise ValueError(f"mes_ref inválido: {mes_ref!r}. Formato esperado 'AAAA-MM'")


def _linhas_de_previa(data: Any) -> list[dict[str, Any]]:
    """Aceita as formas de prévia do Apps Script e devolve lista de dicts."""
    if data is None:
        return []
    if isinstance(data, list):
        return [linha if isinstance(linha, dict) else {"linha": linha} for linha in data]
    if isinstance(data, dict):
        for chave in ("previa", "linhas", "preview", "rows"):
            if chave in data:
                interno = data[chave]
                if isinstance(interno, list) and "colunas" in data:
                    colunas = [str(c) for c in data["colunas"]]
                    return [
                        dict(zip(colunas, linha)) if isinstance(linha, list) else linha
                        for linha in interno
                    ]
                return _linhas_de_previa(interno)
        return [data]
    raise api.RespostaInvalida(f"Prévia em formato inesperado: {data!r}")
