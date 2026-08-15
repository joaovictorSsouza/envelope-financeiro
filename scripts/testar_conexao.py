"""Valida a conexão com o Web App do Apps Script chamando a ação `ping`.

Uso:  python scripts/testar_conexao.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from src.api import ApiError, chamar  # noqa: E402


def main() -> int:
    if not (RAIZ / ".env").exists():
        print("[!] Arquivo .env não encontrado. Copie .env.example para .env "
              "e preencha WEBAPP_URL e API_TOKEN.")
        return 2

    print("→ chamando ação 'ping' ...")
    inicio = time.monotonic()
    try:
        data = chamar("ping")
    except ApiError as exc:
        print(f"[FALHA] {type(exc).__name__}: {exc}")
        return 1

    decorrido = time.monotonic() - inicio
    print(f"[OK] resposta em {decorrido:.2f}s")
    print(f"     data = {data!r}")
    if isinstance(data, dict) and "hora" in data:
        print(f"     hora do servidor = {data['hora']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
