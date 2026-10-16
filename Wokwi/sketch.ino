/*
  Pengingat Obat Lansia - ESP32 (Wokwi)

  Alur:
    Bot Telegram -> MQTT  obatku/<DEVICE_ID>/jadwal  -> ESP32 (alarm sesuai jam)
    Lansia tekan tombol  -> MQTT  obatku/<DEVICE_ID>/event  -> Bot -> notif keluarga

  Format JSON jadwal (publish dengan flag retained):
    {"obat":[{"nama":"Amoxicillin 500mg","dosis":"1 tablet","aturan":"sesudah makan",
              "jam":["07:00","13:00","19:00"]}]}

  Pin:
    OLED SDA=21, SCL=22 | Tombol=4 (ke GND) | Buzzer=18 | LED merah=26 | LED hijau=27
*/

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <time.h>

// ===================== KONFIGURASI =====================
const char*    WIFI_SSID = "Wokwi-GUEST";   // WiFi virtual bawaan Wokwi
const char*    WIFI_PASS = "";
// Broker EMQX Cloud Serverless (TLS). Isi dari halaman Overview deployment kamu.
const char*    MQTT_HOST = "a0006f86.ala.asia-southeast1.emqxsl.com";      // contoh: xxxxxxxx.emqxsl.com
const uint16_t MQTT_PORT = 8883;                 // port TLS (mqtts), cek di Overview
const char*    MQTT_USER = "MedsRemind";  // kredensial khusus alat
const char*    MQTT_PASS = "MedsRemindHackathon";
// Hapus nilai asli ini sebelum project Wokwi dibagikan/dipublikasikan!

// Samakan persis dengan yang dipakai bot Python.
const char* DEVICE_ID = "lansia-001";

const long GMT_OFFSET_DETIK = 7 * 3600;     // WIB = UTC+7

// Alarm dianggap "terlewat" kalau tombol tidak ditekan sampai batas ini.
// Untuk demo ke juri, boleh dipersingkat (misal 60 * 1000UL).
const unsigned long ALARM_TIMEOUT_MS = 10UL * 60UL * 1000UL;
// Seberapa sering kalimat pengingat diulang selama alarm aktif
const unsigned long ULANG_UCAP_MS = 30UL * 1000UL;

// ===================== PIN =====================
#define PIN_BTN        4
#define PIN_BUZZ       18
#define PIN_LED_MERAH  26
#define PIN_LED_HIJAU  27

// ===================== OBJEK =====================
Adafruit_SSD1306 oled(128, 64, &Wire, -1);
WiFiClientSecure wifiClient;   // koneksi MQTT terenkripsi (TLS)
PubSubClient mqtt(wifiClient);

String TOPIC_JADWAL = String("obatku/") + DEVICE_ID + "/jadwal";  // bot -> alat
String TOPIC_TEST   = String("obatku/") + DEVICE_ID + "/test";    // bot/dev -> alat (tes alarm)
String TOPIC_EVENT  = String("obatku/") + DEVICE_ID + "/event";   // alat -> bot

// ===================== DATA JADWAL =====================
struct Pengingat {
  String nama;
  String dosis;
  String aturan;
  int menit;         // menit sejak 00:00 (07:00 -> 420)
  int terakhirYday;  // hari (tm_yday) terakhir alarm ini sudah bunyi, -1 = belum
};

const int MAKS = 24;
Pengingat daftar[MAKS];
int jumlah = 0;
String idJadwalTerakhir = "";   // id jadwal yang sudah diproses (untuk mengabaikan kiriman ulang retained)

// ===================== STATE =====================
bool waktuValid = false;
bool ntpMulai = false;

bool alarmAktif = false;
String alarmNama = "";
String alarmDosis = "";
unsigned long alarmMulai = 0;
unsigned long terakhirKedip = 0;
unsigned long terakhirUcap = 0;
bool kedipState = false;

