"""ETAPA 4 (versão Slack) — o agente planejador de transportes mora no Slack.

O que ele faz sozinho:
  1. Roda as etapas 1-3 (portais -> desempenho -> recomendações).
  2. Confere cada proposta com o Jev e monta a mensagem curta e visual (mensagens.py: ação primeiro, antes → depois,
     risco de não fazer, detalhe técnico só no botão 🔍 Detalhes).
  3. Posta cada proposta no canal (SLACK_CANAL, ex. #logistica) com os botões Aprovar / Recusar.
  4. Aprovada: aplica na regra do site (simulada em saida/regras_site_simulado.csv) e registra em saida/acoes.csv.
  5. Responde quem mencionar @agente-transportes ou mandar mensagem direta.
  6. Roda sozinho toda segunda às 08:00 (horário de Brasília) e quando alguém escreve '@agente-transportes rodar'.
  8. ROTEAMENTO POR ENVIO (5_rotear_envios.py, ML + PO): todo dia útil às 09:00 ou com '@agente-transportes rotear'.
  9. AUDITORIA DE FRETE (auditoria.py): toda segunda depois das propostas ou com '@agente-transportes auditoria' —
     confere CT-e e faturas, calcula a multa por atraso do contrato (financeiro aprova a cobrança), abre os pedidos de
     indenização que faltam e lista as negadas por falta de foto para o CX recorrer.
  7. TORRE DE CONTROLE (torre.py): todo dia útil às 09:00 ou com '@agente-transportes torre' — abre chamados de atraso
     sozinho, abre incidente quando o Jev vê problema sistêmico e chama o CX para os clientes que precisam de resposta.
Só quem está em SLACK_GERENTES (IDs de usuário do Slack, separados por vírgula) pode aprovar; vazio = qualquer um no canal.
Precisa no .env: AI_PROXY_API_KEY, SLACK_BOT_TOKEN (xoxb-...), SLACK_APP_TOKEN (xapp-...), SLACK_CANAL."""
import os, threading, time, datetime, json, traceback
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from config import AQUI, MAX_APROVACOES, CUSTO_APROVACAO
import nucleo as n
import mensagens as msg
import torre
import auditoria
import importlib
import pandas as pd
from config import SAIDA

load_dotenv(AQUI / ".env")
app = App(token=os.environ["SLACK_BOT_TOKEN"])
CANAL = os.environ.get("SLACK_CANAL", "#logistica")
CX = os.environ.get("SLACK_CX", "").strip()            # ID do usuário do CX no Slack (opcional)
NOME_CX = "Juliana Prado (CX)"                          # politicas.csv, contatos
NOME_OPERACAO = {"CD1": "Lucas Ferreira (operação CD1 Fortaleza)", "CD2": "Paulo Teixeira (operação CD2 Extrema)"}  # politicas.csv
FINANCEIRO = os.environ.get("SLACK_FINANCEIRO", "").strip()   # ID do usuário do financeiro no Slack (opcional)
NOME_FIN = "Otávio Ramos (financeiro)"                         # politicas.csv, contatos
GERENTES = {g.strip() for g in os.environ.get("SLACK_GERENTES", "").split(",") if g.strip()}
_rodando = threading.Lock()

def _seguro(etapa, func, client):
    """Roda uma etapa; se falhar, avisa no canal em 1 linha (em vez de sumir) e não derruba o agendador."""
    try:
        func(client)
    except Exception as e:
        traceback.print_exc()
        try:
            client.chat_postMessage(channel=CANAL, text=msg.erro(etapa, e))
        except Exception:
            pass

