"""Testes do agente, com o modelo e as tools dublados. Nenhuma rede.

O modelo é um dublê que devolve uma fila de respostas programadas, e as
tools são funções que registram a chamada em vez de ler a planilha. O que
está sob teste aqui é o **encanamento**: se a tool escolhida roda de
verdade, se o retorno volta ao modelo, se o bloco de contexto chega correto
e se o histórico fica na thread certa.

O que estes testes **não** cobrem, e nenhum teste offline cobre: a escolha
do modelo. Com o modelo dublado, quem decide chamar `ver_acompanhamento` é
a fila programada, não o raciocínio — isso é trabalho do system prompt e só
se verifica conversando de verdade.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from pydantic import Field

from src import agente as agente_mod

HOJE = date(2026, 8, 13)
"""Quinta-feira, meio do mês. Acompanha 2026-08, planeja 2026-09."""


# --------------------------------------------------------------------------
# dublê do modelo
# --------------------------------------------------------------------------
class ModeloDublado(BaseChatModel):
    """Devolve respostas programadas, em ordem, e guarda o que recebeu.

    `vistas` existe para os testes de contexto e de histórico: é a lista de
    mensagens que o modelo enxergou em cada ida ao LLM, incluindo o
    SystemMessage montado pelo `prompt` callable.
    """

    respostas: list[AIMessage] = Field(default_factory=list)
    vistas: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "dublado"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ModeloDublado":
        # As tools não mudam o dublê: a resposta já vem programada.
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.vistas.append(list(messages))
        if self.respostas:
            resposta = self.respostas.pop(0)
        else:
            resposta = AIMessage(content="(fila vazia)")
        return ChatResult(generations=[ChatGeneration(message=resposta)])


def _chamada(nome: str, args: dict[str, Any], id: str = "call-1") -> AIMessage:
    """AIMessage pedindo uma tool, no formato que o ToolNode espera."""
    return AIMessage(
        content="",
        tool_calls=[{"name": nome, "args": args, "id": id, "type": "tool_call"}],
    )


# --------------------------------------------------------------------------
# dublês das tools
# --------------------------------------------------------------------------
@pytest.fixture
def registro() -> dict[str, list[dict[str, Any]]]:
    """Toda chamada de tool cai aqui: `ordem` guarda a sequência dos nomes."""
    return {"ordem": [], "gravadas": []}


@pytest.fixture
def tools_dubladas(registro: dict[str, list[Any]]) -> list[Any]:
    """As tools que o agente recebe. Nenhuma toca em `sheets`."""

    @tool
    def ver_acompanhamento(mes_ref: str | None = None) -> dict[str, Any]:
        """Como o mês em curso está indo contra o envelope."""
        registro["ordem"].append("ver_acompanhamento")
        return {"mes_ref": mes_ref or "2026-08", "livre_por_semana": 412.53}

    @tool
    def ver_orcamento(mes_ref: str | None = None) -> dict[str, Any]:
        """O plano do mês seguinte."""
        registro["ordem"].append("ver_orcamento")
        return {"mes_ref": mes_ref or "2026-09", "envelope_variavel": 1237.60}

    @tool
    def simular_parcelamento(
        descricao: str, valor_parcela: float, n_parcelas: int, mes_inicial: str
    ) -> dict[str, Any]:
        """Prévia do parcelamento. Nunca grava."""
        registro["ordem"].append("simular_parcelamento")
        return {"gravado": False, "dry_run": True, "n_parcelas": n_parcelas}

    @tool
    def confirmar_parcelamento(
        descricao: str, valor_parcela: float, n_parcelas: int, mes_inicial: str
    ) -> dict[str, Any]:
        """Grava o parcelamento nos meses futuros."""
        registro["ordem"].append("confirmar_parcelamento")
        registro["gravadas"].append(
            {"descricao": descricao, "n_parcelas": n_parcelas}
        )
        return {"gravado": True, "n_parcelas": n_parcelas}

    return [
        ver_acompanhamento,
        ver_orcamento,
        simular_parcelamento,
        confirmar_parcelamento,
    ]


def _agente(
    modelo: ModeloDublado, tools: list[Any], checkpointer: Any | None = None
) -> Any:
    return agente_mod.criar_agente(
        modelo=modelo,
        tools=tools,
        checkpointer=checkpointer or MemorySaver(),
        hoje=HOJE,
    )


# --------------------------------------------------------------------------
# system prompt
# --------------------------------------------------------------------------
def test_system_prompt_carrega_do_arquivo_e_nao_esta_vazio() -> None:
    prompt = agente_mod.carregar_system_prompt()
    assert prompt.strip()
    assert len(prompt) > 500  # não é um placeholder esquecido
    assert agente_mod.CAMINHO_PROMPT.suffix == ".md"


def test_system_prompt_traz_as_regras_que_o_agente_depende() -> None:
    """Regras que o resto do sistema assume; se sumirem, é regressão."""
    prompt = agente_mod.carregar_system_prompt().lower()
    assert "nunca calcula" in prompt or "nunca calcule" in prompt
    assert "confirmar_parcelamento" in prompt
    assert "simular_parcelamento" in prompt
    assert "ambiguo" in prompt or "ambiguidade" in prompt


def test_prompt_vazio_e_erro_e_nao_prompt_neutro(tmp_path: Any) -> None:
    vazio = tmp_path / "sistema.md"
    vazio.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError):
        agente_mod.carregar_system_prompt(vazio)


def test_prompt_ausente_falha_com_mensagem_util(tmp_path: Any) -> None:
    with pytest.raises(FileNotFoundError, match="não roda sem ele"):
        agente_mod.carregar_system_prompt(tmp_path / "nao-existe.md")


# --------------------------------------------------------------------------
# bloco de contexto
# --------------------------------------------------------------------------
def test_bloco_de_contexto_traz_a_data_e_os_dois_meses() -> None:
    bloco = agente_mod.bloco_de_contexto(HOJE)
    assert "2026-08-13" in bloco
    assert "2026-08" in bloco  # acompanhamento: o mês corrente
    assert "2026-09" in bloco  # planejamento: o seguinte
    assert "agosto" in bloco and "setembro" in bloco
    assert "meio" in bloco  # fase do mês, dia 13 de um mês de 31 dias


def test_bloco_de_contexto_vira_do_ano_junto_com_financas() -> None:
    """Dezembro planeja janeiro do ano seguinte — quem sabe disso é financas."""
    bloco = agente_mod.bloco_de_contexto(date(2026, 12, 28))
    assert "2027-01" in bloco
    assert "janeiro de 2027" in bloco
    assert "fim" in bloco


def test_contexto_chega_ao_modelo_no_system_message(
    tools_dubladas: list[Any],
) -> None:
    modelo = ModeloDublado(respostas=[AIMessage(content="ok")])
    agente_mod.conversar(_agente(modelo, tools_dubladas), "oi")

    sistema = modelo.vistas[0][0]
    assert isinstance(sistema, SystemMessage)
    assert "2026-08-13" in sistema.content
    assert "2026-08" in sistema.content and "2026-09" in sistema.content
    # O system prompt inteiro vai junto, não só o contexto.
    assert "assistente financeiro pessoal" in sistema.content.lower()


def test_contexto_e_recalculado_a_cada_turno(tools_dubladas: list[Any]) -> None:
    """O bloco não fica preso no histórico: ele é remontado antes de cada ida."""
    modelo = ModeloDublado(
        respostas=[AIMessage(content="um"), AIMessage(content="dois")]
    )
    agente = _agente(modelo, tools_dubladas)
    agente_mod.conversar(agente, "primeira")
    agente_mod.conversar(agente, "segunda")

    assert len(modelo.vistas) == 2
    for vista in modelo.vistas:
        assert isinstance(vista[0], SystemMessage)
        assert "2026-08-13" in vista[0].content
    # E não sobrou bloco de contexto solto entre as mensagens do histórico.
    humanas = [m for m in modelo.vistas[1] if isinstance(m, HumanMessage)]
    assert [m.content for m in humanas] == ["primeira", "segunda"]


# --------------------------------------------------------------------------
# escolha e execução de tool
# --------------------------------------------------------------------------
def test_tool_escolhida_roda_de_verdade_e_o_retorno_volta(
    tools_dubladas: list[Any], registro: dict[str, list[Any]]
) -> None:
    """"quanto posso gastar essa semana" → `ver_acompanhamento`.

    Com o modelo dublado, a escolha vem programada; o que se verifica aqui é
    que a tool foi executada e que o agente respondeu depois do retorno dela.
    """
    modelo = ModeloDublado(
        respostas=[
            _chamada("ver_acompanhamento", {}),
            AIMessage(content="Você tem R$ 412,53 para essa semana."),
        ]
    )
    resposta, chamadas = agente_mod.conversar(
        _agente(modelo, tools_dubladas), "quanto posso gastar essa semana?"
    )

    assert registro["ordem"] == ["ver_acompanhamento"]
    assert [chamada["name"] for chamada in chamadas] == ["ver_acompanhamento"]
    assert resposta == "Você tem R$ 412,53 para essa semana."
    # O modelo foi chamado duas vezes: pedir a tool e falar com o retorno dela.
    assert len(modelo.vistas) == 2


def test_chamadas_de_tool_sao_so_as_do_ultimo_turno(
    tools_dubladas: list[Any]
) -> None:
    """O `--debug` mostra o turno atual, não a conversa inteira."""
    modelo = ModeloDublado(
        respostas=[
            _chamada("ver_acompanhamento", {}),
            AIMessage(content="primeira resposta"),
            _chamada("ver_orcamento", {}),
            AIMessage(content="segunda resposta"),
        ]
    )
    agente = _agente(modelo, tools_dubladas)
    agente_mod.conversar(agente, "como estou?")
    _, chamadas = agente_mod.conversar(agente, "e mês que vem?")

    assert [chamada["name"] for chamada in chamadas] == ["ver_orcamento"]


# --------------------------------------------------------------------------
# alto risco: simular antes de confirmar
# --------------------------------------------------------------------------
def test_confirmar_parcelamento_nao_grava_no_turno_da_previa(
    tools_dubladas: list[Any], registro: dict[str, list[Any]]
) -> None:
    """Turno da prévia: simula, mostra e para. Nada foi gravado."""
    modelo = ModeloDublado(
        respostas=[
            _chamada(
                "simular_parcelamento",
                {
                    "descricao": "notebook",
                    "valor_parcela": 300.0,
                    "n_parcelas": 6,
                    "mes_inicial": "2026-08",
                },
            ),
            AIMessage(content="6x de R$ 300,00 começando em agosto. Confirma?"),
        ]
    )
    resposta, _ = agente_mod.conversar(
        _agente(modelo, tools_dubladas), "parcela o notebook em 6x de 300"
    )

    assert registro["ordem"] == ["simular_parcelamento"]
    assert registro["gravadas"] == []
    assert "confirma" in resposta.lower()


def test_confirmar_parcelamento_so_depois_da_confirmacao_do_usuario(
    tools_dubladas: list[Any], registro: dict[str, list[Any]]
) -> None:
    """O ciclo inteiro: simular num turno, confirmar no seguinte.

    A ordem registrada é a garantia: `confirmar_parcelamento` nunca aparece
    sem `simular_parcelamento` antes dele.
    """
    argumentos = {
        "descricao": "notebook",
        "valor_parcela": 300.0,
        "n_parcelas": 6,
        "mes_inicial": "2026-08",
    }
    modelo = ModeloDublado(
        respostas=[
            _chamada("simular_parcelamento", argumentos),
            AIMessage(content="6x de R$ 300,00. Confirma?"),
            _chamada("confirmar_parcelamento", argumentos),
            AIMessage(content="Gravado: 6x de R$ 300,00."),
        ]
    )
    agente = _agente(modelo, tools_dubladas)

    agente_mod.conversar(agente, "parcela o notebook em 6x de 300")
    assert registro["gravadas"] == []

    agente_mod.conversar(agente, "pode ser")

    assert registro["ordem"] == ["simular_parcelamento", "confirmar_parcelamento"]
    assert registro["ordem"].index("simular_parcelamento") < registro[
        "ordem"
    ].index("confirmar_parcelamento")
    assert registro["gravadas"] == [{"descricao": "notebook", "n_parcelas": 6}]


def test_confirmacao_grava_os_mesmos_argumentos_que_foram_simulados(
    tools_dubladas: list[Any], registro: dict[str, list[Any]]
) -> None:
    argumentos = {
        "descricao": "notebook",
        "valor_parcela": 300.0,
        "n_parcelas": 6,
        "mes_inicial": "2026-08",
    }
    modelo = ModeloDublado(
        respostas=[
            _chamada("simular_parcelamento", argumentos),
            AIMessage(content="Confirma?"),
            _chamada("confirmar_parcelamento", argumentos),
            AIMessage(content="Gravado."),
        ]
    )
    agente = _agente(modelo, tools_dubladas)
    _, previa = agente_mod.conversar(agente, "parcela em 6x")
    _, confirmacao = agente_mod.conversar(agente, "isso")

    assert previa[0]["args"] == confirmacao[0]["args"]


# --------------------------------------------------------------------------
# memória por thread
# --------------------------------------------------------------------------
def test_historico_persiste_entre_turnos_da_mesma_thread(
    tools_dubladas: list[Any]
) -> None:
    modelo = ModeloDublado(
        respostas=[AIMessage(content="oi"), AIMessage(content="era 80 reais")]
    )
    agente = _agente(modelo, tools_dubladas)
    agente_mod.conversar(agente, "gastei 80 no mercado", thread_id="a")
    agente_mod.conversar(agente, "quanto foi mesmo?", thread_id="a")

    segunda = modelo.vistas[1]
    conteudos = [m.content for m in segunda if isinstance(m, HumanMessage)]
    assert conteudos == ["gastei 80 no mercado", "quanto foi mesmo?"]
    assert any(isinstance(m, AIMessage) and m.content == "oi" for m in segunda)


def test_historico_nao_vaza_entre_threads(tools_dubladas: list[Any]) -> None:
    modelo = ModeloDublado(
        respostas=[AIMessage(content="oi"), AIMessage(content="não sei do que fala")]
    )
    agente = _agente(modelo, tools_dubladas)
    agente_mod.conversar(agente, "gastei 80 no mercado", thread_id="a")
    agente_mod.conversar(agente, "quanto foi mesmo?", thread_id="b")

    segunda = modelo.vistas[1]
    conteudos = [m.content for m in segunda if isinstance(m, HumanMessage)]
    assert conteudos == ["quanto foi mesmo?"]
    assert "gastei 80 no mercado" not in str(conteudos)


def test_agentes_distintos_nao_compartilham_memoria(
    tools_dubladas: list[Any]
) -> None:
    """MemorySaver novo a cada `criar_agente`: fechar o REPL perde o histórico."""
    primeiro = ModeloDublado(respostas=[AIMessage(content="oi")])
    segundo = ModeloDublado(respostas=[AIMessage(content="oi de novo")])

    agente_mod.conversar(_agente(primeiro, tools_dubladas), "primeira", thread_id="a")
    agente_mod.conversar(_agente(segundo, tools_dubladas), "segunda", thread_id="a")

    humanas = [m for m in segundo.vistas[0] if isinstance(m, HumanMessage)]
    assert [m.content for m in humanas] == ["segunda"]


# --------------------------------------------------------------------------
# extração da resposta
# --------------------------------------------------------------------------
def test_texto_da_resposta_achata_os_blocos_do_anthropic() -> None:
    """O content da Anthropic vem como lista de blocos, não como string."""
    resultado = {
        "messages": [
            HumanMessage(content="oi"),
            AIMessage(
                content=[
                    {"type": "text", "text": "Sobram R$ 412,53"},
                    {"type": "text", "text": "por semana."},
                ]
            ),
        ]
    }
    assert agente_mod.texto_da_resposta(resultado) == "Sobram R$ 412,53\npor semana."


def test_texto_da_resposta_ignora_a_mensagem_que_so_pede_tool() -> None:
    resultado = {
        "messages": [
            HumanMessage(content="oi"),
            _chamada("ver_acompanhamento", {}),
            AIMessage(content="resposta final"),
        ]
    }
    assert agente_mod.texto_da_resposta(resultado) == "resposta final"


# --------------------------------------------------------------------------
# provedor de modelo
# --------------------------------------------------------------------------
@pytest.fixture
def env_limpo(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Sem nada do .env do usuário: cada teste declara o que precisa."""
    for variavel in (
        "PROVEDOR",
        "MODELO",
        "TEMPERATURA",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(variavel, raising=False)
    return monkeypatch


def test_provedor_padrao_e_google(env_limpo: pytest.MonkeyPatch) -> None:
    """Sem PROVEDOR no .env, vale google — nenhuma rede é tocada aqui."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    env_limpo.setenv("GOOGLE_API_KEY", "chave-de-teste")
    modelo = agente_mod.construir_modelo(com_retry=False)

    assert isinstance(modelo, ChatGoogleGenerativeAI)
    assert modelo.model.endswith(agente_mod.MODELOS_PADRAO["google"])


def test_provedor_anthropic_devolve_chat_anthropic(
    env_limpo: pytest.MonkeyPatch,
) -> None:
    from langchain_anthropic import ChatAnthropic

    env_limpo.setenv("PROVEDOR", "anthropic")
    env_limpo.setenv("ANTHROPIC_API_KEY", "chave-de-teste")
    modelo = agente_mod.construir_modelo(com_retry=False)

    assert isinstance(modelo, ChatAnthropic)
    assert modelo.model == "claude-sonnet-5"


def test_modelo_do_env_sobrescreve_o_padrao_do_provedor(
    env_limpo: pytest.MonkeyPatch,
) -> None:
    env_limpo.setenv("GOOGLE_API_KEY", "chave-de-teste")
    env_limpo.setenv("MODELO", "gemini-2.5-pro")

    assert agente_mod.construir_modelo().model.endswith("gemini-2.5-pro")


def test_provedor_desconhecido_erra_listando_os_validos(
    env_limpo: pytest.MonkeyPatch,
) -> None:
    env_limpo.setenv("PROVEDOR", "openai")
    with pytest.raises(ValueError, match="PROVEDOR desconhecido") as exc:
        agente_mod.construir_modelo()
    assert "anthropic" in str(exc.value) and "google" in str(exc.value)


@pytest.mark.parametrize(
    ("provedor", "variavel", "onde"),
    [
        ("google", "GOOGLE_API_KEY", "aistudio.google.com"),
        ("anthropic", "ANTHROPIC_API_KEY", "console.anthropic.com"),
    ],
)
def test_chave_ausente_nomeia_a_variavel_e_diz_onde_obter(
    env_limpo: pytest.MonkeyPatch, provedor: str, variavel: str, onde: str
) -> None:
    """Erro na largada, com o nome da variável — não um 401 lá na frente."""
    env_limpo.setenv("PROVEDOR", provedor)
    with pytest.raises(RuntimeError) as exc:
        agente_mod.construir_modelo()

    mensagem = str(exc.value)
    assert variavel in mensagem
    assert onde in mensagem


def test_chave_do_outro_provedor_nao_serve(env_limpo: pytest.MonkeyPatch) -> None:
    """Ter a chave da Anthropic não faz o provedor google funcionar."""
    env_limpo.setenv("PROVEDOR", "google")
    env_limpo.setenv("ANTHROPIC_API_KEY", "chave-de-teste")
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        agente_mod.construir_modelo()


# --------------------------------------------------------------------------
# temperatura
# --------------------------------------------------------------------------
def test_temperatura_e_zero_nos_dois_provedores(
    env_limpo: pytest.MonkeyPatch,
) -> None:
    """Agente que não pode calcular nem inventar não tem uso para criatividade."""
    env_limpo.setenv("GOOGLE_API_KEY", "chave-de-teste")
    env_limpo.setenv("ANTHROPIC_API_KEY", "chave-de-teste")

    assert agente_mod.construir_modelo(provedor="google").temperature == 0
    assert agente_mod.construir_modelo(provedor="anthropic").temperature == 0


def test_temperatura_do_env_sobrescreve(env_limpo: pytest.MonkeyPatch) -> None:
    env_limpo.setenv("GOOGLE_API_KEY", "chave-de-teste")
    env_limpo.setenv("TEMPERATURA", "0.4")

    assert agente_mod.construir_modelo().temperature == pytest.approx(0.4)


def test_temperatura_invalida_erra_com_mensagem_util(
    env_limpo: pytest.MonkeyPatch,
) -> None:
    env_limpo.setenv("GOOGLE_API_KEY", "chave-de-teste")
    env_limpo.setenv("TEMPERATURA", "morna")

    with pytest.raises(ValueError, match="TEMPERATURA"):
        agente_mod.construir_modelo()


# --------------------------------------------------------------------------
# retry das falhas transitórias
# --------------------------------------------------------------------------
class Erro503(Exception):
    """Como o ServerError do google-genai: código no texto e em `code`."""

    def __init__(self) -> None:
        super().__init__(
            "503 UNAVAILABLE. This model is currently experiencing high demand."
        )
        self.code = 503


class ErroDeSchema(Exception):
    """400: argumento inválido. Repetir isso só demora para dar o mesmo erro."""

    def __init__(self) -> None:
        super().__init__("400 INVALID_ARGUMENT: unknown field 'foo'")
        self.code = 400


class ModeloInstavel(BaseChatModel):
    """Falha `falhas` vezes e só então responde."""

    falhas: int = 0
    excecao: Any = Erro503
    chamadas: list[int] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "instavel"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ModeloInstavel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.chamadas.append(1)
        if len(self.chamadas) <= self.falhas:
            raise self.excecao()
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="respondi"))]
        )


@pytest.fixture
def sem_espera(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Registra os backoffs sem realmente dormir."""
    dormidas: list[float] = []
    monkeypatch.setattr(agente_mod.time, "sleep", dormidas.append)
    return dormidas


def test_retry_supera_um_503_e_responde(
    sem_espera: list[float], tools_dubladas: list[Any]
) -> None:
    instavel = ModeloInstavel(falhas=1)
    resposta, _ = agente_mod.conversar(
        _agente(agente_mod.ModeloComRetry(interno=instavel), tools_dubladas), "oi"
    )

    assert resposta == "respondi"
    assert len(instavel.chamadas) == 2
    assert sem_espera == [2.0]


def test_retry_usa_os_backoffs_de_2s_e_5s(
    sem_espera: list[float], tools_dubladas: list[Any]
) -> None:
    instavel = ModeloInstavel(falhas=2)
    resposta, _ = agente_mod.conversar(
        _agente(agente_mod.ModeloComRetry(interno=instavel), tools_dubladas), "oi"
    )

    assert resposta == "respondi"
    assert len(instavel.chamadas) == 3
    assert sem_espera == [2.0, 5.0]


def test_retry_desiste_depois_das_duas_tentativas(
    sem_espera: list[float], tools_dubladas: list[Any]
) -> None:
    """Falhou as duas? O erro sobe — e o REPL o imprime e segue."""
    instavel = ModeloInstavel(falhas=99)
    agente = _agente(agente_mod.ModeloComRetry(interno=instavel), tools_dubladas)

    with pytest.raises(Erro503):
        agente_mod.conversar(agente, "oi")

    assert len(instavel.chamadas) == 3  # a original + duas re-tentativas
    assert sem_espera == [2.0, 5.0]


def test_erro_de_schema_nao_tem_retry(
    sem_espera: list[float], tools_dubladas: list[Any]
) -> None:
    """400 não é sobrecarga: repetir dá o mesmo erro mais tarde."""
    instavel = ModeloInstavel(falhas=99, excecao=ErroDeSchema)
    agente = _agente(agente_mod.ModeloComRetry(interno=instavel), tools_dubladas)

    with pytest.raises(ErroDeSchema):
        agente_mod.conversar(agente, "oi")

    assert len(instavel.chamadas) == 1
    assert sem_espera == []


@pytest.mark.parametrize(
    ("excecao", "transitorio"),
    [
        (Erro503(), True),
        (ErroDeSchema(), False),
        (TimeoutError("deu tempo"), True),
        (RuntimeError("429 RESOURCE_EXHAUSTED"), True),
        (RuntimeError("529 overloaded_error"), True),
        (RuntimeError("401 invalid x-api-key"), False),
        (ValueError("tool 'ver_orcamento' devolveu erro"), False),
    ],
)
def test_classificacao_do_que_merece_retry(
    excecao: BaseException, transitorio: bool
) -> None:
    assert agente_mod._e_transitorio(excecao) is transitorio


def test_retry_nao_reexecuta_tools_ja_executadas(
    sem_espera: list[float], registro: dict[str, list[Any]]
) -> None:
    """O retry está no modelo, não no agente.

    Se estivesse no agente, repetir o turno re-executaria as tools do turno
    — e entre elas pode haver escrita. Aqui a tool roda uma vez só, mesmo
    com o modelo falhando depois dela.
    """

    @tool
    def ver_acompanhamento(mes_ref: str | None = None) -> dict[str, Any]:
        """Como o mês está indo."""
        registro["ordem"].append("ver_acompanhamento")
        return {"livre_por_semana": 412.53}

    class FalhaDepoisDaTool(BaseChatModel):
        chamadas: list[int] = Field(default_factory=list)

        @property
        def _llm_type(self) -> str:
            return "falha-depois"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            self.chamadas.append(1)
            if len(self.chamadas) == 1:  # pede a tool
                return ChatResult(
                    generations=[
                        ChatGeneration(message=_chamada("ver_acompanhamento", {}))
                    ]
                )
            if len(self.chamadas) == 2:  # falha DEPOIS da tool ter rodado
                raise Erro503()
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="pronto"))]
            )

    modelo = FalhaDepoisDaTool()
    resposta, _ = agente_mod.conversar(
        _agente(agente_mod.ModeloComRetry(interno=modelo), [ver_acompanhamento]), "oi"
    )

    assert resposta == "pronto"
    assert registro["ordem"] == ["ver_acompanhamento"]  # rodou UMA vez
    assert sem_espera == [2.0]


