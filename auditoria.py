"""AGENTE DE AUDITORIA DE FRETE E INDENIZAÇÕES — o 4º agente: fecha o ciclo depois da entrega (o dinheiro).

O que confere (todos os números saem do código; a IA não calcula nada):
  1. CT-e (Conhecimento de Transporte eletrônico, ctes.csv): frete cobrado = tabela contratada vigente na data do pedido,
     pela faixa de CEP e pelo peso taxado; reentrega = taxa do contrato e só com tentativa registrada;
     devolução = % do frete do contrato (transportadoras.csv).
  2. Faturas (faturas_frete.csv): soma dos CT-e e abatimento das indenizações aprovadas (indenizacoes.csv).
  3. Multa por atraso do contrato (transportadoras.csv: % do frete por dia útil acima do SLA, com teto), que as
     faturas nunca descontaram. Atraso contado da coleta (data_postagem) até a entrega no portal, em dias úteis,
     sem contar feriados nacionais nem os estaduais do destino (as transportadoras não entregam neles: feriados.csv).
  4. Indenizações: extravio/avaria sem pedido aberto (ocorrencias_entrega.csv); negadas por "fotos insuficientes"
     (dá para recorrer com fotos); aprovadas aguardando abatimento; em análise além do prazo do contrato.

O que faz sozinho: abre o pedido de indenização que faltava (simulado, sistemas.py).
O que passa para uma pessoa: cobrança de multa -> financeiro aprova (politicas.csv: "divergência de fatura de frete ->
acionar financeiro"); recurso de indenização negada -> CX junta as fotos.
Valores são da amostra de 1 em cada 6 envios (dicionario_de_dados.md)."""
import datetime
from functools import lru_cache
import numpy as np, pandas as pd
from config import DADOS, SAIDA, AMOSTRA
import sistemas

REFERENCIA = pd.Timestamp("2026-11-02 08:00")   # parametros.md
FAIXAS = [300, 1000, 2000, 5000, 10000]           # faixas de peso de todas as tabelas (dicionario_de_dados.md)
ARQ_MULTAS = SAIDA / "cobrancas_multa.csv"
ARQ_IND = SAIDA / "indenizacoes_abertas.csv"


# ---------------- tabelas de frete (5 formatos) ----------------
def _faixa(peso):
    return next((i for i, f in enumerate(FAIXAS) if peso <= f), len(FAIXAS) - 1)

class Tabelas:
    def __init__(self):
        cab = pd.read_csv(DADOS / "cabecas_cep.csv", dtype=str)
        self.loc5 = cab.set_index("prefixo_cep5").localidade.to_dict()
        self.loc3 = cab.groupby("prefixo_cep3").localidade.agg(lambda s: s.mode()[0]).to_dict()
        self.ej = pd.read_csv(DADOS / "tabela_frete_expressa_ja.csv", dtype={"cep_inicio": str, "cep_fim": str})
        tn = pd.read_csv(DADOS / "tabela_frete_transnorte.csv")
        tn["v"] = tn.valor.str.replace("R$", "", regex=False).str.replace(".", "", regex=False).str.replace(",", ".").astype(float)
        tn["fx"] = tn.faixa_peso.map({k: i for i, k in enumerate(["até 300 g", "301 g a 1 kg", "1 a 2 kg", "2 a 5 kg", "5 a 10 kg"])})
        self.tn = tn
        self.rs = pd.read_csv(DADOS / "tabela_frete_rapido_sul.csv", sep=";", dtype=str)
        self.vl = pd.read_csv(DADOS / "tabela_frete_vialog.csv", dtype={"cep3": str})
        self.ca = pd.read_csv(DADOS / "tabela_frete_correio_agil.csv")

    def _local(self, cep5):
        l = self.loc5.get(cep5) or self.loc3.get(cep5[:3]) or "interior"
        return "capital" if l in ("capital", "metropolitana") else "interior"

    @lru_cache(maxsize=None)
    def preco(self, transp, cep5, uf, fx, data):
        """Frete da tabela vigente em `data` (AAAA-MM-DD). As faixas de CEP das tabelas cabem em 5 dígitos."""
        if transp == "Expressa Já":
            c8 = cep5 + "000"
            t = self.ej[(self.ej.cep_inicio <= c8) & (self.ej.cep_fim >= c8) & (self.ej.vigente_desde <= data)]
            t = t[t.peso_max_g == FAIXAS[fx]]
            return float(t.sort_values("vigente_desde").valor_frete.iloc[-1]) if len(t) else np.nan
        if transp == "TransNorte":
            t = self.tn[(self.tn.uf == uf) & (self.tn.localidade == self._local(cep5).upper()) & (self.tn.fx == fx)]
            return float(t.v.iloc[-1]) if len(t) else np.nan
        if transp == "Rápido Sul":
            t = self.rs[(self.rs.cep5_inicio <= cep5) & (self.rs.cep5_fim >= cep5)]
            return float(t.iloc[0, 2 + fx].replace(",", ".")) if len(t) else np.nan
        if transp == "ViaLog":
            t = self.vl[(self.vl.cep3 == cep5[:3]) & (self.vl.vigencia_inicio <= data)
                        & (self.vl.vigencia_fim.isna() | (self.vl.vigencia_fim >= data))]
            t = t[t.peso_ate_g == FAIXAS[fx]]
            return float(t.valor.iloc[0]) if len(t) else np.nan
        if transp == "Correio Ágil":
            t = self.ca[(self.ca.uf == uf) & (self.ca.localidade == self._local(cep5)) & (self.ca.peso_ate_g == FAIXAS[fx])]
            return float(t.valor_frete.iloc[-1]) if len(t) else np.nan
        return np.nan


