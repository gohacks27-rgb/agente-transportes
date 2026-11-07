# Lógica de funcionamento

Todos os parâmetros de negócio ficam em [`config.py`](../config.py) e vêm dos arquivos do desafio (`parametros.md`, `politicas.csv`).

## Parâmetros principais

| Parâmetro | Valor | Origem |
|---|---|---|
| Meta de cumprimento | 92% no prazo | politicas.csv |
| Evidência mínima | 30 envios por prefixo | politicas.csv |
| Prazo mínimo exibido | 2 dias úteis | politicas.csv |
| Máx. propostas por ciclo | 12 | politicas.csv |
| Custo de uma aprovação | R$ 15 | politicas.csv |
| Ganho mínimo para virar proposta | R$ 5.000/mês | decisão do projeto |
| Margem do produto | 55% | parametros.md |
| Amostra de envios | 1 em cada 6 | dicionario_de_dados.md |
| Janela de medição | pedidos de 03/09 a 23/10/2026 | corta pedidos recentes para não medir só as entregas rápidas |

**Dia útil:** o 1º dia útil depois do pedido é o dia 1; feriados nacionais são descontados (`dias_uteis_entre`).

## Etapa 1 — Unificar portais (`1_unificar_portais.py`)

Cada transportadora exporta um portal com colunas e formatos de código diferentes. A etapa limpa os códigos de rastreio, padroniza os status e liga cada envio de `envios.csv` à sua entrega real → `saida/entregas.csv` (uma linha por envio, com data de entrega e dias de atraso).

## Etapa 2 — Medir (`2_medir.py`)

Para cada **transportadora × prefixo de CEP (3 dígitos)**: nº de envios, % no prazo e os percentis do dia de entrega **p50, p80 e p92**, ao lado do SLA do contrato.

> **p92** = dia útil em que 92% das entregas já tinham chegado. É exatamente o prazo que cumpre a meta.

## Etapa 3 — Recomendar (`3_recomendar.py`)

Para cada prefixo:

```
prazo ideal      = max(p92 real da transportadora, 2)

ganho de vendas  = pedidos/mês × efeito do prazo na conversão × ticket × 55%
                   (só nos dias em que ficamos PIORES que a mediana dos concorrentes)

custo de atrasos = pedidos/mês × custo esperado de atraso com aquele prazo

impacto líquido  = ganho de vendas − aumento do custo de atrasos − diferença de frete (se trocar transportadora)
```

**Efeito do prazo na conversão** — medido com um *diferença-em-diferenças*: prefixos de SP que ganharam +2 dias de prazo em 01/10 contra o resto de SP/RJ/MG, setembro vs outubro (`checkout_conversao.csv`). O resultado é dividido por 2 para virar efeito por dia.

**Custo esperado de um atraso** (`config.custo_atraso`, de parametros.md):

| Dias de atraso | P(contato SAC) | P(cancelamento) |
|---|---|---|
| 0 | 0% | 0% |
| 1–2 | 35% | 4% |
| 3–5 | 60% | 10% |
| 6–10 | 85% | 22% |
| > 10 | 85% | 40% |

```
custo = P(contato) × R$ 9 + P(cancel) × (55% × valor + R$ 12 reversa) + perda de recompra (28% → 14%)
```

**Regras de decisão**

- Compara sempre com a regra vigente (inclui o que o gerente já aprovou).
- Só troca de transportadora se a atual não cumprir 92% nem com o prazo ajustado.
- **Evidência forte** = ≥ 30 envios da atual no prefixo e ≥ 30 da nova (no prefixo ou em UF + localidade). Evidência fraca vai para `saida/em_observacao.csv`.
- Prefixos vizinhos com a mesma mudança são agrupados numa proposta; ficam as 12 de maior ganho acima de R$ 5 mil/mês.

## Etapa 4 — Modelo de atraso (`4_modelo_atraso.py`)

- **Alvo:** o envio foi entregue depois do dia útil prometido.
- **Variáveis (todas conhecidas na hora do pedido):** transportadora, UF, prefixo, região, capital/interior, CD de origem, se saiu de CD de outra região, peso, valor, dia da semana, prazo prometido e nº de feriados no período (a regra do site não desconta os estaduais).
- **Algoritmo:** `HistGradientBoostingClassifier` (árvores de decisão em sequência, cada uma corrigindo a anterior).
- **Validação temporal:** treina com pedidos até 10/10, testa com 11/10 a 23/10.

| Métrica | Modelo | Regra simples (histórico transp. × prefixo) |
|---|---|---|
| AUC | 0,648 | 0,643 |
| Brier (menor é melhor) | 0,0677 | 0,0701 |
| Taxa de atraso no top 10% de risco | 16,2% | — (média geral 7,2%) |

AUC = chance de o modelo dar nota maior a um envio que atrasou do que a um que não atrasou. Brier = erro quadrático da probabilidade.

## Etapa 5 — Roteamento por envio (`5_rotear_envios.py`)

Problema de **programação inteira** resolvido com o solver HiGHS (`scipy.optimize.milp`):

```
x[e,t] ∈ {0,1}   envio e vai pela transportadora t (só candidatas cotadas)

minimizar   Σ x[e,t] × ( frete cotado[e,t] + P(atraso | e,t) × custo de um atraso )

sujeito a   Σ_t x[e,t] = 1                                   para todo envio e
            Σ_{e,t ≠ padrão} x[e,t] ≤ 40% × envios(UF, dia)  para cada UF e dia
```

O risco é calculado contra o prazo **já mostrado ao cliente** na compra. A chance de atraso vem do modelo da etapa 4.

## Torre de controle (`torre.py`)

Detecta, por envio, com base na exportação dos portais:

| Tipo | Regra | Quem age |
|---|---|---|
| `atraso` | em trânsito e passou da data prometida | agente abre chamado |
| `sem_rastreio` | pedido com 2+ dias sem código | operação do CD |
| `sem_evento` | coletado há 2+ dias úteis sem evento no portal | agente abre chamado |
| `extravio_devolucao` | extraviado/devolvido nos últimos 14 dias | agente + CX |
| `em_risco` (preventivo) | vence em ≤ 2 dias úteis e P(atraso) ≥ 20% | agente abre chamado |

- Chamados: sem duplicar os existentes, até 80 por dia.
- CX: extravio/devolução ou atraso ≥ 3 dias úteis.
- **Incidente sistêmico:** para transportadora × UF com 10+ atrasos, o Jev responde se é sistêmico; com probabilidade ≥ 70% abre incidente formal (R$ 15, máx. 2 por transportadora).

## Auditoria de frete (`auditoria.py`)

1. **CT-e** (Conhecimento de Transporte eletrônico): frete cobrado = tabela contratada vigente na data, pela faixa de CEP e peso taxado; reentrega só com tentativa registrada; devolução = % do contrato.
2. **Faturas:** soma dos CT-e e abatimento das indenizações aprovadas.
3. **Multa por atraso do contrato:** % do frete por dia útil acima do SLA, com teto — contado da coleta até a entrega, sem feriados nacionais nem estaduais do destino.
4. **Indenizações:** abre sozinho as que faltam (extravio/avaria); lista as negadas por "fotos insuficientes" para o CX recorrer; acompanha as aprovadas sem abatimento e as vencidas.

A cobrança das multas só acontece com aprovação do financeiro no Slack.
