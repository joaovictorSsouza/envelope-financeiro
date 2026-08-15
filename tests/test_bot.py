from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from src import bot as bot_mod


@dataclass
class MensagemDublada:
    text: str
    respostas: list[str] = field(default_factory=list)
    kwargs: list[dict[str, Any]] = field(default_factory=list)

    async def reply_text(self, texto: str, **kwargs: Any) -> None:
        self.respostas.append(texto)
        self.kwargs.append(kwargs)


@dataclass
class ChatDublado:
    id: int


@dataclass
class UpdateDublado:
    chat_id: int
    texto: str

    def __post_init__(self) -> None:
        self.effective_chat = ChatDublado(self.chat_id)
        self.message = MensagemDublada(self.texto)
        self.effective_message = self.message


@dataclass
class BotTelegramDublado:
    acoes: list[dict[str, Any]] = field(default_factory=list)

    async def send_chat_action(self, **kwargs: Any) -> None:
        self.acoes.append(kwargs)


@dataclass
class ContextoDublado:
    bot: BotTelegramDublado = field(default_factory=BotTelegramDublado)
    error: BaseException | None = None


def _rodar(coro: Any) -> Any:
    return asyncio.run(coro)


def test_mensagem_de_chat_nao_autorizado_nao_chama_o_agente() -> None:
    chamadas: list[Any] = []

    def conversar(*args: Any) -> tuple[str, list[dict[str, Any]]]:
        chamadas.append(args)
        return "nao deveria", []

    logica = bot_mod.BotFinanceiro(
        agente=object(), chat_id_autorizado="123", conversar_fn=conversar
    )
    update = UpdateDublado(chat_id=999, texto="quanto posso gastar?")

    _rodar(logica.mensagem(update, ContextoDublado()))

    assert chamadas == []
    assert update.message.respostas == [bot_mod.MENSAGEM_PRIVADO]


def test_chat_autorizado_chama_o_agente_com_thread_id_derivado_do_chat() -> None:
    chamadas: list[dict[str, Any]] = []

    def conversar(agente: Any, mensagem: str, thread_id: str) -> tuple[str, list[dict[str, Any]]]:
        chamadas.append({"agente": agente, "mensagem": mensagem, "thread_id": thread_id})
        return "voce tem R$ 100,00 livres", []

    agente = object()
    logica = bot_mod.BotFinanceiro(
        agente=agente, chat_id_autorizado="123", conversar_fn=conversar
    )
    update = UpdateDublado(chat_id=123, texto="quanto posso gastar?")
    contexto = ContextoDublado()

    _rodar(logica.mensagem(update, contexto))

    assert chamadas == [
        {
            "agente": agente,
            "mensagem": "quanto posso gastar?",
            "thread_id": "telegram:123:inicial",
        }
    ]
    assert contexto.bot.acoes == [{"chat_id": "123", "action": "typing"}]
    assert update.message.respostas == ["voce tem R$ 100,00 livres"]
    assert update.message.kwargs == [{}]


def test_novo_troca_o_thread_id() -> None:
    thread_ids: list[str] = []

    def conversar(agente: Any, mensagem: str, thread_id: str) -> tuple[str, list[dict[str, Any]]]:
        thread_ids.append(thread_id)
        return "ok", []

    logica = bot_mod.BotFinanceiro(
        agente=object(), chat_id_autorizado="123", conversar_fn=conversar
    )

    _rodar(logica.mensagem(UpdateDublado(123, "primeiro"), ContextoDublado()))
    novo = UpdateDublado(123, "/novo")
    _rodar(logica.novo(novo, ContextoDublado()))
    _rodar(logica.mensagem(UpdateDublado(123, "segundo"), ContextoDublado()))

    assert len(thread_ids) == 2
    assert thread_ids[0] == "telegram:123:inicial"
    assert thread_ids[1].startswith("telegram:123:")
    assert thread_ids[1] != thread_ids[0]
    assert novo.message.respostas == ["Histórico limpo. Pode começar um novo assunto."]


def test_resposta_acima_de_4096_caracteres_e_dividida() -> None:
    texto_longo = "x" * (bot_mod.LIMITE_TELEGRAM + 10)

    def conversar(*args: Any) -> tuple[str, list[dict[str, Any]]]:
        return texto_longo, []

    logica = bot_mod.BotFinanceiro(
        agente=object(), chat_id_autorizado="123", conversar_fn=conversar
    )
    update = UpdateDublado(123, "relatorio")

    _rodar(logica.mensagem(update, ContextoDublado()))

    assert [len(parte) for parte in update.message.respostas] == [
        bot_mod.LIMITE_TELEGRAM,
        10,
    ]
    assert "".join(update.message.respostas) == texto_longo


def test_excecao_do_agente_vira_mensagem_amigavel_e_nao_propaga() -> None:
    def conversar(*args: Any) -> tuple[str, list[dict[str, Any]]]:
        raise RuntimeError("503 planilha fora do ar")

    logica = bot_mod.BotFinanceiro(
        agente=object(), chat_id_autorizado="123", conversar_fn=conversar
    )
    update = UpdateDublado(123, "oi")

    _rodar(logica.mensagem(update, ContextoDublado()))

    assert update.message.respostas == [bot_mod.MENSAGEM_ERRO]


def test_erro_de_cota_tem_mensagem_propria() -> None:
    def conversar(*args: Any) -> tuple[str, list[dict[str, Any]]]:
        raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")

    logica = bot_mod.BotFinanceiro(
        agente=object(), chat_id_autorizado="123", conversar_fn=conversar
    )
    update = UpdateDublado(123, "oi")

    _rodar(logica.mensagem(update, ContextoDublado()))

    assert update.message.respostas == [bot_mod.MENSAGEM_COTA]
