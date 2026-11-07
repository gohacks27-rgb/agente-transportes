"""MENSAGENS DO SLACK — padrão "bate o olho e entende em 20 segundos".

Regras (para todas as etapas):
  1. Começa pela AÇÃO (verbo + o quê), nunca pelo contexto.
  2. Benefício em números, com antes → depois (dias de prazo, R$, pedidos que deixam de atrasar).
  3. Risco de NÃO fazer, quando houver (⏳ Sem ...).
  4. Urgência por ícone: 🔴 urgente (cliente já sendo afetado) · 🟠 importante · 🟡 oportunidade · ✅ feito/ok · ⚠️ cuidado.
  5. Poucas linhas. Detalhe técnico só no botão "🔍 Detalhes" (abre na thread).
Os textos são montados pelo código a partir dos números (nada é escrito pela IA), então não há número inventado."""
import json, hashlib, datetime
from config import SAIDA, META_CUMPRIMENTO

ARQ_DETALHES = SAIDA / "detalhes_mensagens.json"


# ---------------- utilidades ----------------
def brl(v):
    return f"{float(v):,.0f}".replace(",", ".")

def brl2(v):
    s = f"{abs(float(v)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("+" if float(v) >= 0 else "−") + "R$ " + s

def pct(v):
    return f"{float(v) * 100:.0f}%"

def num(v):
    return int(round(float(v)))

def cidades_curto(texto, n=2):
    c = [x.strip() for x in str(texto).split(",") if x.strip()]
    return ", ".join(c[:n]) + (f" +{len(c) - n}" if len(c) > n else "")

def secao(texto):
    return {"type": "section", "text": {"type": "mrkdwn", "text": texto}}

def rodape(texto):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": texto}]}

def botao(texto, action_id, valor, estilo=None):
    b = {"type": "button", "text": {"type": "plain_text", "text": texto}, "action_id": action_id, "value": valor}
    if estilo:
        b["style"] = estilo
    return b

def acoes(*botoes):
    return {"type": "actions", "elements": list(botoes)}


# ---------------- detalhes opcionais (botão 🔍 Detalhes) ----------------
def guardar_detalhe(texto):
    """Guarda o texto técnico e devolve a chave do botão. Fica em saida/detalhes_mensagens.json (sobrevive a reinício)."""
    chave = hashlib.md5((texto + datetime.datetime.now().isoformat()).encode()).hexdigest()[:12]
    try:
        d = json.loads(ARQ_DETALHES.read_text(encoding="utf-8")) if ARQ_DETALHES.exists() else {}
    except Exception:
        d = {}
    d[chave] = texto
    d = dict(list(d.items())[-300:])
    ARQ_DETALHES.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return chave

def ler_detalhe(chave):
    try:
        return json.loads(ARQ_DETALHES.read_text(encoding="utf-8")).get(chave, "Detalhe não encontrado.")
    except Exception:
        return "Detalhe não encontrado."

def botao_detalhes(texto):
    return botao("🔍 Detalhes", "detalhes", guardar_detalhe(texto))


# ---------------- 1. PROPOSTAS (prazo do site / transportadora padrão) ----------------
def numeros_proposta(p):
    ped = float(p["pedidos_mes"])
    antes, depois = float(p["pct_no_prazo_atual"]), float(p["pct_no_prazo_novo"])
    return {"atrasos_antes": num(ped * (1 - antes)), "atrasos_depois": num(ped * (1 - depois)),
            "dias": int(float(p["prazo_novo"])) - int(float(p["prazo_atual"])),
            "ganho": float(p["ganho_estimado_mes_rs"]), "antes": antes, "depois": depois}

def urgencia_proposta(p):
    return "🔴" if float(p["pct_no_prazo_atual"]) < META_CUMPRIMENTO else "🟡"

def titulo_proposta(p):
    n = numeros_proposta(p)
    pa, pn = int(float(p["prazo_atual"])), int(float(p["prazo_novo"]))
    if p["transportadora_nova"] != p["transportadora_atual"]:
        acao = f"Trocar transportadora: {p['transportadora_atual']} → {p['transportadora_nova']}"
        if pn != pa:
            acao += f" (prazo {pa} → {pn} dias)"
    elif n["dias"] < 0:
        acao = f"Reduzir prazo no site: {pa} → {pn} dias úteis"
    else:
        acao = f"Aumentar prazo no site: {pa} → {pn} dias úteis"
    return acao

