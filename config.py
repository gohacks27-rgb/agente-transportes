"""Configuração comum: onde estão os dados, onde salvar e os parâmetros de negócio.
Fontes dos parâmetros: parametros.md e politicas.csv (pasta de dados do desafio)."""
from pathlib import Path
import numpy as np

AQUI = Path(__file__).resolve().parent
_CANDIDATAS = [AQUI / "dados",
               AQUI.parent / "drive-download-20261107T125331Z-1-001",
               Path("/mnt/user-data/uploads/Desktop/drive-download-20261107T125331Z-1-001")]
DADOS = next((p for p in _CANDIDATAS if (p / "envios.csv").exists()), _CANDIDATAS[0])
SAIDA = AQUI / "saida"
SAIDA.mkdir(exist_ok=True)

# Feriados nacionais da regra do site (parametros.md)
FERIADOS_NACIONAIS = np.array(["2026-09-07", "2026-10-12", "2026-11-02", "2026-11-15",
                               "2026-11-20", "2026-12-25", "2027-01-01"], dtype="datetime64[D]")

# Janela de medição: portais começam em 03/09 (dicionario_de_dados.md); portais exportados em 02/11.
# Cortamos pedidos depois de 23/10 para não medir só as entregas rápidas (as lentas ainda não chegaram).
INICIO_MEDICAO = "2026-09-03"
FIM_MEDICAO = "2026-10-23"

META_CUMPRIMENTO = 0.92          # politicas.csv
MIN_ENVIOS_PREFIXO = 30          # politicas.csv (envios em 4 semanas; aqui usamos a janela toda)
PRAZO_MINIMO = 2                 # politicas.csv
MAX_APROVACOES = 12              # politicas.csv
CUSTO_APROVACAO = 15.0           # politicas.csv
AMOSTRA = 6                      # envios.csv é 1 em cada 6 (dicionario_de_dados.md)

# Custo de atraso (parametros.md)
MARGEM_PRODUTO = 0.55
CONTATO_RS = 9.0
REVERSA_RS = 12.0
def custo_atraso(dias_atraso, valor_pedido):
    """Custo esperado (R$) de um envio entregue `dias_atraso` dias úteis depois do prometido."""
    d = np.asarray(dias_atraso)
    p_contato = np.select([d <= 0, d <= 2, d <= 5], [0, .35, .60], .85)
    p_cancel = np.select([d <= 0, d <= 2, d <= 5, d <= 10], [0, .04, .10, .22], .40)
    perda_recompra = np.where(d > 0, (0.28 - 0.14) * 2 * MARGEM_PRODUTO * valor_pedido, 0)
    return p_contato * CONTATO_RS + p_cancel * (MARGEM_PRODUTO * valor_pedido + REVERSA_RS) + perda_recompra

def dias_uteis_entre(inicio, fim):
    """Dia útil em que `fim` cai, contando o 1º dia útil depois de `inicio` como dia 1 (regra de parametros.md)."""
    a = (np.asarray(inicio, dtype="datetime64[D]") + 1)
    b = (np.asarray(fim, dtype="datetime64[D]") + 1)
    return np.busday_count(a, b, holidays=FERIADOS_NACIONAIS)

# Só vira proposta para o gerente a mudança com ganho estimado >= este valor (R$/mês).
# Abaixo disso não vale o clique do gerente nem os R$ 15 da aprovação; entra na próxima análise.
GANHO_MINIMO_PROPOSTA = 5000.0
