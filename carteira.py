"""Lê a carteira da conta Invest da Trading 212 e grava carteira.json.

Inclui valores reais em euros (valor da conta, ganho, dinheiro livre e cada
posição). Atenção: o ficheiro é público, tal como o painel.
"""
import datetime
import json
import os
import re
import sys

import requests

BASE = {"real": "https://live.trading212.com/api/v0",
        "demo": "https://demo.trading212.com/api/v0"}.get(os.environ.get("T212_MODO") or "real")
SAIDA = sys.argv[1] if len(sys.argv) > 1 else "carteira.json"


def api(caminho):
    r = requests.get(BASE + caminho, timeout=30,
                     auth=(os.environ["T212_KEY"], os.environ["T212_SECRET"]))
    r.raise_for_status()
    return r.json()


def curto(t):
    t = t.split("_")[0]                    # VWCEd_EQ -> VWCEd
    return re.sub(r"[a-z]+$", "", t) or t  # VWCEd -> VWCE


def cambios():
    """Quantas unidades de cada moeda vale 1 € (taxas do BCE)."""
    try:
        r = requests.get("https://api.frankfurter.dev/v1/latest?from=EUR", timeout=20)
        taxas = {**r.json()["rates"], "EUR": 1.0}
    except Exception:
        taxas = {"EUR": 1.0, "USD": 1.16, "GBP": 0.87}
    taxas["GBX"] = taxas.get("GBP", 0.87) * 100  # cotações em pence
    return taxas


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    agora = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="minutes")
    try:
        pos = api("/equity/portfolio")
        try:
            meta = {i["ticker"]: i for i in api("/equity/metadata/instruments")}
        except Exception:
            meta = {}
        try:
            resumo = api("/equity/account/summary")
        except Exception:
            resumo = {}
        fx = cambios()

        linhas, custo_total, valor_total = [], 0.0, 0.0
        for p in pos:
            q, pm, pa = num(p.get("quantity")) or 0, num(p.get("averagePrice")) or 0, num(p.get("currentPrice")) or 0
            if q <= 0 or pm <= 0:
                continue
            m = meta.get(p["ticker"], {})
            taxa = fx.get(m.get("currencyCode") or "EUR", 1.0)
            custo, valor = q * pm / taxa, q * pa / taxa
            custo_total += custo
            valor_total += valor
            linhas.append({"ticker": curto(p["ticker"]),
                           "nome": m.get("name") or m.get("shortName") or curto(p["ticker"]),
                           "valor": round(valor, 2), "ganho": round(valor - custo, 2),
                           "pct": round((pa / pm - 1) * 100, 2)})
        caixa = resumo.get("cash") or {}
        dinheiro = num(caixa.get("availableToTrade")) if isinstance(caixa, dict) else num(caixa)
        if dinheiro is None:
            dinheiro = num(resumo.get("free")) or 0.0
        conta_t212 = num(resumo.get("totalValue")) or num(resumo.get("total"))

        # Acerta pequenas diferenças de câmbio para bater com o valor que a app mostra
        if conta_t212 and valor_total:
            k = (conta_t212 - dinheiro) / valor_total
            if 0.9 < k < 1.1:
                for l in linhas:
                    custo_l = l["valor"] - l["ganho"]
                    l["valor"], l["ganho"] = round(l["valor"] * k, 2), round((l["valor"] - custo_l) * k, 2)
                valor_total, custo_total = valor_total * k, custo_total * k

        for l in linhas:
            l["peso"] = round(l["valor"] / valor_total * 100, 1) if valor_total else 0
        linhas.sort(key=lambda l: -l["valor"])

        conta = valor_total + dinheiro

        dados = {"atualizado": agora,
                 "conta": round(conta, 2), "investido": round(valor_total, 2), "dinheiro": round(dinheiro, 2),
                 "ganho": round(valor_total - custo_total, 2),
                 "total_pct": round((valor_total / custo_total - 1) * 100, 2) if custo_total else None,
                 "posicoes": linhas}
    except Exception as e:
        codigo = getattr(getattr(e, "response", None), "status_code", None)
        dados = {"atualizado": agora, "erro": f"Sem ligação à Trading 212 ({codigo or type(e).__name__})", "posicoes": []}
    with open(SAIDA, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    print(json.dumps(dados, ensure_ascii=False))


if __name__ == "__main__":
    main()
