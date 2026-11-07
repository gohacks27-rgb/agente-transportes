"""ETAPA 3 — Recomenda o prazo exibido (e, quando vale, a transportadora) para cada prefixo de CEP,
com o impacto estimado em R$ por mês, e monta as propostas para o gerente (máx. 12, politicas.csv).

Lógica por prefixo:
  prazo ideal = p92 real da transportadora (dia útil em que 92% das entregas já chegaram), mínimo 2 dias
  ganho de vendas  = pedidos/mês × efeito do prazo na conversão × ticket × margem 55%   (checkout_conversao.csv, parametros.md)
                     o efeito vale para os dias em que ficamos PIORES que a mediana dos concorrentes (concorrentes_prazos.csv)
  custo de atrasos = pedidos/mês × custo esperado de atraso com aquele prazo              (parametros.md)
  impacto líquido  = ganho de vendas − aumento do custo de atrasos (− diferença de frete, se trocar transportadora)
Entradas: saida/entregas.csv, saida/desempenho.csv, regras_site_prazo_frete_por_prefixo.csv, checkout_conversao.csv,
          concorrentes_prazos.csv, cotacoes_frete.csv
Saídas:   saida/recomendacoes_por_prefixo.csv, saida/propostas.csv (só evidência forte) e saida/em_observacao.csv
Regras de decisão:
  - compara com a regra vigente (saida/regras_site_simulado.csv, se o gerente já aprovou algo)
  - só troca de transportadora se a atual não cumprir a meta de 92% com o prazo ajustado
  - evidência forte = >= 30 envios da transportadora atual no prefixo e >= 30 da nova (prefixo ou UF+localidade)"""
import math
import pandas as pd, numpy as np
from config import (DADOS, SAIDA, INICIO_MEDICAO, FIM_MEDICAO, MIN_ENVIOS_PREFIXO, PRAZO_MINIMO,
                    MAX_APROVACOES, CUSTO_APROVACAO, MARGEM_PRODUTO, META_CUMPRIMENTO, GANHO_MINIMO_PROPOSTA, custo_atraso)

SP_MARGEM = ["010", "020", "030", "040", "050", "060", "070", "080", "090"]  # regras_site_geral.csv

def efeito_prazo_na_conversao(conv):
    """Variação relativa da conversão por dia útil a mais de prazo.
    Diferença-em-diferenças: prefixos de SP que ganharam +2 dias em 01/10 (regras_site_geral.csv)
    contra o resto de SP/RJ/MG, setembro vs outubro (checkout_conversao.csv)."""
    c = conv[conv.uf.isin(["SP", "RJ", "MG"])].copy()
    c["trat"] = c.prefixo3.isin(SP_MARGEM)
    c["per"] = np.where(c.data < "2026-10-01", "antes", "depois")
    c = c[c.data.between("2026-09-01", "2026-10-31")]
    t = c.groupby(["trat", "per"])[["pedidos", "sessoes_checkout"]].sum()
    taxa = t.pedidos / t.sessoes_checkout
    rel = (taxa[True, "depois"] / taxa[True, "antes"]) / (taxa[False, "depois"] / taxa[False, "antes"]) - 1
    return rel / 2  # 2 dias a mais