def dias_txt(d):
    d = abs(int(d))
    return f"{d} dia" if d == 1 else f"{d} dias"

VEREDITO = {"alta": "👍 *Vale aprovar*", "média": "👍 *Vale aprovar e acompanhar*", "baixa": "⚠️ *Avaliar antes de aprovar*"}

def texto_proposta(p, nivel, riscos):
    """Card de decisão (lê em 20 s):
    1 ação · 2 BENEFÍCIO (R$ e pedidos/dias) · 3 antes → depois · 4 risco de não aprovar · 5 veredito (vale ou não)."""
    n = numeros_proposta(p)
    evitados = max(0, n["atrasos_antes"] - n["atrasos_depois"])
    urgente = n["antes"] < META_CUMPRIMENTO
    ganho = f"*+R$ {brl(n['ganho'])}/mês*"
    if urgente:
        beneficio = [ganho, f"*−{brl(evitados)} pedidos atrasados/mês*"]
        if n["dias"] < 0:
            beneficio.append(f"−{dias_txt(n['dias'])} de prazo")
        elif n["dias"] > 0:
            beneficio.append(f"prazo +{dias_txt(n['dias'])}")
    else:
        beneficio = [ganho + " em vendas", f"*−{dias_txt(n['dias'])} de prazo para o cliente*" if n["dias"] < 0 else f"prazo {n['dias']:+d} dias"]
    try:
        conc = float(p.get("prazo_concorrentes"))
    except (TypeError, ValueError):
        conc = None
    if urgente:
        risco = f"⏳ Sem aprovar: {brl(n['atrasos_antes'])} pedidos/mês continuam atrasando"
    elif conc is not None and float(p["prazo_atual"]) > conc:
        risco = f"⏳ Sem aprovar: prazo segue {dias_txt(float(p['prazo_atual']) - conc)} acima da concorrência"
    else:
        risco = f"⏳ Sem aprovar: deixa de ganhar R$ {brl(n['ganho'])}/mês"
    veredito = VEREDITO[nivel] + (f" — {riscos[0]}" if nivel != "alta" and riscos else " — sem risco relevante")
    return "\n".join([
        f"{urgencia_proposta(p)} *{titulo_proposta(p)}* · {p['uf']}",
        "💰 " + " · ".join(beneficio),
        f"📦 No prazo: {pct(n['antes'])} → {pct(n['depois'])} (meta {pct(META_CUMPRIMENTO)})",
        risco,
        veredito])

def detalhe_proposta(p, nivel, riscos, caso=None, conf=0):
    troca = p["transportadora_nova"] != p["transportadora_atual"]
    d = [f"*Proposta {p['proposta_id']}* ({p['tipo']})",
         f"• Prefixos de CEP: {p['prefixos']}",
         f"• Cidades: {p['cidades']}",
         f"• Envios medidos (amostra de 1 em 6): {num(p.get('envios_medidos_atual') or 0)} com {p['transportadora_atual']}"
         + (f", {num(p.get('envios_medidos_nova') or 0)} com {p['transportadora_nova']}" if troca else "")
         + f" · base: {p.get('base_da_medida', 'prefixo')}",
         f"• Período medido: pedidos de 03/09 a 23/10/2026",
         f"• Prazo mediano dos concorrentes: {float(p.get('prazo_concorrentes') or 0):g} dias úteis",
         f"• Confiança: {nivel} · riscos: {'; '.join(riscos) or 'nenhum relevante'}"]
    if conf:
        d.append(f"• Classificação automática (Jev): {caso} ({pct(conf)})")
    d.append("_Fontes: saida/propostas.csv, saida/desempenho.csv, checkout_conversao.csv, concorrentes_prazos.csv_")
    return "\n".join(d)

def blocos_proposta(p, chave, nivel, riscos, caso=None, conf=0):
    texto = texto_proposta(p, nivel, riscos)
    onde = f"📍 {cidades_curto(p['cidades'], 3)} · {brl(p['pedidos_mes'])} pedidos/mês · proposta {p['proposta_id']}"
    return texto, [secao(texto), rodape(onde),
                   acoes(botao("✅ Aprovar", "aprovar", chave, "primary"), botao("❌ Recusar", "recusar", chave, "danger"),
                         botao_detalhes(detalhe_proposta(p, nivel, riscos, caso, conf)))]

