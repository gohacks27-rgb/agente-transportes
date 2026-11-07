"""ETAPA 4 — Agente planejador de transportes no Telegram.

O que ele faz sozinho:
  1. Roda as etapas 1-3 (junta portais, mede desempenho, recomenda prazo/transportadora).
  2. Confere cada proposta com o Jev (decisão rápida com probabilidade) e redige a mensagem com o chat (gpt).
  3. Manda cada proposta ao gerente no Telegram com os botões Aprovar / Recusar (politicas.csv: só o gerente aprova).
  4. Aprovada: aplica a mudança na regra do site (simulada em saida/regras_site_simulado.csv) e registra em saida/acoes.csv.
  5. Responde perguntas do time no chat ("como está a ViaLog no Sul?") consultando os resultados — números só dos arquivos.
  6. Repete o ciclo toda segunda-feira às 08:00 (se o job-queue estiver instalado) ou quando alguém manda /rodar.

Comandos: /start (registra quem é o gerente), /rodar (roda o ciclo agora), /status (situação das propostas).
Precisa no .env: AI_PROXY_API_KEY e TELEGRAM_BOT_TOKEN."""
import os, json, csv, asyncio, datetime, importlib, logging, hashlib
import pandas as pd
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from config import AQUI, DADOS, SAIDA, MAX_APROVACOES, CUSTO_APROVACAO, META_CUMPRIMENTO
import ia

load_dotenv(AQUI / ".env")
logging.basicConfig(level=logging.WARNING)
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

def conferir_com_jev(p):
    """Jev classifica o caso e dá a confiança na evidência. Confiança baixa vira alerta na mensagem."""
    perguntas = {
        "caso": {"type": "choice", "instructions": "Qual é o problema principal desta região?",
                 "criteria": {"prazo_irreal": "o site promete menos dias do que a entrega real leva e o cumprimento está baixo",
                              "folga_excessiva": "o cumprimento já é quase 100% e o prazo mostrado é maior que o necessário",
                              "transportadora_ruim": "outra transportadora entrega bem melhor na mesma região"}},
        "evidencia_forte": {"type": "noul", "instructions": "A evidência (envios, % no prazo antes e depois) é suficiente para propor a mudança ao gerente?"},
    }
    try:
        r = ia.jev(p, perguntas)
        return r["caso"]["choice"], r["caso"].get("confidence", 0), r["evidencia_forte"]["noul"]
    except Exception as erro:  # sem Jev, segue com a análise numérica e avisa
        print("Jev indisponível:", erro)
        return "não classificado", 0, None

SISTEMA = ("Você é o agente planejador de transportes da Gocase. Escreva em português, direto e curto, para o gerente "
           "de logística aprovar ou recusar pelo celular. Use SOMENTE os números recebidos; não invente nada. "
           "Siga a evidência mínima de politicas.csv: prefixos, transportadora, período, envios, taxa de cumprimento "
           "antes e depois, impacto estimado em R$. Explique siglas na primeira vez. Máximo 9 linhas, sem markdown.")

def redigir(p, caso, conf, evid):
    texto = (f"Proposta {p['proposta_id']} ({p['tipo']}). UF {p['uf']}, prefixos {p['prefixos']} ({p['cidades']}). "
             f"Transportadora atual {p['transportadora_atual']} -> nova {p['transportadora_nova']}. "
             f"Prazo exibido {p['prazo_atual']} -> {p['prazo_novo']} dias úteis. "
             f"% no prazo hoje {float(p['pct_no_prazo_atual']):.0%} -> estimado {float(p['pct_no_prazo_novo']):.0%} (meta {META_CUMPRIMENTO:.0%}). "
             f"Prazo mediano dos concorrentes: {p['prazo_concorrentes']}. Pedidos/mês: {p['pedidos_mes']}. "
             f"Ganho estimado: R$ {float(p['ganho_estimado_mes_rs']):,.0f}/mês. Período medido: pedidos de 03/09 a 23/10/2026. "
             f"Classificação automática do caso: {caso} (confiança {conf:.0%}).")
    try:
        msg = ia.chat([{"role": "system", "content": SISTEMA}, {"role": "user", "content": texto}]).content
    except Exception as erro:
        print("Chat indisponível, mando o texto técnico:", erro)
        msg = texto
    if evid is not None and evid < 0.7:
        msg += "\n\n⚠️ Evidência considerada fraca pela checagem automática: revise com atenção."
    return msg

async def ciclo(app):
    est = ler_estado()
    chat_id = est["gerente_chat_id"]
    if not chat_id:
        print("Nenhum gerente registrado ainda: mande /start ao bot.")
        return
    await app.bot.send_message(chat_id, "🔄 Rodando a análise semanal de prazos e transportadoras...")
    propostas = await asyncio.to_thread(rodar_etapas)
    enviadas = 0
    for p in propostas.to_dict("records"):
        # chave curta (o Telegram aceita no máximo 64 bytes no botão): id da proposta + resumo da mudança
        assinatura = f"{p['prefixos']}|{p['prazo_novo']}|{p['transportadora_nova']}"
        chave = f"{p['proposta_id']}-{hashlib.md5(assinatura.encode()).hexdigest()[:8]}"
        if chave in est["propostas"]:
            continue  # já foi enviada antes
        caso, conf, evid = await asyncio.to_thread(conferir_com_jev, p)
        msg = await asyncio.to_thread(redigir, p, caso, conf, evid)
        botoes = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Aprovar", callback_data=f"ap|{chave}"),
                                        InlineKeyboardButton("❌ Recusar", callback_data=f"re|{chave}")]])
        await app.bot.send_message(chat_id, msg, reply_markup=botoes)
        est["propostas"][chave] = {"status": "aguardando", "dados": p}
        registrar_acao("proposta_enviada", chave)
        enviadas += 1
    salvar_estado(est)
    await app.bot.send_message(chat_id, f"Pronto: {enviadas} proposta(s) nova(s) aguardando sua decisão. "
                                        f"Aprovações usadas: {est['aprovadas']}/{MAX_APROVACOES}.")

