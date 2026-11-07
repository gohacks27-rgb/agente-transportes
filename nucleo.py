"""Núcleo do agente (independente do canal): estado, análise, checagem com Jev, redação, aplicação e respostas.
Usado por agente_slack.py (e pode ser usado por outros canais)."""
import os, json, csv, datetime, importlib, hashlib
import pandas as pd
from config import AQUI, DADOS, SAIDA, MAX_APROVACOES, CUSTO_APROVACAO, META_CUMPRIMENTO
import ia
import sistemas

ESTADO = SAIDA / "estado_agente.json"
REGRAS_SIM = SAIDA / "regras_site_simulado.csv"

# ---------- memória do agente ----------
def ler_estado():
    if ESTADO.exists():
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    return {"gerente_chat_id": None, "propostas": {}, "aprovadas": 0}

def salvar_estado(e):
    ESTADO.write_text(json.dumps(e, ensure_ascii=False, indent=2), encoding="utf-8")

def registrar_acao(acao, detalhe):
    novo = not (SAIDA / "acoes.csv").exists()
    with open(SAIDA / "acoes.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["quando", "acao", "detalhe"])
        w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), acao, detalhe])

# ---------- ciclo de análise ----------
def rodar_etapas():
    for nome in ["1_unificar_portais", "2_medir", "3_recomendar"]:
        importlib.import_module(nome).main()
    return pd.read_csv(SAIDA / "propostas.csv", dtype=str)

def _num(v):
    try:
        f = float(v)
        return None if f != f else f  # NaN -> None
    except (TypeError, ValueError):
        return None

def brl(v):
    return f"{v:,.0f}".replace(",", ".")

def estado_para_jev(p):
    """Só os campos que importam, sem valores vazios (NaN deixa o JSON inválido)."""
    campos = ["tipo", "uf", "cidades", "prefixos", "transportadora_atual", "transportadora_nova", "prazo_atual", "prazo_novo",
              "pct_no_prazo_atual", "pct_no_prazo_novo", "prazo_concorrentes", "envios_medidos_atual", "pedidos_mes"]
    e = {}
    for c in campos:
        v = p.get(c)
        n = _num(v)
        if n is not None:
            e[c] = n
        elif isinstance(v, str) and v and v.lower() != "nan":
            e[c] = v
    e["meta_no_prazo"] = META_CUMPRIMENTO
    return e

def conferir_com_jev(p):
    """Jev classifica o TIPO de problema (é o que ele faz bem). A força da evidência é decidida pelo código
    (3_recomendar.py: >= 30 envios), não pela IA."""
    perguntas = {"caso": {"type": "choice", "instructions": "Qual é o problema principal desta região de entrega?",
                 "criteria": {"prazo_irreal": "o site promete menos dias do que a entrega real leva; % no prazo hoje abaixo da meta",
                              "folga_excessiva": "% no prazo hoje perto de 100% e o prazo novo é MENOR que o atual",
                              "transportadora_ruim": "a proposta troca de transportadora porque a atual entrega abaixo da meta"}}}
    try:
        r = ia.jev(estado_para_jev(p), perguntas)
        return r["caso"]["choice"], r["caso"].get("confidence", 0), None
    except Exception as erro:
        print("Jev indisponível:", erro)
        return "não classificado", 0, None

def avaliar_risco(p):
    """Nível de confiança e principal risco, calculados pelo código (não pela IA)."""
    riscos = []
    novo = float(p["pct_no_prazo_novo"])
    conc = _num(p.get("prazo_concorrentes"))
    troca = p["transportadora_nova"] != p["transportadora_atual"]
    n_nova = int(float(p.get("envios_medidos_nova") or 0))
    if novo < META_CUMPRIMENTO + 0.02:
        riscos.append(f"margem pequena sobre a meta ({novo:.0%} estimado vs {META_CUMPRIMENTO:.0%})")
    if conc is not None and float(p["prazo_novo"]) > conc:
        riscos.append(f"prazo novo acima da mediana dos concorrentes ({conc:g} dias)")
    if troca and n_nova < 50:
        riscos.append(f"transportadora nova medida em só {n_nova} envios")
    if int(float(p["prazo_novo"])) - int(float(p["prazo_atual"])) >= 5:
        riscos.append("aumento grande de prazo pode reduzir vendas além do estimado")
    nivel = "alta" if not riscos else ("média" if len(riscos) <= 2 else "baixa")
    return nivel, riscos