unsigned long hijauSampai = 0;
unsigned long pesanSampai = 0;
String pesan1 = "";
String pesan2 = "";

// Deklarasi di muka supaya urutan fungsi bebas
void publishEvent(const char* status, const String& obat, int jumlahJadwal = -1);
void mulaiAlarm(const String& nama, const String& dosis);

// ===================== MQTT: KIRIM EVENT =====================
void publishEvent(const char* status, const String& obat, int jumlahJadwal) {
  JsonDocument doc;
  doc["device"] = DEVICE_ID;
  doc["status"] = status;
  if (obat.length()) doc["obat"] = obat;
  if (jumlahJadwal >= 0) doc["jumlah"] = jumlahJadwal;

  struct tm t;
  if (getLocalTime(&t, 0)) {
    char buf[6];
    strftime(buf, sizeof(buf), "%H:%M", &t);
    doc["waktu"] = buf;
  }

  String out;
  serializeJson(doc, out);
  mqtt.publish(TOPIC_EVENT.c_str(), out.c_str());
  Serial.println("[MQTT >] " + out);
}

// ===================== MQTT: TERIMA JADWAL =====================
void terimaJadwal(byte* payload, unsigned int len) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload, len);
  if (err) {
    Serial.printf("[JADWAL] JSON error: %s\n", err.c_str());
    publishEvent("schedule_error", "");
    return;
  }

  // Broker mengirim ulang pesan retained setiap alat (re)connect. Kalau id-nya sama dengan yang
  // sudah diproses, abaikan: jangan reset jadwal dan jangan kirim konfirmasi lagi ke Telegram.
  String id = String(doc["id"] | "");
  if (id.length() && id == idJadwalTerakhir) {
    Serial.println("[JADWAL] sama dengan yang sudah tersimpan, dilewati");
    return;
  }
  idJadwalTerakhir = id;

  jumlah = 0;
  for (JsonObject o : doc["obat"].as<JsonArray>()) {
    for (JsonVariant j : o["jam"].as<JsonArray>()) {
      if (jumlah >= MAKS) break;
      const char* s = j | "";
      int hh, mm;
      if (sscanf(s, "%d:%d", &hh, &mm) != 2) continue;
      if (hh < 0 || hh > 23 || mm < 0 || mm > 59) continue;

      daftar[jumlah].nama = String(o["nama"] | "Obat");
      daftar[jumlah].dosis = String(o["dosis"] | "");
      daftar[jumlah].aturan = String(o["aturan"] | "");
      daftar[jumlah].menit = hh * 60 + mm;
      daftar[jumlah].terakhirYday = -1;
      jumlah++;
    }
  }

  Serial.printf("[JADWAL] %d pengingat tersimpan\n", jumlah);
  for (int i = 0; i < jumlah; i++) {
    Serial.printf("  %02d:%02d  %s\n", daftar[i].menit / 60, daftar[i].menit % 60,
                  daftar[i].nama.c_str());
  }
  publishEvent("schedule_received", "", jumlah);   // konfirmasi ke bot
}

void onMqtt(char* topic, byte* payload, unsigned int len) {
  Serial.printf("[MQTT <] %s (%u byte)\n", topic, len);   // log setiap pesan masuk (untuk diagnosis)
  String t = topic;
  if (t == TOPIC_JADWAL) {
    terimaJadwal(payload, len);
  } else if (t == TOPIC_TEST) {
    String nama = "";
    for (unsigned int i = 0; i < len; i++) nama += (char)payload[i];
    if (nama.length() == 0) nama = "Obat Tes";
    mulaiAlarm(nama, "");
  }
}

