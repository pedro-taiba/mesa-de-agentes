"""
Execução real da mesa na Trading 212 — SÓ COMPRA, nunca vende.

Regra:
  - A mesa decidiu "Comprar"  -> compra VALOR_ORDEM €.
  - Último dia útil do mês e a mesa ainda não comprou nada nesse mês
    -> compra na mesma (o aporte do mês nunca se perde).
  - Para quando o total gasto pela mesa chegar a MAX_INVESTIDO.

Interruptor: só envia ordens se a variável MESA_EXECUTAR = sim.
Sem isso, apenas escreve o que faria (simulação).
"""

import datetime
import math
import os
import time

import requests

ISIN = os.environ.get("MESA_ISIN") or "IE00BK5BQT80"  # Vanguard FTSE All-World Acc (VWCE)
VALOR_ORDEM = 1.10     # € por compra (a Trading 212 exige cerca de 1 € no mínimo)
MAX_INVESTIDO = 3.50   # € no total que a mesa pode gastar

EXECUTAR = (os.environ.get("MESA_EXECUTAR") or "").strip().lower() == "sim"
MODO = (os.environ.get("T212_MODO") or "real").strip().lower()
BASE = {"real": "https://live.trading212.com/api/v0",
        "demo": "https://demo.trading212.com/api/v0"}.get(MODO)


def _eur(x):
    return f"{x:.2f} €".replace(".", ",")


def _ultimo_dia_util_do_mes(hoje):
    amanha = hoje + datetime.timedelta(days=1)
    while amanha.weekday() >= 5:
        amanha += datetime.timedelta(days=1)
    return amanha.month != hoje.month


def _api(metodo, caminho, **kw):
    r = requests.request(metodo, BASE + caminho,
                         auth=(os.environ["T212_KEY"], os.environ["T212_SECRET"]),
                         timeout=30, **kw)
    return r


def executar(decisao, preco, hist):
    """Devolve (texto para o histórico, valor gasto em €)."""
    hoje = datetime.date.today()
    gasto = sum(h.get("gasto", 0) for h in hist)
    comprou_este_mes = any(h.get("gasto", 0) > 0 and h["data"][:7] == hoje.strftime("%Y-%m")
                           for h in hist)

    if decisao == "Comprar":
        motivo = "mesa votou comprar"
    elif _ultimo_dia_util_do_mes(hoje) and not comprou_este_mes:
        motivo = "aporte do mês (último dia útil sem compra)"
    else:
        return "Sem compra hoje", 0

    if gasto + VALOR_ORDEM > MAX_INVESTIDO + 1e-9:
        return f"Limite atingido (já gastou {_eur(gasto)}; limite {_eur(MAX_INVESTIDO)})", 0

    if not EXECUTAR:
        return f"Simulação: compraria {_eur(VALOR_ORDEM)} ({motivo})", 0

    if BASE is None:
        return "ERRO: T212_MODO tem de ser 'real' ou 'demo'", 0
    for var in ("T212_KEY", "T212_SECRET"):
        if not os.environ.get(var):
            return f"ERRO: falta o segredo {var} no GitHub", 0

    try:
        r = _api("GET", "/equity/account/summary")
        if r.status_code != 200:
            return f"ERRO ao ler a conta ({r.status_code}): {r.text[:150]}", 0
        dinheiro = float(r.json()["cash"]["availableToTrade"])

        r = _api("GET", "/equity/metadata/instruments")
        if r.status_code != 200:
            return f"ERRO ao ler instrumentos ({r.status_code}): {r.text[:150]}", 0
        candidatos = [i for i in r.json() if i.get("isin") == ISIN]
        em_eur = [i for i in candidatos if i.get("currencyCode") == "EUR"]
        if not em_eur:
            return f"ERRO: não encontrei o ISIN {ISIN} em EUR na Trading 212", 0
        ticker = em_eur[0]["ticker"]

        for casas in (4, 3):
            qtd = math.ceil(VALOR_ORDEM / preco * 10**casas) / 10**casas
            custo = qtd * preco
            if custo * 1.02 > dinheiro:
                return f"Dinheiro insuficiente ({_eur(dinheiro)} livres)", 0
            time.sleep(1)
            r = _api("POST", "/equity/orders/market", json={"ticker": ticker, "quantity": qtd})
            if r.status_code == 200:
                return f"COMPROU {qtd} un. (~{_eur(custo)}) — {motivo}", round(custo, 2)
            if r.status_code == 400 and "precision" in r.text.lower():
                continue  # demasiadas casas decimais: tenta com menos
            return f"ERRO na ordem ({r.status_code}): {r.text[:150]}", 0
        return "ERRO: a Trading 212 recusou a quantidade", 0
    except Exception as e:
        return f"ERRO: {type(e).__name__}: {str(e)[:150]}", 0
