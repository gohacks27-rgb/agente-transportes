"""ETAPA 5 (PO) — Escolhe a transportadora de CADA envio novo antes da postagem (papel do analista, politicas.csv).

Problema de otimização (programação inteira, resolvido com o solver HiGHS via scipy.optimize.milp):
  Decisão: x[e,t] = 1 se o envio e vai pela transportadora t (só as candidatas cotadas em cotacoes_frete.csv).
  Custo de cada opção = frete cotado + chance de atraso (modelo de ML, 4_modelo_atraso.py) × custo de um atraso
                        (parametros.md: contato, cancelamento, recompra perdida — com o atraso médio observado).
  Objetivo: minimizar o custo total.
  Restrições (politicas.csv):
    - cada envio vai por exatamente uma transportadora;
    - por UF e dia, no máximo 40% dos envios fora da transportadora padrão (acima disso precisaria de aprovação).
  O prazo já foi mostrado ao cliente na compra (envios.csv), então o risco é calculado contra ESSE prazo.
Entrada: envios.csv (sem data_postagem = ainda não postado), cotacoes_frete.csv, regra vigente, modelo de atraso.
Saída: saida/roteamento_envios.csv e o registro das ações em saida/roteamentos.csv (via sistemas.py, simulado)."""
import importlib
import numpy as np, pandas as pd
from scipy import sparse
from scipy.optimize import milp, LinearConstraint, Bounds
from config import DADOS, SAIDA, custo_atraso
import sistemas

LIMITE_DESVIO = 0.40  # politicas.csv

def regra_vigente():
    arq = SAIDA / "regras_site_simulado.csv"
    r = pd.read_csv(arq if arq.exists() else DADOS / "regras_site_prazo_frete_por_prefixo.csv", dtype={"prefixo_cep3": str})
    return r.assign(prefixo3=r.prefixo_cep3.str.zfill(3)).set_index("prefixo3").transportadora_padrao

def montar_opcoes():
    modelo = importlib.import_module("4_modelo_atraso")
    env = pd.read_csv(DADOS / "envios.csv", dtype={"cep": str}, parse_dates=["data_pedido"])
    env = env[env.data_postagem.isna()].copy()                       # ainda não postados
    env["prefixo3"] = env.cep.str.replace(r"\D", "", regex=True).str.zfill(8).str[:3]
    env["padrao"] = env.prefixo3.map(regra_vigente())
    cot = pd.read_csv(DADOS / "cotacoes_frete.csv")[["envio_id", "transportadora", "valor_cotado"]]
    op = env.drop(columns=["transportadora"]).merge(cot, on="envio_id")  # uma linha por envio × candidata
    op["prob_atraso"] = modelo.prever(op)                              # ML: risco com ESSA transportadora
    e = pd.read_csv(SAIDA / "entregas.csv")
    atraso_medio = e.loc[e.atraso_du > 0, "atraso_du"].mean()          # atraso médio de quem atrasa (dias úteis)
    op["custo_atraso_se_atrasar"] = custo_atraso(round(atraso_medio), op.valor_pedido)
    op["custo_total"] = op.valor_cotado + op.prob_atraso * op.custo_atraso_se_atrasar
    op["dia"] = op.data_pedido.dt.date.astype(str)
    return op.reset_index(drop=True), atraso_medio

