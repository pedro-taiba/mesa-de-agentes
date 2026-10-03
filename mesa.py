"""
Mesa de agentes — paper trading.

Uma vez por dia, cinco agentes de IA analisam o ETF e votam.
A mesa só "entra" se 3 ou mais votarem comprar e o agente de Risco não vetar.
Cada decisão fica registada em historico.json e o resultado é medido
contra comprar e segurar. A compra real (só compra, nunca vende) está em
executor.py e só acontece com a variável MESA_EXECUTAR = sim.
"""

import json
import math
import os
import re
import sys

import datetime

import anthropic
import yfinance as yf

import executor

TICKER = os.environ.get("MESA_TICKER") or "VWCE.DE"
MODELO = "claude-haiku-4-5-20251001"

PASTA = os.path.dirname(os.path.abspath(__file__))
HISTORICO = os.path.join(PASTA, "historico.json")
TEMPLATE = os.path.join(PASTA, "template.html")
PAINEL = os.path.join(PASTA, "docs", "index.html")

FORMATO = """

Contexto da decisão: a mesa decide se fica comprada no ETF até a próxima
análise (um dia útil). "comprar" = entrar ou manter a posição hoje.
"esperar" = ficar fora hoje. "vender" = sinal claramente negativo, ficar fora.

Responda SÓ com um objeto JSON, sem texto antes ou depois:
{"voto": "comprar" | "esperar" | "vender", "motivo": "uma frase curta em português, até 25 palavras"}

Se receber "o_seu_historico", é o registo dos seus votos anteriores e do que o ETF fez
no dia seguinte. Use-o para se calibrar (por exemplo, se costuma errar quando vota
esperar), mas com poucos dias não tire conclusões fortes."""

AGENTES = [
    ("grafico", "Gráfico",
     "Você é o analista técnico de uma mesa de investimento. Avalie SÓ a tendência: "
     "posição do preço face às médias móveis, momentum (retornos de 5 e 20 dias) e volume. "
     "Não comente o risco nem a volatilidade, isso é trabalho de outro agente. "
     "Seja objetivo e não invente dados que não recebeu."),
    ("noticias", "Notícias",
     "Você é o analista de notícias de uma mesa de investimento. Avalie o sentimento "
     "das manchetes recebidas sobre os mercados de ações globais. Ignore manchetes sobre "
     "empresas isoladas que não mexem com o mercado como um todo. Se não houver manchetes "
     "relevantes, vote esperar. Não invente notícias."),
    ("macro", "Macro",
     "Você é o analista macroeconômico de uma mesa de investimento. Avalie o VIX face à "
     "sua média de 1 ano e a variação dos juros de 10 anos dos EUA. Um VIX muito acima da "
     "média ou uma subida brusca de juros é adverso; valores normais não são sinal de nada "
     "por si só. O dia da semana NÃO é informação útil: nunca o use. "
     "Não invente eventos do calendário que não constem nos dados."),
    ("risco", "Risco",
     "Você é o gestor de risco de uma mesa de investimento. Só recebe dados de risco: "
     "quedas recentes, maior queda diária, distância da máxima e volatilidade de curto "
     "prazo comparada com a de médio prazo. Critérios: vote 'vender' (veto) se houver queda "
     "superior a 3% em 5 dias, queda diária superior a 2% no último dia, ou volatilidade de "
     "20 dias acima de 1,5 vezes a de 60 dias. Vote 'esperar' se o risco estiver a subir mas "
     "abaixo desses limites. Vote 'comprar' se nada disso acontecer. Diga qual critério usou."),
]

DIABO = ("diabo", "Advogado do diabo",
         "Você é o advogado do diabo de uma mesa de investimento. Recebe os dados e os "
         "votos dos outros quatro agentes. Primeiro, encontre o argumento MAIS FORTE contra "
         "a opinião da maioria. Se esse argumento for razoável, vote contra a maioria. Só "
         "concorde com a maioria se o argumento contra for claramente fraco; nesse caso, o "
         "motivo tem de dizer qual era o argumento e porque é fraco.")