def obs_curta(obs):
    """'16 prefixos (AM: 691, 692; BA: ...)' -> '16 prefixos em AM, BA, ...'"""
    import re
    ufs = re.findall(r"([A-Z]{2}):", str(obs))
    return str(obs).split(" (")[0] + (f" em {', '.join(ufs)}" if ufs else "")

def cabecalho_propostas(novas, aguardando, aprovadas, max_aprov, obs):
    """Primeira mensagem do ciclo: o que o gerente precisa fazer, quanto vale."""
    obs = obs_curta(obs)
    if not novas:
        return "Nenhuma proposta esperando decisão", [
            secao("✅ *Nenhuma proposta esperando decisão*"),
            rodape(f"Aprovações usadas: {aprovadas}/{max_aprov} · Em observação (poucos dados): {obs}")]
    urg = sum(urgencia_proposta(p) == "🔴" for p in novas)
    ganho = sum(float(p["ganho_estimado_mes_rs"]) for p in novas)
    evit = sum(max(0, numeros_proposta(p)["atrasos_antes"] - numeros_proposta(p)["atrasos_depois"]) for p in novas)
    t = (f"📋 *{len(novas)} propostas esperando sua decisão*" + (f" · 🔴 {urg} urgentes" if urg else "")
         + f"\n💰 Se aprovar todas: +R$ {brl(ganho)}/mês · −{brl(evit)} pedidos atrasados/mês")
    return t, [secao(t), rodape(f"Somem daqui quando aprovadas ou recusadas · Aprovações usadas: {aprovadas}/{max_aprov} · Em observação (poucos dados): {obs}")]

def proposta_decidida(texto_original, aprovada, usuario, qtd=0):
    linha1 = texto_original.split("\n")[0]
    if aprovada:
        fim = f"✅ *Aprovada* por <@{usuario}> · aplicada em {qtd} prefixo(s) · acompanho o resultado"
    else:
        fim = f"❌ *Recusada* por <@{usuario}> · não volto a propor"
    return linha1 + "\n" + fim, [secao("~" + linha1.replace("*", "") + "~" if not aprovada else linha1), rodape(fim)]

def status(propostas, aprovadas, max_aprov):
    if not propostas:
        return "Nenhuma proposta ainda. Escreva: @agente-transportes rodar"
    icone = {"aguardando": "⏳", "aprovada": "✅", "recusada": "❌", "desatualizada": "➖"}
    cont = {s: sum(v["status"] == s for v in propostas.values()) for s in icone}
    linhas = [f"📋 *{cont['aguardando']} aguardando* · ✅ {cont['aprovada']} aprovadas · ❌ {cont['recusada']} recusadas · "
              f"aprovações {aprovadas}/{max_aprov}"]
    for k, v in propostas.items():
        if v["status"] == "desatualizada":
            continue
        p = v["dados"]
        linhas.append(f"{icone.get(v['status'], '•')} {urgencia_proposta(p)} {p['proposta_id']} {p['uf']} · {titulo_proposta(p)} · "
                      f"+R$ {brl(p['ganho_estimado_mes_rs'])}/mês")
    return "\n".join(linhas)


# ---------------- 2. TORRE DE CONTROLE ----------------
def nome_cd_curto(nomes_cd, cd):
    """'Paulo Teixeira (operação CD2 Extrema)' -> 'CD2 Extrema (Paulo)'"""
    import re
    m = re.match(r"(\S+).*\(operação (.+)\)", nomes_cd.get(cd, ""))
    return f"{m.group(2)} ({m.group(1)})" if m else cd

