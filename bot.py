"""
Bot Telegram Pengingat Obat Lansia

Alur:
  Keluarga kirim foto label obat -> AI (Claude/Gemini) ekstrak jadwal (JSON)
  -> bot tampilkan ringkasan + tombol Benar/Salah
  -> kalau Benar: publish jadwal (retained) ke MQTT  obatku/<device>/jadwal
  -> alat (ESP32) bunyi sesuai jam; event dari alat (taken/missed) masuk lewat
     MQTT obatku/<device>/event dan diteruskan ke Telegram keluarga.

Jalankan:  python bot.py     (isi dulu file .env, lihat .env.example)
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("obat-bot")

# ===================== KONFIGURASI (dari .env) =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_CA_CERT = os.getenv("MQTT_CA_CERT", "")      # opsional: path file CA kalau error sertifikat
PROVIDER = os.getenv("AI_PROVIDER", "claude").lower()   # "claude" atau "gemini"
AI_MODEL = os.getenv("AI_MODEL", "")               # kosong = pakai default di bawah
# Catatan: gemini-2.5-* aksesnya dibatasi untuk akun baru (error 404/403). Pakai seri 3.x.
# Alternatif lebih hemat: gemini-3.5-flash-lite. Cek daftar terbaru di ai.google.dev/gemini-api/docs/models
DEFAULT_MODEL = {"claude": "claude-sonnet-5-5", "gemini": "gemini-3.8-flash"}
# Model cadangan: dipakai kalau model utama sibuk (5xx) atau tidak ditemukan (404). Kosongkan = tanpa cadangan.
MODEL_CADANGAN = os.getenv("AI_MODEL_CADANGAN", "gemini-3.5-flash-lite" if PROVIDER == "gemini" else "")

PAIR_FILE = Path(os.getenv("PAIR_FILE", "pairing.json"))   # device_id -> daftar chat_id
NAMES_FILE = Path(os.getenv("NAMES_FILE", "namaalat.json"))  # device_id -> nama ramah (mis. "Mbah Sri")
# --- Suara (text-to-speech) ---
TTS_LAPTOP = os.getenv("TTS_LAPTOP", "1") == "1"        # putar suara di laptop saat alat membunyikan alarm
TTS_TELEGRAM = os.getenv("TTS_TELEGRAM", "0") == "1"    # kirim voice note ke keluarga tiap alarm mulai
BAHASA_FILE = Path(os.getenv("BAHASA_FILE", "bahasa.json"))   # device_id -> bahasa suara
SUARA_DIR = Path(os.getenv("SUARA_DIR", "suara_cache"))       # cache mp3 supaya cepat dan hemat internet
BAHASA = {   # kalimat Sunda: mohon dicek penutur asli sebelum demo
    "indonesia": {"kode": "id", "label": "Indonesia", "kalimat": "Waktunya minum obat {nama} hari ini"},
    "sunda": {"kode": "su", "label": "Sunda", "kalimat": "Mangga, waktosna nginum ubar {nama} ayeuna"},
}
ALIAS_BAHASA = {"id": "indonesia", "indonesia": "indonesia", "su": "sunda", "sunda": "sunda"}
JEDA_FILE = Path(os.getenv("JEDA_FILE", "jeda.json"))         # device_id -> obat yang dijeda (tidak dikirim ke alat)
JADWAL_FILE = Path(os.getenv("JADWAL_FILE", "jadwal.json"))   # device_id -> jadwal terakhir (untuk /list_obat)

# ===================== ATURAN JAM DEFAULT =====================
# Dipakai kalau label hanya menulis "3x sehari" tanpa jam.
JAM_DEFAULT = {
    1: ["07:00"],
    2: ["07:00", "19:00"],
    3: ["07:00", "13:00", "19:00"],
    4: ["06:00", "12:00", "18:00", "22:00"],
}

PROMPT = """Kamu membaca FOTO LABEL OBAT dari klinik/apotek di Indonesia.
Keluarkan HANYA JSON valid (tanpa teks lain, tanpa ```), dengan bentuk:
{
  "obat": [
    {
      "nama": "nama obat + kekuatan, mis. Amoxicillin 500mg",
      "dosis": "jumlah sekali minum, mis. 1 tablet / 5 ml",
      "frekuensi_per_hari": 3,
      "aturan": "sebelum makan / sesudah makan / bersama makan / null",
      "jam_tertulis": ["07:00"]
    }
  ],
  "catatan": "hal penting lain di label (mis. habiskan antibiotik) atau null"
}
Aturan:
- "3 x 1" atau "3x sehari" berarti frekuensi_per_hari = 3. "tiap 8 jam" berarti 3, "tiap 12 jam" berarti 2.
- Isi "jam_tertulis" HANYA kalau label menulis jam secara eksplisit; kalau tidak, isi [].
- Jangan menebak. Kalau sebuah field tidak terbaca, isi null. Jangan mengarang nama obat.
- Kalau foto bukan label obat atau tidak terbaca sama sekali, kembalikan {"obat": [], "catatan": "alasan singkat"}.
- Satu label bisa berisi lebih dari satu obat; masukkan semuanya."""


# ===================== PAIRING (family <-> alat) =====================
def load_pairs() -> dict:
    try:
        return json.loads(PAIR_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_pairs(d: dict) -> None:
    PAIR_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def device_of(chat_id: int):
    for dev, chats in load_pairs().items():
        if chat_id in chats:
            return dev
    return None


# ===================== NAMA ALAT (nama ramah, opsional) =====================
def load_names() -> dict:
    try:
        return json.loads(NAMES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_names(d: dict) -> None:
    NAMES_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def bersihkan_nama(teks: str) -> str:
    """Buang karakter yang merusak Markdown Telegram dan batasi panjang."""
    return re.sub(r"[*_`\[\]]", "", teks).strip()[:30]


# ===================== JADWAL TERSIMPAN (untuk /list_obat) =====================
WIB = timezone(timedelta(hours=7))      # sama dengan GMT_OFFSET_DETIK di sketch
_kunci = threading.Lock()               # file ditulis dari thread MQTT dan thread Telegram


def bersih(x) -> str:
    """Buang karakter yang merusak Markdown Telegram."""
    return re.sub(r"[*_`\[\]]", "", str(x or "")).strip()


