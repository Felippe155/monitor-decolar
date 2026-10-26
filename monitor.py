"""
Monitor de preço de pacote Decolar — versão nuvem (GitHub Actions).
Roda uma checagem por execução; o GitHub executa a cada 10 minutos.
"""

import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ============================== CONFIG ==============================
URL = "https://www.decolar.com/f/j1ExQJB"
HOTEL = "Torremayor"
PRECO_LIMITE = 1500.00
PARAR_EM = date(2026, 12, 12)      # depois dessa data o monitor para sozinho
AVISAR_APOS_FALHAS = 6             # 6 falhas seguidas = ~1h sem conseguir ler
# ====================================================================

# Credenciais vêm dos "Secrets" do GitHub (ficam escondidas)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WHATSAPP_FONE = os.getenv("WHATSAPP_FONE", "")       # ex.: +5511999998888
WHATSAPP_APIKEY = os.getenv("WHATSAPP_APIKEY", "")   # chave do CallMeBot

PASTA = Path(__file__).parent
HISTORICO = PASTA / "historico_precos.csv"
ESTADO = PASTA / "estado.json"
BRT = timezone(timedelta(hours=-3))


def agora():
    return datetime.now(BRT)


def log(msg):
    print(f"[{agora():%d/%m %H:%M}] {msg}", flush=True)


def brl(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def para_float(txt):
    return float(txt.replace(".", "").replace(",", "."))


def extrair_preco_por_pessoa(texto):
    t = " ".join(texto.split())
    for padrao in (r"por pessoa\D{0,40}?R\$\s*([\d.]+(?:,\d{1,2})?)",
                   r"R\$\s*([\d.]+(?:,\d{1,2})?)\D{0,40}?por pessoa"):
        m = re.search(padrao, t, re.I)
        if m:
            return para_float(m.group(1))
    valores = [para_float(v) for v in re.findall(r"R\$\s*([\d.]+(?:,\d{1,2})?)", t)]
    valores = [v for v in valores if v >= 300]
    return min(valores) if valores else None


JS_CARD = """
(el) => {
  let n = el;
  for (let i = 0; i < 15 && n; i++) {
    const t = n.innerText || "";
    if (/R\\$/.test(t)) return t;
    n = n.parentElement;
  }
  return null;
}
"""


def checar_preco():
    with sync_playwright() as p:
        # usa o Google Chrome que já vem instalado no servidor do GitHub
        browser = p.chromium.launch(channel="chrome", headless=True,
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="pt-BR", timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()
        preco = None
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_function("() => /R\\$\\s*\\d/.test(document.body.innerText)",
                                   timeout=120_000)
            page.wait_for_timeout(5_000)

            alvo = None
            if HOTEL:
                padrao = re.compile(re.escape(HOTEL), re.I)
                for _ in range(12):
                    loc = page.get_by_text(padrao)
                    if loc.count() > 0:
                        alvo = loc.first
                        break
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(2_000)

            if alvo is not None:
                texto = alvo.evaluate(JS_CARD)
            else:
                log(f"Hotel '{HOTEL}' não encontrado; usando a página inteira.")
                texto = page.inner_text("body")
            preco = extrair_preco_por_pessoa(texto) if texto else None
            return preco
        except PWTimeout:
            log("Tempo esgotado carregando a página (lentidão ou bloqueio).")
            return None
        finally:
            if preco is None:
                salvar_diagnostico(page)
            browser.close()


def salvar_diagnostico(page):
    """Guarda print e texto da página quando a leitura falha."""
    try:
        d = PASTA / "debug"
        d.mkdir(exist_ok=True)
        (d / "url.txt").write_text(page.url, encoding="utf-8")
        (d / "titulo.txt").write_text(page.title(), encoding="utf-8")
        (d / "texto.txt").write_text(page.inner_text("body")[:8000], encoding="utf-8")
        page.screenshot(path=str(d / "pagina.png"), full_page=False)
    except Exception as e:
        log(f"Falha ao salvar diagnóstico: {e}")


def enviar_alerta(mensagem):
    enviado = False
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            dados = json.dumps({"chat_id": TELEGRAM_CHAT_ID,
                                "text": f"{mensagem}\n\n{URL}"}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data=dados, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30)
            enviado = True
            log("Alerta enviado no Telegram.")
        except Exception as e:
            log(f"Falha no Telegram: {e}")
    if WHATSAPP_FONE and WHATSAPP_APIKEY:
        try:
            q = urllib.parse.urlencode({"phone": WHATSAPP_FONE,
                                        "text": f"{mensagem}\n\n{URL}",
                                        "apikey": WHATSAPP_APIKEY})
            urllib.request.urlopen(f"https://api.callmebot.com/whatsapp.php?{q}", timeout=30)
            enviado = True
            log("Alerta enviado no WhatsApp.")
        except Exception as e:
            log(f"Falha no WhatsApp: {e}")
    if not enviado:
        log("Nenhum alerta enviado: confira os Secrets do repositório.")
    return enviado


def carregar_estado():
    try:
        return json.loads(ESTADO.read_text())
    except Exception:
        return {"ultimo_alerta": None, "falhas": 0}


def main():
    if "--testar-alerta" in sys.argv:
        ok = enviar_alerta("✅ Teste do monitor Decolar: os alertas estão funcionando!")
        sys.exit(0 if ok else 1)

    if "COLE_AQUI" in URL:
        sys.exit("Cole a URL da Decolar na variável URL do monitor.py.")
    if agora().date() >= PARAR_EM:
        log("Data da viagem chegou; monitor encerrado. Pode desativar o workflow.")
        return

    est = carregar_estado()
    try:
        preco = checar_preco()
    except Exception as e:
        log(f"Erro inesperado: {e}")
        preco = None

    novo = not HISTORICO.exists()
    with HISTORICO.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["data_hora", "preco_por_pessoa"])
        w.writerow([agora().strftime("%Y-%m-%d %H:%M"), f"{preco:.2f}" if preco else ""])

    if preco is None:
        est["falhas"] = est.get("falhas", 0) + 1
        log(f"Não consegui ler o preço ({est['falhas']} falha(s) seguidas).")
        if est["falhas"] == AVISAR_APOS_FALHAS:
            enviar_alerta("⚠️ Monitor Decolar: há 1 hora não consigo ler o preço. "
                          "Confira se a URL ainda abre o pacote.")
    else:
        est["falhas"] = 0
        log(f"Preço por pessoa: {brl(preco)}")
        if preco < PRECO_LIMITE:
            ultimo = est.get("ultimo_alerta")
            if ultimo is None or preco < ultimo:
                if enviar_alerta(f"✈️ BAIXOU! Santiago 12/12–15/12, hotel {HOTEL}: "
                                 f"{brl(preco)} por pessoa (limite {brl(PRECO_LIMITE)}). "
                                 "Corre pra comprar!"):
                    est["ultimo_alerta"] = preco
        else:
            est["ultimo_alerta"] = None

    ESTADO.write_text(json.dumps(est))


if __name__ == "__main__":
    main()
