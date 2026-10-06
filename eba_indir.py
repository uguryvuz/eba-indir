#!/usr/bin/env python3
"""
EBA (FlippingBook) kitap indirici  -  komut satırı + web uygulaması çekirdeği
=============================================================================
Flipbook'taki sayfa görsellerini indirir ve tek bir PDF'e birleştirir.
Sayfalar jpg / png / webp karışık olabilir; her sayfa için uzantı ayrı bulunur.

Komut satırı:
    python eba_indir.py "https://f.eba.gov.tr/flippingbook/.../files/mobile/index.html#35"
    python eba_indir.py LINK1 LINK2        |   python eba_indir.py --liste linkler.txt
    python eba_indir.py --tani "LINK"      (sorun giderme: hangi adresler çalışıyor)
"""
import argparse
import re
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
from PIL import Image

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Kalite sırasına göre FlippingBook sayfa görseli kalıpları
KAYNAKLAR = [
    ("large",      "{base}/files/large/{n}.{ext}"),
    ("html5",      "{base}/files/assets/common/page-html5-substrates/page{n:04d}_4.{ext}"),
    ("substrates", "{base}/files/assets/common/page-substrates/page{n:04d}.{ext}"),
    ("mobile",     "{base}/files/mobile/{n}.{ext}"),
    ("page",       "{base}/files/page/{n}.{ext}"),
    ("thumb",      "{base}/files/thumb/{n}.{ext}"),
]
EXTS = ("jpg", "png", "webp", "jpeg")

CONFIG_YOLLARI = (
    "/javascript/config.js",
    "/files/assets/js/config.js",
    "/files/mobile/javascript/config.js",
)


class AgHatasi(Exception):
    """İnternet/sunucu hatası (sayfa 'yok' demek değildir)."""


class KitapHatasi(Exception):
    """Kullanıcıya gösterilebilecek, anlaşılır hata."""


def kitap_tabani(url):
    """Linkten kitabın kök adresini çıkarır (…/files/… öncesi)."""
    url = url.strip().split("#")[0].split("?")[0]
    p = urlparse(url)
    m = re.match(r"^(.*?)/(?:files|javascript)/", p.path)
    if m:
        yol = m.group(1)
    elif p.path.endswith(".html"):
        yol = p.path.rsplit("/", 1)[0]
    else:
        yol = p.path.rstrip("/")
    return urlunparse((p.scheme, p.netloc, yol, "", "", ""))


def oturum(base):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": base + "/files/mobile/index.html"})
    return s


def goruntu_mu(b):
    return (b[:3] == b"\xff\xd8\xff"
            or b[:8] == b"\x89PNG\r\n\x1a\n"
            or (b[:4] == b"RIFF" and b[8:12] == b"WEBP"))


def indir(s, url, deneme=3):
    """Görseli indirir. Yoksa None; ağ/sunucu sorununda tekrar dener, olmazsa AgHatasi."""
    son_hata = None
    for i in range(deneme):
        try:
            r = s.get(url, timeout=25)
        except requests.RequestException as e:
            son_hata = e
            time.sleep(1.5 * (i + 1))
            continue
        if r.status_code == 200:
            return r.content if goruntu_mu(r.content) else None
        if r.status_code in (404, 403, 410):
            return None
        son_hata = f"HTTP {r.status_code}"
        time.sleep(1.5 * (i + 1))
    raise AgHatasi(str(son_hata))


def sayfa_bul(s, base, kalip, n, ipucu=None, deneme=3):
    """n. sayfayı tüm uzantıları deneyerek arar. (veri, uzanti) ya da None döner."""
    sira = list(EXTS)
    if ipucu and ipucu[0] in sira:
        sira.remove(ipucu[0])
        sira.insert(0, ipucu[0])
    for ext in sira:
        veri = indir(s, kalip.format(base=base, n=n, ext=ext), deneme)
        if veri:
            return veri, ext
    return None


