"""Lê a carteira da Trading 212 e grava carteira.json SÓ com percentagens.

Nenhum valor em euros nem quantidades saem daqui: o ficheiro é público.
"""
import datetime
import json
import os
import re
import sys

import requests

BASE = {"real": "https://live.trading212.com/api/v0",
        "demo": "https://demo.trading212.com/api/v0"}.get(os.environ.get("T212_MODO", "real"))
SAIDA = sys.argv[1] if len(sys.argv) > 1 else "carteira.json"


def api(caminho):
    r = requests.get(BASE + caminho, timeout=30,
                     auth=(os.environ["T212_KEY"], os.environ["T212_SECRET"]))
    r.raise_for_status()
    return r.json()


def curto(t):
    t = t.split("_")[0]                  # VWCEd_EQ -> VWCEd
    return re.sub(r"[a-z]+$", "", t) or t  # VWCEd -> VWCE


def main():
    agora = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="minutes")
    try:
        pos = api("/equity/portfolio")
        try:
            nomes = {i["ticker"]: i.get("shortName") or i.get("name") for i in api("/equity/metadata/instruments")}
        except Exception:
            nomes = {}
        linhas, investido, atual = [], 0.0, 0.0
        for p in pos:
            q, pm, pa = float(p.get("quantity") or 0), float(p.get("averagePrice") or 0), float(p.get("currentPrice") or 0)
            if q <= 0 or pm <= 0:
                continue
            investido += q * pm
            atual += q * pa
            linhas.append({"ticker": curto(p["ticker"]), "nome": nomes.get(p["ticker"]) or curto(p["ticker"]),
                           "pct": round((pa / pm - 1) * 100, 2), "_v": q * pa})
        for l in linhas:
            l["peso"] = round(l.pop("_v") / atual * 100, 1) if atual else 0
        linhas.sort(key=lambda l: -l["peso"])
        dados = {"atualizado": agora, "total_pct": round((atual / investido - 1) * 100, 2) if investido else None,
                 "posicoes": linhas}
    except Exception as e:
        codigo = getattr(getattr(e, "response", None), "status_code", None)
        dados = {"atualizado": agora, "erro": f"Sem ligação à Trading 212 ({codigo or type(e).__name__})", "posicoes": []}
    with open(SAIDA, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    print(json.dumps(dados, ensure_ascii=False))


if __name__ == "__main__":
    main()
