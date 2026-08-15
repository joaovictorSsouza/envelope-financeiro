"""Bot do Telegram: transporte fino entre chat e agente.

Esta camada nao calcula nada, nao escolhe tool e nao mexe nas regras de
negocio. Ela valida o chat autorizado, chama o agente com um `thread_id`
derivado do chat e entrega texto simples de volta ao Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from dotenv import load_dotenv

from . import agente as agente_mod

load_dotenv()

LIMITE_TELEGRAM: Final[int] = 4096
MENSAGEM_PRIVADO: Final[str] = "este bot é privado"
MENSAGEM_ERRO: Final[str] = (
    "Não consegui responder agora. Tente de novo em alguns instantes."
)
MENSAGEM_COTA: Final[str] = "atingi o limite diário do modelo, tente amanhã"

logger = logging.getLogger(__name__)

ConversarFn = Callable[[Any, str, str], tuple[str, list[dict[str, Any]]]]


def _chat_id(update: Any) -> str | None:
    chat = getattr(update, "effective_chat", None)
    if chat is None:
        return None
    identificador = getattr(chat, "id", None)
    if identificador is None:
        return None
    return str(identificador)


def _texto(update: Any) -> str:
    mensagem = getattr(update, "message", None) or getattr(
        update, "effective_message", None
    )
    return str(getattr(mensagem, "text", "") or "").strip()


def _dividir(texto: str, limite: int = LIMITE_TELEGRAM) -> list[str]:
    if not texto:
        return ["Não consegui montar uma resposta agora. Tente de novo."]
    return [texto[inicio : inicio + limite] for inicio in range(0, len(texto), limite)]


def _erro_de_cota(exc: BaseException) -> bool:
    texto = str(exc).lower()
    marcas = (
        "resource_exhausted",
        "quota",
        "cota",
        "rate limit",
        "too many requests",
        "429",
        "limite diário",
        "limite diario",
    )
    return any(marca in texto for marca in marcas)


async def _reply(update: Any, texto: str) -> None:
    mensagem = getattr(update, "message", None) or getattr(
        update, "effective_message", None
    )
    if mensagem is None:
        return
    await mensagem.reply_text(texto)


@dataclass
class Sessoes:
    """Mapeia chat autorizado para a thread atual do agente."""

    sufixos: dict[str, str] = field(default_factory=dict)

    def thread_id(self, chat_id: str) -> str:
        return f"telegram:{chat_id}:{self.sufixos.setdefault(chat_id, 'inicial')}"

    def nova(self, chat_id: str) -> str:
        self.sufixos[chat_id] = uuid.uuid4().hex
        return self.thread_id(chat_id)


@dataclass
class BotFinanceiro:
    agente: Any
    chat_id_autorizado: str
    conversar_fn: ConversarFn = agente_mod.conversar
    sessoes: Sessoes = field(default_factory=Sessoes)
    log: logging.Logger = logger

    def __post_init__(self) -> None:
        self.chat_id_autorizado = str(self.chat_id_autorizado).strip()

    def autorizado(self, chat_id: str | None) -> bool:
        return chat_id is not None and chat_id == self.chat_id_autorizado

    async def _bloquear(self, update: Any, chat_id: str | None) -> None:
        self.log.warning("Tentativa de acesso nao autorizado ao bot: chat_id=%s", chat_id)
        await _reply(update, MENSAGEM_PRIVADO)

    async def _exigir_autorizacao(self, update: Any) -> str | None:
        chat_id = _chat_id(update)
        if not self.autorizado(chat_id):
            await self._bloquear(update, chat_id)
            return None
        return chat_id

    async def start(self, update: Any, context: Any) -> None:
        if await self._exigir_autorizacao(update) is None:
            return
        await _reply(
            update,
            "Sou seu agente financeiro. Mande uma pergunta ou registre um gasto.",
        )

    async def ajuda(self, update: Any, context: Any) -> None:
        if await self._exigir_autorizacao(update) is None:
            return
        await _reply(
            update,
            "\n".join(
                [
                    "Exemplos:",
                    "quanto posso gastar essa semana?",
                    "como está meu orçamento de setembro?",
                    "gastei R$ 42,50 no mercado",
                    "simula um notebook de R$ 1.800 em 6x",
                    "marque a faculdade como paga",
                ]
            ),
        )

    async def novo(self, update: Any, context: Any) -> None:
        chat_id = await self._exigir_autorizacao(update)
        if chat_id is None:
            return
        self.sessoes.nova(chat_id)
        await _reply(update, "Histórico limpo. Pode começar um novo assunto.")

    async def mensagem(self, update: Any, context: Any) -> None:
        chat_id = await self._exigir_autorizacao(update)
        if chat_id is None:
            return

        texto = _texto(update)
        if not texto:
            return

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            thread_id = self.sessoes.thread_id(chat_id)
            resposta, _ = await asyncio.to_thread(
                self.conversar_fn, self.agente, texto, thread_id
            )
        except Exception as exc:  # noqa: BLE001 - turno falho nao derruba o bot
            self.log.exception("Erro ao processar turno do Telegram: chat_id=%s", chat_id)
            resposta = MENSAGEM_COTA if _erro_de_cota(exc) else MENSAGEM_ERRO

        for parte in _dividir(resposta):
            await _reply(update, parte)

    async def erro_global(self, update: object, context: Any) -> None:
        exc = getattr(context, "error", None)
        exc_info = (type(exc), exc, exc.__traceback__) if exc else None
        self.log.error("Erro global no bot do Telegram", exc_info=exc_info)
        if update is not None:
            mensagem = MENSAGEM_COTA if _erro_de_cota(exc or Exception()) else MENSAGEM_ERRO
            await _reply(update, mensagem)


def _env_obrigatoria(nome: str, valor: str | None = None) -> str:
    escolhido = valor if valor is not None else os.getenv(nome)
    if not escolhido:
        raise RuntimeError(f"{nome} não está no .env.")
    return escolhido.strip()


def criar_aplicacao(
    token: str | None = None,
    chat_id_autorizado: str | None = None,
    agente: Any | None = None,
    conversar_fn: ConversarFn = agente_mod.conversar,
) -> Any:
    """Monta a Application do python-telegram-bot para rodar via polling."""
    try:
        from telegram.ext import Application, CommandHandler, MessageHandler, filters
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "python-telegram-bot não está instalado. Instale a dependência antes "
            "de rodar o bot."
        ) from exc

    app = Application.builder().token(_env_obrigatoria("TELEGRAM_TOKEN", token)).build()
    logica = BotFinanceiro(
        agente=agente if agente is not None else agente_mod.criar_agente(),
        chat_id_autorizado=_env_obrigatoria(
            "TELEGRAM_CHAT_ID", chat_id_autorizado
        ),
        conversar_fn=conversar_fn,
    )

    app.add_handler(CommandHandler("start", logica.start))
    app.add_handler(CommandHandler("novo", logica.novo))
    app.add_handler(CommandHandler("ajuda", logica.ajuda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, logica.mensagem))
    app.add_error_handler(logica.erro_global)
    return app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    criar_aplicacao().run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
