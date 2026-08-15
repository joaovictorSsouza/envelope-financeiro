"""Cliente HTTP do Web App do Google Apps Script.

Único ponto de I/O do projeto: nenhum outro módulo fala com a rede.

Protocolo: POST com corpo JSON {token, acao, params} e resposta
{"ok": true, "data": ...} ou {"ok": false, "erro": "..."}.

Dois detalhes do Apps Script que não são negociáveis:
1. O POST responde 302 para script.googleusercontent.com — precisamos
   seguir o redirect (allow_redirects=True).
2. O Content-Type tem que ser "text/plain": com "application/json" o
   navegador/cliente dispara preflight CORS e o Apps Script recusa.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Final

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT_S: Final[int] = 30
"""O Apps Script hiberna: o primeiro acesso pode levar dezenas de segundos."""

TENTATIVAS: Final[int] = 3
BACKOFF_BASE_S: Final[float] = 1.0
CACHE_TTL_S: Final[int] = 60

ACOES_LEITURA: Final[frozenset[str]] = frozenset(
    {"ler_lancamentos", "ler_recorrentes", "ler_config"}
)
"""Só estas são cacheadas. `ping` é diagnóstico e precisa ser sempre real."""

ACOES_ESCRITA: Final[frozenset[str]] = frozenset(
    {
        "adicionar_lancamento",
        "adicionar_parcelamento",
        "gerar_mes",
        "atualizar_status",
        "atualizar_lancamento",
    }
)
"""Qualquer uma delas invalida o cache inteiro de leitura."""


class ApiError(RuntimeError):
    """A API respondeu, mas com ok=false. A mensagem é o campo `erro`.

    Guarda o envelope inteiro em `payload`: erros como o de ambiguidade
    trazem dados extras (candidatos) que quem chama precisa ler.
    """

    def __init__(self, mensagem: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.payload: dict[str, Any] = payload or {}


class ErroDeRede(ApiError):
    """Falha de transporte (timeout, DNS, conexão) após esgotar as tentativas."""


class RespostaInvalida(ApiError):
    """A resposta não é o JSON esperado — normalmente HTML de erro do Google."""


_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def _config_ambiente() -> tuple[str, str]:
    """Lê WEBAPP_URL e API_TOKEN do ambiente, falhando cedo e com clareza."""
    url = os.getenv("WEBAPP_URL", "").strip()
    token = os.getenv("API_TOKEN", "").strip()
    if not url or not token:
        faltando = [
            nome
            for nome, valor in (("WEBAPP_URL", url), ("API_TOKEN", token))
            if not valor
        ]
        raise ApiError(
            f"Variáveis de ambiente ausentes: {', '.join(faltando)}. "
            "Copie .env.example para .env e preencha."
        )
    return url, token


def _chave_cache(acao: str, params: dict[str, Any]) -> str:
    return json.dumps([acao, params], sort_keys=True, default=str)


def _do_post(url: str, corpo: dict[str, Any]) -> requests.Response:
    return requests.post(
        url,
        data=json.dumps(corpo).encode("utf-8"),
        headers={"Content-Type": "text/plain;charset=utf-8"},
        timeout=TIMEOUT_S,
        allow_redirects=True,
    )


def _extrair_data(resposta: requests.Response) -> Any:
    """Valida o envelope {ok, data|erro} e devolve apenas `data`."""
    try:
        corpo = resposta.json()
    except ValueError as exc:
        trecho = resposta.text[:200].replace("\n", " ")
        raise RespostaInvalida(
            f"Resposta não-JSON (HTTP {resposta.status_code}): {trecho}"
        ) from exc

    if not isinstance(corpo, dict) or "ok" not in corpo:
        raise RespostaInvalida(f"Envelope inesperado: {corpo!r}")

    if not corpo.get("ok"):
        raise ApiError(str(corpo.get("erro", "erro desconhecido")), payload=corpo)

    return corpo.get("data")


def chamar(acao: str, params: dict[str, Any] | None = None) -> Any:
    """Executa uma ação no Web App e devolve o conteúdo de `data`.

    Leituras são servidas de um cache de 60s; escritas limpam esse cache.
    Erros de rede são repetidos 3 vezes com backoff exponencial; ok=false
    do servidor é erro de negócio e NÃO é repetido.
    """
    params = dict(params or {})
    url, token = _config_ambiente()

    cacheavel = acao in ACOES_LEITURA
    chave = _chave_cache(acao, params)

    if cacheavel:
        with _lock:
            registro = _cache.get(chave)
            if registro is not None and time.monotonic() - registro[0] < CACHE_TTL_S:
                return registro[1]

    corpo = {"token": token, "acao": acao, "params": params}
    ultimo_erro: Exception | None = None

    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resposta = _do_post(url, corpo)
            resposta.raise_for_status()
            data = _extrair_data(resposta)
            break
        except (requests.RequestException, RespostaInvalida) as exc:
            # Transporte instável ou HTML de erro do Google: vale repetir.
            ultimo_erro = exc
            if tentativa == TENTATIVAS:
                raise ErroDeRede(
                    f"Falha ao chamar '{acao}' após {TENTATIVAS} tentativas: {exc}"
                ) from exc
            time.sleep(BACKOFF_BASE_S * 2 ** (tentativa - 1))
    else:  # pragma: no cover - inalcançável: o loop sempre sai por break/raise
        raise ErroDeRede(str(ultimo_erro))

    with _lock:
        if cacheavel:
            _cache[chave] = (time.monotonic(), data)
        elif acao in ACOES_ESCRITA:
            _cache.clear()

    return data


def limpar_cache() -> None:
    """Descarta o cache de leitura. Útil em testes e após escrita externa."""
    with _lock:
        _cache.clear()