def bersihkan_obat(daftar) -> list:
    """Validasi daftar obat dari sumber mana pun (bot atau pesan retained di broker)."""
    hasil = []
    if not isinstance(daftar, list):
        return hasil
    for o in daftar:
        if not isinstance(o, dict):
            continue
        jam = []
        for j in o.get("jam") or []:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(j).strip())
            if m and int(m[1]) < 24 and int(m[2]) < 60:
                jam.append(f"{int(m[1]):02d}:{m[2]}")
        nama = bersih(o.get("nama"))
        if nama and jam:
            hasil.append({"nama": nama, "dosis": bersih(o.get("dosis")),
                          "aturan": bersih(o.get("aturan")), "jam": sorted(set(jam))})
    return hasil


def load_jadwal() -> dict:
    try:
        return json.loads(JADWAL_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def simpan_jadwal(device: str, obat, id_=None, diterima=None) -> None:
    """Catat jadwal terakhir sebuah alat. Jadwal dengan id baru mereset status 'diterima'."""
    obat = bersihkan_obat(obat)
    if not obat:
        return
    with _kunci:
        d = load_jadwal()
        rec = d.get(device, {})
        if id_ is None or rec.get("id") != id_:
            rec["diterima"] = diterima
            rec["diperbarui"] = datetime.now(WIB).strftime("%d/%m %H:%M")
        elif diterima is not None:
            rec["diterima"] = diterima
        rec["obat"], rec["id"] = obat, id_
        d[device] = rec
        JADWAL_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def tandai_diterima(device: str) -> None:
    with _kunci:
        d = load_jadwal()
        if device in d:
            d[device]["diterima"] = True
            d[device]["diterima_pada"] = datetime.now(WIB).strftime("%H:%M")
            JADWAL_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def load_jeda() -> dict:
    try:
        return json.loads(JEDA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def simpan_jeda(device: str, daftar: list) -> None:
    with _kunci:
        d = load_jeda()
        if daftar:
            d[device] = daftar
        else:
            d.pop(device, None)
        JEDA_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def jeda_buang_nama(device: str, nama_list) -> None:
    """Obat yang ditambahkan/diperbarui lewat foto atau manual otomatis aktif lagi (keluar dari daftar jeda)."""
    nama = {n.casefold() for n in nama_list}
    jeda = load_jeda().get(device, [])
    sisa = [o for o in jeda if o["nama"].casefold() not in nama]
    if len(sisa) != len(jeda):
        simpan_jeda(device, sisa)


def hapus_rekaman(device: str) -> None:
    """Hapus catatan jadwal lokal sebuah alat (dipakai saat obat terakhir dihapus)."""
    with _kunci:
        d = load_jadwal()
        if d.pop(device, None) is not None:
            JADWAL_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def pengingat_berikutnya(obat: list, sekarang: datetime):
    """Cari jam alarm terdekat (hari ini, atau besok kalau sudah lewat semua)."""
    menit_now = sekarang.hour * 60 + sekarang.minute
    slot = {}                                    # menit -> daftar nama obat
    for o in obat:
        for j in o["jam"]:
            h, m = map(int, j.split(":"))
            slot.setdefault(h * 60 + m, []).append(o["nama"])
    if not slot:
        return None
    sisa = [m for m in slot if m >= menit_now]
    target, hari = (min(sisa), "hari ini") if sisa else (min(slot), "besok")
    return f"{target // 60:02d}:{target % 60:02d} ({hari}) - {', '.join(slot[target])}"


def teks_daftar(device: str, sekarang=None):
    rec = load_jadwal().get(device)
    jeda = load_jeda().get(device, [])
    if (not rec or not rec.get("obat")) and not jeda:
        return None
    if not rec or not rec.get("obat"):
        return ("⏸ *Semua obat sedang dijeda* (alat tidak berbunyi):\n"
                + "\n".join(f"🔕 {o['nama']}" for o in jeda) + "\n\nAktifkan lagi lewat /pengingat.")
    sekarang = sekarang or datetime.now(WIB)
    nama = load_names().get(device)
    judul = f"📋 *Daftar obat - {nama}* (`{device}`)" if nama else f"📋 *Daftar obat - alat* `{device}`"
    if rec.get("diterima") is True:
        status = f"✅ Alat sudah menerima jadwal ini (pukul {rec.get('diterima_pada', '?')})"
    elif rec.get("diterima") is False:
        status = "⏳ Menunggu konfirmasi dari alat"
    else:
        status = ""
    berikut = pengingat_berikutnya(rec["obat"], sekarang)
    bagian = [judul, ringkas(rec["obat"])]
    if berikut:
        bagian.append(f"⏭️ Pengingat berikutnya: {berikut}")
    if jeda:
        bagian.append("🔕 *Dijeda* (tidak berbunyi): " + ", ".join(o["nama"] for o in jeda)
                      + "\nAktifkan lagi lewat /pengingat.")
    if status:
        bagian.append(status)
    if rec.get("diperbarui"):
        bagian.append(f"🕒 Jadwal diperbarui {rec['diperbarui']} WIB")
    return "\n\n".join(bagian)


# ===================== AI: BACA LABEL =====================
def ekstrak_json(teks: str) -> dict:
    """Ambil objek JSON pertama dari balasan model (toleran terhadap ```json ... ```)."""
    a, b = teks.find("{"), teks.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("balasan model bukan JSON")
    return json.loads(teks[a:b + 1])


def _gemini(foto: bytes, model: str) -> dict:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=foto, mime_type="image/jpeg"), PROMPT],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return ekstrak_json(resp.text)


def _claude(foto: bytes, model: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(foto).decode()}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    return ekstrak_json("".join(b.text for b in resp.content if b.type == "text"))


def _kode(e: Exception):
    return getattr(e, "code", None) or getattr(e, "status_code", None)   # Gemini: .code, Claude: .status_code


SERVER_SIBUK = (500, 502, 503, 504)


def baca_label_sync(foto: bytes) -> dict:
    """Panggil AI. Error server sibuk (5xx) diulang 3x dengan jeda; kalau tetap gagal
    (atau model 404), pindah ke model cadangan."""
    fn = _gemini if PROVIDER == "gemini" else _claude
    utama = AI_MODEL or DEFAULT_MODEL.get(PROVIDER, "")
    urutan = [utama] + ([MODEL_CADANGAN] if MODEL_CADANGAN and MODEL_CADANGAN != utama else [])

    for idx, model in enumerate(urutan):
        try:
            for percobaan in range(3):
                try:
                    return fn(foto, model)
                except Exception as e:
                    if _kode(e) in SERVER_SIBUK and percobaan < 2:
                        jeda = 2 * (percobaan + 1)
                        log.warning("AI sibuk (%s) di %s, coba lagi %ss...", _kode(e), model, jeda)
                        time.sleep(jeda)
                        continue
                    raise
        except Exception as e:
            if idx < len(urutan) - 1 and _kode(e) in (404, *SERVER_SIBUK):
                log.warning("Model %s gagal (%s), pindah ke cadangan %s", model, _kode(e), urutan[idx + 1])
                continue
            raise


