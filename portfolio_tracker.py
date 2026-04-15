# -*- coding: utf-8 -*-
"""
Portföy Takip Sistemi v2.0
===========================
- bot.py (Scanner v7) ve SMC.py (SMC Sniper v4) sinyallerini HTTP POST ile alır
- Açık pozisyonları 5 dakikada bir Binance REST API ile kontrol eder
- TP1'de pozisyon KAPANIR (gerçek sonuç)
- TP2 shadow tracking: TP1 kapandıktan sonra da fiyatı izler,
  TP2'ye ulaşıp ulaşmadığını kaydeder (analiz için)
- HTML dashboard + TP2 analiz bölümü
- JSON dosya tabanlı kayıt
"""

import json
import os
import time
import threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
from flask import Flask, request, jsonify

# ============================================================
# AYARLAR
# ============================================================
TR_TZ = timezone(timedelta(hours=3))
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
SIGNALS_FILE = os.path.join(DATA_DIR, "portfolio_signals.json")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # 5 dakika
EXPIRE_HOURS = int(os.getenv("EXPIRE_HOURS", "48"))
SHADOW_EXPIRE_HOURS = int(os.getenv("SHADOW_EXPIRE_HOURS", "72"))  # TP2 shadow takip süresi

AUTH_TOKEN = os.getenv("PORTFOLIO_AUTH_TOKEN", "")

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"

app = Flask(__name__)

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ============================================================
# VERİ KATMANI
# ============================================================
signals_db = []
_lock = threading.Lock()


def load_signals():
    global signals_db
    try:
        if os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                signals_db = json.load(f)
            print(f"[DB] {len(signals_db)} sinyal yüklendi.", flush=True)
        else:
            signals_db = []
    except Exception as e:
        print(f"[DB] Yükleme hatası: {e}", flush=True)
        signals_db = []


def save_signals():
    try:
        with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
            json.dump(signals_db[-2000:], f, ensure_ascii=False, default=str, indent=None)
    except Exception as e:
        print(f"[DB] Kayıt hatası: {e}", flush=True)


def tr_now():
    return datetime.now(timezone.utc).astimezone(TR_TZ)


def tr_now_str():
    return tr_now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# SİNYAL ALMA ENDPOINT'İ
# ============================================================
@app.route("/api/signal", methods=["POST"])
def receive_signal():
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "no json body"}), 400

    required = ["symbol", "entry", "stop", "tp1"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"missing field: {field}"}), 400

    now = tr_now()
    signal = {
        "id": f"{data['symbol']}_{int(now.timestamp())}",
        "symbol": data["symbol"],
        "entry": float(data["entry"]),
        "stop": float(data["stop"]),
        "tp1": float(data["tp1"]),
        "tp2": float(data.get("tp2", 0)) or None,
        "sig_type": data.get("sig_type", data.get("type", "unknown")),
        "sub_type": data.get("sub_type", data.get("subtype", data.get("tp_system", ""))),
        "source": data.get("source", "bot"),
        "phase": data.get("phase", ""),
        "candle": data.get("candle", ""),
        "funding_neg": data.get("funding_neg", False),
        # === ANA SONUÇ (TP1 bazlı) ===
        "status": "open",
        "open_time": now.isoformat(),
        "close_time": None,
        "close_price": None,
        "close_reason": None,
        "close_pct": None,
        # === Fiyat takip ===
        "peak_price": float(data["entry"]),
        "peak_pct": 0.0,
        "low_price": float(data["entry"]),
        "low_pct": 0.0,
        "current_price": float(data["entry"]),
        "current_pct": 0.0,
        "tp1_hit": False,
        "tp1_time": None,
        # === TP2 SHADOW TAKİP ===
        "tp2_shadow": "watching",
        "tp2_hit": False,
        "tp2_time": None,
        "tp2_peak_after_tp1": 0.0,
        "tp2_shadow_end": None,
        # === Meta ===
        "last_check": now.isoformat(),
        "checks": 0,
        "extra": {k: v for k, v in data.items() if k not in required + [
            "sig_type", "type", "sub_type", "subtype", "tp_system",
            "source", "phase", "candle", "funding_neg", "tp2"
        ]},
    }

    with _lock:
        signals_db.insert(0, signal)
        save_signals()

    print(f"[SİNYAL] {signal['sig_type'].upper()} | {signal['symbol']} | "
          f"Giriş: {signal['entry']} | Kaynak: {signal['source']}", flush=True)

    return jsonify({"ok": True, "id": signal["id"]}), 201