VOTOS_VALIDOS = {"comprar", "esperar", "vender"}


# ---------------------------------------------------------------- dados

def num(x, casas=2):
    try:
        x = float(x)
        return None if math.isnan(x) else round(x, casas)
    except (TypeError, ValueError):
        return None


def dados_mercado():
    df = yf.Ticker(TICKER).history(period="1y", auto_adjust=True)
    # Se correr durante o pregão, ignora a barra de hoje (ainda não é um fecho).
    agora = datetime.datetime.now(datetime.timezone.utc)
    if not df.empty and df.index[-1].date() == agora.date() and agora.hour < 17:
        df = df.iloc[:-1]
    if df.empty or len(df) < 60:
        sys.exit(f"Sem dados suficientes para {TICKER}. Confira o ticker.")
    c, v = df["Close"].dropna(), df["Volume"]
    rets = c.pct_change().dropna()

    def ret(n):
        return num((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100)

    media_vol = v.tail(20).mean()
    return df.index[-1].strftime("%Y-%m-%d"), {
        "ativo": TICKER,
        "data_ultimo_fecho": df.index[-1].strftime("%Y-%m-%d"),
        "fecho": num(c.iloc[-1]),
        "media_20d": num(c.tail(20).mean()),
        "media_50d": num(c.tail(50).mean()),
        "media_200d": num(c.tail(200).mean()) if len(c) >= 200 else None,
        "retorno_1d_%": ret(1),
        "retorno_5d_%": ret(5),
        "retorno_20d_%": ret(20),
        "maxima_20d": num(c.tail(20).max()),
        "minima_20d": num(c.tail(20).min()),
        "distancia_da_maxima_1a_%": num((c.iloc[-1] / c.max() - 1) * 100),
        "volatilidade_20d_anual_%": num(rets.tail(20).std() * math.sqrt(252) * 100),
        "volatilidade_60d_anual_%": num(rets.tail(60).std() * math.sqrt(252) * 100),
        "volume_vs_media_20d": num(v.iloc[-1] / media_vol) if media_vol else None,
        "ultimos_10_fechos": [num(x) for x in c.tail(10)],
        "maior_queda_diaria_20d_%": num(rets.tail(20).min() * 100),
    }


def dados_macro():
    out = {}
    for nome, t in (("vix", "^VIX"), ("juros_10a_eua_%", "^TNX")):
        try:
            c = yf.Ticker(t).history(period="1mo")["Close"].dropna()
            out[nome] = num(c.iloc[-1])
            out[nome + "_variacao_5d"] = num(c.iloc[-1] - c.iloc[-6])
            if nome == "vix":
                out["vix_media_1a"] = num(yf.Ticker(t).history(period="1y")["Close"].dropna().mean())
        except Exception:
            out[nome] = None
    return out


def manchetes():
    titulos = []
    for t in (TICKER, "SPY", "^STOXX50E"):
        try:
            for item in (yf.Ticker(t).news or [])[:8]:
                conteudo = item.get("content") or item
                titulo = conteudo.get("title")
                if titulo and titulo not in titulos:
                    titulos.append(titulo)
        except Exception:
            pass
    return titulos[:15]


# ---------------------------------------------------------------- agentes

def perguntar(cliente, sistema, dados):
    try:
        resp = cliente.messages.create(
            model=MODELO,
            max_tokens=300,
            system=sistema + FORMATO,
            messages=[{"role": "user", "content": json.dumps(dados, ensure_ascii=False, indent=1)}],
        )
        texto = "".join(b.text for b in resp.content if b.type == "text")
        achado = re.search(r"\{.*\}", texto, re.S)
        r = json.loads(achado.group(0)) if achado else {}
        voto = str(r.get("voto", "")).lower().strip()
        if voto not in VOTOS_VALIDOS:
            return {"voto": "esperar", "motivo": "Resposta inválida; voto neutro."}
        return {"voto": voto, "motivo": str(r.get("motivo", "")).strip()[:200]}
    except anthropic.AuthenticationError:
        sys.exit("Chave da API inválida. Confira o segredo ANTHROPIC_API_KEY no GitHub.")
    except Exception as e:
        return {"voto": "esperar", "motivo": f"Erro ao consultar o agente ({type(e).__name__}); voto neutro."}


TENDENCIA = ("ativo", "data_ultimo_fecho", "fecho", "media_20d", "media_50d", "media_200d",
             "retorno_5d_%", "retorno_20d_%", "maxima_20d", "minima_20d",
             "volume_vs_media_20d", "ultimos_10_fechos")
RISCO = ("ativo", "data_ultimo_fecho", "retorno_1d_%", "retorno_5d_%", "maior_queda_diaria_20d_%",
         "distancia_da_maxima_1a_%", "volatilidade_20d_anual_%", "volatilidade_60d_anual_%")


def historico_do_agente(hist, id_, n=8):
    """Votos anteriores do agente e o que o ETF fez no pregão seguinte."""
    feitos, acertos = [], 0
    for i in range(len(hist) - 1):
        v = hist[i].get("votos", {}).get(id_)
        if not v:
            continue
        r = hist[i + 1]["fecho"] / hist[i]["fecho"] - 1
        certo = (v["voto"] == "comprar") == (r > 0)
        acertos += certo
        feitos.append({"data": hist[i]["data"], "voto": v["voto"],
                       "etf_no_dia_seguinte_%": num(r * 100), "acertou": certo})
    if not feitos:
        return None
    return {"dias_avaliados": len(feitos), "acertos": acertos, "ultimos": feitos[-n:]}


def reunir_mesa(mercado, hist):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Falta o segredo ANTHROPIC_API_KEY no GitHub.")
    cliente = anthropic.Anthropic()
    macro, titulos = dados_macro(), manchetes()

    entradas = {
        "grafico": {k: mercado[k] for k in TENDENCIA if k in mercado},
        "noticias": {"manchetes": titulos or ["(sem manchetes disponíveis hoje)"]},
        "macro": macro,
        "risco": {k: mercado[k] for k in RISCO if k in mercado},
    }

    def com_historico(id_, dados):
        h = historico_do_agente(hist, id_)
        return {**dados, "o_seu_historico": h} if h else dados

    votos = {}
    for id_, _, sistema in AGENTES:
        votos[id_] = perguntar(cliente, sistema, com_historico(id_, entradas[id_]))
        print(f"{id_:>9}: {votos[id_]['voto']:<8} {votos[id_]['motivo']}")

    id_, _, sistema = DIABO
    votos[id_] = perguntar(cliente, sistema, com_historico(
        id_, {"mercado": mercado, "macro": macro, "votos_dos_outros": votos}))
    print(f"{id_:>9}: {votos[id_]['voto']:<8} {votos[id_]['motivo']}")
    return votos


def decidir(votos):
    compras = sum(1 for v in votos.values() if v["voto"] == "comprar")
    if votos["risco"]["voto"] == "vender":
        return "Esperar", "Risco vetou", compras
    if compras >= 3:
        return "Comprar", f"{compras} de 5 votaram comprar", compras
    return "Esperar", f"Só {compras} de 5 votaram comprar", compras


# ---------------------------------------------------------------- resultados

def calcular(hist):
    """Mede cada decisão pelo fecho seguinte (aproximação fecho a fecho)."""
    linhas, curva = [], []
    etf = mesa = 100.0
    acertos = dias = 0
    if hist:
        curva.append({"data": hist[0]["data"], "etf": 100.0, "mesa": 100.0})
    for i, h in enumerate(hist):
        base = {"data": h["data"], "decisao": h["decisao"], "compras": h["compras"],
                "ordem": h.get("ordem", "")}
        if i + 1 < len(hist):
            r = hist[i + 1]["fecho"] / h["fecho"] - 1
            dentro = h["decisao"] == "Comprar"
            etf *= 1 + r
            if dentro:
                mesa *= 1 + r
            dias += 1
            acertos += (dentro and r > 0) or (not dentro and r <= 0)
            curva.append({"data": hist[i + 1]["data"], "etf": round(etf, 3), "mesa": round(mesa, 3)})
            linhas.append({**base, "etf": r, "mesa": r if dentro else None, "pendente": False})
        else:
            linhas.append({**base, "etf": None, "mesa": None, "pendente": True})
    return {
        "linhas": linhas, "curva": curva,
        "resumo": {"dias": dias, "acertos": acertos,
                   "mesa_ret": mesa / 100 - 1, "etf_ret": etf / 100 - 1,
                   "mesa_final": mesa, "etf_final": etf},
    }


def pct(x):
    return f"{x * 100:+.2f}%".replace(".", ",")


def gerar_painel(hist):
    ultimo = hist[-1]
    dados = {"ativo": TICKER, "ultima": ultimo,
             "gasto_total": round(sum(h.get("gasto", 0) for h in hist), 2), **calcular(hist)}
    js = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read().replace("__DADOS__", js)
    os.makedirs(os.path.dirname(PAINEL), exist_ok=True)
    with open(PAINEL, "w", encoding="utf-8") as f:
        f.write(html)
    return {**dados, "historico": hist}


def resumo_no_github(dados):
    caminho = os.environ.get("GITHUB_STEP_SUMMARY")
    if not caminho:
        return
    u, r = dados["ultima"], dados["resumo"]
    nomes = {a[0]: a[1] for a in AGENTES + [DIABO]}
    linhas = [f"## Mesa de agentes — {u['data']}", "",
              f"**Decisão: {u['decisao']}** ({u['regra']})", "",
              f"**Trading 212:** {u.get('ordem', '—')}", "",
              "| Agente | Voto | Motivo |", "|---|---|---|"]
    for id_, v in u["votos"].items():
        linhas.append(f"| {nomes.get(id_, id_)} | {v['voto']} | {v['motivo']} |")
    gasto = sum(h.get("gasto", 0) for h in dados["historico"])
    linhas += ["", f"Total comprado pela mesa até hoje: {gasto:.2f} €".replace(".", ",")]
    if r["dias"]:
        linhas += ["", f"Em {r['dias']} dias de teste: mesa {pct(r['mesa_ret'])}, "
                       f"ETF parado {pct(r['etf_ret'])}. Acertou a direção em {r['acertos']} de {r['dias']} dias."]
    with open(caminho, "a", encoding="utf-8") as f:
        f.write("\n".join(linhas) + "\n")


# ---------------------------------------------------------------- principal

def main():
    hist = []
    if os.path.exists(HISTORICO):
        with open(HISTORICO, encoding="utf-8") as f:
            hist = json.load(f)

    data, mercado = dados_mercado()
    if hist and hist[-1]["data"] == data:
        print(f"Já existe análise para {data}; só atualizo o painel.")
    else:
        votos = reunir_mesa(mercado, hist)
        decisao, regra, compras = decidir(votos)
        print(f"Decisão: {decisao} ({regra})")
        ordem, gasto = executor.executar(decisao, mercado["fecho"], hist)
        print(f"Execução: {ordem}")
        hist.append({"data": data, "fecho": mercado["fecho"], "decisao": decisao,
                     "regra": regra, "compras": compras, "votos": votos,
                     "ordem": ordem, "gasto": gasto})
        with open(HISTORICO, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)

    resumo_no_github(gerar_painel(hist))


if __name__ == "__main__":
    main()