# ---------------- 1. CT-e e faturas ----------------
def conferir_ctes():
    c = pd.read_csv(DADOS / "ctes.csv")
    env = pd.read_csv(DADOS / "envios.csv", dtype={"cep": str}, usecols=["envio_id", "cep", "uf", "data_pedido"])
    dig = env.cep.str.replace(r"\D", "", regex=True)
    env["cep_ok"] = dig.str.len() == 8                   # CEP sem um dígito no ERP: não dá para saber a faixa
    env["cep5"] = dig.str.zfill(8).str[:5]
    n = c[c.tipo == "normal"].merge(env, on="envio_id", how="left")
    tab = Tabelas()
    ok = n[n.cep_ok.fillna(False)].copy()
    ok["tabela"] = [tab.preco(t, c5, u, _faixa(p), str(d)[:10])
                    for t, c5, u, p, d in zip(ok.transportadora, ok.cep5, ok.uf, ok.peso_taxado_g, ok.data_pedido)]
    ok["cobrado_a_mais"] = (ok.valor_frete_peso - ok.tabela).round(2)
    acima = ok[ok.cobrado_a_mais > 0.01]
    # complementares
    t = pd.read_csv(DADOS / "transportadoras.csv").set_index("transportadora")
    oc = pd.read_csv(DADOS / "ocorrencias_entrega.csv")
    tent = oc[oc.tipo.eq("tentativa_sem_sucesso")].groupby("envio_id").size()
    cp = c[c.tipo == "complementar"].copy()
    frete = n.set_index("envio_id").valor_frete_peso
    re = cp[cp.motivo_complemento == "reentrega"].copy()
    re["devido"] = re.transportadora.map(t.taxa_reentrega_rs)
    re["n"] = re.groupby("envio_id").cte_id.transform("size")
    re_erro = re[(re.valor_total - re.devido > 0.01) | (re.n > re.envio_id.map(tent).fillna(0))]
    dv = cp[cp.motivo_complemento == "devolucao"].copy()
    dv["devido"] = (dv.envio_id.map(frete) * dv.transportadora.map(t.frete_devolucao_pct_frete) / 100).round(2)
    dv_erro = dv[dv.valor_total - dv.devido > 0.05]
    # faturas
    f = pd.read_csv(DADOS / "faturas_frete.csv")
    soma = c.groupby("numero_fatura").valor_total.sum().round(2)
    ind = pd.read_csv(DADOS / "indenizacoes.csv")
    abat = ind[ind.status.eq("aprovada")].groupby("numero_fatura_abatimento").valor_aprovado.sum().round(2)
    f_erro = f[((f.valor_ctes - f.numero_fatura.map(soma)).abs() > 0.05)
               | ((f.valor_abatimento_indenizacoes - f.numero_fatura.map(abat).fillna(0)).abs() > 0.05)]
    return {"ctes_conferidos": int(len(ok) + len(cp)), "ctes_nao_conferidos_cep": int((~n.cep_ok.fillna(False)).sum()),
            "frete_acima_tabela_qtd": int(len(acima)), "frete_acima_tabela_rs": round(float(acima.cobrado_a_mais.sum()), 2),
            "complementos_errados_qtd": int(len(re_erro) + len(dv_erro)),
            "complementos_errados_rs": round(float((re_erro.valor_total - re_erro.devido).clip(lower=0).sum()
                                                   + (dv_erro.valor_total - dv_erro.devido).sum()), 2),
            "faturas_conferidas": int(len(f)), "faturas_com_erro": f_erro.numero_fatura.tolist()}, acima