def kaynak_bul(s, base, tercih):
    """1. ve 2. sayfası bulunan ilk görsel klasörünü seçer."""
    adaylar = KAYNAKLAR if tercih == "auto" else [k for k in KAYNAKLAR if k[0] == tercih]
    sadece_ilk = None
    for ad, kalip in adaylar:
        try:
            if sayfa_bul(s, base, kalip, 1, deneme=1):
                if sayfa_bul(s, base, kalip, 2, deneme=1):
                    return ad, kalip
                if sadece_ilk is None:
                    sadece_ilk = (ad, kalip)
        except AgHatasi:
            continue
    return sadece_ilk


def sayfa_sayisi_bul(s, base):
    for yol in CONFIG_YOLLARI:
        try:
            r = s.get(base + yol, timeout=20)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            m = re.search(r"totalPageCount[\"']?\s*[=:]\s*(\d+)", r.text)
            if m:
                return int(m.group(1))
    return None


def sayfalari_indir(s, base, kalip, toplam, klasor, kanal, ilerleme=None, en_fazla=None):
    ilerleme = ilerleme or (lambda *a: None)
    if toplam and en_fazla and toplam > en_fazla:
        raise KitapHatasi(f"Bu kitap {toplam} sayfa; en fazla {en_fazla} sayfa destekleniyor.")
    klasor.mkdir(parents=True, exist_ok=True)
    ipucu = [EXTS[0]]

    def isle(n):
        """Path = indirildi, None = sayfa yok, False = ağ hatası."""
        for var in klasor.glob(f"sayfa_{n:04d}.*"):          # kaldığı yerden devam
            if var.stat().st_size > 0:
                return n, var
        try:
            sonuc = sayfa_bul(s, base, kalip, n, ipucu)
        except AgHatasi:
            return n, False
        if sonuc is None:
            return n, None
        veri, ext = sonuc
        ipucu[0] = ext
        dosya = klasor / f"sayfa_{n:04d}.{ext}"
        dosya.write_bytes(veri)
        return n, dosya

    sonuc = {}
    with ThreadPoolExecutor(max_workers=kanal) as havuz:
        if toplam:
            for n, d in havuz.map(isle, range(1, toplam + 1)):
                sonuc[n] = d
                ilerleme("indiriliyor", len(sonuc), toplam)
        else:
            n0, bitti = 1, False
            while not bitti:
                for n, d in havuz.map(isle, range(n0, n0 + kanal * 2)):
                    if d is None:          # sayfa yok -> kitap bitti
                        bitti = True
                        break
                    sonuc[n] = d
                if en_fazla and len(sonuc) > en_fazla:
                    raise KitapHatasi(f"Bu kitap çok büyük; en fazla {en_fazla} sayfa destekleniyor.")
                n0 += kanal * 2
                ilerleme("indiriliyor", len(sonuc), None)

    dosyalar = [d for n, d in sorted(sonuc.items()) if d]
    eksik = [n for n, d in sorted(sonuc.items()) if not d]
    return dosyalar, eksik


