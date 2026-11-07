"""TORRE DE CONTROLE — o agente acompanha os envios em andamento e age sozinho.

O que faz (data de referência: exportação dos portais em 02/11/2026, parametros.md):
  1. Detecta problemas por envio (saida/entregas.csv + envios.csv):
       - atraso: em trânsito e já passou da data prometida
       - sem_rastreio: pedido de 2+ dias sem código de rastreio (problema interno: avisa a operação do CD, não a transportadora)
       - sem_evento: coletado no CD (data_postagem) há 2+ dias úteis e nenhum evento no portal
       - extravio_devolucao: extraviado ou devolvido ao remetente nos últimos 14 dias
       - em_risco (PREVENTIVO, ML): em trânsito, vence em até 2 dias úteis e o modelo (4_modelo_atraso.py) prevê >= 20% de chance de atraso
  2. Abre chamado na transportadora SOZINHO (politicas.csv: o analista abre chamado por envio, sem custo e sem aprovação),
     sem duplicar chamados que já existem (chamados_transportadoras.csv), até 80 por dia (ferramentas.csv).
     Os chamados são simulados (sem API dos portais) e ficam em saida/chamados_abertos.csv.
  3. Separa os clientes que precisam de resposta do CX (extravio/devolução ou atraso >= 3 dias úteis) em saida/cx_casos.csv
     (politicas.csv, escalonamento: "cliente esperando resposta sobre envio -> acionar cx").
  4. Pergunta ao Jev se os atrasos de uma transportadora numa UF parecem problema sistêmico; se sim, abre incidente formal
     (simulado, R$ 15, máx. 2 abertos por transportadora — ferramentas.csv) em saida/incidentes.csv.
Os números são do código; o Jev só julga o padrão; o chat só redige o resumo."""
import csv, datetime, json
import numpy as np, pandas as pd
from config import DADOS, SAIDA, dias_uteis_entre
import ia
import sistemas

REFERENCIA = pd.Timestamp("2026-11-02 06:00")   # exportação dos portais (parametros.md)
LIMITE_CHAMADOS_DIA = 80                         # ferramentas.csv
MAX_INCIDENTES_POR_TRANSP = 2                    # ferramentas.csv
CUSTO_INCIDENTE = 15.0                           # ferramentas.csv
ATRASO_CX = 3                                    # dias úteis de atraso a partir dos quais o CX deve falar com o cliente
MOTIVO_CHAMADO = {"atraso": "atraso_na_entrega", "sem_evento": "rastreio_indisponivel"}  # sem_rastreio é interno (CD)

def detectar():
    e = pd.read_csv(SAIDA / "entregas.csv", dtype={"prefixo3": str},
                    parse_dates=["data_pedido", "data_prometida", "entregue_em", "falha_em"])
    extra = pd.read_csv(DADOS / "envios.csv", usecols=["envio_id", "order_id", "data_postagem"], parse_dates=["data_postagem"])
    e = e.merge(extra, on="envio_id", how="left")
    hoje = np.datetime64(REFERENCIA.normalize().date())
    casos = []
    # atraso: em trânsito com data prometida vencida
    a = e[(e.situacao == "em_transito") & (e.data_prometida < REFERENCIA.normalize())].copy()
    a["dias_atraso"] = dias_uteis_entre(a.data_prometida, np.full(len(a), hoje)) + 1
    casos.append(a.assign(tipo="atraso"))
    recentes = e.data_pedido >= REFERENCIA - pd.Timedelta(days=30)  # só o último mês interessa à operação
    # sem rastreio: 2+ dias úteis depois do pedido (a etiqueta não voltou ao ERP: problema interno, vai para a operação do CD)
    r = e[(e.situacao == "sem_rastreio") & recentes].copy()
    r = r[dias_uteis_entre(r.data_pedido, np.full(len(r), hoje)) >= 2]
    casos.append(r.assign(tipo="sem_rastreio", dias_atraso=0))
    # sem evento: coletado há 2+ dias úteis e nada no portal
    s = e[(e.situacao == "sem_evento_no_portal") & e.data_postagem.notna() & recentes].copy()
    s = s[dias_uteis_entre(s.data_postagem, np.full(len(s), hoje)) >= 2]
    casos.append(s.assign(tipo="sem_evento", dias_atraso=0))
    # extravio/devolução recente
    f = e[(e.situacao == "falha") & (e.falha_em >= REFERENCIA - pd.Timedelta(days=14))].copy()
    casos.append(f.assign(tipo="extravio_devolucao", dias_atraso=0))
    # EM RISCO (preventivo, modelo de ML 4_modelo_atraso.py): em trânsito, prazo ainda não venceu, vence em até 2 dias úteis
    # e o modelo dá chance de atraso >= 20%. Avisa antes do atraso acontecer.
    try:
        modelo = __import__("importlib").import_module("4_modelo_atraso")
        v = e[(e.situacao == "em_transito") & (e.data_prometida >= REFERENCIA.normalize())].copy()
        v = v[dias_uteis_entre(np.full(len(v), hoje), v.data_prometida) <= 2]
        if len(v):
            v = v.drop(columns=[c for c in ["cd_origem"] if c in v.columns]).merge(
                pd.read_csv(DADOS / "envios.csv", usecols=["envio_id", "cd_origem"]), on="envio_id")
            v["prob_atraso"] = modelo.prever(v)
            casos.append(v[v.prob_atraso >= 0.20].assign(tipo="em_risco", dias_atraso=0))
    except FileNotFoundError:
        print("Modelo de atraso ainda não treinado (rode 4_modelo_atraso.py); sigo sem a parte preventiva.")
    c = pd.concat(casos, ignore_index=True)
    cols = ["envio_id", "order_id", "transportadora", "codigo_rastreio", "uf", "cidade", "prefixo3", "tipo", "dias_atraso",
            "data_pedido", "data_prometida", "valor_pedido", "tentativas", "prob_atraso"]
    if "prob_atraso" not in c.columns:
        c["prob_atraso"] = np.nan
    return c[cols].sort_values(["tipo", "dias_atraso"], ascending=[True, False]).reset_index(drop=True)