def blocos_torre(resumo, cx, quem_cx, nomes_cd):
    """Uma linha por dono: CX, operação do CD, e o que o agente já fez."""
    m = resumo.get("cx_por_motivo", {})
    n_cx = int(resumo.get("clientes_para_cx", 0))
    sr = resumo.get("sem_rastreio_por_cd", {})
    n_sr = int(resumo.get("envios_sem_rastreio_para_operacao_do_cd", 0))
    inc = resumo.get("incidentes_abertos", [])
    tarefas = (n_cx > 0) + (n_sr > 0)
    blocos = [secao(f"🗼 *Torre de controle — {tarefas} tarefa(s) para hoje*" if tarefas else "🗼 *Torre de controle — tudo em dia* ✅")]
    if n_cx:
        valor = float(cx.valor_pedido.sum()) if "valor_pedido" in cx else 0
        partes = []
        if m.get("extravio_devolucao"): partes.append(f"{m['extravio_devolucao']} extravio/devolução")
        if m.get("atraso"): partes.append(f"{m['atraso']} atrasados 3+ dias")
        if m.get("em_risco"): partes.append(f"{m['em_risco']} vão atrasar (avisar antes)")
        cor = "🔴" if (m.get("extravio_devolucao") or m.get("atraso")) else "🟠"
        blocos += [secao(f"{cor} *{quem_cx}: contatar {n_cx} clientes*\n" + " · ".join(partes)
                         + f"\n⏳ Sem contato: R$ {brl(valor)} em pedidos com cliente sem resposta"),
                   acoes(botao("📋 Ver lista", "cx_lista", "cx"), botao("✅ CX assumiu", "cx_assumiu", "cx", "primary"))]
    if n_sr:
        por_cd = " · ".join(f"{nome_cd_curto(nomes_cd, cd)}: {q}" for cd, q in sr.items())
        blocos += [secao(f"🟠 *Operação do CD: reemitir {n_sr} etiquetas sem rastreio*\n{por_cd}"
                         "\n⏳ Sem etiqueta válida, o pedido fica parado no CD"),
                   acoes(botao("📋 Ver lista", "cd_lista", "cd"))]
    feito = [f"{resumo.get('chamados_abertos_agora', 0)} chamados abertos nas transportadoras"]
    feito += [f"🔴 incidente com {i['transportadora']} ({i['uf']}): {num(i['envios_atrasados'])} atrasos" for i in inc] or ["0 incidentes novos"]
    blocos.append(secao("✅ *Já feito pelo agente:* " + " · ".join(feito)))
    det = ["*Torre de controle — detalhes*",
           f"• {resumo.get('total_casos', 0)} casos: " + " · ".join(f"{k} {v}" for k, v in resumo.get("por_tipo", {}).items()),
           "• Por transportadora: " + " · ".join(f"{k} {v}" for k, v in resumo.get("por_transportadora", {}).items()),
           f"• Chamados que já existiam: {resumo.get('chamados_que_ja_existiam', 0)} · ficaram para amanhã (limite 80/dia): "
           f"{resumo.get('chamados_para_amanha_por_limite_de_80_dia', 0)}",
           "• Chamados e incidentes são SIMULADOS (sem acesso aos portais). Referência: exportação dos portais em 02/11/2026.",
           "• 'Vão atrasar' = em trânsito, vence em até 2 dias úteis e o modelo de ML dá 20%+ de chance de atraso.",
           "_Fontes: saida/entregas.csv, saida/cx_casos.csv, saida/operacao_sem_rastreio.csv, saida/chamados_abertos.csv_"]
    blocos.append(acoes(botao_detalhes("\n".join(det))))
    texto = f"Torre: {n_cx} clientes para o CX, {n_sr} etiquetas para o CD"
    return texto, blocos

def lista_cx(cx, n=20):
    ordem = {"extravio_devolucao": 0, "atraso": 1, "em_risco": 2}
    cx = cx.assign(_o=cx.tipo.map(ordem), _p=cx.prob_atraso.fillna(0)).sort_values(["_o", "dias_atraso", "_p"], ascending=[True, False, False])
    linhas = []
    for r in cx.head(n).itertuples():
        if r.tipo == "extravio_devolucao":
            o = "🔴", "extraviado → reenvio ou reembolso"
        elif r.tipo == "atraso":
            o = "🔴", f"{dias_txt(r.dias_atraso)} de atraso → nova previsão"
        else:
            o = "🟠", f"{pct(r.prob_atraso)} de chance de atrasar → avisar antes"
        linhas.append(f"{o[0]} *{r.order_id}* · {o[1]} · {r.cidade}/{r.uf} · _{r.transportadora} {r.codigo_rastreio}_")
    return f"*{len(cx)} clientes* (primeiros {min(n, len(cx))}; lista completa em saida/cx_casos.csv):\n" + "\n".join(linhas)