# ============================================================
# BİNANCE FİYAT KONTROLÜ
# ============================================================
def get_current_price_hl(symbol):
    pair = symbol.replace("/", "").replace("USDT", "USDT")
    try:
        r = requests.get(BINANCE_KLINE_URL, params={
            "symbol": pair, "interval": "5m", "limit": 1
        }, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                k = data[0]
                return {"high": float(k[2]), "low": float(k[3]), "close": float(k[4])}
    except Exception as e:
        print(f"[BINANCE] {symbol} hata: {e}", flush=True)
    return None


# ============================================================
# POZİSYON KONTROL DÖNGÜSÜ (5dk)
# ============================================================
def check_open_positions():
    now = tr_now()

    with _lock:
        active = [s for s in signals_db
                  if s["status"] == "open" or s.get("tp2_shadow") == "watching"]

    if not active:
        return

    open_count = sum(1 for s in active if s["status"] == "open")
    shadow_count = sum(1 for s in active if s["status"] != "open" and s.get("tp2_shadow") == "watching")
    print(f"[CHECK] {open_count} açık + {shadow_count} shadow takip...", flush=True)

    closed_count = 0
    need_save = False

    for sig in active:
        symbol = sig["symbol"]
        price_data = get_current_price_hl(symbol)
        if not price_data:
            continue

        high = price_data["high"]
        low = price_data["low"]
        close = price_data["close"]
        entry = sig["entry"]

        # ====================================================
        # A) AÇIK POZİSYON — TP1 veya Stop ile kapanır
        # ====================================================
        if sig["status"] == "open":
            stop = sig["stop"]
            tp1 = sig["tp1"]
            tp2 = sig.get("tp2")

            if high > sig["peak_price"]:
                sig["peak_price"] = high
                sig["peak_pct"] = round((high - entry) / entry * 100, 2)
            if low < sig["low_price"]:
                sig["low_price"] = low
                sig["low_pct"] = round((low - entry) / entry * 100, 2)

            sig["current_price"] = close
            sig["current_pct"] = round((close - entry) / entry * 100, 2)
            sig["last_check"] = now.isoformat()
            sig["checks"] = sig.get("checks", 0) + 1

            close_reason = None
            close_price = None

            if low <= stop:
                close_reason = "stop"
                close_price = stop
                sig["status"] = "loss"
                sig["tp2_shadow"] = "n/a"
            elif high >= tp1:
                close_reason = "tp1"
                close_price = tp1
                sig["status"] = "win_tp1"
                sig["tp1_hit"] = True
                sig["tp1_time"] = now.isoformat()
                if tp2:
                    sig["tp2_shadow"] = "watching"
                else:
                    sig["tp2_shadow"] = "n/a"
            else:
                open_time = datetime.fromisoformat(sig["open_time"])
                if open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=TR_TZ)
                elapsed_h = (now - open_time).total_seconds() / 3600
                if elapsed_h >= EXPIRE_HOURS:
                    close_reason = "expired"
                    close_price = close
                    sig["status"] = "expired"
                    sig["tp2_shadow"] = "n/a"

            if close_reason:
                sig["close_time"] = now.isoformat()
                sig["close_price"] = round(close_price, 8)
                sig["close_reason"] = close_reason
                sig["close_pct"] = round((close_price - entry) / entry * 100, 2)
                closed_count += 1
                need_save = True

                if close_reason == "tp1":
                    emoji = "🟢"
                elif close_reason == "stop":
                    emoji = "🔴"
                else:
                    emoji = "⏰"

                pct = sig["close_pct"]
                print(f"  {emoji} KAPANDI: {symbol} | {close_reason.upper()} | "
                      f"{pct:+.2f}% | Peak: {sig['peak_pct']:+.2f}%", flush=True)

        # ====================================================
        # B) TP2 SHADOW TAKİP
        # ====================================================
        elif sig.get("tp2_shadow") == "watching" and sig.get("tp2"):
            tp2 = sig["tp2"]
            sig["last_check"] = now.isoformat()

            after_tp1_pct = round((high - entry) / entry * 100, 2)
            if after_tp1_pct > sig.get("tp2_peak_after_tp1", 0):
                sig["tp2_peak_after_tp1"] = after_tp1_pct
                need_save = True

            if high >= tp2:
                sig["tp2_shadow"] = "hit"
                sig["tp2_hit"] = True
                sig["tp2_time"] = now.isoformat()
                sig["tp2_shadow_end"] = now.isoformat()
                need_save = True

                tp2_pct = round((tp2 - entry) / entry * 100, 2)
                tp1_pct = sig.get("close_pct", 0) or 0
                extra_pct = round(tp2_pct - tp1_pct, 2)

                sym = symbol.replace("/USDT", "")
                print(f"  🎯 TP2 SHADOW HIT: {sym} | +{tp2_pct}% | Ekstra: +{extra_pct}%", flush=True)

            else:
                close_time = datetime.fromisoformat(sig.get("close_time", sig["open_time"]))
                if close_time.tzinfo is None:
                    close_time = close_time.replace(tzinfo=TR_TZ)
                elapsed_h = (now - close_time).total_seconds() / 3600
                if elapsed_h >= SHADOW_EXPIRE_HOURS:
                    sig["tp2_shadow"] = "missed"
                    sig["tp2_shadow_end"] = now.isoformat()
                    need_save = True

        time.sleep(0.15)

    if need_save or closed_count > 0:
        with _lock:
            save_signals()
        if closed_count > 0:
            print(f"[CHECK] {closed_count} pozisyon kapandı.", flush=True)


