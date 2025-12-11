import os
import sys
import time
import math
from typing import Tuple, Optional

import psycopg2
from psycopg2.extras import DictCursor
from pybit.unified_trading import HTTP

# ==========================
# KONFIGURÁCIÓ (ENV VAROK)
# ==========================

SYMBOL = "SOLUSDT"

# Bybit API kulcsok (Railway-en env var-ként)
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")

# Telegram értesítések (ugyanaz a csoport mehet, mint a jelző botnál)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")  # pl. -5025566211

# Postgres – Railway DATABASE_URL, pl:
# postgres://user:pass@hostname:port/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Stratégiaparaméterek
DIP_PERCENT = float(os.getenv("DIP_PERCENT", "5.0"))   # 5% esésnél vesz
TP_PERCENT = float(os.getenv("TP_PERCENT", "4.0"))     # 4% emelkedésnél ad el
ORDER_USDT = float(os.getenv("ORDER_USDT", "25.0"))   # 25 USDT / vétel
MIN_POSITION_USDT = float(os.getenv("MIN_POSITION_USDT", "5.0"))  # ekkora érték felett tekintjük "van pozíció"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))

# Mennyiség lépésköz – Bybit qtyStep SOL-nál tipikusan 0.001 vagy 0.01
# ha hibát ad a tőzsde, ezt az értéket kell igazítani!
QTY_STEP = float(os.getenv("QTY_STEP", "0.001"))


# ==========================
# SEGÉDFÜGGVÉNYEK
# ==========================

def send_telegram(text: str) -> None:
    """Egyszerű Telegram üzenetküldő."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Nincs Telegram beállítva, nem küldök üzenetet.")
        return

    import requests  # csak itt importáljuk, hogy requirements-ben egyszerű maradjon

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[ERR] Telegram hiba: {e}", file=sys.stderr)


def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL nincs beállítva")
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=DictCursor)
    conn.autocommit = True
    return conn


def get_base_price(conn, symbol: str) -> Optional[float]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT base_price FROM sol_bot_state WHERE symbol = %s LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()
        return float(row["base_price"]) if row else None


def set_base_price(conn, symbol: str, price: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sol_bot_state (symbol, base_price, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (symbol)
            DO UPDATE SET base_price = EXCLUDED.base_price,
                          updated_at = EXCLUDED.updated_at
            """,
            (symbol, price),
        )


def quantize_qty(qty: float) -> float:
    """Mennyiség igazítása a lot step-hez."""
    if qty <= 0:
        return 0.0
    steps = math.floor(qty / QTY_STEP)
    return round(steps * QTY_STEP, 6)


def safe_float(v) -> float:
    """
    Bybit néha üres stringgel vagy None-nal tér vissza.
    Ez a helper mindig ad egy használható floatot.
    """
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


# ==========================
# BYBIT KLIENS
# ==========================

def create_bybit_session() -> HTTP:
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        raise RuntimeError("BYBIT_API_KEY / BYBIT_API_SECRET nincs beállítva")
    session = HTTP(
        testnet=False,  # ha teszten akarod: True és változó url ha kell
        api_key=BYBIT_API_KEY,
        api_secret=BYBIT_API_SECRET,
    )
    return session


def get_spot_price(session: HTTP, symbol: str) -> float:
    """Aktuális spot ár lekérése."""
    resp = session.get_tickers(category="spot", symbol=symbol)
    if resp.get("retCode") != 0:
        raise RuntimeError(f"Bybit tickers hiba: {resp}")
    lst = resp["result"]["list"]
    if not lst:
        raise RuntimeError("Bybit tickers: üres lista")
    last_price = float(lst[0]["lastPrice"])
    return last_price


def get_balances(session: HTTP) -> Tuple[float, float]:
    """
    Visszaadja: (sol_total, usdt_available)
    accountType=UNIFIED feltételezve.
    safe_float-tal védve az üres stringek ellen.
    """
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="SOL,USDT")
    if resp.get("retCode") != 0:
        raise RuntimeError(f"Bybit wallet-balance hiba: {resp}")

    sol_total = 0.0
    usdt_available = 0.0

    for acct in resp["result"]["list"]:
        for coin in acct.get("coin", []):
            c = coin["coin"]

            wallet_balance = safe_float(coin.get("walletBalance"))
            available_to_withdraw = safe_float(coin.get("availableToWithdraw"))

            if c == "SOL":
                sol_total = wallet_balance
            elif c == "USDT":
                usdt_available = available_to_withdraw

    return sol_total, usdt_available


def place_market_order(
    session: HTTP, side: str, qty: float
) -> dict:
    """Egyszerű market order spoton."""
    qty_adj = quantize_qty(qty)
    if qty_adj <= 0:
        raise RuntimeError("qty_adj <= 0, nem küldök ordert")

    print(f"[ORDER] {side} {qty_adj} {SYMBOL}")
    resp = session.place_order(
        category="spot",
        symbol=SYMBOL,
        side=side,              # "Buy" vagy "Sell"
        orderType="Market",
        qty=str(qty_adj),
        timeInForce="IOC",
    )
    if resp.get("retCode") != 0:
        raise RuntimeError(f"Bybit order hiba: {resp}")
    return resp


# ==========================
# FŐ LOGIKA
# ==========================