def lista_cd(op, nomes_cd, n=20):
    linhas = [f"🟠 *{r.order_id}* · {nome_cd_curto(nomes_cd, r.cd_origem)} · {r.cidade}/{r.uf} · {r.transportadora}"
              for r in op.head(n).itertuples()]
    return f"*{len(op)} etiquetas sem rastreio* (primeiras {min(n, len(op))}; lista completa em saida/operacao_sem_rastreio.csv):\n" + "\n".join(linhas)


# ---------------- 3. ROTEAMENTO POR ENVIO (ML + PO) ----------------
def blocos_roteamento(res):
    trocados, total = int(res["trocados_da_padrao"]), int(res["envios"])
    economia = float(res["custo_total_padrao_rs"]) - float(res["custo_total_otimizado_rs"])
    a0, a1 = float(res["atrasos_esperados_padrao"]), float(res["atrasos_esperados_otimizado"])
    ok = res["status_otimizacao"] == "ótimo"
    t = (f"{'✅' if ok else '⚠️'} *Roteamento feito: {brl(trocados)} de {brl(total)} envios trocaram de transportadora*"
         f"\n💰 −R$ {brl(economia)} no custo · 📦 atrasos esperados {a0:.0f} → {a1:.0f} (−{a0 - a1:.0f})")
    det = ["*Roteamento — detalhes*",
           f"• Frete: R$ {brl(res['frete_total_padrao_rs'])} → R$ {brl(res['frete_total_otimizado_rs'])}",
           f"• Custo total (frete + risco de atraso): R$ {brl(res['custo_total_padrao_rs'])} → R$ {brl(res['custo_total_otimizado_rs'])}",
           "• Principais trocas: " + " · ".join(f"{k.replace('->', '→')} ({v})" for k, v in res.get("trocas_por_rota", {}).items())]
    ex = res.get("exemplos", [])
    if ex:
        det.append("• Exemplos (maior ganho por pedido):")
        det += [f"   🔁 Pedido {e['order_id']} ({e['cidade']}/{e['uf']}): {e['de']} → {e['para']} · chance de atraso "
                f"{pct(e['risco_de'])} → {pct(e['risco_para'])} · frete {brl2(e['frete_dif_rs'])}" for e in ex]
    det += [f"• Otimização: {res['status_otimizacao']} · modelo de ML (chance de atraso) + programação inteira (HiGHS)",
            "• Simulado (sem API do SIG). _Fontes: saida/roteamento_envios.csv, cotacoes_frete.csv_"]
    return t, [secao(t), rodape(f"Nenhuma ação sua: dentro do limite de 40% fora da padrão por UF/dia ({pct(res['pct_trocados'])} hoje)"),
               acoes(botao_detalhes("\n".join(det)))]


# ---------------- erros ----------------
def erro(etapa, e):
    return f"🔴 *{etapa} falhou* — {type(e).__name__}: {str(e)[:150]}\nDetalhe completo na janela do agente."


