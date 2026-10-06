"""
EBA Kitap İndirici - web uygulaması
Çalıştırma:  python app.py        (geliştirme)
Yayın:       gunicorn -w 1 --threads 8 --timeout 120 app:app
NOT: İş durumları bellekte tutulduğu için tek worker (-w 1) kullanın.
"""
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file

import eba_indir as eba

# ---------------------------- Ayarlar (ortam değişkenleriyle değişir) ----------------------------
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "cache"))
IZINLI_ALANLAR = {a.strip().lower() for a in os.environ.get("IZINLI_ALANLAR", "f.eba.gov.tr").split(",")}
HTTP_IZNI = os.environ.get("IZIN_HTTP") == "1"            # yalnızca yerel test için
EN_FAZLA_SAYFA = int(os.environ.get("EN_FAZLA_SAYFA", "600"))
AYNI_ANDA_IS = int(os.environ.get("AYNI_ANDA_IS", "3"))
SAATLIK_LIMIT = int(os.environ.get("SAATLIK_LIMIT", "15"))   # IP başına saatte yeni indirme
SAYFA_PARCACIGI = int(os.environ.get("SAYFA_PARCACIGI", "6"))
ONBELLEK_SAAT = float(os.environ.get("ONBELLEK_SAAT", "12"))
IS_OMRU_SN = 3600

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10_000
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ISLER = {}
KILIT = threading.Lock()
SEMAFOR = threading.BoundedSemaphore(AYNI_ANDA_IS)
ISTEKLER = defaultdict(deque)


# ---------------------------- Yardımcılar ----------------------------
def url_dogrula(ham):
    """Kullanıcının verdiği linki doğrular; güvenli, normalleştirilmiş bir adres döndürür."""
    ham = (ham or "").strip()
    if not ham or len(ham) > 500:
        raise ValueError("Lütfen kitap linkini yapıştırın.")
    if any(c in ham for c in ("\\", " ", "\t", "\n", "\r")) or "@" in ham:
        raise ValueError("Link geçersiz karakterler içeriyor.")
    p = urlparse(ham)
    izinli_semalar = ("https", "http") if HTTP_IZNI else ("https",)
    if p.scheme not in izinli_semalar:
        raise ValueError("Link https:// ile başlamalı.")
    host = (p.hostname or "").lower()
    if host not in IZINLI_ALANLAR:
        raise ValueError("Şimdilik yalnızca f.eba.gov.tr adreslerindeki kitaplar destekleniyor.")
    if not HTTP_IZNI and p.port not in (None, 443):
        raise ValueError("Link geçersiz.")
    if not p.path.startswith("/flippingbook/") or ".." in p.path \
            or not re.fullmatch(r"[A-Za-z0-9_\-./%]+", p.path):
        raise ValueError("Bu bir EBA flipbook kitap linkine benzemiyor.")
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme}://{host}{port}{p.path}"


def istemci_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else request.remote_addr) or "?"


def limit_asildi(ip):
    simdi = time.time()
    kuyruk = ISTEKLER[ip]
    while kuyruk and simdi - kuyruk[0] > 3600:
        kuyruk.popleft()
    if len(kuyruk) >= SAATLIK_LIMIT:
        return True
    kuyruk.append(simdi)
    return False


def kitap_kimligi(guvenli_url):
    base = eba.kitap_tabani(guvenli_url)
    slug = re.sub(r"[^A-Za-z0-9_\-]", "_", base.rstrip("/").rsplit("/", 1)[-1])[:80] or "kitap"
    return hashlib.sha1(base.encode()).hexdigest()[:12], slug


def onbellek_yolu(kimlik):
    return CACHE_DIR / f"{kimlik}.pdf"


def onbellekte_taze_mi(yol):
    return yol.exists() and (time.time() - yol.stat().st_mtime) < ONBELLEK_SAAT * 3600