def otimizar(op):
    """Programação inteira com o solver HiGHS que vem dentro do SciPy (instalado junto com o scikit-learn)."""
    n = len(op)
    c = op.custo_total.to_numpy()                              # custo de cada opção (envio × transportadora)
    linhas, cols, vals, lo, hi = [], [], [], [], []
    r = 0
    for _, idx in op.groupby("envio_id").groups.items():       # 1 transportadora por envio: soma = 1
        for i in idx:
            linhas.append(r); cols.append(i); vals.append(1.0)
        lo.append(1); hi.append(1); r += 1
    for (uf, dia), g in op.groupby(["uf", "dia"]):             # no máx. 40% fora da padrão por UF e dia
        fora = [i for i in g.index if g.transportadora[i] != g.padrao[i]]
        if not fora:
            continue
        for i in fora:
            linhas.append(r); cols.append(i); vals.append(1.0)
        lo.append(0); hi.append(int(np.floor(LIMITE_DESVIO * g.envio_id.nunique()))); r += 1
    A = sparse.csr_matrix((vals, (linhas, cols)), shape=(r, n))
    res = milp(c, constraints=LinearConstraint(A, lo, hi), integrality=np.ones(n), bounds=Bounds(0, 1),
               options={"time_limit": 120})
    op["escolhida"] = np.round(res.x).astype(int) if res.x is not None else 0
    return {0: "ótimo", 1: "limite de tempo (melhor solução encontrada)"}.get(res.status, f"falhou ({res.message})")

def main():
    op, atraso_medio = montar_opcoes()
    status = otimizar(op)
    esc = op[op.escolhida == 1]
    # cenário de comparação: tudo pela transportadora padrão (o que aconteceria sem o agente desde 02/11)
    pad = op[op.transportadora == op.padrao].drop_duplicates("envio_id")
    sem_padrao = op.envio_id.nunique() - pad.envio_id.nunique()
    trocados = esc[esc.transportadora != esc.padrao]
    # exemplos por pedido (as 3 trocas que mais reduzem o custo total) para a mensagem do Slack
    ref = pad.set_index("envio_id")[["transportadora", "valor_cotado", "prob_atraso", "custo_total"]]
    t = trocados.join(ref, on="envio_id", rsuffix="_pad").dropna(subset=["transportadora_pad"])
    t = t.assign(ganho=t.custo_total_pad - t.custo_total).nlargest(3, "ganho")
    exemplos = [{"order_id": str(r.order_id), "uf": r.uf, "cidade": r.cidade, "de": r.transportadora_pad, "para": r.transportadora,
                 "risco_de": float(r.prob_atraso_pad), "risco_para": float(r.prob_atraso),
                 "frete_dif_rs": float(r.valor_cotado - r.valor_cotado_pad)} for r in t.itertuples()]
    res = {
        "exemplos": exemplos,
        "status_otimizacao": status, "envios": int(op.envio_id.nunique()),
        "trocados_da_padrao": int(len(trocados)), "pct_trocados": round(len(trocados) / max(1, len(esc)), 3),
        "frete_total_padrao_rs": round(pad.valor_cotado.sum(), 2), "frete_total_otimizado_rs": round(esc.valor_cotado.sum(), 2),
        "atrasos_esperados_padrao": round(pad.prob_atraso.sum(), 1), "atrasos_esperados_otimizado": round(esc.prob_atraso.sum(), 1),
        "custo_total_padrao_rs": round(pad.custo_total.sum(), 2), "custo_total_otimizado_rs": round(esc.custo_total.sum(), 2),
        "envios_sem_padrao_cotada": int(sem_padrao), "atraso_medio_considerado_du": round(atraso_medio, 1),
        "trocas_por_rota": {f"{u}: {p} -> {t}": int(q) for (u, p, t), q in trocados.groupby(["uf", "padrao", "transportadora"]).size().sort_values(ascending=False).head(6).items()},
    }
    cols = ["envio_id", "order_id", "uf", "cidade", "prefixo3", "padrao", "transportadora", "valor_cotado", "prob_atraso", "custo_total"]
    esc[cols].to_csv(SAIDA / "roteamento_envios.csv", index=False)
    for r in esc.itertuples():
        sistemas.rotear_envio(r.envio_id, r.transportadora, f"padrão {r.padrao}; risco {r.prob_atraso:.0%}; frete R$ {r.valor_cotado:.2f}")
    return res

if __name__ == "__main__":
    import json
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=str))