def normalisasi(hasil: dict) -> list:
    """Bersihkan output model jadi daftar obat yang aman dikirim ke alat.
    Jam dihitung di sini (bukan dipercayakan ke model) supaya konsisten."""
    keluar = []
    for o in hasil.get("obat") or []:
        nama = bersih(o.get("nama"))
        if not nama:
            continue
        jam = []
        for j in o.get("jam_tertulis") or []:
            m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(j))
            if m and int(m[1]) < 24 and int(m[2]) < 60:
                jam.append(f"{int(m[1]):02d}:{m[2]}")
        if not jam:
            try:
                f = int(o.get("frekuensi_per_hari"))
            except (TypeError, ValueError):
                f = 0
            jam = list(JAM_DEFAULT.get(f, []))
        if not jam:
            continue                      # frekuensi tidak jelas -> jangan kirim jadwal tebakan
        keluar.append({
            "nama": nama,
            "dosis": bersih(o.get("dosis")),
            "aturan": bersih(o.get("aturan")),
            "jam": sorted(set(jam)),
        })
    return keluar


def ringkas(obat: list) -> str:
    baris = []
    for o in obat:
        tambah = " · ".join(x for x in [o["dosis"], o["aturan"]] if x)
        baris.append(f"💊 *{o['nama']}*\n   {tambah or '-'}\n   ⏰ {', '.join(o['jam'])}")
    return "\n\n".join(baris)


# ===================== SUARA (TTS) =====================
def bahasa_alat(device: str) -> str:
    with _kunci:
        try:
            b = json.loads(BAHASA_FILE.read_text(encoding="utf-8")).get(device)
        except (OSError, ValueError):
            b = None
    return b if b in BAHASA else "indonesia"