def main():
    e = pd.read_csv(SAIDA / "entregas.csv", dtype={"prefixo3": str}, parse_dates=["data_pedido"])
    d = pd.read_csv(SAIDA / "desempenho.csv", dtype={"prefixo3": str})
    # compara com a regra VIGENTE: a original, ou a já alterada pelas aprovações do gerente (simulada)
    arq_regras = SAIDA / "regras_site_simulado.csv"
    if not arq_regras.exists():
        arq_regras = DADOS / "regras_site_prazo_frete_por_prefixo.csv"
    print(f"Regra vigente lida de: {arq_regras.name}")
    regras = pd.read_csv(arq_regras, dtype={"prefixo_cep3": str})
    regras["prefixo3"] = regras.prefixo_cep3.str.zfill(3)
    conv = pd.read_csv(DADOS / "checkout_conversao.csv", dtype={"prefixo_cep3": str}, parse_dates=["data"])
    conv["prefixo3"] = conv.prefixo_cep3.str.zfill(3)

    elas = efeito_prazo_na_conversao(conv)
    print(f"Efeito do prazo na conversão: {elas:+.1%} de pedidos por dia útil a mais (DiD em SP, checkout_conversao.csv)")

    # volume (população inteira) e ticket por prefixo
    ult = conv[conv.data >= conv.data.max() - pd.Timedelta(days=27)]
    ped_mes = ult.groupby("prefixo3").pedidos.sum() * 30 / 28
    ticket = e.groupby("prefixo3").valor_pedido.mean()
    # frete por transportadora × prefixo (motor de cotação)
    cot = pd.read_csv(DADOS / "cotacoes_frete.csv").merge(e[["envio_id", "prefixo3"]], on="envio_id")
    frete = cot.groupby(["prefixo3", "transportadora"]).valor_cotado.mean()
    # concorrentes: mediana do prazo na coleta mais recente com produto disponível
    cc = pd.read_csv(DADOS / "concorrentes_prazos.csv", dtype={"cep": str})
    cc = cc[cc.estoque_exibido.eq("disponivel")]
    cc["prefixo3"] = cc.cep.str.zfill(8).str[:3]
    cc = cc[cc.data_coleta >= cc.data_coleta.max()[:8] + "01"]
    conc = cc.groupby("prefixo3").prazo_dias_uteis.median()

    # distribuições reais do dia de entrega, para calcular o custo esperado de atraso com qualquer prazo
    m = e[(e.situacao == "entregue") & e.data_pedido.between(INICIO_MEDICAO, FIM_MEDICAO + " 23:59")]
    dist_pref = m.groupby(["transportadora", "prefixo3"]).dia_entrega.apply(np.array)
    m_uf = m.merge(d[["prefixo3", "grupo_loc"]].drop_duplicates(), on="prefixo3", how="left")
    dist_uf = m_uf.groupby(["transportadora", "uf", "grupo_loc"]).dia_entrega.apply(np.array)

    def distribuicao(transp, p3, uf, gl):
        a = dist_pref.get((transp, p3))
        if a is not None and len(a) >= MIN_ENVIOS_PREFIXO:
            return a, "prefixo"
        b = dist_uf.get((transp, uf, gl))
        return (b, "UF+localidade") if b is not None and len(b) >= 10 else (None, "sem dados")

    linhas = []
    for r in regras.itertuples():
        p3, uf, atual, padrao = r.prefixo3, r.uf, int(r.prazo_exibido_dias_uteis), r.transportadora_padrao
        gl = "interior" if r.localidade == "interior" else "capital_e_metropolitana"
        n_mes, tk = ped_mes.get(p3, 0), ticket.get(p3, np.nan)
        if not n_mes or np.isnan(tk):
            continue
        pc = conc.get(p3, np.nan)
        def dias_pior(prazo):  # o cliente pune os dias acima do prazo dos concorrentes
            return max(0, prazo - pc) if not np.isnan(pc) else prazo
        def avaliar(transp, prazo):
            a, base = distribuicao(transp, p3, uf, gl)
            if a is None:
                return None
            pedidos = n_mes * max(0.3, 1 + elas * (dias_pior(prazo) - dias_pior(atual)))
            atraso = custo_atraso(a - prazo, tk).mean()
            return dict(pedidos=pedidos, custo_atraso=atraso, pct_no_prazo=(a <= prazo).mean(), envios_medidos=len(a),
                        frete=frete.get((p3, transp), np.nan), base=base, p92=float(np.quantile(a, .92)))
        base_atual = avaliar(padrao, atual)
        if base_atual is None:
            continue
        def valor(op):  # margem mensal: venda − atrasos − frete pago à transportadora
            return op["pedidos"] * (MARGEM_PRODUTO * tk - op["custo_atraso"] - (op["frete"] if not np.isnan(op["frete"]) else 0))
        v0 = valor(base_atual)
        opcoes = []
        for transp in d[d.prefixo3.eq(p3)].transportadora.unique():
            a, _ = distribuicao(transp, p3, uf, gl)
            if a is None:
                continue
            ideal = max(PRAZO_MINIMO, math.ceil(np.quantile(a, .92)))
            op = avaliar(transp, ideal)
            if op is None or (transp != padrao and np.isnan(op["frete"])):
                continue
            opcoes.append(dict(transportadora=transp, prazo=ideal, ganho_mes=valor(op) - v0, **op))
        if not opcoes:
            continue
        mesma = next((o for o in opcoes if o["transportadora"] == padrao), None)
        cumprem = [o for o in opcoes if o["pct_no_prazo"] >= META_CUMPRIMENTO]
        melhor = max(cumprem or opcoes, key=lambda o: o["ganho_mes"])
        # Troca de transportadora só quando ela melhora o SERVIÇO: a atual não cumpre a meta, ou a nova
        # permite mostrar um prazo menor. Troca só para economizar frete não é proposta (fica com a atual).
        if mesma and melhor is not mesma:
            atual_cumpre = mesma["pct_no_prazo"] >= META_CUMPRIMENTO
            if atual_cumpre and melhor["prazo"] >= mesma["prazo"]:
                melhor = mesma
        # evidência forte (politicas.csv: mínimo de 30 envios):
        #   - o problema da transportadora ATUAL medido no próprio prefixo (>= 30 envios ali), e
        #   - a transportadora NOVA com >= 30 envios medidos (no prefixo ou na mesma UF + localidade)
        falhas = []
        if base_atual["base"] != "prefixo":
            falhas.append(f"menos de {MIN_ENVIOS_PREFIXO} envios da {padrao} no prefixo")
        if melhor["transportadora"] != padrao and melhor["envios_medidos"] < MIN_ENVIOS_PREFIXO:
            falhas.append(f"só {melhor['envios_medidos']} envios medidos da {melhor['transportadora']} na região")
        evidencia = "fraca" if falhas else "forte"
        motivo = "; ".join(falhas)
        tipo = ("roteamento_padrao" if melhor["transportadora"] != padrao else
                "prazo_exibido" if melhor["prazo"] != atual else "manter")
        linhas.append(dict(prefixo3=p3, uf=uf, cidade=r.cidade_referencia, localidade=r.localidade,
                           transportadora_atual=padrao, prazo_atual=atual,
                           pct_no_prazo_atual=round(base_atual["pct_no_prazo"], 3),
                           envios_medidos_atual=base_atual["envios_medidos"], envios_medidos_nova=melhor["envios_medidos"],
                           tipo=tipo, transportadora_nova=melhor["transportadora"], prazo_novo=melhor["prazo"],
                           pct_no_prazo_novo=round(melhor["pct_no_prazo"], 3), p92_real=melhor["p92"],
                           prazo_concorrentes=conc.get(p3, np.nan), pedidos_mes=round(n_mes),
                           ganho_estimado_mes_rs=round(melhor["ganho_mes"], 0), base_da_medida=melhor["base"],
                           evidencia=evidencia, motivo_evidencia=motivo))
    rec = pd.DataFrame(linhas)
    rec.to_csv(SAIDA / "recomendacoes_por_prefixo.csv", index=False)

    # agrupa prefixos com a mesma mudança numa proposta só (politicas.csv aceita lista de prefixos)
    mud_todas = rec[rec.tipo.ne("manter") & (rec.ganho_estimado_mes_rs > CUSTO_APROVACAO)]
    obs = mud_todas[mud_todas.evidencia.eq("fraca")]
    obs.to_csv(SAIDA / "em_observacao.csv", index=False)
    mud = mud_todas[mud_todas.evidencia.eq("forte")]
    g = mud.groupby(["tipo", "uf", "transportadora_atual", "transportadora_nova", "prazo_atual", "prazo_novo"]).agg(
        prefixos=("prefixo3", lambda s: "; ".join(sorted(s))), cidades=("cidade", lambda s: ", ".join(sorted(set(s)))),
        pedidos_mes=("pedidos_mes", "sum"), envios_medidos_atual=("envios_medidos_atual", "sum"),
        envios_medidos_nova=("envios_medidos_nova", "max"), base_da_medida=("base_da_medida", lambda s: ", ".join(sorted(set(s)))),
        pct_no_prazo_atual=("pct_no_prazo_atual", "mean"),
        pct_no_prazo_novo=("pct_no_prazo_novo", "mean"), prazo_concorrentes=("prazo_concorrentes", "median"),
        ganho_estimado_mes_rs=("ganho_estimado_mes_rs", "sum")).reset_index()
    g = g[g.ganho_estimado_mes_rs >= GANHO_MINIMO_PROPOSTA]  # ganho pequeno não vale a aprovação (config.py)
    g = g.sort_values("ganho_estimado_mes_rs", ascending=False).head(MAX_APROVACOES)
    g.insert(0, "proposta_id", [f"P{i+1:02d}" for i in range(len(g))])
    g.round(3).to_csv(SAIDA / "propostas.csv", index=False)
    print(f"{len(rec)} prefixos avaliados | {len(mud_todas)} com mudança que compensa | {len(mud)} com evidência forte "
          f"| {len(g)} propostas (máx. {MAX_APROVACOES}) | {len(obs)} prefixos em observação (evidência fraca)")
    print(g[["proposta_id", "tipo", "uf", "prefixos", "transportadora_nova", "prazo_atual", "prazo_novo",
             "pct_no_prazo_atual", "pct_no_prazo_novo", "ganho_estimado_mes_rs"]].to_string(index=False))
    print(f"Ganho estimado total: R$ {g.ganho_estimado_mes_rs.sum():,.0f}/mês")

if __name__ == "__main__":
    main()
