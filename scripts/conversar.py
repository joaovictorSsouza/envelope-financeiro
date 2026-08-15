"""REPL de terminal para conversar com o agente. Interface da Fase 3.

    python scripts/conversar.py
    python scripts/conversar.py --debug

`--debug` imprime, depois de cada resposta, as ferramentas que o modelo
escolheu e com que argumentos — é a única forma de ver se ele está chamando
`ver_acompanhamento` quando devia ou respondendo de cabeça.

A conversa inteira roda numa thread só, então o histórico persiste entre as
mensagens; fechar o script perde tudo, porque o checkpointer é de memória.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from src import agente as agente_mod  # noqa: E402

SAIDAS = {"sair", "exit", "quit", ":q"}


def _utf8() -> None:
    """O console do Windows abre em cp1252 e engasga com 'ç' e 'R$'."""
    for fluxo in (sys.stdout, sys.stderr):
        if hasattr(fluxo, "reconfigure"):
            fluxo.reconfigure(encoding="utf-8", errors="replace")


def _mostrar_chamadas(chamadas: list[dict]) -> None:
    if not chamadas:
        print("  [debug] nenhuma tool chamada")
        return
    for chamada in chamadas:
        argumentos = json.dumps(chamada.get("args", {}), ensure_ascii=False)
        print(f"  [debug] {chamada.get('name')}({argumentos})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Conversa com o agente financeiro.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="mostra as tools chamadas e seus argumentos",
    )
    parser.add_argument(
        "--thread",
        default=agente_mod.THREAD_PADRAO,
        help="id da thread; troque para começar uma conversa limpa",
    )
    args = parser.parse_args()

    _utf8()

    try:
        agente = agente_mod.criar_agente()
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"Não consegui montar o agente: {exc}")
        return 1

    print(f"Agente financeiro [{agente_mod.descrever_modelo()}]. 'sair' para encerrar.\n")
    print(agente_mod.bloco_de_contexto())
    print()

    while True:
        try:
            mensagem = input("você> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not mensagem:
            continue
        if mensagem.lower() in SAIDAS:
            return 0

        try:
            resposta, chamadas = agente_mod.conversar(agente, mensagem, args.thread)
        except KeyboardInterrupt:
            print("\n[interrompido]\n")
            continue
        except Exception as exc:  # noqa: BLE001 — REPL não pode morrer no turno
            print(f"\n[erro] {type(exc).__name__}: {exc}\n")
            continue

        if args.debug:
            _mostrar_chamadas(chamadas)
        print(f"\nagente> {resposta}\n")


if __name__ == "__main__":
    raise SystemExit(main())