def set_bahasa(device: str, bahasa: str) -> None:
    with _kunci:
        try:
            d = json.loads(BAHASA_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d[device] = bahasa
        BAHASA_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def buat_suara(teks: str, kode: str):
    """Teks -> file mp3 (di-cache). Mengembalikan Path, atau None kalau gagal (log menjelaskan sebabnya)."""
    try:
        from gtts import gTTS
    except ImportError:
        log.warning("TTS: gTTS belum terpasang. Jalankan: python -m pip install gTTS")
        return None
    SUARA_DIR.mkdir(exist_ok=True)
    f = SUARA_DIR / (hashlib.md5(f"{kode}|{teks}".encode()).hexdigest() + ".mp3")
    if f.exists() and f.stat().st_size > 0:
        return f
    try:
        gTTS(teks, lang=kode).save(str(f))      # butuh internet
    except Exception as e:
        log.warning("TTS gagal (bahasa=%s): %s", kode, e)
        f.unlink(missing_ok=True)
        return None
    return f


def suara_untuk(device: str, nama_obat: str):
    """File suara pengingat sesuai bahasa alat; kalau bahasa daerah gagal, jatuh ke Indonesia."""
    b = BAHASA[bahasa_alat(device)]
    f = buat_suara(b["kalimat"].format(nama=nama_obat), b["kode"])
    if f is None and b["kode"] != "id":
        i = BAHASA["indonesia"]
        f = buat_suara(i["kalimat"].format(nama=nama_obat), i["kode"])
    return f


def putar_laptop(f: Path) -> None:
    """Putar mp3 di laptop tanpa jendela pemutar (Windows: PowerShell + MediaPlayer bawaan)."""
    path = str(f.resolve())
    try:
        if os.name == "nt":
            q = path.replace("'", "''")
            ps = ("Add-Type -AssemblyName presentationCore; $p = New-Object System.Windows.Media.MediaPlayer; "
                  f"$p.Open([uri]'{q}'); Start-Sleep -Milliseconds 700; $p.Play(); Start-Sleep -Seconds 12")
            subprocess.run(["powershell", "-NoProfile", "-Sta", "-Command", ps], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin":
            subprocess.run(["afplay", path], timeout=30)
        else:
            pemutar = shutil.which("mpg123") or shutil.which("ffplay")
            if not pemutar:
                log.warning("TTS: tidak ada pemutar suara (pasang mpg123).")
                return
            args = [pemutar, "-q", path] if pemutar.endswith("mpg123") else [pemutar, "-nodisp", "-autoexit", path]
            subprocess.run(args, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warning("TTS: gagal memutar suara: %s", e)


def bunyikan_laptop(device: str, nama_obat: str) -> None:
    f = suara_untuk(device, nama_obat)
    if f:
        log.info("TTS: memutar suara di laptop (%s)", f.name)
        putar_laptop(f)


async def kirim_suara(bot, chat_id: int, device: str, nama_obat: str, caption: str = "🔊 Pengingat suara"):
    f = await asyncio.to_thread(suara_untuk, device, nama_obat)
    if f is None:
        await bot.send_message(chat_id, "😕 Suara belum bisa dibuat (cek internet dan gTTS di terminal bot).")
        return
    with open(f, "rb") as fh:
        await bot.send_voice(chat_id, fh, caption=caption)


# ===================== MQTT =====================
class Jembatan:
    """Menghubungkan thread MQTT (paho) ke event loop Telegram."""

    def __init__(self):
        self.loop = None
        self.app = None
        self.ack = {}                     # device -> waktu terakhir alat mengirim alarm_started
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="bot-obat-" + os.urandom(3).hex())
        self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        if MQTT_CA_CERT:
            self.client.tls_set(ca_certs=MQTT_CA_CERT)
        else:
            self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED)   # pakai CA bawaan sistem
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def start(self, loop, app):
        self.loop, self.app = loop, app
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_start()          # thread sendiri, auto-reconnect

    def on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            log.info("MQTT tersambung")
            client.subscribe("obatku/+/event", qos=1)   # re-subscribe tiap (re)connect
            client.subscribe("obatku/+/jadwal", qos=1)  # terima juga jadwal retained yang sudah ada di broker
        else:
            log.error("MQTT gagal: %s", rc)

    def publish_jadwal(self, device: str, obat: list) -> bool:
        # "id" unik per konfirmasi: alat memakainya untuk mengabaikan kiriman ulang pesan retained
        id_ = f"{int(time.time())}-{os.urandom(2).hex()}"
        info = self.client.publish(f"obatku/{device}/jadwal", json.dumps({"id": id_, "obat": obat}),
                                   qos=1, retain=True)
        ok = info.rc == mqtt.MQTT_ERR_SUCCESS
        if ok:
            simpan_jadwal(device, obat, id_, diterima=False)    # dicatat untuk /list_obat
        return ok

    def publish_kosong(self, device: str) -> bool:
        """Kirim jadwal kosong (semua obat dihapus). Pesan retained lama di broker tertimpa."""
        id_ = f"{int(time.time())}-{os.urandom(2).hex()}"
        info = self.client.publish(f"obatku/{device}/jadwal", json.dumps({"id": id_, "obat": []}),
                                   qos=1, retain=True)
        ok = info.rc == mqtt.MQTT_ERR_SUCCESS
        if ok:
            hapus_rekaman(device)
        return ok

    def publish_test(self, device: str, nama: str) -> bool:
        info = self.client.publish(f"obatku/{device}/test", nama, qos=0)
        log.info("TES -> obatku/%s/test (rc=%s, bot tersambung=%s)", device, info.rc, self.client.is_connected())
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def on_message(self, client, userdata, msg):
        try:
            _, device, jenis = msg.topic.split("/")[:3]
            data = json.loads(msg.payload.decode())
        except Exception:
            return
        if jenis == "jadwal":                                 # jadwal (juga yang retained) -> catat untuk /list_obat
            if isinstance(data, dict):
                simpan_jadwal(device, data.get("obat"), data.get("id"))
            return
        ev = data
        log.info("EVENT %s: %s", device, ev)
        if ev.get("status") == "alarm_started":
            self.ack[device] = time.time()                    # bukti alat menerima perintah (dipakai /tes)
            nama_obat = str(ev.get("obat") or "obat")
            if TTS_LAPTOP:                                    # suara diputar di laptop = "speaker alat"
                threading.Thread(target=bunyikan_laptop, args=(device, nama_obat), daemon=True).start()
            if TTS_TELEGRAM:
                for chat_id in load_pairs().get(device, []):
                    asyncio.run_coroutine_threadsafe(
                        kirim_suara(self.app.bot, chat_id, device, nama_obat), self.loop)
        elif ev.get("status") == "schedule_received":
            tandai_diterima(device)
        teks = teks_event(ev)
        if not teks:
            return
        nama = load_names().get(device)
        if nama:
            teks = f"🏷️ *{nama}*\n{teks}"
        for chat_id in load_pairs().get(device, []):
            asyncio.run_coroutine_threadsafe(
                self.app.bot.send_message(chat_id, teks, parse_mode="Markdown"), self.loop)


def teks_event(ev: dict):
    st, obat, waktu = ev.get("status"), ev.get("obat", ""), ev.get("waktu", "")
    if st == "taken":
        return f"✅ *{obat}* sudah diminum (pukul {waktu})."
    if st == "missed":
        return f"⚠️ *{obat}* BELUM diminum. Alarm sudah berbunyi tapi tombol tidak ditekan (pukul {waktu}). Mohon dicek."
    if st == "schedule_received":
        return f"📲 Alat sudah menerima {ev.get('jumlah', '?')} pengingat. Alarm akan berbunyi sesuai jam."
    if st == "schedule_error":
        return "❌ Alat gagal membaca jadwal. Coba kirim ulang fotonya."
    return None   # online / alarm_started: cukup dicatat di log


jembatan = Jembatan()


# ===================== HANDLER TELEGRAM =====================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Halo! Aku pengingat obat untuk lansia.\n\n"
            "Hubungkan dulu ke alat: ketik `/start ID_ALAT` (contoh: `/start lansia-001`).\n"
            "Setelah itu kirim *foto label obat* dan aku yang mengurus jadwalnya "
            "(atau ketik manual: /tambah_manual). Lihat semua perintah di /bantuan.",
            parse_mode="Markdown")
        return
    dev = re.sub(r"[^a-zA-Z0-9_-]", "", ctx.args[0])[:40]
    if not dev:
        await update.message.reply_text("ID alat tidak valid.")
        return
    pairs = load_pairs()
    for d in list(pairs):                          # satu chat hanya satu alat
        if update.effective_chat.id in pairs[d]:
            pairs[d].remove(update.effective_chat.id)
    pairs.setdefault(dev, []).append(update.effective_chat.id)
    save_pairs(pairs)
    await update.message.reply_text(
        f"✅ Terhubung ke alat `{dev}`. Sekarang kirim foto label obat.\n"
        "Tips: beri nama alat dengan /namaalat (mis. `/namaalat Mbah Sri`).", parse_mode="Markdown")


async def cmd_alat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    nama = load_names().get(dev)
    await update.message.reply_text(
        f"📟 ID alat: `{dev}`\n"
        f"🏷️ Nama: {nama if nama else '_(belum diberi nama)_'}\n\n"
        "Ubah nama: `/namaalat Nama Baru`\n"
        "Ganti alat: `/start ID_ALAT_BARU`",
        parse_mode="Markdown")


async def cmd_namaalat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    nama = bersihkan_nama(" ".join(ctx.args))
    names = load_names()
    if not nama:
        if dev in names:
            await update.message.reply_text(
                f"Nama alat saat ini: *{names[dev]}*\nUbah dengan `/namaalat Nama Baru`, "
                "hapus dengan `/namaalat -`", parse_mode="Markdown")
        else:
            await update.message.reply_text("Tulis namanya, contoh: `/namaalat Mbah Sri`", parse_mode="Markdown")
        return
    if nama == "-":
        names.pop(dev, None)
        save_names(names)
        await update.message.reply_text("Nama alat dihapus.")
        return
    names[dev] = nama
    save_names(names)
    await update.message.reply_text(f"✅ Alat `{dev}` sekarang bernama *{nama}*.", parse_mode="Markdown")


async def cmd_tes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    t0 = time.time()
    if not jembatan.publish_test(dev, "Obat Tes"):
        await update.message.reply_text(
            "❌ Bot belum tersambung ke broker MQTT, jadi perintah tes tidak terkirim.\n"
            "Lihat terminal bot (cari baris 'MQTT'), cek MQTT_HOST/USER/PASS di .env, lalu restart bot.")
        return
    await update.message.reply_text("🔔 Perintah tes dikirim. Menunggu respons alat (maks 6 detik)...")
    ctx.application.create_task(cek_tes(ctx.bot, update.effective_chat.id, dev, t0))


