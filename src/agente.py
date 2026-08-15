"""Montagem do agente: modelo + ferramentas + prompt + memória.

Esta camada não calcula nada e não conhece regra de negócio. Ela só junta
as peças que já existem — `tools.TOOLS` para os números, `financas.py` para
o calendário, `prompts/sistema.md` para o comportamento — e mantém o
histórico por `thread_id`.

Duas decisões que valem a explicação:

1. **O system prompt é relido do disco a cada turno.** O arquivo é pequeno e
   o ganho é concreto: dá para editar `prompts/sistema.md` com o REPL aberto
   e ver o efeito na mensagem seguinte, sem reiniciar nada.

2. **O bloco de contexto não entra no histórico.** Ele é montado dentro do
   middleware `dynamic_prompt`, que roda antes de cada ida ao modelo, então
   cada turno vê a data de hoje — e não a data em que a conversa começou. Se
   o bloco fosse concatenado à mensagem do usuário, ele viraria linha do
   checkpoint e envelheceria junto com ela.

Todo `date.today()` do módulo está em `_hoje`. Datas vindas de outro lugar
são bug: o resto do código recebe `hoje` como argumento, do mesmo jeito que
`financas.py` faz.
"""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path
from typing import Any, Final, Sequence

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from . import financas
from .tools import TOOLS

load_dotenv()

PROVEDOR_PADRAO: Final[str] = "google"
"""Sobrescrito por PROVEDOR no .env."""

MODELOS_PADRAO: Final[dict[str, str]] = {
    "google": "gemini-3.5-flash-lite",
    "anthropic": "claude-sonnet-5",
}
"""Modelo de cada provedor quando MODELO não está no .env.

O padrão do Google é `gemini-3.5-flash-lite` por causa da cota, e a diferença
não é de grau: no free tier a família `flash` tem **20 requisições por dia**
e a família `flash-lite` tem MEDIDO_RPD_LITE. Um turno de conversa gasta
1 requisição por ida ao modelo, e um turno com tool costuma gastar 2 ou 3;
com 20/dia o agente morre no meio da primeira conversa do dia. Os números
foram medidos contra a API, não lidos na documentação: o corpo do 429 traz
`limit: N` junto do `quotaId`. O README tem a tabela completa.

O que se perde é pouco: na bateria com as 17 tools o lite acertou tanto
quanto o flash, e responde em ~2,3s contra 9–17s do flash.

Duas armadilhas já pagas:

- `gemini-2.5-*` não serve: a API os recusa para chaves novas com 404 "no
  longer available to new users", mesmo aparecendo em `models.list()`.
- Os apelidos (`gemini-flash-lite-latest`) resolvem para um modelo concreto
  e dividem o balde de cota com ele — não rendem cota extra, e mudam de
  modelo sem aviso. Por isso aqui se fixa a versão.

`flash-lite` ignora `temperature` (usa sampling fixo) e avisa num UserWarning.
Como TEMPERATURA_PADRAO é 0.0, o que se perde é só a possibilidade de subir a
temperatura — quem quiser isso usa PROVEDOR=anthropic ou MODELO=gemini-3.5-flash.
"""

CHAVES: Final[dict[str, str]] = {
    "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

ONDE_OBTER: Final[dict[str, str]] = {
    "google": "https://aistudio.google.com/apikey (grátis, sem cartão)",
    "anthropic": "https://console.anthropic.com/settings/keys",
}

TEMPERATURA_PADRAO: Final[float] = 0.0
"""Um agente que não pode calcular nem inventar não tem uso para criatividade.
Sobrescrito por TEMPERATURA no .env, para quem quiser experimentar."""

CAMINHO_PROMPT: Final[Path] = Path(__file__).parent / "prompts" / "sistema.md"

THREAD_PADRAO: Final[str] = "terminal"
"""Thread usada pelo REPL. O Telegram usará o id do chat."""

_DIAS: Final[tuple[str, ...]] = (
    "segunda-feira",
    "terça-feira",
    "quarta-feira",
    "quinta-feira",
    "sexta-feira",
    "sábado",
    "domingo",
)

_MESES: Final[tuple[str, ...]] = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)


def _hoje(hoje: date | None = None) -> date:
    """O único ponto do módulo que olha o relógio."""
    return hoje or date.today()


