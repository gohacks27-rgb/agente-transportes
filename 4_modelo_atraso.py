"""ETAPA 4 (ML) — Modelo que prevê a CHANCE DE UM ENVIO ATRASAR.

Para que serve:
  - Torre de controle PREVENTIVA: aponta envios em trânsito com risco alto antes de vencer o prazo.
  - Roteamento por envio: compara o risco do mesmo envio com cada transportadora candidata.

Dados de treino: envios entregues de pedidos feitos entre 03/09 e 23/10/2026 (saida/entregas.csv, que vem de
envios.csv + portal_*.csv) — o mesmo recorte das etapas 2 e 3, para não medir só as entregas rápidas.
Alvo: atrasou (entregue depois do dia útil prometido).
Variáveis (todas conhecidas na hora do pedido): transportadora, UF, prefixo de CEP, região, capital/interior,
CD de origem, se saiu do CD de outra região, peso, valor, dia da semana do pedido, prazo prometido e
quantos feriados estaduais/nacionais caem no período (feriados.csv: a regra do site não desconta os estaduais).
Algoritmo: HistGradientBoostingClassifier (scikit-learn) — árvores de decisão em sequência, cada uma corrigindo a anterior.
Validação: treina com pedidos até 10/10 e testa com pedidos de 11/10 a 23/10 (dados que o modelo não viu),
comparando com a regra simples de hoje (% de atraso histórico da transportadora no prefixo).
Saídas: saida/modelo_atraso.joblib (modelo) e saida/modelo_atraso_relatorio.txt (métricas)."""
import joblib
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score, brier_score_loss
from config import DADOS, SAIDA, INICIO_MEDICAO, FIM_MEDICAO

CATEGORICAS = ["transportadora", "uf", "prefixo3", "regiao", "grupo_loc", "cd_origem"]
NUMERICAS = ["peso_g", "valor_pedido", "dia_semana", "prazo_exibido_dias_uteis", "cd_outra_regiao",
             "feriado_estadual_no_prazo", "feriado_nacional_no_prazo"]
CORTE_TREINO = "2026-10-10 23:59"
REGIAO = {**dict.fromkeys(["SP", "RJ", "MG", "ES"], "SE"), **dict.fromkeys(["PR", "SC", "RS"], "S"),
          **dict.fromkeys(["DF", "GO", "MT", "MS"], "CO"), **dict.fromkeys(["AM", "PA", "AC", "RO", "RR", "AP", "TO"], "N")}

def preparar(df):
    """Monta as variáveis do modelo. Serve para treino e para envios novos/em trânsito."""
    x = df.copy()
    loc = pd.read_csv(DADOS / "regras_site_prazo_frete_por_prefixo.csv", dtype={"prefixo_cep3": str})
    loc = loc.assign(prefixo3=loc.prefixo_cep3.str.zfill(3)).set_index("prefixo3").localidade
    x["prefixo3"] = x.prefixo3.astype(str).str.zfill(3)
    x["regiao"] = x.uf.map(REGIAO).fillna("NE")
    x["grupo_loc"] = np.where(x.prefixo3.map(loc).fillna("interior").eq("interior"), "interior", "capital")
    cd_reg = np.where(x.regiao.isin(["NE", "N"]), "CD1", "CD2")
    x["cd_outra_regiao"] = (x.cd_origem != cd_reg).astype(int)
    x["dia_semana"] = pd.to_datetime(x.data_pedido).dt.dayofweek
    # feriados entre o pedido e a data prometida (feriados.csv): as transportadoras param, a regra do site só desconta os nacionais
    fer = pd.read_csv(DADOS / "feriados.csv", parse_dates=["data"])
    ped = pd.to_datetime(x.data_pedido).dt.normalize()
    fim = ped + pd.to_timedelta(x.prazo_exibido_dias_uteis.astype(float) * 1.4 + 2, unit="D")  # janela aproximada em dias corridos
    est = fer[fer.abrangencia.eq("estadual")]
    nac = fer[fer.abrangencia.eq("nacional")]
    x["feriado_estadual_no_prazo"] = [int(((est.uf == u) & est.data.between(a, b)).sum()) for u, a, b in zip(x.uf, ped, fim)]
    x["feriado_nacional_no_prazo"] = [int(nac.data.between(a, b).sum()) for a, b in zip(ped, fim)]
    return x

