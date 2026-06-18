"""
CrossList Pro — AI Photo Analyzer
Analisa foto de produto e gera listagem completa como vendedor experiente.
"""
import anthropic, base64, json, re
from pathlib import Path

client = anthropic.Anthropic()

SELLER_PROMPT = """És um vendedor experiente em marketplaces portugueses (Vinted, OLX, Wallapop, eBay).
Analisas a foto do produto e crias uma listagem irresistível.

Responde APENAS em JSON válido, sem markdown, sem texto extra.

{
  "titulo": "título curto e directo (max 60 chars), inclui marca+modelo+característica chave",
  "marca": "marca do produto (ou 'Sem marca' se não identificável)",
  "categoria": "uma de: Componentes mobilidade | Travões | Motores e-bike | Cargo bike | Automotive | Electrodomésticos | Roupa | Electrónica | Desporto | Casa | Outro",
  "condicao": "one of: new | very_good | good | acceptable",
  "descricao_vinted": "descrição para Vinted (120-150 chars). Tom casual, honesto, emojis 1-2. Destaca estado e ponto diferenciador.",
  "descricao_olx": "descrição para OLX (150-200 chars). Tom neutro-profissional. Inclui condição, motivo de venda, disponibilidade.",
  "descricao_wallapop": "descrição para Wallapop (100-130 chars). Muito directo, jovem, inclui negociável ou não.",
  "descricao_ebay": "descrição para eBay (200-250 chars em inglês). Técnica, inclui compatibilidade se relevante, condição, envio.",
  "descricao_geral": "descrição base completa (200-300 chars). Profissional, todos os detalhes visíveis, estado, o que inclui.",
  "tags": ["5 a 8 tags relevantes em português"],
  "hook": "frase de abertura apelativa (max 80 chars) como se fosses o vendedor a chamar atenção — usa urgência ou valor",
  "notas_vendedor": "2-3 dicas rápidas de como optimizar esta venda nestes marketplaces",
  "condicao_visual": "observação honesta sobre o estado visível na foto"
}"""

async def analyze_photo(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Analisa imagem e retorna dados estruturados para listagem."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": SELLER_PROMPT}
            ]
        }]
    )
    
    raw = msg.content[0].text
    clean = re.sub(r"```json|```", "", raw).strip()
    return json.loads(clean)