# --------------------------------------------------------------------------
# system prompt
# --------------------------------------------------------------------------
def carregar_system_prompt(caminho: Path | None = None) -> str:
    """Lê o prompt do arquivo .md. Prompt vazio é erro, não é prompt neutro."""
    arquivo = caminho or CAMINHO_PROMPT
    try:
        texto = arquivo.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"System prompt não encontrado em {arquivo}. "
            "O agente não roda sem ele."
        ) from exc
    if not texto:
        raise ValueError(f"System prompt vazio em {arquivo}.")
    return texto


# --------------------------------------------------------------------------
# contexto do turno
# --------------------------------------------------------------------------
def _por_extenso(mes_ref: str) -> str:
    """'2026-09' -> 'setembro de 2026', para o modelo não ter que traduzir."""
    ano, mes = mes_ref.split("-")
    return f"{_MESES[int(mes) - 1]} de {ano}"


def bloco_de_contexto(hoje: date | None = None) -> str:
    """Data e meses de referência do turno, prontos para o modelo.

    Os três valores derivados vêm de `financas.py` — é lá que mora a regra
    de que se acompanha o mês corrente e se planeja o seguinte.
    """
    referencia = _hoje(hoje)
    acompanhamento = financas.mes_de_acompanhamento(referencia)
    planejamento = financas.mes_de_planejamento(referencia)

    return "\n".join(
        [
            "# CONTEXTO ATUAL",
            "",
            f"- Hoje: {referencia.isoformat()} "
            f"({_DIAS[referencia.weekday()]}, {referencia.day} de "
            f"{_por_extenso(acompanhamento)})",
            f"- Mês de acompanhamento (o corrente): {acompanhamento} "
            f"— {_por_extenso(acompanhamento)}",
            f"- Mês de planejamento (o seguinte): {planejamento} "
            f"— {_por_extenso(planejamento)}",
            f"- Fase do mês: {financas.fase_do_mes(referencia)}",
            "",
            "Estes são os valores válidos agora. Não deduza data de outro "
            "lugar da conversa.",
        ]
    )


def texto_do_sistema(hoje: date | None = None) -> str:
    """O system prompt do turno: o arquivo .md mais o bloco de contexto.

    Relido a cada ida ao modelo, de propósito — ver o cabeçalho do módulo.
    """
    return f"{carregar_system_prompt()}\n\n---\n\n{bloco_de_contexto(hoje)}"


def montar_prompt(hoje: date | None = None) -> Any:
    """Devolve o middleware que o `create_agent` chama antes de cada ida ao modelo.

    `hoje` fixo serve aos testes; em produção fica None e a data é lida na
    hora, para uma conversa aberta durante a virada do mês não continuar
    respondendo sobre o mês anterior.

    Até o `create_react_agent`, isto era um callable `state -> [SystemMessage,
    *mensagens]`. O `create_agent` não recebe mais um callable de prompt: quem
    varia o system prompt por turno é o middleware `dynamic_prompt`, que
    devolve só o texto do sistema e deixa o agente prepará-lo às mensagens do
    estado. O resultado que chega ao modelo é o mesmo.
    """

    @dynamic_prompt
    def prompt(request: ModelRequest) -> str:
        return texto_do_sistema(hoje)

    return prompt


# --------------------------------------------------------------------------
# modelo e agente
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# retry: só para o que é transitório
# --------------------------------------------------------------------------
ESPERAS_RETRY: Final[tuple[float, ...]] = (2.0, 5.0)
"""Duas re-tentativas, esperando 2s e depois 5s. Falhou as duas, o turno morre
e o REPL segue — melhor perder um turno que insistir num erro de verdade."""

_CODIGOS_TRANSITORIOS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

_MARCAS_TRANSITORIAS: Final[tuple[str, ...]] = (
    "unavailable",
    "resource_exhausted",
    "overloaded",
    "rate limit",
    "too many requests",
    "high demand",
    "timeout",
    "timed out",
    "deadline",
)