SISTEMA = ("Você é o agente planejador de transportes da Gocase. Escreva em português, direto e curto, para o gerente "
           "de logística aprovar ou recusar pelo celular. Use SOMENTE os números recebidos; não invente nada e não comente "
           "dados que não recebeu. Use formato brasileiro de número (R$ 88.667). Termine com a recomendação e o nível de "
           "confiança recebidos, citando o principal risco quando houver. Não diga 'recomendo aprovar' se a confiança for baixa. "
           "Siga a evidência mínima de politicas.csv: prefixos, transportadora, período, envios, taxa de cumprimento "
           "antes e depois, impacto estimado em R$. Explique siglas na primeira vez. Máximo 9 linhas, sem markdown.")

def redigir(p, caso, conf, evid):
    conc = _num(p.get("prazo_concorrentes"))
    texto = (f"Proposta {p['proposta_id']} ({p['tipo']}). UF {p['uf']}, prefixos {p['prefixos']} ({p['cidades']}). "
             f"Transportadora atual {p['transportadora_atual']} -> nova {p['transportadora_nova']}. "
             f"Prazo exibido {p['prazo_atual']} -> {p['prazo_novo']} dias úteis. "
             f"% no prazo hoje {float(p['pct_no_prazo_atual']):.0%} -> estimado {float(p['pct_no_prazo_novo']):.0%} (meta {META_CUMPRIMENTO:.0%}). "
             f"Envios medidos (amostra de 1 em 6): {int(float(p.get('envios_medidos_atual') or 0))} com a transportadora atual"
             + (f", {int(float(p.get('envios_medidos_nova') or 0))} com a nova na mesma região" if p['transportadora_nova'] != p['transportadora_atual'] else "")
             + f" ({p.get('base_da_medida', 'prefixo')}). "
             + (f"Prazo mediano dos concorrentes: {conc:g} dias úteis. " if conc is not None else "")
             + f"Pedidos/mês: {brl(float(p['pedidos_mes']))}. Ganho estimado: R$ {brl(float(p['ganho_estimado_mes_rs']))}/mês. "
             f"Período medido: pedidos de 03/09 a 23/10/2026."
             + (f" Classificação automática do caso (Jev): {caso} (confiança {conf:.0%})." if conf else ""))
    nivel, riscos = avaliar_risco(p)
    rec = {"alta": "recomendo aprovar", "média": "recomendo aprovar com acompanhamento",
           "baixa": "recomendo avaliar com cuidado antes de aprovar"}[nivel]
    texto += f" Confiança da proposta: {nivel}. Recomendação: {rec}. Riscos: {'; '.join(riscos) or 'nenhum relevante'}."
    try:
        msg = ia.chat([{"role": "system", "content": SISTEMA}, {"role": "user", "content": texto}]).content
    except Exception as erro:
        print("Chat indisponível, mando o texto técnico:", erro)
        msg = texto
    if nivel == "baixa":
        msg = "⚠️ " + msg
    return msg

# ---------- ação aprovada: aplica na regra do site (simulada) ----------
def aplicar(p):
    """Aplica a proposta aprovada pelo gerente no sistema do site (camada de sistemas: sistemas.py)."""
    return sistemas.atualizar_regra_site(p["prefixos"].split("; "), p["prazo_novo"], p["transportadora_nova"])

# ---------- perguntas livres do time ----------
FERRAMENTAS = [
    {"type": "function", "function": {"name": "consultar_desempenho",
     "description": "Desempenho real por transportadora e prefixo de CEP (saida/desempenho.csv): envios, % no prazo, p50/p92 do dia de entrega, SLA do contrato.",
     "parameters": {"type": "object", "properties": {"transportadora": {"type": "string"}, "uf": {"type": "string"},
                    "prefixo3": {"type": "string"}, "regiao": {"type": "string", "description": "SE, S, CO, NE ou N"}}}}},
    {"type": "function", "function": {"name": "listar_propostas",
     "description": "Propostas enviadas ao gerente e a situação de cada uma.", "parameters": {"type": "object", "properties": {}}}},
]

