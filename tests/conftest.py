"""Configuração dos testes: raiz no sys.path e rede bloqueada.

Nenhum teste deste projeto pode fazer requisição real. A fixture autouse
abaixo troca `requests.post` por uma função que explode, então qualquer
chamada não mockada aparece como falha explícita e não como lentidão.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from src import api, tools  # noqa: E402


@pytest.fixture(autouse=True)
def _sem_rede(monkeypatch: pytest.MonkeyPatch) -> None:
    def _proibido(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Teste tentou acessar a rede — mocke api.chamar")

    monkeypatch.setattr(api.requests, "post", _proibido)
    api.limpar_cache()


@pytest.fixture(autouse=True)
def _sem_simulacoes_vazadas() -> None:
    """O guard de dois passos guarda estado de processo: zera entre testes.

    Sem isso, uma simulação feita num teste liberaria a confirmação de
    outro — e o teste que prova que o guard segura passaria por acidente.
    """
    tools.limpar_simulacoes()