async def cek_tes(bot, chat_id: int, dev: str, t0: float):
    """Tunggu alat membalas alarm_started; kalau tidak ada, beri checklist penyebab."""
    for _ in range(12):
        await asyncio.sleep(0.5)
        if jembatan.ack.get(dev, 0) >= t0:
            await bot.send_message(chat_id, "✅ Alat merespons: alarm tes berbunyi. Tekan tombol OK di alat untuk mematikannya.")
            return
    await bot.send_message(chat_id, (
        "⚠️ Perintah tes sudah dikirim dari bot, tapi alat tidak merespons.\n\n"
        "Cek berurutan:\n"
        "1. Wokwi menyala dan Serial Monitor menampilkan \"[MQTT] tersambung\".\n"
        f"2. Saat /tes, Serial Monitor harus mencetak \"[MQTT <] obatku/{dev}/test\". "
        "Kalau tidak muncul: bandingkan ID di /alat dengan DEVICE_ID di sketch, harus sama persis.\n"
        "3. Mungkin alarm sedang aktif (tidak dikirim ulang). Tekan tombol OK di alat, lalu coba /tes lagi.\n"
        "4. Di EMQX: Access Control > Authorization, pastikan user alat boleh subscribe topic obatku/..."))


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    teks = teks_daftar(dev)
    if not teks:
        await update.message.reply_text(
            "Belum ada jadwal obat tercatat untuk alat ini.\nKirim *foto label obat* untuk membuatnya.",
            parse_mode="Markdown")
        return
    await update.message.reply_text(teks, parse_mode="Markdown")


CONTOH_MANUAL = ("Format: `/tambah_manual NAMA | DOSIS/ATURAN | JADWAL`\n"
                 "JADWAL = frekuensi (1x sampai 4x) atau jam sendiri.\n\n"
                 "Contoh:\n"
                 "`/tambah_manual Paracetamol 500mg | 1 tablet sesudah makan | 3x`\n"
                 "`/tambah_manual Amlodipine 5mg | 1 tablet | 08:00`\n"
                 "`/tambah_manual Vitamin C | 07:00 19:00`")


def parse_jadwal(jadwal: str):
    """Teks jadwal ('3x' atau '07:00 19:00') -> (daftar_jam, pesan_error). Jam default dihitung normalisasi()."""
    jam = [f"{h}:{m}" for h, m in re.findall(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", jadwal)]
    frek = None
    if not jam:
        m = re.search(r"(\d+)\s*(?:x|kali)", jadwal, re.I)
        frek = int(m[1]) if m else None
        if frek not in JAM_DEFAULT:
            return None, "JADWAL tidak jelas. Tulis frekuensi 1x sampai 4x (mis. `3x`) atau jam (mis. `07:00 19:00`)."
    o = normalisasi({"obat": [{"nama": "x", "jam_tertulis": jam, "frekuensi_per_hari": frek}]})
    if not o:
        return None, "Jam tidak valid (harus 00:00 sampai 23:59)."
    return o[0]["jam"], None


def gabung(lama: list, baru: list) -> list:
    """Tambahkan obat baru ke jadwal lama. Nama sama (tanpa peduli huruf besar/kecil) = diganti, bukan dobel."""
    nama_baru = {o["nama"].casefold() for o in baru}
    return [o for o in lama if o["nama"].casefold() not in nama_baru] + baru


def parse_manual(teks: str):
    """Ubah teks '/tambah_manual ...' jadi (daftar_obat, pesan_error). Jam dihitung lewat normalisasi()."""
    bagian = [b.strip() for b in teks.split("|")]
    if len(bagian) not in (2, 3) or not bagian[0] or not bagian[-1]:
        return None, "Format belum pas."
    nama, jadwal = bagian[0], bagian[-1]
    aturan = bagian[1] if len(bagian) == 3 else ""
    jam, err = parse_jadwal(jadwal)
    if err:
        return None, err
    obat = normalisasi({"obat": [{"nama": nama, "dosis": "", "aturan": aturan, "jam_tertulis": jam}]})
    if not obat:
        return None, "Nama obat tidak valid."
    return obat, None


async def cmd_tambah(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    teks = update.message.text or ""
    isi = teks.split(None, 1)[1] if len(teks.split(None, 1)) > 1 else ""
    if not isi.strip():
        await update.message.reply_text("➕ *Tambah obat manual*\n\n" + CONTOH_MANUAL, parse_mode="Markdown")
        return
    obat, err = parse_manual(isi)
    if err:
        await update.message.reply_text(f"❌ {err}\n\n{CONTOH_MANUAL}", parse_mode="Markdown")
        return
    ctx.user_data["pending_manual"] = obat
    tombol = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Benar, tambahkan", callback_data="man:ok"),
        InlineKeyboardButton("❌ Batal", callback_data="man:no"),
    ]])
    await update.message.reply_text(
        f"Obat ini akan *ditambahkan* ke jadwal yang sudah ada:\n\n{ringkas(obat)}\n\nSudah benar?",
        parse_mode="Markdown", reply_markup=tombol)


async def on_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    baru = ctx.user_data.pop("pending_manual", None)
    if q.data == "man:no" or not baru:
        await q.edit_message_text("Oke, dibatalkan." if q.data == "man:no"
                                  else "Sesi kedaluwarsa. Ketik /tambah_manual lagi.")
        return
    dev = device_of(q.message.chat_id)
    if not dev:
        await q.edit_message_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    rec = load_jadwal().get(dev) or {}
    gabungan = gabung(rec.get("obat", []), baru)      # nama sama = diganti, bukan dobel
    if not jembatan.publish_jadwal(dev, gabungan):
        await q.edit_message_text("❌ Gagal mengirim ke alat (MQTT belum tersambung). Coba lagi sebentar.")
        return
    jeda_buang_nama(dev, [o["nama"] for o in baru])
    await q.edit_message_text(f"📤 Jadwal lengkap dikirim ke alat `{dev}`:\n\n{ringkas(gabungan)}\n\n"
                              "Menunggu konfirmasi alat...", parse_mode="Markdown")


TTL_UBAH = 300   # detik; setelah itu permintaan ubah jam kedaluwarsa