# --------------------------------------------------------------------------
# schema das tools no Gemini
# --------------------------------------------------------------------------
def test_todas_as_tools_convertem_para_o_schema_do_gemini() -> None:
    """O Gemini é mais rígido que o Claude com JSON Schema de ferramenta.

    `bind_tools` não prova nada: ele guarda as tools no formato OpenAI e só
    converte na hora do request. Quem converte de verdade é
    `convert_to_genai_function_declarations` — a mesma função que o
    `_prepare_request` do ChatGoogleGenerativeAI chama. É módulo privado de
    propósito: se um upgrade mudar o caminho, este teste quebra e avisa, em
    vez de a falha aparecer no meio de uma conversa.
    """
    from langchain_google_genai import _function_utils

    from src.tools import TOOLS

    convertidas = _function_utils.convert_to_genai_function_declarations(list(TOOLS))
    declaracoes = convertidas[0].function_declarations

    assert len(declaracoes) == len(TOOLS) == 18

    for declaracao in declaracoes:
        propriedades = (declaracao.parameters.properties or {}).items()
        for nome, schema in propriedades:
            # Campo sem tipo, ou com union não resolvido, é o que o Gemini recusa.
            assert schema.type is not None, f"{declaracao.name}.{nome} sem tipo"
            assert not getattr(schema, "any_of", None), (
                f"{declaracao.name}.{nome} virou anyOf"
            )