def ciclo(client):
    """Roda a análise e mostra TODAS as propostas sem decisão (novas e antigas).
    Só deixam de aparecer quando o gerente aprova ou recusa."""
    if not _rodando.acquire(blocking=False):
        client.chat_postMessage(channel=CANAL, text="⏳ Já estou rodando uma análise; aguarde.")
        return
    try:
        client.chat_postMessage(channel=CANAL, text="🔄 Analisando prazos e transportadoras…")
        propostas = n.rodar_etapas()
        est = n.ler_estado()
        # identifica cada proposta pelo CONTEÚDO (prefixos + prazo + transportadora), não pelo número P01, P02...,
        # que muda quando uma aprovação altera a regra do site
        por_conteudo = {n.assinatura(v["dados"]): k for k, v in est["propostas"].items()}
        atuais = set()
        for p in propostas.to_dict("records"):
            chave = por_conteudo.get(n.assinatura(p))
            usados = {v["dados"]["proposta_id"] for v in est["propostas"].values()}
            if chave is None:
                if p["proposta_id"] in usados:   # número já é de outra proposta: pega o próximo livre (P10, P11...)
                    p["proposta_id"] = f"P{max(int(u[1:]) for u in usados) + 1:02d}"
                chave = n.chave_da(p)
                caso, conf, _ = n.conferir_com_jev(p)
                est["propostas"][chave] = {"status": "aguardando", "dados": p, "jev": [caso, conf]}
                n.registrar_acao("proposta_nova", chave)
            elif est["propostas"][chave]["status"] == "aguardando":
                p["proposta_id"] = est["propostas"][chave]["dados"]["proposta_id"]   # o número da proposta não muda
                est["propostas"][chave]["dados"] = p          # números atualizados da análise de hoje
            atuais.add(chave)
        # pendente que a análise de hoje não recomenda mais (ex.: outra aprovação mudou a regra) sai da lista
        for k, v in est["propostas"].items():
            if v["status"] == "aguardando" and k not in atuais:
                v["status"] = "desatualizada"
                n.registrar_acao("proposta_desatualizada", k)
        n.salvar_estado(est)
        postar_pendentes(client)
    finally:
        _rodando.release()

def postar_pendentes(client, thread_ts=None):
    """Reposta no formato novo as propostas que ainda esperam decisão (os botões continuam valendo)."""
    est = n.ler_estado()
    pend = [(k, v["dados"]) for k, v in est["propostas"].items() if v["status"] == "aguardando"]
    if not pend:
        texto, blocos = msg.cabecalho_propostas([], 0, est["aprovadas"], MAX_APROVACOES, n.em_observacao())
        client.chat_postMessage(channel=CANAL, text=texto, blocks=blocos)
        return
    pend.sort(key=lambda kp: (msg.urgencia_proposta(kp[1]) != "🔴", -float(kp[1]["ganho_estimado_mes_rs"])))
    texto, blocos = msg.cabecalho_propostas([p for _, p in pend], len(pend), est["aprovadas"], MAX_APROVACOES, n.em_observacao())
    client.chat_postMessage(channel=CANAL, text=texto, blocks=blocos)
    for chave, p in pend:
        nivel, riscos = n.avaliar_risco(p)
        caso, conf = est["propostas"][chave].get("jev", [None, 0])
        texto, blocos = msg.blocos_proposta(p, chave, nivel, riscos, caso, conf)
        n.registrar_acao("proposta_enviada", chave)
        client.chat_postMessage(channel=CANAL, text=texto, blocks=blocos)

@app.action("mostrar_pendentes")
def acao_mostrar_pendentes(ack, body, client):
    ack()
    threading.Thread(target=_seguro, args=("Propostas pendentes", postar_pendentes, client), daemon=True).start()

def decidir(body, client, aprovar):
    usuario = body["user"]["id"]
    chave = body["actions"][0]["value"]
    canal, ts = body["channel"]["id"], body["message"]["ts"]
    texto_original = body["message"]["blocks"][0]["text"]["text"]
    if GERENTES and usuario not in GERENTES:
        client.chat_postEphemeral(channel=canal, user=usuario, text="🔒 Só o gerente de logística pode decidir.")
        return
    est = n.ler_estado()
    item = est["propostas"].get(chave)
    if not item:
        return
    if item["status"] != "aguardando":   # cópia antiga da mesma proposta, postada em outra rodada
        client.chat_postEphemeral(channel=canal, user=usuario, text=f"ℹ️ Essa proposta já foi {item['status']}." if item["status"] != "desatualizada"
                                else "ℹ️ Essa proposta saiu da lista: a análise mais recente não a recomenda mais.")
        return
    qtd = 0
    if aprovar:
        if est["aprovadas"] >= MAX_APROVACOES:
            client.chat_postEphemeral(channel=canal, user=usuario, text=f"⚠️ Limite de {MAX_APROVACOES} aprovações atingido.")
            return
        qtd = n.aplicar(item["dados"])
        item["status"], est["aprovadas"] = "aprovada", est["aprovadas"] + 1
        n.registrar_acao("aprovada_e_aplicada", f"{chave} por {usuario}; {qtd} prefixos; custo aprovação R$ {CUSTO_APROVACAO:.2f}")
    else:
        item["status"] = "recusada"
        n.registrar_acao("recusada", f"{chave} por {usuario}")
    n.salvar_estado(est)
    texto, blocos = msg.proposta_decidida(texto_original, aprovar, usuario, qtd)
    client.chat_update(channel=canal, ts=ts, text=texto, blocks=blocos)