# ---------- ação aprovada: aplica na regra do site (simulada) ----------
def aplicar(p):
    if not REGRAS_SIM.exists():
        pd.read_csv(DADOS / "regras_site_prazo_frete_por_prefixo.csv", dtype=str).to_csv(REGRAS_SIM, index=False)
    r = pd.read_csv(REGRAS_SIM, dtype=str)
    alvo = r.prefixo_cep3.str.zfill(3).isin(p["prefixos"].split("; "))
    r.loc[alvo, "prazo_exibido_dias_uteis"] = p["prazo_novo"]
    r.loc[alvo, "transportadora_padrao"] = p["transportadora_nova"]
    r.to_csv(REGRAS_SIM, index=False)
    return int(alvo.sum())

async def botao(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    acao, chave = q.data.split("|", 1)
    est = ler_estado()
    item = est["propostas"].get(chave)
    if not item or item["status"] != "aguardando":
        await q.edit_message_reply_markup(None)
        return
    quem = q.from_user.full_name
    if acao == "ap":
        if est["aprovadas"] >= MAX_APROVACOES:
            await q.message.reply_text(f"Limite de {MAX_APROVACOES} aprovações por cenário atingido (politicas.csv).")
            return
        n = await asyncio.to_thread(aplicar, item["dados"])
        item["status"], est["aprovadas"] = "aprovada", est["aprovadas"] + 1
        registrar_acao("aprovada_e_aplicada", f"{chave} por {quem}; {n} prefixos; custo aprovação R$ {CUSTO_APROVACAO:.2f}")
        await q.edit_message_text(q.message.text + f"\n\n✅ Aprovada por {quem}. Aplicada em {n} prefixo(s). "
                                  "Vou acompanhar o cumprimento nas próximas semanas.")
    else:
        item["status"] = "recusada"
        registrar_acao("recusada", f"{chave} por {quem}")
        await q.edit_message_text(q.message.text + f"\n\n❌ Recusada por {quem}. Não volto a propor esta mudança.")
    salvar_estado(est)

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
    msgs = [{"role": "system", "content": "Você é o agente de transportes da Gocase e responde o time no chat, em português, curto. "
             "Use as ferramentas para obter números e cite o arquivo de origem (saida/desempenho.csv). Nunca invente números. "
             "Você não aprova mudanças: só o gerente aprova. Explique siglas na primeira vez."},
            {"role": "user", "content": pergunta}]
    for _ in range(4):
        m = ia.chat(msgs, ferramentas=FERRAMENTAS)
        if not m.tool_calls:
            return m.content
        msgs.append({"role": "assistant", "content": m.content, "tool_calls": [t.model_dump() for t in m.tool_calls]})
        for t in m.tool_calls:
            args = json.loads(t.function.arguments or "{}")
            res = consultar_desempenho(**args) if t.function.name == "consultar_desempenho" else listar_propostas()
            msgs.append({"role": "tool", "tool_call_id": t.id, "content": res})
    return "Não consegui fechar a resposta; tente reformular."

# ---------- comandos ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    est = ler_estado()
    if not est["gerente_chat_id"]:
        est["gerente_chat_id"] = update.effective_chat.id
        salvar_estado(est)
        registrar_acao("gerente_registrado", update.effective_user.full_name)
    await update.message.reply_text("Olá! Sou o agente de transportes. Vou analisar prazos e transportadoras toda semana "
                                    "e te mandar propostas para aprovar. Use /rodar para analisar agora, /status para ver as "
                                    "propostas, ou me pergunte algo como 'como está a ViaLog no Sul?'.")

async def cmd_rodar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ciclo(ctx.application)

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    est = ler_estado()
    linhas = [f"{k.split('-')[0]} ({v['dados']['uf']}): {v['status']}" for k, v in est["propostas"].items()]
    await update.message.reply_text("\n".join(linhas) or "Nenhuma proposta ainda. Mande /rodar.")

async def texto_livre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    resposta = await asyncio.to_thread(responder, update.message.text)
    await update.message.reply_text(resposta)

async def semanal(ctx: ContextTypes.DEFAULT_TYPE):
    await ciclo(ctx.application)

def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rodar", cmd_rodar))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(botao))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto_livre))
    if app.job_queue:  # pip install "python-telegram-bot[job-queue]"
        app.job_queue.run_daily(semanal, time=datetime.time(8, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=-3))), days=(1,))
    print("Agente no ar. Mande /start para o bot no Telegram. (Ctrl+C para parar)")
    app.run_polling()

if __name__ == "__main__":
    main()