def position_checker_loop():
    while True:
        try:
            check_open_positions()
        except Exception as e:
            print(f"[CHECK] Döngü hatası: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)


# ============================================================
# PERFORMANS HESAPLAMA
# ============================================================
def calc_performance():
    with _lock:
        all_sigs = list(signals_db)

    result = {
        "total": len(all_sigs), "open": 0, "closed": 0,
        "wins": 0, "losses": 0, "expired": 0, "tp1_hits": 0,
        "total_pnl": 0.0, "avg_peak": 0.0, "win_rate": 0.0,
        "tp2_shadow_total": 0, "tp2_shadow_hit": 0,
        "tp2_shadow_missed": 0, "tp2_shadow_watching": 0,
        "tp2_potential_extra_pnl": 0.0,
        "by_type": {}, "daily": {}, "weekly": {}, "monthly": {},
    }

    closed_peaks = []
    type_stats = defaultdict(lambda: {
        "total": 0, "open": 0, "wins": 0, "losses": 0, "expired": 0,
        "tp1_hits": 0, "total_pnl": 0.0, "peaks": [],
        "tp2_hits": 0, "tp2_total": 0, "tp2_extra_pnl": 0.0,
    })

    for sig in all_sigs:
        status = sig.get("status", "open")
        sig_type = sig.get("sig_type", "unknown")
        sub = sig.get("sub_type", "")
        source = sig.get("source", "bot")

        if source == "smc":
            type_key = f"SMC-{sig.get('phase', '').replace('phase', 'P')}"
        elif sig_type == "tp":
            type_key = f"TP-{sub.capitalize()}" if sub else "TP"
        else:
            type_key = sig_type.upper()

        ts = type_stats[type_key]
        ts["total"] += 1

        if status == "open":
            result["open"] += 1
            ts["open"] += 1
        else:
            result["closed"] += 1
            pct = sig.get("close_pct", 0) or 0
            result["total_pnl"] += pct
            ts["total_pnl"] += pct

            if sig.get("tp1_hit"):
                result["tp1_hits"] += 1
                ts["tp1_hits"] += 1

            peak = sig.get("peak_pct", 0)
            closed_peaks.append(peak)
            ts["peaks"].append(peak)

            if status == "win_tp1":
                result["wins"] += 1; ts["wins"] += 1
            elif status == "loss":
                result["losses"] += 1; ts["losses"] += 1
            elif status == "expired":
                result["expired"] += 1; ts["expired"] += 1

            tp2_shadow = sig.get("tp2_shadow", "n/a")
            if tp2_shadow != "n/a":
                result["tp2_shadow_total"] += 1; ts["tp2_total"] += 1
                if tp2_shadow == "hit":
                    result["tp2_shadow_hit"] += 1; ts["tp2_hits"] += 1
                    tp2 = sig.get("tp2"); entry = sig.get("entry")
                    tp1_pct = sig.get("close_pct", 0) or 0
                    if tp2 and entry and entry > 0:
                        extra = (tp2 - entry) / entry * 100 - tp1_pct
                        result["tp2_potential_extra_pnl"] += extra
                        ts["tp2_extra_pnl"] += extra
                elif tp2_shadow == "missed":
                    result["tp2_shadow_missed"] += 1
                elif tp2_shadow == "watching":
                    result["tp2_shadow_watching"] += 1

            close_time = sig.get("close_time") or sig.get("open_time", "")
            if close_time:
                try:
                    dt = datetime.fromisoformat(close_time)
                    day_key = dt.strftime("%Y-%m-%d")
                    week_key = dt.strftime("%Y-W%W")
                    month_key = dt.strftime("%Y-%m")
                    for bucket, key in [(result["daily"], day_key),
                                        (result["weekly"], week_key),
                                        (result["monthly"], month_key)]:
                        if key not in bucket:
                            bucket[key] = {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}
                        bucket[key]["trades"] += 1
                        bucket[key]["pnl"] += pct
                        if status == "win_tp1": bucket[key]["wins"] += 1
                        elif status == "loss": bucket[key]["losses"] += 1
                except Exception:
                    pass

    if closed_peaks:
        result["avg_peak"] = round(sum(closed_peaks) / len(closed_peaks), 2)
    if result["closed"] > 0:
        result["win_rate"] = round(result["wins"] / result["closed"] * 100, 1)
    result["total_pnl"] = round(result["total_pnl"], 2)
    result["tp2_potential_extra_pnl"] = round(result["tp2_potential_extra_pnl"], 2)

    for tk, ts in type_stats.items():
        closed = ts["wins"] + ts["losses"] + ts["expired"]
        ts["win_rate"] = round(ts["wins"] / closed * 100, 1) if closed > 0 else 0
        ts["avg_peak"] = round(sum(ts["peaks"]) / len(ts["peaks"]), 2) if ts["peaks"] else 0
        ts["total_pnl"] = round(ts["total_pnl"], 2)
        ts["tp2_extra_pnl"] = round(ts["tp2_extra_pnl"], 2)
        ts["tp2_rate"] = round(ts["tp2_hits"] / ts["tp2_total"] * 100, 1) if ts["tp2_total"] > 0 else 0
        del ts["peaks"]

    result["by_type"] = dict(type_stats)
    return result


# ============================================================
# API ENDPOINT'LERİ
# ============================================================
@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": tr_now_str()})

@app.route("/api/performance")
def api_performance():
    return jsonify(calc_performance())

@app.route("/api/signals")
def api_signals():
    status_filter = request.args.get("status", "all")
    type_filter = request.args.get("type", "all")
    limit = int(request.args.get("limit", "100"))
    with _lock:
        sigs = list(signals_db)
    if status_filter != "all":
        sigs = [s for s in sigs if s.get("status") == status_filter]
    if type_filter != "all":
        sigs = [s for s in sigs if s.get("sig_type") == type_filter]
    return jsonify(sigs[:limit])

@app.route("/api/open")
def api_open():
    with _lock:
        return jsonify([s for s in signals_db if s.get("status") == "open"])

@app.route("/api/signal/<signal_id>", methods=["DELETE"])
def delete_signal(signal_id):
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
    with _lock:
        before = len(signals_db)
        signals_db[:] = [s for s in signals_db if s.get("id") != signal_id]
        if len(signals_db) != before:
            save_signals()
            return jsonify({"ok": True, "deleted": signal_id})
        return jsonify({"error": "not found"}), 404

@app.route("/api/signals/clear-test", methods=["POST"])
def clear_test_signals():
    if AUTH_TOKEN:
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != AUTH_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
    with _lock:
        before = len(signals_db)
        signals_db[:] = [s for s in signals_db if s.get("source") != "test"]
        save_signals()
    return jsonify({"ok": True, "removed": before - len(signals_db)})


# ============================================================
# HTML DASHBOARD
# ============================================================
def fmt_price(p):
    if p is None: return "—"
    p = float(p)
    if p >= 100: return f"{p:.2f}"
    if p >= 1: return f"{p:.3f}"
    if p >= 0.01: return f"{p:.4f}"
    return f"{p:.6f}"

def pct_color(pct):
    if pct is None: return "#8a9bb0", "—"
    pct = float(pct)
    color = "#2ecc71" if pct > 0 else ("#e74c3c" if pct < 0 else "#8a9bb0")
    return color, f"{pct:+.2f}%"

def status_badge(status):
    colors = {"open": ("#3498db", "AÇIK"), "win_tp1": ("#2ecc71", "WIN (TP1)"),
              "loss": ("#e74c3c", "LOSS"), "expired": ("#f39c12", "EXPIRED")}
    c, label = colors.get(status, ("#8a9bb0", status.upper()))
    return f'<span style="background:{c}22;color:{c};padding:2px 8px;border-radius:3px;font-size:.7rem;font-weight:bold">{label}</span>'

def tp2_shadow_badge(sig):
    shadow = sig.get("tp2_shadow", "n/a")
    m = {"hit": ("#2ecc71", "✅ TP2 HIT"), "missed": ("#e74c3c", "❌ MISS"),
         "watching": ("#3498db", "👁 İZLENİYOR")}
    if shadow in m:
        c, l = m[shadow]
        return f'<span style="background:{c}22;color:{c};padding:1px 6px;border-radius:3px;font-size:.6rem">{l}</span>'
    return '<span style="color:#5a6a7a;font-size:.6rem">—</span>'

def type_badge(sig):
    sig_type = sig.get("sig_type", "unknown")
    sub = sig.get("sub_type", "")
    source = sig.get("source", "bot")
    if source == "smc":
        phase = sig.get("phase", "")
        return f'<span style="background:#e67e2222;color:#e67e22;padding:2px 6px;border-radius:3px;font-size:.65rem">SMC {phase}</span>'
    colors = {"dip": "#2ecc71", "trend": "#3498db", "birikim": "#9b59b6", "tp": "#e67e22"}
    c = colors.get(sig_type, "#8a9bb0")
    label = sig_type.upper() + (f" {sub}" if sub else "")
    return f'<span style="background:{c}22;color:{c};padding:2px 6px;border-radius:3px;font-size:.65rem">{label}</span>'


@app.route("/")
def dashboard():
    perf = calc_performance()
    now = tr_now_str()

    with _lock:
        all_sigs = list(signals_db)

    open_sigs = [s for s in all_sigs if s.get("status") == "open"]
    closed_sigs = [s for s in all_sigs if s.get("status") != "open"]
    shadow_watching = [s for s in all_sigs if s.get("tp2_shadow") == "watching" and s.get("status") != "open"]

    # === AÇIK POZİSYONLAR ===
    open_rows = ""
    for sig in open_sigs[:50]:
        cur_c, cur_s = pct_color(sig.get("current_pct"))
        peak_c, peak_s = pct_color(sig.get("peak_pct"))
        low_c, low_s = pct_color(sig.get("low_pct"))
        sym = sig["symbol"].replace("/USDT", "")
        tp1_pct = round((sig["tp1"] - sig["entry"]) / sig["entry"] * 100, 1) if sig["entry"] > 0 else 0
        open_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td><td>{type_badge(sig)}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td style="color:{cur_c};font-weight:bold">{fmt_price(sig.get('current_price'))} ({cur_s})</td>
            <td style="color:{peak_c}">{peak_s}</td><td style="color:{low_c}">{low_s}</td>
            <td>{fmt_price(sig['stop'])}</td><td>{fmt_price(sig['tp1'])} (+{tp1_pct}%)</td>
            <td style="font-size:.7rem;color:#7f8c8d">{(sig.get('open_time',''))[:16]}</td></tr>"""

    # === KAPANMIŞ İŞLEMLER ===
    closed_rows = ""
    for sig in closed_sigs[:100]:
        close_c, close_s = pct_color(sig.get("close_pct"))
        peak_c, peak_s = pct_color(sig.get("peak_pct"))
        sym = sig["symbol"].replace("/USDT", "")
        closed_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td><td>{type_badge(sig)}</td>
            <td>{status_badge(sig.get('status','unknown'))}</td>
            <td>{fmt_price(sig['entry'])}</td>
            <td style="color:{close_c};font-weight:bold">{close_s}</td>
            <td style="color:{peak_c}">{peak_s}</td><td>{tp2_shadow_badge(sig)}</td>
            <td style="font-size:.7rem;color:#7f8c8d">{(sig.get('open_time',''))[:16]}</td>
            <td style="font-size:.7rem;color:#7f8c8d">{(sig.get('close_time') or '')[:16]}</td></tr>"""

    # === TÜR BAZLI ===
    type_rows = ""
    for tk, ts in sorted(perf.get("by_type", {}).items()):
        wr = ts.get("win_rate", 0)
        wr_c = "#2ecc71" if wr >= 60 else ("#f39c12" if wr >= 40 else "#e74c3c")
        pnl = ts.get("total_pnl", 0)
        pnl_c = "#2ecc71" if pnl > 0 else ("#e74c3c" if pnl < 0 else "#8a9bb0")
        tp2r = ts.get("tp2_rate", 0)
        tp2r_c = "#2ecc71" if tp2r >= 50 else ("#f39c12" if tp2r >= 25 else "#8a9bb0")
        tp2e = ts.get("tp2_extra_pnl", 0)
        tp2e_c = "#2ecc71" if tp2e > 0 else "#8a9bb0"
        type_rows += f"""<tr>
            <td style="color:#ecf0f1;font-weight:bold">{tk}</td>
            <td>{ts.get('total',0)}</td><td style="color:#3498db">{ts.get('open',0)}</td>
            <td style="color:#2ecc71">{ts.get('wins',0)}</td><td style="color:#e74c3c">{ts.get('losses',0)}</td>
            <td style="color:#f39c12">{ts.get('expired',0)}</td>
            <td style="color:{wr_c};font-weight:bold">%{wr}</td>
            <td style="color:{pnl_c};font-weight:bold">{pnl:+.2f}%</td>
            <td>{ts.get('avg_peak',0)}%</td>
            <td style="color:{tp2r_c}">{ts.get('tp2_hits',0)}/{ts.get('tp2_total',0)} (%{tp2r})</td>
            <td style="color:{tp2e_c}">{tp2e:+.2f}%</td></tr>"""

    # === SHADOW WATCHING ===
    shadow_rows = ""
    for sig in shadow_watching[:30]:
        sym = sig["symbol"].replace("/USDT", "")
        entry = sig["entry"]; tp1_pct = sig.get("close_pct", 0) or 0
        tp2 = sig.get("tp2")
        tp2_pct = round((tp2 - entry) / entry * 100, 1) if tp2 and entry > 0 else 0
        pa = sig.get("tp2_peak_after_tp1", 0)
        pa_c = "#2ecc71" if pa > tp1_pct else "#8a9bb0"
        remaining = ""
        try:
            ct = datetime.fromisoformat(sig.get("close_time", sig["open_time"]))
            if ct.tzinfo is None: ct = ct.replace(tzinfo=TR_TZ)
            remaining = f"{max(0, SHADOW_EXPIRE_HOURS - (tr_now() - ct).total_seconds() / 3600):.0f}s"
        except Exception: pass
        shadow_rows += f"""<tr>
            <td style="color:#ecf0f1"><b>{sym}</b></td><td>{type_badge(sig)}</td>
            <td style="color:#2ecc71">+{tp1_pct:.2f}%</td>
            <td>{fmt_price(tp2)} (+{tp2_pct}%)</td>
            <td style="color:{pa_c}">+{pa:.2f}%</td>
            <td style="color:#7f8c8d;font-size:.7rem">{remaining}</td></tr>"""

    # === GÜNLÜK ===
    daily_rows = ""
    for day_key in sorted(perf.get("daily", {}).keys(), reverse=True)[:14]:
        d = perf["daily"][day_key]; pnl = d.get("pnl", 0)
        pnl_c = "#2ecc71" if pnl > 0 else ("#e74c3c" if pnl < 0 else "#8a9bb0")
        daily_rows += f"""<tr>
            <td style="color:#ecf0f1">{day_key}</td><td>{d.get('trades',0)}</td>
            <td style="color:#2ecc71">{d.get('wins',0)}</td><td style="color:#e74c3c">{d.get('losses',0)}</td>
            <td style="color:{pnl_c};font-weight:bold">{pnl:+.2f}%</td></tr>"""

    total_pnl = perf.get("total_pnl", 0)
    pnl_color = "#2ecc71" if total_pnl > 0 else ("#e74c3c" if total_pnl < 0 else "#8a9bb0")
    tp2_extra_total = perf.get("tp2_potential_extra_pnl", 0)
    tp2_extra_color = "#2ecc71" if tp2_extra_total > 0 else "#8a9bb0"
    tp2_hit_count = perf.get("tp2_shadow_hit", 0)
    tp2_total_count = perf.get("tp2_shadow_total", 0)
    tp2_rate = round(tp2_hit_count / tp2_total_count * 100, 1) if tp2_total_count > 0 else 0

    shadow_section = ""
    if shadow_rows:
        shadow_section = f"""
<div class="section">
    <h2>👁 TP2 SHADOW İZLEME ({len(shadow_watching)})</h2>
    <p class="note">TP1'de kapanmış ama TP2'ye ulaşıp ulaşmayacağı izleniyor.</p>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>TP1 Kâr</th><th>TP2 Hedef</th><th>Peak Sonrası</th><th>Kalan</th>
    </tr></thead><tbody>{shadow_rows}</tbody></table></div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8"><title>Portföy Takip v2.0</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<style>
:root {{--bg:#0a0e14;--card:#0f1319;--border:#1a2030;--text:#c0cdd8;--text-dim:#5a6a7a;
  --accent:#00b4d8;--green:#2ecc71;--red:#e74c3c;--orange:#f39c12;--purple:#9b59b6;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:'JetBrains Mono','Fira Code','Consolas',monospace;
  padding:20px;max-width:1200px;margin:0 auto;line-height:1.5;}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;
  padding-bottom:16px;border-bottom:1px solid var(--border);}}
.header h1{{color:var(--accent);font-size:1.1rem;letter-spacing:3px;}}
.header .time{{color:var(--text-dim);font-size:.75rem;}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:24px;}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:14px;text-align:center;}}
.card .val{{font-size:1.3rem;font-weight:bold;color:var(--accent);display:block;margin-bottom:4px;}}
.card .lbl{{font-size:.55rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px;}}
.section{{margin-bottom:28px;}}
.section h2{{color:var(--accent);font-size:.85rem;letter-spacing:2px;margin-bottom:12px;
  padding-bottom:6px;border-bottom:1px solid var(--border);}}
.section .note{{color:var(--text-dim);font-size:.65rem;margin-top:-8px;margin-bottom:12px;font-style:italic;}}
table{{width:100%;border-collapse:collapse;font-size:.73rem;}}
th{{background:var(--card);color:var(--text-dim);font-size:.58rem;text-transform:uppercase;
  letter-spacing:1px;padding:8px 8px;text-align:left;border-bottom:1px solid var(--border);position:sticky;top:0;}}
td{{padding:7px 8px;border-bottom:1px solid #0d111a;vertical-align:middle;}}
tr:hover td{{background:var(--card);}}
.table-wrap{{overflow-x:auto;border:1px solid var(--border);border-radius:6px;}}
.empty{{color:var(--text-dim);padding:20px;text-align:center;font-size:.8rem;}}
.tp2-box{{background:#0d1520;border:1px solid #1a3050;border-radius:6px;padding:16px;margin-bottom:24px;}}
.tp2-box h3{{color:#3498db;font-size:.8rem;margin-bottom:10px;}}
.tp2-stats{{display:flex;gap:20px;flex-wrap:wrap;font-size:.75rem;}}
.tp2-stat{{display:flex;flex-direction:column;align-items:center;}}
.tp2-stat .v{{font-size:1.1rem;font-weight:bold;}}
.tp2-stat .l{{font-size:.55rem;color:var(--text-dim);margin-top:2px;}}
.footer{{color:var(--text-dim);font-size:.6rem;margin-top:20px;padding-top:12px;
  border-top:1px solid var(--border);text-align:center;}}
@media(max-width:768px){{body{{padding:10px;}}.cards{{grid-template-columns:repeat(3,1fr);}}
  table{{font-size:.63rem;}}td,th{{padding:5px 5px;}}}}
</style></head><body>

<div class="header">
    <h1>📊 PORTFÖY TAKİP</h1>
    <span class="time">{now} | v2.0</span>
</div>

<div class="cards">
    <div class="card"><span class="val">{perf.get('total',0)}</span><span class="lbl">Toplam</span></div>
    <div class="card"><span class="val" style="color:#3498db">{perf.get('open',0)}</span><span class="lbl">Açık</span></div>
    <div class="card"><span class="val" style="color:var(--green)">{perf.get('wins',0)}</span><span class="lbl">Win (TP1)</span></div>
    <div class="card"><span class="val" style="color:var(--red)">{perf.get('losses',0)}</span><span class="lbl">Loss</span></div>
    <div class="card"><span class="val" style="color:var(--orange)">{perf.get('expired',0)}</span><span class="lbl">Expired</span></div>
    <div class="card"><span class="val" style="color:{'var(--green)' if perf.get('win_rate',0)>=50 else 'var(--red)'}"
        >%{perf.get('win_rate',0)}</span><span class="lbl">Win Rate</span></div>
    <div class="card"><span class="val" style="color:{pnl_color}">{total_pnl:+.2f}%</span><span class="lbl">TP1 Net P&L</span></div>
    <div class="card"><span class="val">{perf.get('avg_peak',0)}%</span><span class="lbl">Ort. Peak</span></div>
</div>

<div class="tp2-box">
    <h3>🎯 TP2 ANALİZ — "TP2'de kapatsaydık ne olurdu?"</h3>
    <div class="tp2-stats">
        <div class="tp2-stat"><span class="v" style="color:#3498db">{tp2_hit_count}/{tp2_total_count}</span>
            <span class="l">TP2'ye Ulaşan</span></div>
        <div class="tp2-stat"><span class="v" style="color:{'#2ecc71' if tp2_rate>=50 else '#f39c12'}">{tp2_rate}%</span>
            <span class="l">TP2 Hit Rate</span></div>
        <div class="tp2-stat"><span class="v" style="color:{tp2_extra_color}">{tp2_extra_total:+.2f}%</span>
            <span class="l">Kaçırılan Ekstra Kâr</span></div>
        <div class="tp2-stat"><span class="v" style="color:#3498db">{perf.get('tp2_shadow_watching',0)}</span>
            <span class="l">Hâlâ İzlenen</span></div>
        <div class="tp2-stat"><span class="v" style="color:{pnl_color}">{total_pnl:+.2f}%</span>
            <span class="l">Gerçek P&L (TP1)</span></div>
        <div class="tp2-stat"><span class="v" style="color:{'#2ecc71' if (total_pnl+tp2_extra_total)>0 else '#e74c3c'}"
            >{(total_pnl+tp2_extra_total):+.2f}%</span><span class="l">TP2 Olsaydı P&L</span></div>
    </div>
</div>

<div class="section">
    <h2>📈 SİNYAL TÜRÜ BAZLI KIRILIM</h2>
    <div class="table-wrap"><table><thead><tr>
        <th>Tür</th><th>Toplam</th><th>Açık</th><th>Win</th><th>Loss</th><th>Exp.</th>
        <th>Win Rate</th><th>TP1 P&L</th><th>Ort. Peak</th><th>TP2 Hit</th><th>TP2 Ekstra</th>
    </tr></thead><tbody>
        {type_rows if type_rows else '<tr><td colspan="11" class="empty">Henüz veri yok</td></tr>'}
    </tbody></table></div>
</div>

<div class="section">
    <h2>🔵 AÇIK POZİSYONLAR ({len(open_sigs)})</h2>
    <p class="note">TP1'e ulaşınca otomatik kapanır. Stop'a düşerse zarar yazılır.</p>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>Giriş</th><th>Şu An</th><th>Peak</th><th>Dip</th>
        <th>Stop</th><th>TP1</th><th>Açılış</th>
    </tr></thead><tbody>
        {open_rows if open_rows else '<tr><td colspan="9" class="empty">Açık pozisyon yok</td></tr>'}
    </tbody></table></div>
</div>

{shadow_section}

<div class="section">
    <h2>📋 KAPANMIŞ İŞLEMLER (son 100)</h2>
    <div class="table-wrap"><table><thead><tr>
        <th>Sembol</th><th>Tür</th><th>Sonuç</th><th>Giriş</th><th>Getiri</th><th>Peak</th>
        <th>TP2</th><th>Açılış</th><th>Kapanış</th>
    </tr></thead><tbody>
        {closed_rows if closed_rows else '<tr><td colspan="9" class="empty">Henüz kapanmış işlem yok</td></tr>'}
    </tbody></table></div>
</div>

<div class="section">
    <h2>📅 GÜNLÜK PERFORMANS (son 14 gün)</h2>
    <div class="table-wrap"><table><thead><tr>
        <th>Tarih</th><th>İşlem</th><th>Win</th><th>Loss</th><th>P&L</th>
    </tr></thead><tbody>
        {daily_rows if daily_rows else '<tr><td colspan="5" class="empty">Henüz veri yok</td></tr>'}
    </tbody></table></div>
</div>

<div class="footer">
    Portföy Takip v2.0 | TP1'de kapat + TP2 shadow takip |
    Kontrol: {CHECK_INTERVAL//60}dk | Expire: {EXPIRE_HOURS}s | Shadow: {SHADOW_EXPIRE_HOURS}s | {now}
</div>
</body></html>"""
    return html


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("=" * 50, flush=True)
    print("📊 Portföy Takip Sistemi v2.0", flush=True)
    print("   TP1'de kapat + TP2 shadow takip", flush=True)
    print("=" * 50, flush=True)
    print(f"  Kontrol aralığı  : {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60} dk)", flush=True)
    print(f"  Expire süresi    : {EXPIRE_HOURS} saat", flush=True)
    print(f"  TP2 shadow süresi: {SHADOW_EXPIRE_HOURS} saat", flush=True)
    print(f"  Data dizini      : {DATA_DIR}", flush=True)
    print("=" * 50, flush=True)

    load_signals()
    threading.Thread(target=position_checker_loop, daemon=True).start()

    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