def consultar_desempenho(transportadora=None, uf=None, prefixo3=None, regiao=None):
    d = pd.read_csv(SAIDA / "desempenho.csv", dtype={"prefixo3": str})
    if transportadora: d = d[d.transportadora.str.lower().str.contains(transportadora.lower())]
    if uf: d = d[d.uf.str.upper() == uf.upper()]
    if prefixo3: d = d[d.prefixo3 == str(prefixo3).zfill(3)]
    if regiao: d = d[d.regiao.str.upper() == regiao.upper()]
    if d.empty:
        return "Nenhum dado para esse filtro."
    tot = {"envios": int(d.envios.sum()), "pct_no_prazo_ponderado": round((d.pct_no_prazo * d.envios).sum() / d.envios.sum(), 3)}
    cols = ["transportadora", "prefixo3", "uf", "envios", "pct_no_prazo", "p50", "p92", "sla_contratual_dias_uteis"]
    return json.dumps({"total": tot, "linhas_com_mais_envios": d.nlargest(15, "envios")[cols].to_dict("records")}, ensure_ascii=False)

def listar_propostas():
    est = ler_estado()
    return json.dumps([{"proposta": k.split("-")[0], "prefixos": v["dados"]["prefixos"], "status": v["status"]}
                       for k, v in est["propostas"].items()], ensure_ascii=False) or "Nenhuma proposta ainda."

def responder(pergunta):
    msgs = [{"role": "system", "content": "Você é o agente de transportes da Gocase e responde o time no Slack, em português. "
             "Formato obrigatório (a pessoa precisa entender em menos de 20 segundos): "
             "1ª linha = a resposta direta com o número principal e um ícone de situação (🔴 abaixo da meta de 92% no prazo, "
             "🟡 perto da meta, ✅ acima). Depois no máximo 3 linhas curtas com números e comparações (antes → depois, "
             "melhor/pior), começando com ação quando houver algo a fazer. Sem parágrafos, sem títulos, sem explicações longas. "
             "Negrito do Slack é *texto* (UM asterisco). Última linha: _Fonte: saida/desempenho.csv_. "
             "Use as ferramentas para obter números. Nunca invente números. Você não aprova mudanças: só o gerente aprova."},
            {"role": "user", "content": pergunta}]
    for _ in range(4):
        m = ia.chat(msgs, ferramentas=FERRAMENTAS)
        if not m.tool_calls:
            return (m.content or "").replace("**", "*")
        msgs.append({"role": "assistant", "content": m.content, "tool_calls": [t.model_dump() for t in m.tool_calls]})
        for t in m.tool_calls:
            args = json.loads(t.function.arguments or "{}")
            res = consultar_desempenho(**args) if t.function.name == "consultar_desempenho" else listar_propostas()
            msgs.append({"role": "tool", "tool_call_id": t.id, "content": res})
    return "Não consegui fechar a resposta; tente reformular."


def assinatura(p):
    """Identidade da proposta pelo conteúdo: os mesmos prefixos com o mesmo prazo e transportadora = mesma proposta."""
    return f"{p['prefixos']}|{int(float(p['prazo_novo']))}|{p['transportadora_nova']}"

def chave_da(p):
    assinatura = f"{p['prefixos']}|{p['prazo_novo']}|{p['transportadora_nova']}"
    return f"{p['proposta_id']}-{hashlib.md5(assinatura.encode()).hexdigest()[:8]}"

def em_observacao():
    """Resumo dos prefixos que o agente segurou por evidência fraca (saida/em_observacao.csv)."""
    arq = SAIDA / "em_observacao.csv"
    if not arq.exists():
        return "nenhum"
    o = pd.read_csv(arq, dtype={"prefixo3": str})
    if o.empty:
        return "nenhum"
    grupos = o.groupby("uf").prefixo3.apply(lambda s: ", ".join(s))
    return f"{len(o)} prefixos ({'; '.join(f'{uf}: {p}' for uf, p in grupos.items())})"