def main():
    print("[INIT] SOL Autotrade bot indul...")
    print(f"[INIT] Symbol: {SYMBOL}")
    print(f"[INIT] DIP: -{DIP_PERCENT:.2f}%  TP: +{TP_PERCENT:.2f}%  Order: {ORDER_USDT} USDT")

    conn = db_connect()
    session = create_bybit_session()

    send_telegram(
        "🤖 *SOL Autotrade bot indul.*\n"
        f"Stratégia: -{DIP_PERCENT:.1f}% BUY, +{TP_PERCENT:.1f}% SELL\n"
        f"Order méret: {ORDER_USDT} USDT"
    )

    while True:
        try:
            price = get_spot_price(session, SYMBOL)
            sol_total, usdt_available = get_balances(session)
        except Exception as e:
            print(f"[ERR] Ár vagy balance lekérési hiba: {e}", file=sys.stderr)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        position_value = sol_total * price
        has_position = position_value >= MIN_POSITION_USDT

        # Bázisár DB-ből
        try:
            base_price = get_base_price(conn, SYMBOL)
        except Exception as e:
            print(f"[ERR] DB hibája base_price lekérdezéskor: {e}", file=sys.stderr)
            base_price = None

        if base_price is None:
            # Első indulás – beállítjuk bázisnak az aktuális árat
            print(f"[INIT] Nincs bázisár a DB-ben, beállítom: {price:.4f} USDT")
            try:
                set_base_price(conn, SYMBOL, price)
            except Exception as e:
                print(f"[ERR] DB hibája base_price mentéskor: {e}", file=sys.stderr)
            send_telegram(
                f"ℹ️ *SOL Autotrade*: induló bázisár beállítva: `{price:.4f}` USDT"
            )
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # százalékos eltérés
        change_pct = (price - base_price) / base_price * 100.0

        print(
            f"[STATE] price={price:.4f}  base={base_price:.4f}  change={change_pct:.2f}%  "
            f"sol_total={sol_total:.5f} (~{position_value:.2f} USDT)  usdt_avail={usdt_available:.2f}"
        )

        # =====================================
        # 1) VAN POZÍCIÓ -> TAKE PROFIT LOGIKA
        # =====================================
        if has_position:
            tp_level = base_price * (1.0 + TP_PERCENT / 100.0)

            if price >= tp_level:
                # eladunk mindent
                try:
                    resp = place_market_order(session, side="Sell", qty=sol_total)
                except Exception as e:
                    print(f"[ERR] Sell order hiba: {e}", file=sys.stderr)
                else:
                    msg = (
                        "✅ *SOL TAKE PROFIT végrehajtva*\n"
                        f"Eladott mennyiség: `{sol_total:.5f}` SOL\n"
                        f"Ár (kb.): `{price:.4f}` USDT\n"
                        f"Régi bázisár: `{base_price:.4f}`\n"
                        f"Eltérés: +{change_pct:.2f}%\n\n"
                        "Új bázisár a mostani árhoz igazítva."
                    )
                    print("[TP]", msg.replace("\n", " | "))
                    send_telegram(msg)

                    # új bázisár: a mostani ár
                    try:
                        set_base_price(conn, SYMBOL, price)
                    except Exception as e:
                        print(f"[ERR] DB hibája base_price frissítéskor (TP után): {e}", file=sys.stderr)

            # ha van pozíció, de nincs TP, csak várunk
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # ============================
        # 2) NINCS POZÍCIÓ -> DIP BUY
        # ============================
        dip_level = base_price * (1.0 - DIP_PERCENT / 100.0)

        if price <= dip_level:
            # elvileg BUY jel
            if usdt_available < ORDER_USDT:
                print(
                    f"[WARN] BUY jel lenne, de nincs elég USDT. "
                    f"Elérhető: {usdt_available:.2f}, kellene: {ORDER_USDT:.2f}"
                )
                send_telegram(
                    "⚠️ *SOL BUY jel*, de nincs elég USDT a számlán.\n"
                    f"Elérhető: `{usdt_available:.2f}` USDT\n"
                    f"Beállított order méret: `{ORDER_USDT:.2f}` USDT"
                )
            else:
                # mennyiség számítás
                est_qty = ORDER_USDT / price
                qty = quantize_qty(est_qty)
                try:
                    resp = place_market_order(session, side="Buy", qty=qty)
                except Exception as e:
                    print(f"[ERR] Buy order hiba: {e}", file=sys.stderr)
                else:
                    new_base = price  # egyszerűen a mostani árat tekintjük entry-nek
                    try:
                        set_base_price(conn, SYMBOL, new_base)
                    except Exception as e:
                        print(f"[ERR] DB hibája base_price frissítéskor (BUY után): {e}", file=sys.stderr)

                    msg = (
                        "🟢 *SOL DIP BUY végrehajtva*\n"
                        f"Vett mennyiség: `{qty:.5f}` SOL\n"
                        f"Ár (kb.): `{price:.4f}` USDT\n"
                        f"Régi bázisár: `{base_price:.4f}`\n"
                        f"Eltérés: {change_pct:.2f}%\n\n"
                        f"Új bázisár: `{new_base:.4f}`"
                    )
                    print("[BUY]", msg.replace("\n", " | "))
                    send_telegram(msg)

        # ha nincs jel, csak várunk
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[EXIT] SOL Autotrade bot leállítva billentyűzetről.")