# ---------------- 2. multa por atraso do contrato ----------------
def calcular_multas():
    e = pd.read_csv(SAIDA / "entregas.csv", dtype={"prefixo3": str}, parse_dates=["entregue_em"])
    e = e[e.situacao.eq("entregue")]
    e = e.merge(pd.read_csv(DADOS / "envios.csv", usecols=["envio_id", "data_postagem"], parse_dates=["data_postagem"]), on="envio_id")
    c = pd.read_csv(DADOS / "ctes.csv")
    cn = c[c.tipo.eq("normal")].set_index("envio_id")
    e["frete"], e["fatura"] = e.envio_id.map(cn.valor_frete_peso), e.envio_id.map(cn.numero_fatura)
    cab = pd.read_csv(DADOS / "cabecas_cep.csv", dtype=str)
    loc = cab.groupby("prefixo_cep3").localidade.agg(lambda s: s.mode()[0])
    e["localidade"] = np.where(e.prefixo3.map(loc).isin(["capital", "metropolitana"]), "capital_e_metropolitana", "interior")
    sla = pd.read_csv(DADOS / "transportadoras_cobertura_sla.csv").set_index(["transportadora", "uf", "localidade"]).sla_contratual_dias_uteis
    e["sla"] = [sla.get(k, np.nan) for k in zip(e.transportadora, e.uf, e.localidade)]
    e = e[e.data_postagem.notna() & e.sla.notna() & e.frete.notna()].copy()
    fer = pd.read_csv(DADOS / "feriados.csv", parse_dates=["data"])
    nac = fer[fer.abrangencia.eq("nacional")].data.values.astype("datetime64[D]")
    e["transito"] = 0
    for uf, g in e.groupby("uf"):
        h = np.concatenate([nac, fer[fer.uf.eq(uf)].data.values.astype("datetime64[D]")])
        a = g.data_postagem.values.astype("datetime64[D]") + np.timedelta64(1, "D")
        b = g.entregue_em.values.astype("datetime64[D]") + np.timedelta64(1, "D")
        e.loc[g.index, "transito"] = np.busday_count(a, b, holidays=h)
    e["dias_acima_sla"] = (e.transito - e.sla).clip(lower=0).astype(int)
    t = pd.read_csv(DADOS / "transportadoras.csv").set_index("transportadora")
    pct = np.minimum(e.transportadora.map(t.multa_atraso_pct_frete_por_dia_util) * e.dias_acima_sla,
                     e.transportadora.map(t.multa_atraso_teto_pct_frete))
    e["multa_rs"] = (pct / 100 * e.frete).round(2)
    a_vencer = set(pd.read_csv(DADOS / "faturas_frete.csv").query("status == 'a_vencer'").numero_fatura)
    e["fatura_a_vencer"] = e.fatura.isin(a_vencer)
    resumo = e.groupby("transportadora").agg(envios=("envio_id", "size"), fora_sla=("dias_acima_sla", lambda s: int((s > 0).sum())),
                                             cumprimento=("dias_acima_sla", lambda s: float((s == 0).mean())),
                                             multa_rs=("multa_rs", "sum")).reset_index()
    resumo["meta"] = resumo.transportadora.map(t.meta_cumprimento_sla_pct) / 100
    ja = set(pd.read_csv(ARQ_MULTAS).envio_id) if ARQ_MULTAS.exists() else set()
    devidas = e[(e.multa_rs > 0) & ~e.envio_id.isin(ja)]
    return resumo, devidas