def codificar(x, categorias):
    """Transforma texto em códigos inteiros (o HistGradientBoosting trata como categoria). Valor novo vira -1 (ausente)."""
    m = pd.DataFrame(index=x.index)
    for c in CATEGORICAS:
        mapa = {v: i for i, v in enumerate(categorias[c])}
        m[c] = x[c].astype(str).map(mapa).astype(float)  # valor novo vira NaN (tratado como ausente)
    for c in NUMERICAS:
        m[c] = x[c].astype(float)
    return m

def treinar():
    e = pd.read_csv(SAIDA / "entregas.csv", dtype={"prefixo3": str}, parse_dates=["data_pedido"])
    extra = pd.read_csv(DADOS / "envios.csv", usecols=["envio_id", "cd_origem"])
    e = e.drop(columns=[c for c in ["cd_origem"] if c in e.columns]).merge(extra, on="envio_id")
    e = e[(e.situacao == "entregue") & e.data_pedido.between(INICIO_MEDICAO, FIM_MEDICAO + " 23:59")]
    x = preparar(e)
    y = (x.atraso_du > 0).astype(int)
    treino = x.data_pedido <= CORTE_TREINO
    categorias = {c: sorted(x.loc[treino, c].astype(str).unique()) for c in CATEGORICAS}
    X = codificar(x, categorias)
    modelo = HistGradientBoostingClassifier(categorical_features=[X.columns.get_loc(c) for c in CATEGORICAS],
                                            max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                            l2_regularization=1.0, random_state=42)
    modelo.fit(X[treino], y[treino])
    p = modelo.predict_proba(X[~treino])[:, 1]
    # regra simples de hoje: % de atraso histórico da transportadora no prefixo (no treino), com reserva na transportadora
    hist = y[treino].groupby([x.loc[treino, "transportadora"], x.loc[treino, "prefixo3"]]).mean()
    hist_t = y[treino].groupby(x.loc[treino, "transportadora"]).mean()
    base = [hist.get((t, pf), hist_t.get(t, y[treino].mean())) for t, pf in zip(x.loc[~treino, "transportadora"], x.loc[~treino, "prefixo3"])]
    yt = y[~treino]
    top = np.argsort(-p)[: int(len(p) * 0.10)]
    linhas = [
        "MODELO DE ATRASO — relatório de validação",
        f"Treino: {treino.sum()} envios (pedidos 03/09 a 10/10) | Teste: {(~treino).sum()} envios (pedidos 11/10 a 23/10)",
        f"Taxa real de atraso no teste: {yt.mean():.1%}",
        f"AUC do modelo: {roc_auc_score(yt, p):.3f}   | AUC da regra simples (histórico transportadora×prefixo): {roc_auc_score(yt, base):.3f}",
        "  (AUC = chance de o modelo dar nota maior a um envio que atrasou do que a um que não atrasou; 0,5 = sorteio, 1 = perfeito)",
        f"Brier do modelo: {brier_score_loss(yt, p):.4f} | Brier da regra simples: {brier_score_loss(yt, base):.4f}  (erro da probabilidade; menor é melhor)",
        f"Nos 10% de envios com maior risco previsto, {yt.iloc[top].mean():.1%} atrasaram de fato (vs {yt.mean():.1%} na média)",
        f"Probabilidade média prevista: {p.mean():.1%} (bem calibrado se perto da taxa real)",
    ]
    # re-treina com todos os dados para uso no dia a dia
    categorias = {c: sorted(x[c].astype(str).unique()) for c in CATEGORICAS}
    X = codificar(x, categorias)
    modelo.fit(X, y)
    joblib.dump({"modelo": modelo, "categorias": categorias}, SAIDA / "modelo_atraso.joblib")
    (SAIDA / "modelo_atraso_relatorio.txt").write_text("\n".join(linhas), encoding="utf-8")
    print("\n".join(linhas))
    print(f"Modelo salvo em {SAIDA / 'modelo_atraso.joblib'}")

def prever(df):
    """Probabilidade de atraso para envios (DataFrame com as colunas de envios.csv/entregas.csv)."""
    pacote = joblib.load(SAIDA / "modelo_atraso.joblib")
    X = codificar(preparar(df), pacote["categorias"])
    return pacote["modelo"].predict_proba(X)[:, 1]

if __name__ == "__main__":
    treinar()