// ===================== KONEKSI =====================
void sambungMqtt() {
  static unsigned long terakhirCoba = 0;
  if (mqtt.connected()) return;          // PENTING: connect() saat sudah tersambung tetap return true,
                                         // sehingga tanpa baris ini alat subscribe ulang tiap 5 detik
  if (WiFi.status() != WL_CONNECTED) return;
  if (terakhirCoba != 0 && millis() - terakhirCoba < 5000) return;
  terakhirCoba = millis();

  String cid = "esp32-" + String(DEVICE_ID) + "-" + String((uint32_t)esp_random(), HEX);
  Serial.println("[MQTT] menyambung...");
  if (mqtt.connect(cid.c_str(), MQTT_USER, MQTT_PASS)) {
    Serial.println("[MQTT] tersambung");
    mqtt.subscribe(TOPIC_JADWAL.c_str(), 1);
    mqtt.subscribe(TOPIC_TEST.c_str());
    publishEvent("online", "");
  } else {
    Serial.printf("[MQTT] gagal, rc=%d\n", mqtt.state());
  }
}

// ===================== ALARM =====================
void ucapkan() {
  String kalimat = "Waktunya minum obat " + alarmNama + " hari ini";
  Serial.println("[SUARA] " + kalimat);
  // Versi hardware (ESP32 + MAX98357A + speaker): panggil TTS di sini, misal
  //   audio.connecttospeech(kalimat.c_str(), "id");
  // Di Wokwi suara tidak bisa disimulasikan, jadi cukup dicetak ke Serial.
}

void mulaiAlarm(const String& nama, const String& dosis) {
  if (alarmAktif) {                      // ada obat lain di jam yang sama -> gabung
    if (alarmNama.indexOf(nama) < 0) alarmNama += ", " + nama;
    return;
  }
  alarmAktif = true;
  alarmNama = nama;
  alarmDosis = dosis;
  alarmMulai = millis();
  terakhirUcap = 0;
  terakhirKedip = 0;
  publishEvent("alarm_started", nama);
}

void hentikanAlarm() {
  alarmAktif = false;
  noTone(PIN_BUZZ);
  digitalWrite(PIN_LED_MERAH, LOW);
}

void tampilPesan(const String& a, const String& b, unsigned long durasi) {
  pesan1 = a;
  pesan2 = b;
  pesanSampai = millis() + durasi;
}

void cekJadwal() {
  static unsigned long terakhir = 0;
  if (millis() - terakhir < 1000) return;
  terakhir = millis();

  struct tm t;
  if (!getLocalTime(&t, 0)) { waktuValid = false; return; }
  waktuValid = true;

  int sekarang = t.tm_hour * 60 + t.tm_min;
  for (int i = 0; i < jumlah; i++) {
    if (daftar[i].menit == sekarang && daftar[i].terakhirYday != t.tm_yday) {
      daftar[i].terakhirYday = t.tm_yday;   // tandai supaya tidak bunyi dua kali di menit yang sama
      mulaiAlarm(daftar[i].nama, daftar[i].dosis);
    }
  }
}

void urusAlarm() {
  if (!alarmAktif) return;
  unsigned long now = millis();

  if (now - alarmMulai >= ALARM_TIMEOUT_MS) {
    publishEvent("missed", alarmNama);
    hentikanAlarm();
    tampilPesan("Dosis terlewat", "Keluarga diberi tahu", 4000);
    return;
  }

  if (now - terakhirKedip >= 500) {          // kedip LED + bunyi buzzer tiap 0,5 detik
    terakhirKedip = now;
    kedipState = !kedipState;
    digitalWrite(PIN_LED_MERAH, kedipState);
    if (kedipState) tone(PIN_BUZZ, 2000); else noTone(PIN_BUZZ);
  }

  if (terakhirUcap == 0 || now - terakhirUcap >= ULANG_UCAP_MS) {
    terakhirUcap = now;
    ucapkan();
  }
}

