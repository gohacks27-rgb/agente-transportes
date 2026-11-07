"""Acesso à IA pelo AI Proxy do evento (https://ai-proxy.gogroupbr.com/v1).
- chat(): texto (redigir propostas, responder pessoas) — modelo mais forte ("latest").
- jev(): decisão tipada rápida (sim/não, escolha, nota) com probabilidade — endpoint /v1/systemone.
Toda chamada registra o consumo em saida/custos_ia.csv e imprime a linha 💲."""
import os, csv, json, datetime
import urllib.request, urllib.error
from dotenv import load_dotenv
from openai import OpenAI
from config import AQUI, SAIDA

load_dotenv(AQUI / ".env")
BASE = "https://ai-proxy.gogroupbr.com/v1"
_cliente = OpenAI(base_url=BASE, api_key=os.environ["AI_PROXY_API_KEY"], timeout=180)
_sessao = {"chamadas": 0, "tokens": 0, "usd": 0.0}

def _registrar(modelo, entrada, saida, usd=None):
    _sessao["chamadas"] += 1
    _sessao["tokens"] += (entrada or 0) + (saida or 0)
    if usd:
        _sessao["usd"] += usd
    preco = f"US$ {usd:.7f}" if usd else "sem preço por chamada"
    print(f"💲 {modelo} · entrada {entrada} tok · saída {saida} tok · {preco} | sessão: "
          f"{_sessao['chamadas']} chamadas, {_sessao['tokens']} tok, US$ {_sessao['usd']:.7f}")
    novo = not (SAIDA / "custos_ia.csv").exists()
    with open(SAIDA / "custos_ia.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["quando", "modelo", "tokens_entrada", "tokens_saida", "usd_estimado"])
        w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), modelo, entrada, saida, usd or ""])

def chat(mensagens, ferramentas=None, modelo="latest"):
    """Uma chamada de chat. Devolve a mensagem do modelo (pode conter tool_calls)."""
    kw = {"model": modelo, "messages": mensagens}
    if ferramentas:
        kw["tools"] = ferramentas
    r = _cliente.chat.completions.create(**kw)
    u = r.usage
    _registrar(r.model, getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None))
    return r.choices[0].message

def jev(estado, perguntas):
    """Decisão estruturada (Jev). `perguntas` no formato do /v1/systemone. Devolve o dicionário `answers`."""
    req = urllib.request.Request(
        f"{BASE}/systemone", method="POST",
        data=json.dumps({"state": estado, "questions": perguntas}, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {os.environ['AI_PROXY_API_KEY']}", "Content-Type": "application/json",
                 "Accept": "application/json",
                 # sem User-Agent próprio, a Cloudflare do proxy bloqueia o urllib (erro 403 / 1010)
                 "User-Agent": "agente-transportes/1.0 (python)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            dados = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as erro:  # mostra o motivo que o proxy devolveu
        raise RuntimeError(f"Jev HTTP {erro.code}: {erro.read().decode('utf-8', 'ignore')[:300]}") from None
    u = dados.get("usage", {})
    _registrar(dados.get("model", "jev"), u.get("input_tokens"), u.get("output_tokens"), u.get("estimated_cost_usd"))
    return dados["answers"]