@app.action("aprovar")
def acao_aprovar(ack, body, client):
    ack(); decidir(body, client, True)

@app.action("recusar")
def acao_recusar(ack, body, client):
    ack(); decidir(body, client, False)

@app.action("detalhes")
def acao_detalhes(ack, body, client):
    """Detalhe técnico opcional: abre na thread da mensagem, só para quem quiser ver."""
    ack()
    client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                            text=msg.ler_detalhe(body["actions"][0]["value"]))


# ---------------- TORRE DE CONTROLE ----------------
def ciclo_torre(client):
    if not _rodando.acquire(blocking=False):
        client.chat_postMessage(channel=CANAL, text="⏳ Já estou rodando uma análise; aguarde.")
        return
    try:
        client.chat_postMessage(channel=CANAL, text="🗼 Verificando envios em andamento…")
        resumo, cx = torre.rodar()
        quem_cx = f"<@{CX}>" if CX else NOME_CX
        texto, blocos = msg.blocos_torre(resumo, cx, quem_cx, NOME_OPERACAO)
        client.chat_postMessage(channel=CANAL, text=texto, blocks=blocos)
        n.registrar_acao("torre_executada", json.dumps({k: v for k, v in resumo.items() if k != "incidentes_abertos"}, ensure_ascii=False, default=str))
    finally:
        _rodando.release()

@app.action("cx_lista")
def acao_cx_lista(ack, body, client):
    ack()
    cx = pd.read_csv(SAIDA / "cx_casos.csv")
    client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"], text=msg.lista_cx(cx))

@app.action("cd_lista")
def acao_cd_lista(ack, body, client):
    ack()
    op = pd.read_csv(SAIDA / "operacao_sem_rastreio.csv")
    client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"], text=msg.lista_cd(op, NOME_OPERACAO))

@app.action("cx_assumiu")
def acao_cx_assumiu(ack, body, client):
    ack()
    usuario = body["user"]["id"]
    n.registrar_acao("cx_assumiu", f"por {usuario}")
    client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"],
                            text=f"✅ <@{usuario}> assumiu o contato com os clientes.")

# ---------------- ROTEAMENTO POR ENVIO (ML + PO) ----------------
def ciclo_roteamento(client):
    if not _rodando.acquire(blocking=False):
        client.chat_postMessage(channel=CANAL, text="⏳ Já estou rodando uma análise; aguarde.")
        return
    try:
        client.chat_postMessage(channel=CANAL, text="🚚 Escolhendo a transportadora de cada envio novo…")
        res = importlib.import_module("5_rotear_envios").main()
        texto, blocos = msg.blocos_roteamento(res)
        client.chat_postMessage(channel=CANAL, text=texto, blocks=blocos)
        n.registrar_acao("roteamento_executado", json.dumps(res, ensure_ascii=False, default=str))
    finally:
        _rodando.release()

# ---------------- AUDITORIA DE FRETE E INDENIZAÇÕES ----------------
def ciclo_auditoria(client):
    if not _rodando.acquire(blocking=False):
        client.chat_postMessage(channel=CANAL, text="⏳ Já estou rodando uma análise; aguarde.")
        return
    try:
        client.chat_postMessage(channel=CANAL, text="💰 Conferindo CT-e, faturas, multas e indenizações…")
        r = auditoria.rodar()
        texto, blocos = msg.blocos_auditoria(r, f"<@{FINANCEIRO}>" if FINANCEIRO else NOME_FIN, f"<@{CX}>" if CX else NOME_CX)
        client.chat_postMessage(channel=CANAL, text=texto, blocks=blocos)
        n.registrar_acao("auditoria_executada", json.dumps(r, ensure_ascii=False, default=str))
    finally:
        _rodando.release()

def decidir_multas(body, client, aprovar):
    usuario = body["user"]["id"]
    canal, ts = body["channel"]["id"], body["message"]["ts"]
    if FINANCEIRO and usuario != FINANCEIRO:
        client.chat_postEphemeral(channel=canal, user=usuario, text="🔒 Só o financeiro pode decidir a cobrança de multas.")
        return
    if aprovar:
        por = auditoria.registrar_cobranca(usuario)
        n.registrar_acao("multas_cobradas", json.dumps(por, ensure_ascii=False))
        fim = msg.multas_decididas(True, usuario, por)
    else:
        n.registrar_acao("multas_nao_cobradas", f"por {usuario}")
        fim = msg.multas_decididas(False, usuario)
    blocos = [b for b in body["message"]["blocks"]
              if not (b["type"] == "actions" and b["elements"][0]["action_id"] == "cobrar_multas")]
    i = next(k for k, b in enumerate(blocos) if b["type"] == "section" and "multas por atraso" in b["text"]["text"])
    blocos.insert(i + 1, msg.rodape(fim))
    client.chat_update(channel=canal, ts=ts, text=fim, blocks=blocos)