void urusTombol() {
  static bool lastState = HIGH;
  static unsigned long lastChange = 0;
  bool s = digitalRead(PIN_BTN);

  if (s != lastState && millis() - lastChange > 50) {   // debounce 50 ms
    lastChange = millis();
    lastState = s;
    if (s == LOW && alarmAktif) {
      publishEvent("taken", alarmNama);
      hentikanAlarm();
      digitalWrite(PIN_LED_HIJAU, HIGH);
      hijauSampai = millis() + 3000;
      tampilPesan("Terima kasih!", "Sudah minum obat", 3000);
    }
  }
  if (hijauSampai && millis() > hijauSampai) {
    digitalWrite(PIN_LED_HIJAU, LOW);
    hijauSampai = 0;
  }
}

// ===================== TAMPILAN OLED =====================
void gambarLayar() {
  static unsigned long terakhir = 0;
  if (millis() - terakhir < 250) return;
  terakhir = millis();

  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(1);
  oled.setCursor(0, 0);

  if (millis() < pesanSampai) {
    oled.setTextSize(2);
    oled.println(pesan1);
    oled.setTextSize(1);
    oled.println();
    oled.println(pesan2);
  } else if (alarmAktif) {
    oled.println("WAKTUNYA MINUM OBAT!");
    oled.println();
    oled.setTextSize(2);
    oled.println(alarmNama);
    oled.setTextSize(1);
    if (alarmDosis.length()) oled.println(alarmDosis);
    oled.setCursor(0, 56);
    oled.print("Tekan tombol OK");
  } else {
    oled.println("Pengingat Obat");
    struct tm t;
    if (waktuValid && getLocalTime(&t, 0)) {
      char buf[9];
      strftime(buf, sizeof(buf), "%H:%M:%S", &t);
      oled.setTextSize(2);
      oled.setCursor(0, 14);
      oled.println(buf);
      oled.setTextSize(1);

      int sekarang = t.tm_hour * 60 + t.tm_min;
      int terbaik = -1, selisihMin = 9999;
      for (int i = 0; i < jumlah; i++) {
        int d = (daftar[i].menit - sekarang + 1440) % 1440;
        if (d < selisihMin) { selisihMin = d; terbaik = i; }
      }
      oled.setCursor(0, 36);
      if (terbaik >= 0) {
        oled.printf("Berikut: %02d:%02d\n", daftar[terbaik].menit / 60, daftar[terbaik].menit % 60);
        oled.println(daftar[terbaik].nama);
      } else {
        oled.println("Belum ada jadwal");
      }
    } else {
      oled.setCursor(0, 20);
      oled.println("Sinkron waktu...");
    }
    oled.setCursor(0, 56);
    oled.printf("WiFi:%s MQTT:%s", WiFi.status() == WL_CONNECTED ? "ok" : "--",
                mqtt.connected() ? "ok" : "--");
  }
  oled.display();
}

// ===================== SETUP & LOOP =====================
void setup() {
  Serial.begin(115200);
  pinMode(PIN_BTN, INPUT_PULLUP);
  pinMode(PIN_BUZZ, OUTPUT);
  pinMode(PIN_LED_MERAH, OUTPUT);
  pinMode(PIN_LED_HIJAU, OUTPUT);

  Wire.begin(21, 22);
  if (!oled.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("OLED tidak ditemukan");
  }
  oled.clearDisplay();
  oled.display();

  WiFi.begin(WIFI_SSID, WIFI_PASS, 6);

  wifiClient.setInsecure();   // enkripsi jalan, sertifikat server tidak diverifikasi (cukup untuk demo)
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqtt);
  mqtt.setBufferSize(1024);                  // default 256 byte terlalu kecil untuk banyak obat
}

void loop() {
  if (WiFi.status() == WL_CONNECTED && !ntpMulai) {
    configTime(GMT_OFFSET_DETIK, 0, "pool.ntp.org", "time.google.com");
    ntpMulai = true;
  }

  sambungMqtt();
  mqtt.loop();

  cekJadwal();
  urusAlarm();
  urusTombol();
  gambarLayar();
}