def _ja_tem_chamado():
    ch = pd.read_csv(DADOS / "chamados_transportadoras.csv", usecols=["envio_id", "status"])
    ids = set(ch.loc[ch.status.ne("encerrado"), "envio_id"]) | set(ch.envio_id)  # qualquer chamado sobre o envio
    arq = SAIDA / "chamados_abertos.csv"
    if arq.exists():
        ids |= set(pd.read_csv(arq).envio_id)
    return ids

def abrir_chamados(casos):
    """Abre (simula) um chamado por envio com problema, sem duplicar e até o limite diário. Devolve os abertos."""
    existentes = _ja_tem_chamado()
    candidatos = casos[casos.tipo.isin(MOTIVO_CHAMADO)]
    ja_existiam = int(candidatos.envio_id.isin(existentes).sum())
    alvo = candidatos[~candidatos.envio_id.isin(existentes)]
    alvo = alvo.assign(_ord=alvo.tipo.eq("atraso").astype(int)).sort_values(["_ord", "dias_atraso"], ascending=False)
    sem_vaga = max(0, len(alvo) - LIMITE_CHAMADOS_DIA)
    alvo = alvo.head(LIMITE_CHAMADOS_DIA)
    linhas = []
    for i, r in enumerate(alvo.itertuples(), 1):
        prot = sistemas.abrir_chamado_transportadora(r.transportadora, r.envio_id, r.codigo_rastreio,
                                                     MOTIVO_CHAMADO[r.tipo], r.dias_atraso, sequencia=i)
        linhas.append({"protocolo": prot, "envio_id": r.envio_id, "transportadora": r.transportadora,
                       "motivo": MOTIVO_CHAMADO[r.tipo], "dias_atraso": r.dias_atraso})
    return pd.DataFrame(linhas), ja_existiam, sem_vaga

def casos_para_cx(casos):
    cx = casos[(casos.tipo.isin(["extravio_devolucao", "em_risco"])) | ((casos.tipo == "atraso") & (casos.dias_atraso >= ATRASO_CX))].copy()
    cx["motivo_cx"] = np.select(
        [cx.tipo == "extravio_devolucao", cx.tipo == "em_risco"],
        ["extravio ou devolução: oferecer reenvio ou reembolso",
         "RISCO de atraso (" + (cx.prob_atraso * 100).round().astype("Int64").astype(str) + "% pelo modelo): avisar o cliente antes"],
        "atraso de " + cx.dias_atraso.astype(int).astype(str) + " dias úteis: avisar e dar nova previsão")
    cx.to_csv(SAIDA / "cx_casos.csv", index=False)
    return cx