def test_opcionais_viram_nullable_e_ficam_fora_de_required() -> None:
    """`mes_ref: str | None = None` não pode virar argumento obrigatório."""
    from langchain_google_genai import _function_utils

    from src.tools import TOOLS

    convertidas = _function_utils.convert_to_genai_function_declarations(list(TOOLS))
    por_nome = {d.name: d for d in convertidas[0].function_declarations}

    acompanhamento = por_nome["ver_acompanhamento"].parameters
    assert acompanhamento.properties["mes_ref"].nullable
    assert not (acompanhamento.required or [])

    # E o que é obrigatório de verdade continua obrigatório.
    parcelamento = por_nome["confirmar_parcelamento"].parameters
    assert set(parcelamento.required or []) == {
        "descricao",
        "valor_parcela",
        "n_parcelas",
        "mes_inicial",
        "categoria",
    }
    assert parcelamento.properties["dia_do_mes"].nullable


def test_bind_das_tools_nos_dois_provedores(env_limpo: pytest.MonkeyPatch) -> None:
    """As 17 tools entram nos dois modelos sem erro de schema. Sem rede."""
    env_limpo.setenv("GOOGLE_API_KEY", "chave-de-teste")
    env_limpo.setenv("ANTHROPIC_API_KEY", "chave-de-teste")

    from src.tools import TOOLS

    for provedor in ("google", "anthropic"):
        modelo = agente_mod.construir_modelo(provedor=provedor, com_retry=False)
        ligado = modelo.bind_tools(list(TOOLS))
        assert len(ligado.kwargs["tools"]) == len(TOOLS)


def test_retry_sobrevive_ao_bind_das_tools(env_limpo: pytest.MonkeyPatch) -> None:
    """`with_retry` perderia o bind_tools; o wrapper tem que preservar os dois."""
    env_limpo.setenv("GOOGLE_API_KEY", "chave-de-teste")

    from src.tools import TOOLS

    modelo = agente_mod.construir_modelo()
    assert isinstance(modelo, agente_mod.ModeloComRetry)

    ligado = modelo.bind_tools(list(TOOLS))
    assert isinstance(ligado, agente_mod.ModeloComRetry)  # o retry continua lá
    assert len(ligado.interno.kwargs["tools"]) == len(TOOLS)