async def cmd_ubah(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("ubah_jam", None)
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    rec = load_jadwal().get(dev)
    if not rec or not rec.get("obat"):
        await update.message.reply_text("Belum ada obat. Kirim foto label atau pakai /tambah_manual dulu.")
        return
    jid = rec.get("id") or "-"
    tombol = [[InlineKeyboardButton(f"⏰ {o['nama']} ({', '.join(o['jam'])})"[:60],
                                    callback_data=f"jam:{i}:{jid}")]
              for i, o in enumerate(rec["obat"])]
    tombol.append([InlineKeyboardButton("Batal", callback_data="jam:x:-")])
    await update.message.reply_text("Pilih obat yang jam pengingatnya mau diubah:",
                                    reply_markup=InlineKeyboardMarkup(tombol))


async def on_ubah(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, idx, jid = q.data.split(":", 2)
    except ValueError:
        return
    if idx == "x":
        ctx.user_data.pop("ubah_jam", None)
        await q.edit_message_text("Oke, tidak ada yang diubah.")
        return
    dev = device_of(q.message.chat_id)
    rec = load_jadwal().get(dev) if dev else None
    obat = rec.get("obat") if rec else None
    if not obat or (rec.get("id") or "-") != jid or not idx.isdigit() or int(idx) >= len(obat):
        await q.edit_message_text("Daftar obat sudah berubah. Ketik /ubah_jam lagi.")
        return
    o = obat[int(idx)]
    ctx.user_data["ubah_jam"] = {"nama": o["nama"], "ts": time.time()}
    await q.edit_message_text(
        f"⏰ *{o['nama']}*\nJam sekarang: {', '.join(o['jam'])}\n\n"
        "Balas pesan ini dengan jam baru:\n"
        "• jam sendiri: `07:00 12:00 19:00`\n"
        "• atau frekuensi: `3x` (jam otomatis)\n\n"
        "Ketik `batal` untuk membatalkan.", parse_mode="Markdown")


async def terapkan_ubah(update: Update, ctx: ContextTypes.DEFAULT_TYPE, minta: dict):
    """Dipanggil dari on_teks saat user sedang mengubah jam sebuah obat."""
    teks = (update.message.text or "").strip()
    if teks.lower() == "batal":
        ctx.user_data.pop("ubah_jam", None)
        await update.message.reply_text("Oke, tidak ada yang diubah.")
        return
    jam, err = parse_jadwal(teks)
    if err:
        await update.message.reply_text(f"❌ {err}\nCoba lagi, atau ketik `batal`.", parse_mode="Markdown")
        return
    dev = device_of(update.effective_chat.id)
    rec = (load_jadwal().get(dev) or {}) if dev else {}
    obat = [dict(o) for o in rec.get("obat", [])]
    target = next((o for o in obat if o["nama"].casefold() == minta["nama"].casefold()), None)
    if not target:
        ctx.user_data.pop("ubah_jam", None)
        await update.message.reply_text("Obat itu sudah tidak ada di jadwal. Ketik /ubah_jam lagi.")
        return
    target["jam"] = jam
    if not jembatan.publish_jadwal(dev, obat):
        await update.message.reply_text("❌ Gagal mengirim ke alat (MQTT belum tersambung). Kirim jamnya lagi sebentar lagi.")
        return
    ctx.user_data.pop("ubah_jam", None)
    await update.message.reply_text(f"📤 Jam *{target['nama']}* diubah. Jadwal lengkap dikirim ke alat:\n\n{ringkas(obat)}\n\n"
                                    "Menunggu konfirmasi alat...", parse_mode="Markdown")


async def cmd_bahasa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    if not ctx.args:
        skrg = BAHASA[bahasa_alat(dev)]["label"]
        await update.message.reply_text(
            f"🗣 Bahasa suara alat sekarang: *{skrg}*\n\nGanti dengan:\n"
            "`/bahasa indonesia`\n`/bahasa sunda`", parse_mode="Markdown")
        return
    pilihan = ALIAS_BAHASA.get(ctx.args[0].lower())
    if not pilihan:
        await update.message.reply_text("Pilihan: `indonesia` atau `sunda`.", parse_mode="Markdown")
        return
    set_bahasa(dev, pilihan)
    await update.message.reply_text(f"✅ Bahasa suara diganti ke *{BAHASA[pilihan]['label']}*. Ini contoh suaranya:",
                                    parse_mode="Markdown")
    await kirim_suara(ctx.bot, update.effective_chat.id, dev, "Obat Tes", caption="🔊 Contoh suara")


async def cmd_suara(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    obat = (load_jadwal().get(dev) or {}).get("obat") or []
    nama = obat[0]["nama"] if obat else "Obat Tes"
    await kirim_suara(ctx.bot, update.effective_chat.id, dev, nama)


def daftar_pengingat(dev: str):
    """(aktif, dijeda, id_jadwal) untuk sebuah alat."""
    rec = load_jadwal().get(dev) or {}
    return rec.get("obat") or [], load_jeda().get(dev, []), rec.get("id") or "-"


def tampilan_pengingat(dev: str):
    aktif, jeda, jid = daftar_pengingat(dev)
    tombol = []
    for i, o in enumerate(aktif + jeda):
        label = (f"🔔 {o['nama']} ({', '.join(o['jam'])})" if i < len(aktif)
                 else f"🔕 {o['nama']} (dijeda)")
        tombol.append([InlineKeyboardButton(label[:60], callback_data=f"tog:{i}:{jid}")])
    tombol.append([InlineKeyboardButton("Selesai", callback_data="tog:x:-")])
    teks = ("Atur obat mana yang berbunyi di alat.\n"
            "🔔 = berbunyi, 🔕 = dijeda. Tekan sebuah obat untuk menjeda atau mengaktifkannya.")
    return teks, InlineKeyboardMarkup(tombol)


async def cmd_pengingat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    aktif, jeda, _ = daftar_pengingat(dev)
    if not aktif and not jeda:
        await update.message.reply_text("Belum ada obat. Kirim foto label atau pakai /tambah_manual dulu.")
        return
    teks, kb = tampilan_pengingat(dev)
    await update.message.reply_text(teks, reply_markup=kb)


async def on_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, idx, jid = q.data.split(":", 2)
    except ValueError:
        return
    dev = device_of(q.message.chat_id)
    if idx == "x":
        aktif, jeda, _ = daftar_pengingat(dev) if dev else ([], [], "-")
        teks = "Selesai. 🔔 Berbunyi: " + (", ".join(o["nama"] for o in aktif) or "-")
        if jeda:
            teks += "\n🔕 Dijeda: " + ", ".join(o["nama"] for o in jeda)
        await q.edit_message_text(teks)
        return
    if not dev:
        return
    aktif, jeda, jid_skrg = daftar_pengingat(dev)
    if jid != jid_skrg or not idx.isdigit() or int(idx) >= len(aktif) + len(jeda):
        await q.edit_message_text("Daftar obat sudah berubah. Ketik /pengingat lagi.")
        return
    i = int(idx)
    if i < len(aktif):                                   # aktif -> jeda
        o = aktif[i]
        aktif_baru, jeda_baru = aktif[:i] + aktif[i + 1:], jeda + [o]
        aksi = f"🔕 {o['nama']} dijeda."
    else:                                                # jeda -> aktif
        o = jeda[i - len(aktif)]
        aktif_baru = gabung(aktif, [o])
        jeda_baru = [x for x in jeda if x is not o and x["nama"] != o["nama"]]
        aksi = f"🔔 {o['nama']} diaktifkan lagi."
    ok = jembatan.publish_jadwal(dev, aktif_baru) if aktif_baru else jembatan.publish_kosong(dev)
    if not ok:
        await q.edit_message_text("❌ Gagal mengirim ke alat (MQTT belum tersambung). Coba lagi sebentar.",
                                  reply_markup=q.message.reply_markup)
        return
    simpan_jeda(dev, jeda_baru)
    teks, kb = tampilan_pengingat(dev)
    await q.edit_message_text(f"{aksi} Jadwal terbaru sudah dikirim ke alat.\n\n{teks}", reply_markup=kb)


async def cmd_hapus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dev = device_of(update.effective_chat.id)
    if not dev:
        await update.message.reply_text("Belum terhubung. Ketik `/start ID_ALAT` dulu.", parse_mode="Markdown")
        return
    rec = load_jadwal().get(dev)
    if not rec or not rec.get("obat"):
        await update.message.reply_text("Belum ada obat yang tercatat untuk dihapus.")
        return
    jid = rec.get("id") or "-"
    tombol = [[InlineKeyboardButton(f"🗑 {o['nama']} ({', '.join(o['jam'])})"[:60],
                                    callback_data=f"del:{i}:{jid}")]
              for i, o in enumerate(rec["obat"])]
    tombol.append([InlineKeyboardButton("Batal", callback_data="del:x:-")])
    await update.message.reply_text("Pilih obat yang mau dihapus dari jadwal:",
                                    reply_markup=InlineKeyboardMarkup(tombol))


async def on_hapus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, idx, jid = q.data.split(":", 2)
    except ValueError:
        return
    if idx == "x":
        await q.edit_message_text("Oke, tidak ada yang dihapus.")
        return
    dev = device_of(q.message.chat_id)
    rec = load_jadwal().get(dev) if dev else None
    obat = rec.get("obat") if rec else None
    if not obat or (rec.get("id") or "-") != jid or not idx.isdigit() or int(idx) >= len(obat):
        await q.edit_message_text("Daftar obat sudah berubah. Ketik /hapus_obat lagi.")
        return
    i = int(idx)
    nama, sisa = obat[i]["nama"], obat[:i] + obat[i + 1:]
    ok = jembatan.publish_jadwal(dev, sisa) if sisa else jembatan.publish_kosong(dev)
    if not ok:
        await q.edit_message_text("❌ Gagal mengirim ke alat (MQTT belum tersambung). Coba lagi sebentar.")
        return
    if sisa:
        await q.edit_message_text(f"🗑 *{nama}* dihapus. Jadwal baru dikirim ke alat:\n\n{ringkas(sisa)}",
                                  parse_mode="Markdown")
    else:
        await q.edit_message_text(f"🗑 *{nama}* dihapus. Tidak ada obat tersisa; jadwal kosong dikirim ke alat.",
                                  parse_mode="Markdown")


async def cmd_bantuan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 CARA PAKAI SiMbah\n\n"
        "1) Hubungkan ke alat (sekali saja)\n"
        "/start ID_ALAT : contoh /start lansia-001\n"
        "/alat : lihat ID dan nama alat yang terhubung\n"
        "/namaalat NAMA : beri nama alat, mis. /namaalat Mbah Sri (kirim - untuk hapus nama)\n\n"
        "2) Isi jadwal obat\n"
        "• Kirim FOTO label obat. Cek hasil bacaan AI, lalu tekan Benar. "
        "Obat dari foto DITAMBAHKAN ke jadwal yang sudah ada (nama obat sama = diperbarui).\n"
        "• Atau ketik manual kalau foto buram:\n"
        "/tambah_manual NAMA | DOSIS/ATURAN | JADWAL\n"
        "  /tambah_manual Paracetamol 500mg | 1 tablet | 3x\n"
        "  /tambah_manual Amlodipine 5mg | 1 tablet | 08:00\n"
        "  JADWAL = 1x sampai 4x (jam otomatis) atau jam sendiri. Obat manual DITAMBAHKAN ke jadwal yang ada.\n\n"
        "3) Kelola jadwal\n"
        "/list_obat : lihat obat, jam alarm, dan status alat (alias /jadwal)\n"
        "/pengingat : pilih obat mana yang berbunyi (jeda atau aktifkan tiap obat)\n"
        "/ubah_jam : pilih obat dari daftar, lalu ganti jam pengingatnya\n"
        "/hapus_obat : hapus satu obat dari jadwal\n\n"
        "4) Cek alat\n"
        "/tes : bunyikan alarm tes di alat\n"
        "/suara : kirim contoh voice note pengingat\n"
        "/bahasa : pilih bahasa suara pengingat (indonesia atau sunda)\n\n"
        "Jam otomatis: 1x=07:00 | 2x=07:00,19:00 | 3x=07:00,13:00,19:00 | 4x=06:00,12:00,18:00,22:00\n"
        "Setelah alat berbunyi dan tombol ditekan, kamu dapat notifikasi sudah diminum. "
        "Kalau tidak ditekan, notifikasi belum diminum.\n\n"
        "ℹ️ Ini alat bantu pengingat, bukan pengganti saran dokter/apoteker.")


def jelaskan_error(e: Exception) -> str:
    """Terjemahkan error API AI jadi petunjuk yang bisa ditindaklanjuti."""
    kode = getattr(e, "code", None) or getattr(e, "status_code", None)   # Gemini: .code, Claude: .status_code
    if not isinstance(kode, int):
        return (f"❌ Gagal memproses foto ({type(e).__name__}). Coba kirim ulang. "
                "Kalau terus gagal, lihat detail error di terminal bot.")
    model = AI_MODEL or DEFAULT_MODEL.get(PROVIDER, "?")
    if kode == 404:
        return (f"❌ Model AI {model} tidak ditemukan/tidak tersedia untuk akunmu (kode 404).\n"
                "Isi AI_MODEL= di .env dengan model yang tersedia, lalu restart bot.")
    if kode in (400, 401, 403):
        return (f"❌ Permintaan ke AI ditolak (kode {kode}). Cek API key dan nama model "
                f"({model}) di .env, lalu restart bot. Detail ada di terminal.")
    if kode == 429:
        return "⏳ Kuota/batas permintaan AI terlampaui (kode 429). Tunggu 1 menit lalu kirim ulang."
    if kode >= 500:
        return f"⚠️ Server AI sedang sibuk (kode {kode}), sudah dicoba beberapa kali. Kirim ulang fotonya 1-2 menit lagi."
    return f"❌ Gagal memproses foto (kode {kode}). Detail ada di terminal bot."


async def on_foto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    dev = device_of(chat_id)
    if not dev:
        await update.message.reply_text("Hubungkan ke alat dulu: `/start ID_ALAT`", parse_mode="Markdown")
        return
    pesan = await update.message.reply_text("🔍 Membaca label obat...")
    try:
        file = await update.message.photo[-1].get_file()       # resolusi terbesar
        foto = bytes(await file.download_as_bytearray())
        hasil = await asyncio.to_thread(baca_label_sync, foto)
    except Exception as e:
        log.exception("gagal baca label")      # detail lengkap ada di terminal
        await pesan.edit_text(jelaskan_error(e))
        return

    obat = normalisasi(hasil)
    if not obat:
        alasan = hasil.get("catatan") or "tidak ada obat/jadwal yang terbaca"
        await pesan.edit_text(f"😕 Aku tidak bisa membaca jadwal dari foto ini ({alasan}).\n"
                              "Coba foto lebih dekat, terang, dan tidak buram, atau ketik manual dengan /tambah_manual.")
        return

    ctx.user_data["pending"] = obat
    catatan = f"\n\n📝 {hasil['catatan']}" if hasil.get("catatan") else ""
    tombol = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Benar, tambahkan", callback_data="ok"),
        InlineKeyboardButton("❌ Salah", callback_data="no"),
    ]])
    await pesan.edit_text(
        "Aku membaca ini dari foto. *Mohon cocokkan dengan labelnya:*\n\n"
        f"{ringkas(obat)}{catatan}\n\n"
        "_Obat ini DITAMBAHKAN ke jadwal yang sudah ada. Jam dibuat otomatis dari aturan minum "
        "(mis. 3x sehari = 07:00, 13:00, 19:00); bisa diubah nanti lewat /ubah_jam._",
        parse_mode="Markdown", reply_markup=tombol)


