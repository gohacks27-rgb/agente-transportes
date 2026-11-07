"""ETAPA 2 — Mede o desempenho real de cada transportadora por prefixo de CEP (3 primeiros dígitos).
Entrada: saida/entregas.csv (etapa 1) + regras_site_prazo_frete_por_prefixo.csv (localidade) + transportadoras_cobertura_sla.csv (SLA)
Saída:   saida/desempenho.csv  — transportadora × prefixo: envios, % no prazo, p50/p80/p92 do dia de entrega, SLA do contrato
p92 = dia útil em que 92% das entregas já tinham chegado (a meta de politicas.csv é 92% no prazo)."""
import pandas as pd, numpy as np
from config import DADOS, SAIDA, INICIO_MEDICAO, FIM_MEDICAO, MIN_ENVIOS_PREFIXO

REGIAO = {**dict.fromkeys(["SP", "RJ", "MG", "ES"], "SE"), **dict.fromkeys(["PR", "SC", "RS"], "S"),
          **dict.fromkeys(["DF", "GO", "MT", "MS"], "CO"),
          **dict.fromkeys(["AM", "PA", "AC", "RO", "RR", "AP", "TO"], "N")}

def resumo(g):
    d = g.dia_entrega
    return pd.Series({"envios": len(g), "pct_no_prazo": g.no_prazo.mean(),
                      "p50": d.quantile(.5), "p80": d.quantile(.8), "p92": d.quantile(.92),
                      "prazo_exibido_medio": g.prazo_exibido_dias_uteis.mean()})

def main():
    e = pd.read_csv(SAIDA / "entregas.csv", dtype={"prefixo3": str}, parse_dates=["data_pedido"])
    e = e[(e.situacao == "entregue") & e.data_pedido.between(INICIO_MEDICAO, FIM_MEDICAO + " 23:59")]
    regras = pd.read_csv(DADOS / "regras_site_prazo_frete_por_prefixo.csv", dtype={"prefixo_cep3": str})
    regras["prefixo3"] = regras.prefixo_cep3.str.zfill(3)
    loc = regras.set_index("prefixo3").localidade
    e["localidade"] = e.prefixo3.map(loc).fillna("interior")
    e["grupo_loc"] = np.where(e.localidade.eq("interior"), "interior", "capital_e_metropolitana")
    e["regiao"] = e.uf.map(REGIAO).fillna("NE")

    # nível 1: transportadora × prefixo
    pref = e.groupby(["transportadora", "prefixo3"]).apply(resumo, include_groups=False).reset_index()
    first = e.groupby("prefixo3")[["uf", "grupo_loc", "regiao"]].first()
    pref = pref.join(first, on="prefixo3")
    # nível 2 (para prefixos com pouco volume): transportadora × UF × capital/interior
    ufl = e.groupby(["transportadora", "uf", "grupo_loc"]).apply(resumo, include_groups=False).add_suffix("_uf").reset_index()
    pref = pref.merge(ufl, on=["transportadora", "uf", "grupo_loc"], how="left")
    pouco = pref.envios < MIN_ENVIOS_PREFIXO
    pref["base_da_medida"] = np.where(pouco, "UF+localidade (prefixo com poucos envios)", "prefixo")
    for c in ["pct_no_prazo", "p50", "p80", "p92"]:
        pref[c + "_usado"] = np.where(pouco, pref[c + "_uf"], pref[c])

    # SLA do contrato (transportadoras_cobertura_sla.csv) para comparar promessa x realidade
    sla = pd.read_csv(DADOS / "transportadoras_cobertura_sla.csv")
    sla = sla[sla.atende.eq("sim")][["transportadora", "uf", "localidade", "sla_contratual_dias_uteis"]]
    sla = sla.rename(columns={"localidade": "grupo_loc"})
    pref = pref.merge(sla, on=["transportadora", "uf", "grupo_loc"], how="left")
    pref["prazo_pela_regra"] = 1 + pref.sla_contratual_dias_uteis      # 1 dia de separação + SLA (parametros.md)
    pref["dias_alem_do_sla"] = pref.p92_usado - pref.prazo_pela_regra  # >0: transportadora entrega pior que o contrato

    pref.round(3).to_csv(SAIDA / "desempenho.csv", index=False)
    vol = pref[pref.envios >= MIN_ENVIOS_PREFIXO]
    print(f"{len(pref)} combinações transportadora × prefixo | {len(vol)} com >= {MIN_ENVIOS_PREFIXO} envios")
    print(f"No prazo (geral): {e.no_prazo.mean():.1%} | combinações com volume abaixo da meta: {(vol.pct_no_prazo < .92).sum()}")
    print("Piores (com volume):")
    print(vol.nsmallest(8, "pct_no_prazo")[["transportadora", "prefixo3", "uf", "envios", "pct_no_prazo", "p92", "prazo_exibido_medio", "sla_contratual_dias_uteis"]].to_string(index=False))
    print(f"Salvo em {SAIDA / 'desempenho.csv'}")

if __name__ == "__main__":
    main()