def avaliar_incidentes(casos):
    """Para cada transportadora × UF com 10+ atrasos, o Jev julga se é problema sistêmico. Abre incidente se for."""
    arq = SAIDA / "incidentes.csv"
    abertos = pd.read_csv(arq) if arq.exists() else pd.DataFrame(columns=["transportadora", "uf"])
    at = casos[casos.tipo == "atraso"]
    grupos = at.groupby(["transportadora", "uf"]).agg(atrasos=("envio_id", "size"), dias_medio=("dias_atraso", "mean"),
                                                      cidades=("cidade", lambda s: s.value_counts().head(5).to_dict())).reset_index()
    grupos = grupos[grupos.atrasos >= 10]
    novos = []
    for g in grupos.itertuples():
        if ((abertos.transportadora == g.transportadora) & (abertos.uf == g.uf)).any():
            continue
        if (abertos.transportadora == g.transportadora).sum() + sum(n["transportadora"] == g.transportadora for n in novos) >= MAX_INCIDENTES_POR_TRANSP:
            continue
        estado = {"transportadora": g.transportadora, "uf": g.uf, "envios_atrasados_agora": int(g.atrasos),
                  "dias_uteis_de_atraso_medio": round(float(g.dias_medio), 1), "cidades_mais_afetadas": g.cidades,
                  "total_de_envios_atrasados_todas_transportadoras": int(len(at))}
        try:
            r = ia.jev(estado, {"sistemico": {"type": "noul", "instructions":
                "Os atrasos parecem um problema sistêmico desta transportadora nesta UF (concentrados, muitos casos), "
                "que justifica abrir incidente formal com ela, em vez de casos isolados?"}})
            prob = r["sistemico"]["noul"]
        except Exception as erro:
            print("Jev indisponível:", erro)
            continue
        if prob >= 0.7:
            novos.append({"aberto_em": datetime.datetime.now().isoformat(timespec="seconds"), "transportadora": g.transportadora,
                          "uf": g.uf, "envios_atrasados": int(g.atrasos), "dias_atraso_medio": round(float(g.dias_medio), 1),
                          "prob_sistemico_jev": round(prob, 2), "custo_rs": CUSTO_INCIDENTE, "status": "aberto (simulado)"})
    for reg in novos:
        sistemas.abrir_incidente_transportadora(reg)
    return pd.DataFrame(novos)

def rodar():
    """Ciclo completo. Devolve um dicionário com tudo o que o canal precisa mostrar."""
    importlib_main = __import__("importlib").import_module("1_unificar_portais").main
    importlib_main()  # atualiza saida/entregas.csv a partir dos portais
    casos = detectar()
    abertos, ja_existiam, sem_vaga = abrir_chamados(casos)
    op = casos[casos.tipo == "sem_rastreio"].merge(
        pd.read_csv(DADOS / "envios.csv", usecols=["envio_id", "cd_origem"]), on="envio_id")
    sistemas.registrar_pendencia_operacao(op)
    cx = casos_para_cx(casos)
    inc = avaliar_incidentes(casos)
    resumo = {"total_casos": len(casos), "por_tipo": casos.tipo.value_counts().to_dict(),
              "por_transportadora": casos.groupby("transportadora").size().to_dict(),
              "chamados_abertos_agora": len(abertos), "chamados_que_ja_existiam": ja_existiam,
              "chamados_para_amanha_por_limite_de_80_dia": sem_vaga,
              "envios_sem_rastreio_para_operacao_do_cd": len(op),
              "sem_rastreio_por_cd": op.cd_origem.value_counts().to_dict(),
              "clientes_para_cx": len(cx), "cx_por_motivo": cx.tipo.value_counts().to_dict(),
              "incidentes_abertos": inc.to_dict("records")}
    return resumo, cx

def redigir_resumo(resumo):
    sistema = ("Você é o agente de transportes da Gocase (torre de controle). Escreva em português, curto, para o canal de "
               "logística no Slack. Use SOMENTE os números recebidos. Explique siglas na primeira vez. Diga o que você JÁ FEZ "
               "sozinho (chamados, incidentes), o que precisa do CX (Customer Experience, o atendimento) e o que vai para a operação "
               "do CD (Centro de Distribuição: etiquetas sem rastreio). Máximo 9 linhas, "
               "sem markdown pesado. Diga que os chamados e incidentes são simulados (sem acesso aos portais).")
    try:
        return ia.chat([{"role": "system", "content": sistema},
                        {"role": "user", "content": json.dumps(resumo, ensure_ascii=False, default=str)}]).content
    except Exception as erro:
        print("Chat indisponível:", erro)
        return json.dumps(resumo, ensure_ascii=False, default=str)

if __name__ == "__main__":  # teste sem Slack
    r, cx = rodar()
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
