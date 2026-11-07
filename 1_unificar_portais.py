"""ETAPA 1 — Junta os 5 portais das transportadoras num formato único e liga cada envio à sua entrega.
Entrada: envios.csv + portal_*.csv (+ portal_vialog_codigos.csv)
Saída:   saida/entregas.csv (uma linha por envio, com data real de entrega e atraso)"""
import pandas as pd, numpy as np
from config import DADOS, SAIDA, INICIO_MEDICAO, dias_uteis_entre

def limpa_codigo(s):  # códigos vêm com espaço no meio e em minúsculas (dicionario_de_dados.md)
    return s.astype(str).str.replace(r"\s", "", regex=True).str.upper()

def ler_portais():
    p = []
    # Expressa Já: CSV, hora local
    x = pd.read_csv(DADOS / "portal_expressa_ja.csv")
    ev = x.evento.str.strip().str.upper()
    p.append(pd.DataFrame({"cod": limpa_codigo(x.codigo_rastreio), "quando": pd.to_datetime(x.data_hora),
                           "entregue": ev.str.startswith("ENTREGUE"), "falha": ev.str.contains("EXTRAVI|DEVOLV"),
                           "tentativa": ev.str.contains("TENTATIVA"), "portal": "Expressa Já"}))
    # Rápido Sul: ponto e vírgula, data DD/MM/AAAA
    x = pd.read_csv(DADOS / "portal_rapido_sul.csv", sep=";")
    ev = x.ocorrencia.str.strip().str.lower()
    p.append(pd.DataFrame({"cod": limpa_codigo(x.rastreio), "quando": pd.to_datetime(x.data_hora, format="%d/%m/%Y %H:%M"),
                           "entregue": ev.str.startswith("entregue"), "falha": ev.str.contains("extravi|devolu"),
                           "tentativa": ev.str.contains("não realizada"), "portal": "Rápido Sul"}))
    # TransNorte: status em código de texto
    x = pd.read_csv(DADOS / "portal_transnorte.csv")
    ev = x.status.str.strip().str.lower()
    p.append(pd.DataFrame({"cod": limpa_codigo(x.rastreio), "quando": pd.to_datetime(x.quando),
                           "entregue": ev.eq("entregue"), "falha": ev.isin(["extraviado", "devolvido"]),
                           "tentativa": ev.eq("tentativa_sem_sucesso"), "portal": "TransNorte"}))
    # ViaLog: código numérico (portal_vialog_codigos.csv): 50/55 entregue, 60/90 falha, 40/41 tentativa
    x = pd.read_csv(DADOS / "portal_vialog.csv")
    c = pd.to_numeric(x.cod_status, errors="coerce")
    p.append(pd.DataFrame({"cod": limpa_codigo(x.awb), "quando": pd.to_datetime(x.timestamp),
                           "entregue": c.isin([50, 55]), "falha": c.isin([60, 90]),
                           "tentativa": c.isin([40, 41]), "portal": "ViaLog"}))
    # Correio Ágil: hora em UTC -> converte para Brasília
    x = pd.read_csv(DADOS / "portal_correio_agil.csv")
    ev = x.descricao.str.strip().str.lower()
    q = pd.to_datetime(x.data_hora_utc, utc=True).dt.tz_convert("America/Sao_Paulo").dt.tz_localize(None)
    p.append(pd.DataFrame({"cod": limpa_codigo(x.objeto), "quando": q,
                           "entregue": ev.str.startswith("objeto entregue"), "falha": ev.str.contains("extravi|devolvido"),
                           "tentativa": ev.str.contains("não pode ser efetuada"), "portal": "Correio Ágil"}))
    return pd.concat(p, ignore_index=True).drop_duplicates()

def main():
    ev = ler_portais()
    por_cod = ev.groupby("cod").agg(
        entregue_em=("quando", lambda s: s[ev.loc[s.index, "entregue"]].min()),
        falha_em=("quando", lambda s: s[ev.loc[s.index, "falha"]].min()),
        tentativas=("tentativa", "sum"), eventos=("quando", "size"))
    env = pd.read_csv(DADOS / "envios.csv", dtype={"cep": str}, parse_dates=["data_pedido", "data_prometida"])
    env["cod"] = limpa_codigo(env.codigo_rastreio.fillna(""))
    env["cep8"] = env.cep.str.replace(r"\D", "", regex=True).str.zfill(8)  # CEP com hífen ou sem zero à esquerda
    env["prefixo3"] = env.cep8.str[:3]
    env = env.join(por_cod, on="cod")
    ok = env.entregue_em.notna()
    env.loc[ok, "dia_entrega"] = dias_uteis_entre(env.loc[ok, "data_pedido"], env.loc[ok, "entregue_em"])
    env["atraso_du"] = env.dia_entrega - env.prazo_exibido_dias_uteis
    env["no_prazo"] = np.where(ok, env.atraso_du <= 0, np.nan)
    antes = env.data_pedido < pd.Timestamp(INICIO_MEDICAO)  # portais só têm eventos a partir de 03/09
    env["situacao"] = np.select([ok, env.falha_em.notna(), env.codigo_rastreio.isna(), antes & env.eventos.isna(), env.eventos.isna()],
                                ["entregue", "falha", "sem_rastreio", "antes_dos_portais", "sem_evento_no_portal"], "em_transito")
    cols = ["envio_id", "marca", "cd_origem", "data_pedido", "uf", "cidade", "prefixo3", "peso_g", "valor_pedido",
            "transportadora", "codigo_rastreio", "prazo_exibido_dias_uteis", "data_prometida", "custo_frete_cotado",
            "frete_cobrado_cliente", "entregue_em", "falha_em", "tentativas", "dia_entrega", "atraso_du", "no_prazo", "situacao"]
    env[cols].to_csv(SAIDA / "entregas.csv", index=False)
    print(f"{len(env)} envios | situação: {env.situacao.value_counts().to_dict()}")
    print(f"Salvo em {SAIDA / 'entregas.csv'}")

if __name__ == "__main__":
    main()
