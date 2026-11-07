"""Teste rápido do AI Proxy do evento: manda uma pergunta e mostra resposta e tokens gastos.
Na primeira vez, se não houver .env, pede o token e cria o arquivo sozinho."""
import os
from getpass import getpass
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

ENV = Path(__file__).with_name(".env")
load_dotenv(ENV)
if not os.environ.get("AI_PROXY_API_KEY"):
    token = getpass("Cole o token do AI Proxy (gw-tok-...) e aperte Enter (ele não aparece na tela): ").strip()
    ENV.write_text(f"AI_PROXY_API_KEY={token}\n", encoding="utf-8")
    os.environ["AI_PROXY_API_KEY"] = token
    print(f".env criado em {ENV}")

client = OpenAI(base_url="https://ai-proxy.gogroupbr.com/v1", api_key=os.environ["AI_PROXY_API_KEY"])
r = client.chat.completions.create(
    model="latest",
    messages=[{"role": "user", "content": "Responda em uma frase: o que é SLA de entrega?"}],
)
print(r.choices[0].message.content)
u = r.usage
print(f"💲 {r.model} · entrada {u.prompt_tokens} tok · saída {u.completion_tokens} tok · sem preço por chamada")