def beyaza_yatir(im):
    """Şeffaf alanları siyah değil beyaz yapar, RGB'ye çevirir."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        zemin = Image.new("RGB", im.size, (255, 255, 255))
        zemin.paste(im, mask=im.split()[3])
        return zemin
    return im if im.mode == "RGB" else im.convert("RGB")


def pdf_yap(dosyalar, cikti, gecici):
    # Sayfa boyutlarının çoğunluğunu referans al; farklı olanları ona ölçekle
    boyutlar = []
    for d in dosyalar:
        with Image.open(d) as im:
            boyutlar.append(im.size)
    referans = Counter(boyutlar).most_common(1)[0][0]

    gecici.mkdir(parents=True, exist_ok=True)
    yollar = []
    for d, boyut in zip(dosyalar, boyutlar):
        if d.suffix.lower() in (".jpg", ".jpeg") and boyut == referans:
            yollar.append(d)                       # JPEG'i olduğu gibi kullan
            continue
        with Image.open(d) as im:
            im = beyaza_yatir(im)
            if boyut != referans:
                im = im.resize(referans, Image.LANCZOS)
            yeni = gecici / (d.stem + ".png")
            im.save(yeni)
        yollar.append(yeni)

    try:                                   # 1) img2pdf: yeniden sıkıştırmadan, diske akıtarak
        import img2pdf
        with open(cikti, "wb") as f:
            img2pdf.convert([str(y) for y in yollar], outputstream=f)
        return
    except Exception:
        pass
    imgs = [beyaza_yatir(Image.open(y)) for y in yollar]      # 2) Pillow yedeği
    imgs[0].save(cikti, "PDF", save_all=True, append_images=imgs[1:],
                 resolution=150.0, quality=95)


def pdf_olustur(url, cikti_pdf, calisma_klasoru, kanal=6, kaynak="auto",
                sayfa_sayisi=None, en_fazla=None, ilerleme=None, temizle=False):
    """
    Bir kitabı indirip PDF yapar. Hem komut satırı hem web uygulaması bunu kullanır.
    ilerleme(asama, a, b) geri çağrısı: "kaynak" | "sayfa_sayisi" | "indiriliyor" | "pdf"
    Dönüş: {"sayfa": int, "eksik": [..], "toplam": int|None}
    """
    ilerleme = ilerleme or (lambda *a: None)
    calisma_klasoru = Path(calisma_klasoru)
    base = kitap_tabani(url)
    s = oturum(base)

    bulunan = kaynak_bul(s, base, kaynak)
    if not bulunan:
        raise KitapHatasi("Kitabın sayfa görselleri bulunamadı "
                          "(link yanlış olabilir ya da EBA sunucusuna erişilemiyor).")
    kaynak_ad, kalip = bulunan
    ilerleme("kaynak", kaynak_ad, None)

    toplam = sayfa_sayisi or sayfa_sayisi_bul(s, base)
    ilerleme("sayfa_sayisi", toplam, None)

    dosyalar, eksik = sayfalari_indir(s, base, kalip, toplam, calisma_klasoru,
                                      kanal, ilerleme, en_fazla)
    if not dosyalar:
        raise KitapHatasi("Hiç sayfa indirilemedi.")

    ilerleme("pdf", len(dosyalar), None)
    Path(cikti_pdf).parent.mkdir(parents=True, exist_ok=True)
    pdf_yap(dosyalar, cikti_pdf, calisma_klasoru / "_duz")

    if temizle and not eksik:
        shutil.rmtree(calisma_klasoru, ignore_errors=True)
    return {"sayfa": len(dosyalar), "eksik": eksik, "toplam": toplam}


# ----------------------------- Komut satırı -----------------------------

def _cli_ilerleme(asama, a, b):
    if asama == "kaynak":
        print(f"  kaynak: {a}")
    elif asama == "sayfa_sayisi":
        print(f"  sayfa sayısı: {a if a else 'bilinmiyor, sonuna kadar denenecek'}")
    elif asama == "indiriliyor":
        print(f"\r  indiriliyor: {a}/{b}" if b else f"\r  indiriliyor: {a} sayfa", end="", flush=True)
    elif asama == "pdf":
        print(f"\n  PDF oluşturuluyor ({a} sayfa)…")


def kitabi_indir(url, args):
    base = kitap_tabani(url)
    ad = base.rstrip("/").rsplit("/", 1)[-1]
    print(f"\n=== {ad} ===")

    cikti_klasoru = Path(args.cikti)
    cikti_klasoru.mkdir(parents=True, exist_ok=True)
    pdf_yolu = cikti_klasoru / f"{ad}.pdf"
    try:
        sonuc = pdf_olustur(url, pdf_yolu, cikti_klasoru / f"{ad}_sayfalar",
                            kanal=args.is_parcacigi, kaynak=args.kaynak,
                            sayfa_sayisi=args.sayfa_sayisi, ilerleme=_cli_ilerleme,
                            temizle=not args.resimleri_sakla)
    except KitapHatasi as e:
        print(f"\n  ! {e}")
        print('    Sorun sürerse şunu çalıştırıp çıktıyı paylaşın:  python eba_indir.py --tani "LINK"')
        return False

    if sonuc["sayfa"] == 1 and sonuc["toplam"] != 1:
        print("  ! Yalnızca 1 sayfa indirilebildi. Şunu çalıştırıp çıktıyı paylaşın:")
        print('    python eba_indir.py --tani "LINK"')
    if sonuc["eksik"]:
        print(f"  ! İndirilemeyen sayfalar: {sonuc['eksik']} (tekrar çalıştırırsanız yalnızca eksikler denenir)")
    print(f"  ✓ Hazır: {pdf_yolu}")
    return True


def tani(base):
    """Hangi adreslerin çalıştığını ekrana döker (sorun gidermek için)."""
    s = oturum(base)
    print(f"Kitap kökü: {base}\n")

    print("-- config.js denemeleri --")
    for yol in CONFIG_YOLLARI:
        try:
            r = s.get(base + yol, timeout=20)
            m = re.search(r"totalPageCount[\"']?\s*[=:]\s*(\d+)", r.text)
            print(f"{r.status_code}  {yol}  sayfa sayısı: {m.group(1) if m else '-'}")
        except requests.RequestException as e:
            print(f"HATA  {yol}  {e}")

    print("\n-- sayfa görseli denemeleri (404 olmayanlar) --")
    adresler = []
    for ad, kalip in KAYNAKLAR:
        for ext in EXTS:
            for n in (1, 2, 3, 4):
                adresler.append(kalip.format(base=base, n=n, ext=ext))
    for klasor in ("page-vectorshapes", "page-textlayers", "page-svgs", "page-text"):
        for ext in ("svg", "json", "js", "html"):
            for n in (1, 2, 3):
                adresler.append(f"{base}/files/assets/common/{klasor}/page{n:04d}.{ext}")
    for n in (1, 2, 3):
        adresler.append(f"{base}/files/assets/basic-html/page-{n}.html")

    bulundu = False
    for url in adresler:
        try:
            r = s.get(url, timeout=20)
        except requests.RequestException as e:
            print(f"HATA  {url}  {e}")
            continue
        if r.status_code != 404:
            bulundu = True
            print(f"{r.status_code}  {r.headers.get('Content-Type', '?')}  "
                  f"{len(r.content)} bayt  {url.replace(base, '')}")
    if not bulundu:
        print("Hiçbir kalıp çalışmadı.")


def main():
    ap = argparse.ArgumentParser(description="EBA FlippingBook kitap indirici")
    ap.add_argument("linkler", nargs="*", help="Kitap linkleri")
    ap.add_argument("--liste", help="Her satırda bir link olan metin dosyası")
    ap.add_argument("--cikti", default="eba_kitaplar")
    ap.add_argument("--kaynak", default="auto", choices=["auto"] + [k[0] for k in KAYNAKLAR])
    ap.add_argument("--sayfa-sayisi", type=int)
    ap.add_argument("--is-parcacigi", type=int, default=6)
    ap.add_argument("--resimleri-sakla", action="store_true")
    ap.add_argument("--tani", action="store_true", help="İndirmez; hangi adreslerin çalıştığını gösterir")
    args = ap.parse_args()

    linkler = list(args.linkler)
    if args.liste:
        linkler += [l.strip() for l in Path(args.liste).read_text(encoding="utf-8").splitlines()
                    if l.strip() and not l.strip().startswith("#")]
    if not linkler:
        ap.print_help()
        sys.exit(1)

    if args.tani:
        for l in linkler:
            tani(kitap_tabani(l))
        return

    basarili = sum(kitabi_indir(l, args) for l in linkler)
    print(f"\nBitti: {basarili}/{len(linkler)} kitap indirildi.")


if __name__ == "__main__":
    main()