@app.action("cobrar_multas")
def acao_cobrar_multas(ack, body, client):
    ack(); decidir_multas(body, client, True)

@app.action("nao_cobrar")
def acao_nao_cobrar(ack, body, client):
    ack(); decidir_multas(body, client, False)

@app.action("recursos_lista")
def acao_recursos_lista(ack, body, client):
    ack()
    neg = pd.read_csv(SAIDA / "auditoria_recursos_fotos.csv")
    client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["ts"], text=msg.lista_recursos(neg))

def status_texto():
    est = n.ler_estado()
    return msg.status(est["propostas"], est["aprovadas"], MAX_APROVACOES)

def tratar(texto, say, client, thread_ts=None):
    """Comandos por texto (sem barra): rodar, pendentes, torre, rotear, auditoria, status. O resto é pergunta livre."""
    t = texto.strip().lower()
    if t in ("rodar", "/rodar", "analisar"):
        say(text="👍 Analisando; as propostas chegam no canal.", thread_ts=thread_ts)
        threading.Thread(target=_seguro, args=("Análise de propostas", ciclo, client), daemon=True).start()
    elif t in ("torre", "torre de controle", "atrasos"):
        say(text="👍 Verificando os envios em andamento.", thread_ts=thread_ts)
        threading.Thread(target=_seguro, args=("Torre de controle", ciclo_torre, client), daemon=True).start()
    elif t in ("rotear", "roteamento", "rotear envios"):
        say(text="👍 Escolhendo a transportadora de cada envio novo.", thread_ts=thread_ts)
        threading.Thread(target=_seguro, args=("Roteamento", ciclo_roteamento, client), daemon=True).start()
    elif t in ("pendentes", "propostas", "mostrar pendentes"):
        threading.Thread(target=_seguro, args=("Propostas pendentes", postar_pendentes, client), daemon=True).start()
    elif t in ("auditoria", "auditar", "faturas", "multas"):
        say(text="👍 Conferindo CT-e, faturas, multas e indenizações.", thread_ts=thread_ts)
        threading.Thread(target=_seguro, args=("Auditoria", ciclo_auditoria, client), daemon=True).start()
    elif t in ("status", "/status"):
        say(text=status_texto(), thread_ts=thread_ts)
    else:
        say(text=n.responder(texto), thread_ts=thread_ts)

@app.event("app_mention")
def mencao(event, say, client):
    texto = event["text"].split(">", 1)[-1].strip()  # tira o @agente do começo
    tratar(texto, say, client, thread_ts=event.get("thread_ts", event["ts"]))

@app.event("message")
def mensagem_direta(event, say, client):
    if event.get("channel_type") == "im" and not event.get("bot_id") and event.get("text"):
        tratar(event["text"], say, client)

def agendador():
    """Propostas: toda segunda às 08:00, seguidas da auditoria. Roteamento + torre: todo dia útil às 09:00 (Brasília).
    Cada etapa roda protegida: se uma falhar, avisa no canal e as outras continuam."""
    brt = datetime.timezone(datetime.timedelta(hours=-3))
    ultimo = ultimo_torre = None
    while True:
        agora = datetime.datetime.now(brt)
        if agora.weekday() == 0 and agora.hour == 8 and ultimo != agora.date():
            ultimo = agora.date()
            _seguro("Análise de propostas", ciclo, app.client)
            _seguro("Auditoria", ciclo_auditoria, app.client)
        if agora.weekday() < 5 and agora.hour == 9 and ultimo_torre != agora.date():
            ultimo_torre = agora.date()
            _seguro("Roteamento", ciclo_roteamento, app.client)   # antes da coleta da tarde (cds.csv: janela 15h-21h)
            _seguro("Torre de controle", ciclo_torre, app.client)
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=agendador, daemon=True).start()
    print(f"Agente no ar no Slack (canal {CANAL}). No canal, escreva: @agente-transportes rodar. (Ctrl+C para parar)")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