# ---------------- 3. indenizações ----------------
def conferir_indenizacoes():
    ind = pd.read_csv(DADOS / "indenizacoes.csv", parse_dates=["aberta_em"])
    oc = pd.read_csv(DADOS / "ocorrencias_entrega.csv")
    exp = pd.read_csv(DADOS / "envios_expedicao.csv", usecols=["envio_id", "valor_declarado"])
    env = pd.read_csv(DADOS / "envios.csv", usecols=["envio_id", "order_id", "codigo_rastreio", "cidade", "uf"])
    t = pd.read_csv(DADOS / "transportadoras.csv").set_index("transportadora")
    ja = set(pd.read_csv(ARQ_IND).ocorrencia_id) if ARQ_IND.exists() else set()
    sem = oc[oc.tipo.isin(["extravio", "avaria"]) & ~oc.ocorrencia_id.isin(ind.ocorrencia_id) & ~oc.ocorrencia_id.isin(ja)]
    sem = sem.merge(exp, on="envio_id", how="left").merge(env, on="envio_id", how="left")
    sem["valor_declarado"] = np.minimum(sem.valor_declarado, sem.transportadora.map(t.seguro_incluso_ate_rs))
    neg = ind[ind.status.eq("negada") & ind.motivo_negativa.eq("fotos insuficientes")].merge(env, on="envio_id", how="left")
    ag = ind[ind.status.eq("aprovada") & ind.numero_fatura_abatimento.isna()]
    an = ind[ind.status.eq("em_analise")].copy()
    an["dias"] = (REFERENCIA - an.aberta_em).dt.days
    vencidas = an[an.dias > an.transportadora.map(t.prazo_analise_indenizacao_dias)]
    return sem, neg, ag, an, vencidas


# ---------------- ciclo ----------------
def rodar():
    """Roda a auditoria inteira. Abre sozinho os pedidos de indenização que faltam. Devolve o resumo para o Slack."""
    __import__("importlib").import_module("1_unificar_portais").main()   # atualiza saida/entregas.csv
    ctes, acima = conferir_ctes()
    resumo_sla, multas = calcular_multas()
    sem, neg, ag, an, vencidas = conferir_indenizacoes()
    abertas = [sistemas.abrir_pedido_indenizacao(r.transportadora, r.envio_id, r.ocorrencia_id, r.tipo, r.valor_declarado)
               for r in sem.itertuples()]
    multas.to_csv(SAIDA / "auditoria_multas.csv", index=False)
    neg.to_csv(SAIDA / "auditoria_recursos_fotos.csv", index=False)
    acima.to_csv(SAIDA / "auditoria_frete_acima_tabela.csv", index=False)
    abaixo = resumo_sla[resumo_sla.cumprimento < resumo_sla.meta]
    return {
        "multa_rs": round(float(multas.multa_rs.sum()), 2), "multa_envios": int(len(multas)),
        "multa_a_vencer_rs": round(float(multas[multas.fatura_a_vencer].multa_rs.sum()), 2),
        "multa_por_transportadora": multas.groupby("transportadora").multa_rs.sum().round(2).sort_values(ascending=False).to_dict(),
        "cumprimento_sla": {r.transportadora: round(r.cumprimento, 3) for r in resumo_sla.itertuples()},
        "transportadoras_abaixo_meta": abaixo.transportadora.tolist(), "meta_sla": float(resumo_sla.meta.max()),
        "recurso_fotos_qtd": int(len(neg)), "recurso_fotos_rs": round(float(neg.valor_solicitado.sum()), 2),
        "indenizacoes_abertas_agora": len(abertas), "indenizacoes_abertas_rs": round(float(sem.valor_declarado.sum()), 2),
        "aguardando_abatimento_qtd": int(len(ag)), "aguardando_abatimento_rs": round(float(ag.valor_aprovado.sum()), 2),
        "em_analise_qtd": int(len(an)), "em_analise_rs": round(float(an.valor_solicitado.sum()), 2),
        "em_analise_vencidas": int(len(vencidas)), **ctes, "amostra": AMOSTRA}

def registrar_cobranca(usuario):
    """Financeiro aprovou: registra a cobrança das multas (simulado) e não cobra os mesmos envios de novo."""
    m = pd.read_csv(SAIDA / "auditoria_multas.csv")
    return sistemas.registrar_cobranca_multa(m, usuario)

if __name__ == "__main__":
    import json
    print(json.dumps(rodar(), ensure_ascii=False, indent=2, default=str))