async def on_tombol(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    obat = ctx.user_data.pop("pending", None)
    if q.data == "no" or not obat:
        await q.edit_message_text("Oke, dibatalkan. Kirim ulang foto yang lebih jelas ya."
                                  if q.data == "no" else "Sesi kedaluwarsa. Kirim ulang fotonya.")
        return
    dev = device_of(q.message.chat_id)
    rec = (load_jadwal().get(dev) or {}) if dev else {}
    gabungan = gabung(rec.get("obat", []), obat)      # ditambahkan ke jadwal lama, bukan menimpa
    if not dev or not jembatan.publish_jadwal(dev, gabungan):
        await q.edit_message_text("❌ Gagal mengirim ke alat (MQTT belum tersambung). Coba lagi sebentar.")
        return
    jeda_buang_nama(dev, [o["nama"] for o in obat])
    await q.edit_message_text(f"📤 Jadwal lengkap dikirim ke alat `{dev}`:\n\n{ringkas(gabungan)}\n\n"
                              "Menunggu konfirmasi alat...", parse_mode="Markdown")


async def on_teks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    minta = ctx.user_data.get("ubah_jam")
    if minta and time.time() - minta["ts"] < TTL_UBAH:
        await terapkan_ubah(update, ctx, minta)
        return
    ctx.user_data.pop("ubah_jam", None)
    await update.message.reply_text("Kirim *foto label obat* ya 📷 (atau /bantuan).", parse_mode="Markdown")


async def post_init(app: Application):
    jembatan.start(asyncio.get_running_loop(), app)


def main():
    for k in ("TELEGRAM_TOKEN", "MQTT_HOST", "MQTT_USER", "MQTT_PASS"):
        if not os.getenv(k):
            raise SystemExit(f"Isi {k} di file .env dulu (lihat .env.example)")
    key = "GEMINI_API_KEY" if PROVIDER == "gemini" else "ANTHROPIC_API_KEY"
    if not os.getenv(key):
        raise SystemExit(f"AI_PROVIDER={PROVIDER} tapi {key} belum diisi di .env")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("alat", cmd_alat))
    app.add_handler(CommandHandler("namaalat", cmd_namaalat))
    app.add_handler(CommandHandler(["list_obat", "listobat", "jadwal"], cmd_list))
    app.add_handler(CommandHandler(["tambah_manual", "tambahmanual"], cmd_tambah))
    app.add_handler(CommandHandler(["pengingat", "jeda"], cmd_pengingat))
    app.add_handler(CommandHandler(["ubah_jam", "ubahjam"], cmd_ubah))
    app.add_handler(CommandHandler("bahasa", cmd_bahasa))
    app.add_handler(CommandHandler("suara", cmd_suara))
    app.add_handler(CommandHandler(["hapus_obat", "hapusobat"], cmd_hapus))
    app.add_handler(CommandHandler("tes", cmd_tes))
    app.add_handler(CommandHandler("bantuan", cmd_bantuan))
    app.add_handler(MessageHandler(filters.PHOTO, on_foto))
    app.add_handler(CallbackQueryHandler(on_manual, pattern=r"^man:"))
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^tog:"))
    app.add_handler(CallbackQueryHandler(on_ubah, pattern=r"^jam:"))
    app.add_handler(CallbackQueryHandler(on_hapus, pattern=r"^del:"))   # harus sebelum handler umum
    app.add_handler(CallbackQueryHandler(on_tombol))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_teks))
    log.info("Bot jalan (AI: %s). Tekan Ctrl+C untuk berhenti.", PROVIDER)
    app.run_polling()


if __name__ == "__main__":
    main()