# ---------------- 4. AUDITORIA DE FRETE E INDENIZAÇÕES ----------------
def blocos_auditoria(r, quem_fin, quem_cx):
    """Dinheiro a recuperar primeiro; uma linha por dono (financeiro, CX); depois o que o agente já fez."""
    recuperar = r["multa_rs"] + r["recurso_fotos_rs"] + r["indenizacoes_abertas_rs"]
    x = r.get("amostra", 1)
    blocos = [secao(f"💰 *Auditoria de frete — R$ {brl(recuperar)} a recuperar*"),
              rodape(f"Valores da amostra de 1 em {x} envios · na operação inteira ≈ R$ {brl(recuperar * x)}")]
    if r["multa_rs"] > 0:
        abaixo = len(r["transportadoras_abaixo_meta"])
        pior = min(r["cumprimento_sla"].items(), key=lambda kv: kv[1])
        blocos += [secao(f"🔴 *{quem_fin}: aprovar cobrança de multas por atraso — R$ {brl(r['multa_rs'])}*\n"
                         f"{abaixo} de {len(r['cumprimento_sla'])} transportadoras abaixo da meta de {pct(r['meta_sla'])} do contrato "
                         f"(pior: {pior[0]} {pct(pior[1])}) · {brl(r['multa_envios'])} envios fora do SLA\n"
                         f"⏳ Sem aprovar: R$ {brl(r['multa_a_vencer_rs'])} deixam de ser abatidos nas faturas a vencer"),
                   acoes(botao("✅ Cobrar multas", "cobrar_multas", "m", "primary"), botao("❌ Não cobrar", "nao_cobrar", "m", "danger"))]
    if r["recurso_fotos_qtd"]:
        blocos += [secao(f"🟠 *{quem_cx}: juntar fotos e recorrer de {r['recurso_fotos_qtd']} indenizações negadas — R$ {brl(r['recurso_fotos_rs'])}*\n"
                         "Negadas só por \"fotos insuficientes\" · ⏳ Sem recurso: o valor fica perdido"),
                   acoes(botao("📋 Ver lista", "recursos_lista", "r"), botao("✅ CX assumiu", "cx_assumiu", "recursos", "primary"))]
    feito = []
    if r["indenizacoes_abertas_agora"]:
        feito.append(f"{r['indenizacoes_abertas_agora']} pedidos de indenização abertos (R$ {brl(r['indenizacoes_abertas_rs'])})")
    feito.append(f"{brl(r['ctes_conferidos'])} CT-e conferidos: "
                 + ("nenhuma cobrança acima do contrato" if not (r["frete_acima_tabela_qtd"] or r["complementos_errados_qtd"])
                    else f"🔴 {r['frete_acima_tabela_qtd'] + r['complementos_errados_qtd']} acima do contrato "
                         f"(R$ {brl(r['frete_acima_tabela_rs'] + r['complementos_errados_rs'])})"))
    feito.append(f"{r['faturas_conferidas']} faturas " + ("batem" if not r["faturas_com_erro"] else f"· 🔴 {len(r['faturas_com_erro'])} com erro"))
    blocos.append(secao("✅ *Já feito pelo agente:* " + " · ".join(feito)))
    blocos.append(rodape(f"👀 Acompanhando: R$ {brl(r['aguardando_abatimento_rs'])} em indenizações aprovadas a abater na próxima fatura · "
                         f"R$ {brl(r['em_analise_rs'])} em análise ({r['em_analise_vencidas']} fora do prazo)"))
    det = ["*Auditoria — detalhes*",
           "• Multa (transportadoras.csv): % do frete por dia útil acima do SLA do contrato, com teto. Atraso contado da coleta "
           "até a entrega no portal, sem feriados nacionais nem estaduais do destino.",
           "• Multa por transportadora: " + " · ".join(f"{k} R$ {brl(v)}" for k, v in r["multa_por_transportadora"].items()),
           "• Cumprimento do SLA: " + " · ".join(f"{k} {pct(v)}" for k, v in r["cumprimento_sla"].items()) + f" (meta {pct(r['meta_sla'])})",
           f"• CT-e (Conhecimento de Transporte eletrônico): frete recalculado pela tabela vigente na data do pedido e peso taxado; "
           f"reentrega e devolução pelo contrato. {r['ctes_nao_conferidos_cep']} não conferidos: CEP incompleto no ERP (7 dígitos).",
           "• Ações simuladas (sem API do financeiro e do SAC das transportadoras).",
           "_Fontes: ctes.csv, faturas_frete.csv, indenizacoes.csv, ocorrencias_entrega.csv, transportadoras.csv, saida/auditoria_multas.csv_"]
    blocos.append(acoes(botao_detalhes("\n".join(det))))
    return f"Auditoria: R$ {brl(recuperar)} a recuperar", blocos

def lista_recursos(neg, n=20):
    linhas = [f"🟠 *{r.order_id}* · {r.transportadora} · {r.tipo} · R$ {brl2(r.valor_solicitado)[4:]} · {r.cidade}/{r.uf}"
              for r in neg.head(n).itertuples()]
    return (f"*{len(neg)} indenizações para recorrer com fotos* (lista completa em saida/auditoria_recursos_fotos.csv):\n"
            + "\n".join(linhas))

def multas_decididas(aprovado, usuario, por_transp=None):
    if aprovado:
        return (f"✅ *Cobrança aprovada* por <@{usuario}> · R$ {brl(sum(por_transp.values()))} a abater nas próximas faturas · "
                + " · ".join(f"{k} R$ {brl(v)}" for k, v in por_transp.items()))
    return f"❌ *Cobrança não aprovada* por <@{usuario}> · as multas continuam na próxima auditoria"