def _e_transitorio(exc: BaseException) -> bool:
    """Vale a pena tentar de novo?

    Sim para sobrecarga (503/429), erro de servidor e timeout. Não para
    schema recusado, chave inválida ou erro de tool — repetir isso só
    demora mais para dar o mesmo erro.
    """
    nome = type(exc).__name__.lower()
    if "timeout" in nome or "deadline" in nome:
        return True

    for atributo in ("status_code", "code", "http_status"):
        valor = getattr(exc, atributo, None)
        if isinstance(valor, int) and valor in _CODIGOS_TRANSITORIOS:
            return True

    texto = str(exc).lower()
    return any(marca in texto for marca in _MARCAS_TRANSITORIAS)


class ModeloComRetry(BaseChatModel):
    """Envolve um chat model repetindo só as falhas transitórias.

    `Runnable.with_retry` não serve aqui: ele devolve um RunnableRetry, que
    não tem `bind_tools`, e o `create_agent` precisa ligar as tools.
    Este wrapper é um BaseChatModel de verdade, e o `bind_tools` devolve
    outro wrapper — assim o retry sobrevive à ligação das ferramentas.

    O retry fica na chamada ao MODELO, de propósito. Repetir o agente
    inteiro re-executaria as tools já executadas no turno, e entre elas
    pode haver escrita.
    """

    interno: Any
    esperas: tuple[float, ...] = ESPERAS_RETRY

    @property
    def _llm_type(self) -> str:
        return f"retry[{getattr(self.interno, '_llm_type', 'modelo')}]"

    @property
    def temperature(self) -> Any:
        return getattr(self.interno, "temperature", None)

    @property
    def model(self) -> Any:
        return getattr(self.interno, "model", None)

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ModeloComRetry":
        return ModeloComRetry(
            interno=self.interno.bind_tools(tools, **kwargs), esperas=self.esperas
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        from langchain_core.outputs import ChatGeneration, ChatResult

        ultima: BaseException | None = None
        for tentativa in range(len(self.esperas) + 1):
            try:
                resposta = self.interno.invoke(messages, stop=stop, **kwargs)
                return ChatResult(generations=[ChatGeneration(message=resposta)])
            except BaseException as exc:  # noqa: BLE001 — reerguida abaixo
                if not _e_transitorio(exc) or tentativa >= len(self.esperas):
                    raise
                ultima = exc
                time.sleep(self.esperas[tentativa])

        raise ultima  # type: ignore[misc]  # inalcançável: o loop sempre sai


def _temperatura(temperatura: float | None = None) -> float:
    """Zero por padrão. TEMPERATURA no .env sobrescreve, se for número."""
    if temperatura is not None:
        return float(temperatura)
    bruto = os.getenv("TEMPERATURA")
    if bruto is None or not bruto.strip():
        return TEMPERATURA_PADRAO
    try:
        return float(bruto)
    except ValueError as exc:
        raise ValueError(
            f"TEMPERATURA no .env não é um número: {bruto!r}. "
            f"Use algo como 0 ou 0.3, ou remova a linha para valer "
            f"{TEMPERATURA_PADRAO}."
        ) from exc


def _chave(provedor: str) -> str:
    """A chave do provedor escolhido, ou um erro dizendo qual falta e onde pegar."""
    variavel = CHAVES[provedor]
    chave = os.getenv(variavel)
    if not chave:
        raise RuntimeError(
            f"{variavel} não está no .env, e o provedor configurado é "
            f"'{provedor}'. Pegue a chave em {ONDE_OBTER[provedor]} e "
            f"adicione a linha {variavel}=... ao .env (veja .env.example)."
        )
    return chave


def construir_modelo(
    provedor: str | None = None,
    modelo: str | None = None,
    temperatura: float | None = None,
    com_retry: bool = True,
) -> BaseChatModel:
    """Instancia o chat model do provedor configurado no .env.

    PROVEDOR escolhe entre "google" (padrão) e "anthropic"; MODELO
    sobrescreve o modelo padrão daquele provedor; TEMPERATURA sobrescreve o
    zero. A chave sai de GOOGLE_API_KEY ou ANTHROPIC_API_KEY conforme o caso.

    `com_retry=False` devolve o modelo cru, sem o wrapper — serve a quem
    quiser inspecionar a instância do provedor.

    Os imports são locais de propósito: quem usa só um provedor não precisa
    ter o SDK do outro instalado, e os testes com modelo dublado não
    precisam de nenhum dos dois.
    """
    escolhido = (provedor or os.getenv("PROVEDOR") or PROVEDOR_PADRAO).strip().lower()
    if escolhido not in MODELOS_PADRAO:
        raise ValueError(
            f"PROVEDOR desconhecido: {escolhido!r}. "
            f"Use um destes: {', '.join(sorted(MODELOS_PADRAO))}."
        )

    nome = modelo or os.getenv("MODELO") or MODELOS_PADRAO[escolhido]
    chave = _chave(escolhido)
    calor = _temperatura(temperatura)

    if escolhido == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        base: BaseChatModel = ChatGoogleGenerativeAI(
            model=nome, google_api_key=chave, temperature=calor
        )
    else:
        from langchain_anthropic import ChatAnthropic

        base = ChatAnthropic(
            model=nome, api_key=chave, temperature=calor, max_tokens=2048
        )

    if not com_retry:
        return base
    return ModeloComRetry(interno=base)


def descrever_modelo() -> str:
    """'google / gemini-2.5-flash' — para o REPL dizer com quem está falando."""
    provedor = (os.getenv("PROVEDOR") or PROVEDOR_PADRAO).strip().lower()
    nome = os.getenv("MODELO") or MODELOS_PADRAO.get(provedor, "?")
    return f"{provedor} / {nome}"


def criar_agente(
    modelo: BaseChatModel | str | None = None,
    tools: Sequence[Any] | None = None,
    checkpointer: Any | None = None,
    hoje: date | None = None,
) -> Any:
    """Monta o agente ReAct com memória por thread.

    `modelo` aceita um BaseChatModel já pronto (é assim que o teste injeta o
    dublê), o nome de um modelo, ou None — e aí vale o provedor do .env. O
    `checkpointer` padrão é um MemorySaver novo: memória de processo, some
    ao fechar.
    """
    if modelo is None or isinstance(modelo, str):
        modelo = construir_modelo(modelo=modelo)

    return create_agent(
        modelo,
        tools=list(TOOLS if tools is None else tools),
        middleware=[montar_prompt(hoje)],
        checkpointer=checkpointer if checkpointer is not None else MemorySaver(),
    )


# --------------------------------------------------------------------------
# conversa
# --------------------------------------------------------------------------
def enviar(
    agente: Any, mensagem: str, thread_id: str = THREAD_PADRAO
) -> dict[str, Any]:
    """Manda uma mensagem e devolve o estado bruto, com o histórico da thread."""
    return agente.invoke(
        {"messages": [HumanMessage(content=mensagem)]},
        config={"configurable": {"thread_id": thread_id}},
    )


def _texto(conteudo: Any) -> str:
    """Achata o content do Anthropic, que vem como lista de blocos."""
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        partes = [
            bloco.get("text", "")
            for bloco in conteudo
            if isinstance(bloco, dict) and bloco.get("type") == "text"
        ]
        return "\n".join(parte for parte in partes if parte).strip()
    return str(conteudo)


def texto_da_resposta(resultado: dict[str, Any]) -> str:
    """A fala final do agente: a última AIMessage sem tool calls."""
    for mensagem in reversed(resultado.get("messages", [])):
        if isinstance(mensagem, AIMessage) and not mensagem.tool_calls:
            return _texto(mensagem.content)
    return ""


def chamadas_de_tool(resultado: dict[str, Any]) -> list[dict[str, Any]]:
    """Tools chamadas NO ÚLTIMO turno, na ordem, com os argumentos.

    Corta no último HumanMessage: o histórico da thread inteira volta em
    `resultado`, e o que interessa ao `--debug` é o turno que acabou de
    acontecer.
    """
    mensagens = resultado.get("messages", [])
    inicio = 0
    for indice, mensagem in enumerate(mensagens):
        if isinstance(mensagem, HumanMessage):
            inicio = indice

    chamadas: list[dict[str, Any]] = []
    for mensagem in mensagens[inicio:]:
        if isinstance(mensagem, AIMessage):
            chamadas.extend(mensagem.tool_calls or [])
    return chamadas


def conversar(
    agente: Any, mensagem: str, thread_id: str = THREAD_PADRAO
) -> tuple[str, list[dict[str, Any]]]:
    """Um turno completo: devolve a resposta e as tools que ela usou."""
    resultado = enviar(agente, mensagem, thread_id)
    return texto_da_resposta(resultado), chamadas_de_tool(resultado)
