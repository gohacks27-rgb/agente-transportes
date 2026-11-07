"""Teste rápido do AI Proxy do evento: manda 'ping' e mostra resposta e tokens gastos."""
import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # lê o AI_PROXY_API_KEY do arquivo .env (nunca coloque o token no código)
client = OpenAI(base_url="https://ai-proxy.gogroupbr.com/v1", api_key=os.environ["AI_PROXY_API_KEY"])

r = client.chat.completions.create(
    model="latest",
    messages=[{"role": "user", "content": "Responda em uma frase: o que é SLA de entrega?"}],
)
print(r.choices[0].message.content)
u = r.usage
print(f"💲 {r.model} · entrada {u.prompt_tokens} tok · saída {u.completion_tokens} tok · sem preço por chamada")
