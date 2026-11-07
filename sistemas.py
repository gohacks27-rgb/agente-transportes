"""CAMADA DE SISTEMAS — todas as ações do agente em sistemas externos ficam AQUI, e só aqui.

Hoje cada função é SIMULADA (grava em arquivos da pasta saida/), porque não temos API do site, do SIG/ERP
(Sistema Integrado de Gestão) nem dos portais das transportadoras. Para ligar o agente aos sistemas reais,
basta trocar o corpo destas funções pela chamada à API correspondente; o resto do agente não muda.

| Função                          | Sistema real (dono)                                   | Ferramenta equivalente (ferramentas.csv) |
|---------------------------------|-------------------------------------------------------|------------------------------------------|
| atualizar_regra_site()          | motor de frete do site / SIG (empresa)                | propor_mudanca + aprovar_proposta        |
| abrir_chamado_transportadora()  | portal ou API de chamados de cada transportadora      | abrir_chamado_portal                     |
| abrir_incidente_transportadora()| portal comercial / e-mail da transportadora           | abrir_incidente_transportadora           |
| registrar_pendencia_operacao()  | WMS (Warehouse Management System) / SIG (empresa)     | acionar_pessoa (operação do CD)          |
| abrir_pedido_indenizacao()      | SAC / portal de indenização da transportadora         | abrir_chamado_portal (extravio/avaria)   |
| registrar_cobranca_multa()      | financeiro: abatimento na próxima fatura de frete     | acionar_pessoa (financeiro)              |
"""
import csv, datetime
import pandas as pd
from config import DADOS, SAIDA

REGRAS_SIM = SAIDA / "regras_site_simulado.csv"

def _anexar(arquivo, cabecalho, linhas):
    novo = not arquivo.exists()
    with open(arquivo, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if novo:
            w.writerow(cabecalho)
        w.writerows(linhas)

def atualizar_regra_site(prefixos, prazo_novo, transportadora_nova):
    """SIMULADO. Real: chamada à API do motor de frete do site/SIG para mudar prazo e transportadora padrão dos prefixos."""
    if not REGRAS_SIM.exists():
        pd.read_csv(DADOS / "regras_site_prazo_frete_por_prefixo.csv", dtype=str).to_csv(REGRAS_SIM, index=False)
    r = pd.read_csv(REGRAS_SIM, dtype=str)
    alvo = r.prefixo_cep3.str.zfill(3).isin(prefixos)
    r.loc[alvo, "prazo_exibido_dias_uteis"] = str(prazo_novo)
    r.loc[alvo, "transportadora_padrao"] = transportadora_nova
    r.to_csv(REGRAS_SIM, index=False)
    return int(alvo.sum())

def abrir_chamado_transportadora(transportadora, envio_id, codigo_rastreio, motivo, dias_atraso, sequencia=1):
    """SIMULADO. Real: API/portal de chamados da transportadora (resposta em 4 a 24 h, ferramentas.csv). Devolve o protocolo."""
    agora = datetime.datetime.now()
    protocolo = f"SIM-{transportadora[:2].upper()}-{agora:%m%d%H%M}-{sequencia:03d}"
    _anexar(SAIDA / "chamados_abertos.csv",
            ["protocolo", "aberto_em", "envio_id", "transportadora", "codigo_rastreio", "motivo", "dias_atraso", "origem"],
            [[protocolo, agora.isoformat(timespec="seconds"), envio_id, transportadora, codigo_rastreio, motivo, dias_atraso, "agente (simulado)"]])
    return protocolo

def abrir_incidente_transportadora(registro):
    """SIMULADO. Real: incidente formal com a transportadora (prazo de resposta 24 h, politicas.csv; custo R$ 15)."""
    arq = SAIDA / "incidentes.csv"
    atual = pd.read_csv(arq) if arq.exists() else pd.DataFrame()
    pd.concat([atual, pd.DataFrame([registro])], ignore_index=True).to_csv(arq, index=False)

def registrar_pendencia_operacao(casos_sem_rastreio):
    """SIMULADO. Real: tarefa no WMS/SIG para a operação do CD reemitir a etiqueta. Grava a lista para a operação."""
    arq = SAIDA / "operacao_sem_rastreio.csv"
    casos_sem_rastreio.to_csv(arq, index=False)
    return arq

def rotear_envio(envio_id, transportadora, justificativa):
    """SIMULADO. Real: API do SIG/ERP para definir a transportadora do envio antes da postagem
    (ferramentas.csv: rotear_envio, papel analista_logistica, sem aprovação até 40% por UF/dia)."""
    _anexar(SAIDA / "roteamentos.csv", ["quando", "envio_id", "transportadora", "justificativa"],
            [[datetime.datetime.now().isoformat(timespec="seconds"), envio_id, transportadora, justificativa]])

def abrir_pedido_indenizacao(transportadora, envio_id, ocorrencia_id, tipo, valor):
    """SIMULADO. Real: pedido de indenização no SAC da transportadora (análise em até 30 dias, transportadoras.csv)."""
    agora = datetime.datetime.now()
    protocolo = f"SIM-IND-{agora:%m%d%H%M}-{ocorrencia_id}"
    _anexar(SAIDA / "indenizacoes_abertas.csv",
            ["protocolo", "aberto_em", "ocorrencia_id", "envio_id", "transportadora", "tipo", "valor_solicitado", "origem"],
            [[protocolo, agora.isoformat(timespec="seconds"), ocorrencia_id, envio_id, transportadora, tipo, round(float(valor), 2), "agente (simulado)"]])
    return protocolo

def registrar_cobranca_multa(multas, aprovado_por):
    """SIMULADO. Real: o financeiro lança a multa como abatimento na próxima fatura de cada transportadora."""
    agora = datetime.datetime.now().isoformat(timespec="seconds")
    _anexar(SAIDA / "cobrancas_multa.csv",
            ["registrado_em", "aprovado_por", "envio_id", "transportadora", "fatura_origem", "dias_acima_sla", "multa_rs"],
            [[agora, aprovado_por, r.envio_id, r.transportadora, r.fatura, r.dias_acima_sla, r.multa_rs] for r in multas.itertuples()])
    return multas.groupby("transportadora").multa_rs.sum().round(2).to_dict()