def is_calistir(is_id, guvenli_url, kimlik):
    isi = ISLER[is_id]
    gecici = Path(tempfile.mkdtemp(prefix="eba_"))
    gecici_pdf = gecici / "kitap.pdf"

    def ilerleme(asama, a, b):
        if asama == "indiriliyor":
            isi.update(asama="indiriliyor", mevcut=a, toplam=b)
        elif asama == "sayfa_sayisi":
            isi.update(toplam=a)
        elif asama == "pdf":
            isi.update(asama="pdf", mevcut=a)

    try:
        isi.update(durum="calisiyor", asama="hazirlaniyor")
        sonuc = eba.pdf_olustur(guvenli_url, gecici_pdf, gecici / "sayfalar",
                                kanal=SAYFA_PARCACIGI, en_fazla=EN_FAZLA_SAYFA, ilerleme=ilerleme)
        hedef = onbellek_yolu(kimlik)
        ara = hedef.with_suffix(".tmp")          # önce yanına kopyala, sonra atomik olarak yerine koy
        shutil.move(str(gecici_pdf), str(ara))
        os.replace(ara, hedef)
        isi.update(durum="bitti", dosya=str(hedef), sayfa=sonuc["sayfa"], eksik=sonuc["eksik"])
    except eba.KitapHatasi as e:
        isi.update(durum="hata", mesaj=str(e))
    except Exception:
        app.logger.exception("Beklenmeyen hata")
        isi.update(durum="hata", mesaj="Beklenmeyen bir hata oluştu. Lütfen daha sonra tekrar deneyin.")
    finally:
        shutil.rmtree(gecici, ignore_errors=True)
        SEMAFOR.release()


def temizlik_dongusu():
    while True:
        time.sleep(600)
        simdi = time.time()
        with KILIT:
            for k in [k for k, v in ISLER.items() if simdi - v["olusma"] > IS_OMRU_SN]:
                ISLER.pop(k, None)
        for f in CACHE_DIR.glob("*.pdf"):
            if simdi - f.stat().st_mtime > ONBELLEK_SAAT * 3600:
                f.unlink(missing_ok=True)


threading.Thread(target=temizlik_dongusu, daemon=True).start()


# ---------------------------- Sayfalar ----------------------------
@app.get("/")
def anasayfa():
    return render_template("index.html")


@app.get("/saglik")
def saglik():
    return "ok"


@app.post("/api/baslat")
def baslat():
    veri = request.get_json(silent=True) or {}
    try:
        guvenli_url = url_dogrula(veri.get("url"))
    except ValueError as e:
        return jsonify(hata=str(e)), 400

    kimlik, slug = kitap_kimligi(guvenli_url)
    is_id = uuid.uuid4().hex
    yeni = {"durum": "bekliyor", "asama": "", "mevcut": 0, "toplam": None, "mesaj": "",
            "dosya": None, "ad": f"{slug}.pdf", "sayfa": None, "eksik": [], "olusma": time.time()}

    # Önbellekte taze PDF varsa EBA'yı yormadan hemen ver
    yol = onbellek_yolu(kimlik)
    if onbellekte_taze_mi(yol):
        yeni.update(durum="bitti", dosya=str(yol))
        with KILIT:
            ISLER[is_id] = yeni
        return jsonify(id=is_id), 202

    if limit_asildi(istemci_ip()):
        return jsonify(hata="Saatlik indirme limitine ulaştınız. Biraz sonra tekrar deneyin."), 429
    if not SEMAFOR.acquire(blocking=False):
        return jsonify(hata="Şu an çok yoğunuz. Birkaç dakika sonra tekrar deneyin."), 429

    with KILIT:
        ISLER[is_id] = yeni
    threading.Thread(target=is_calistir, args=(is_id, guvenli_url, kimlik), daemon=True).start()
    return jsonify(id=is_id), 202


def _isi_getir(is_id):
    if not re.fullmatch(r"[0-9a-f]{32}", is_id):
        return None
    return ISLER.get(is_id)


@app.get("/api/durum/<is_id>")
def durum(is_id):
    isi = _isi_getir(is_id)
    if not isi:
        return jsonify(hata="İş bulunamadı veya süresi doldu."), 404
    return jsonify({k: isi[k] for k in ("durum", "asama", "mevcut", "toplam", "mesaj", "sayfa", "eksik")})


@app.get("/api/indir/<is_id>")
def indir(is_id):
    isi = _isi_getir(is_id)
    if not isi or isi["durum"] != "bitti" or not isi["dosya"] or not Path(isi["dosya"]).exists():
        return jsonify(hata="Dosya hazır değil veya süresi doldu. Lütfen yeniden oluşturun."), 404
    return send_file(isi["dosya"], mimetype="application/pdf", as_attachment=True, download_name=isi["ad"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
