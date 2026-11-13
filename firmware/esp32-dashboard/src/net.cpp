#include "net.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include <stdio.h>
#include <stdlib.h>
#include <sys/time.h>
#include <time.h>

#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASS
#define WIFI_PASS ""
#endif

namespace net {

static const char *NOW_URL  = "https://cms.liveinpassion.me/api/life/now";
static const char *DAYS_URL = "https://cms.liveinpassion.me/api/life?days=2";

static int g_httpCode = 0;
static String g_error;

int lastHttpCode() { return g_httpCode; }
const String &lastError() { return g_error; }

// 屏幕只有 ASCII 字形；翻译失败时可能残留中文，换成 '?' 而不是画方块
static String asciiify(const String &in) {
  String out;
  out.reserve(in.length());
  bool prevBad = false;
  for (size_t i = 0; i < in.length(); i++) {
    uint8_t c = (uint8_t)in[i];
    if (c >= 0x80) {
      if (!prevBad) out += '?';
      prevBad = true;
    } else {
      out += (char)c;
      prevBad = false;
    }
  }
  return out;
}

bool wifiBegin(uint32_t timeoutMs) {
  if (!*WIFI_SSID) return false;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // 常亮看板，别为省电牺牲响应速度
  WiFi.setHostname("life-dashboard");
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) {
    delay(200);
  }
  return WiFi.status() == WL_CONNECTED;
}

bool wifiConnected() { return WiFi.status() == WL_CONNECTED; }

void wifiEnsure() {
  if (!*WIFI_SSID) return;
  static uint32_t lastTry = 0;
  if (WiFi.status() == WL_CONNECTED || millis() - lastTry < 15000) return;
  lastTry = millis();
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

// 不用 SNTP：SNTP 后台解析和我们的 HTTPS 解析会撞到 lwIP 的 DNS 缓存清理，
// 触发 "Required to lock TCPIP core functionality" 断言。改用接口自带的 ts_utc 校时。
void timeBegin(const char *tz) {
  setenv("TZ", tz, 1);
  tzset();
}

// newlib 的 ESP32 版本没有 timegm，用 Howard Hinnant 的 days_from_civil 自己算
static time_t daysFromCivil(int y, int m, int d) {
  y -= m <= 2;
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = (unsigned)(y - era * 400);
  const unsigned doy = (153u * (m + (m > 2 ? -3 : 9)) + 2u) / 5u + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return (time_t)(era * 146097) + (time_t)doe - 719468;
}

void setClockFromIso(const String &iso) {
  int y, mo, d, h, mi, s;
  if (iso.length() < 19) return;
  if (sscanf(iso.c_str(), "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &s) != 6) return;
  if (y < 2020 || mo < 1 || mo > 12) return;
  time_t t = daysFromCivil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s;
  struct timeval tv = {t, 0};
  settimeofday(&tv, nullptr);
}

bool timeSynced() { return time(nullptr) > 1700000000; }

static String fmt(const char *f) {
  if (!timeSynced()) return "--";
  struct tm tm;
  time_t now = time(nullptr);
  localtime_r(&now, &tm);
  char buf[32];
  strftime(buf, sizeof(buf), f, &tm);
  return String(buf);
}

String clockHm()   { return fmt("%H:%M"); }
String clockDate() { return fmt("%a %d %b"); }

static bool httpGetOnce(const char *url, String &body) {
  WiFiClientSecure client;
  client.setInsecure();  // 公开只读快照；不做证书固定，换取稳定与省 RAM
  client.setTimeout(12000);

  HTTPClient http;
  http.setTimeout(12000);
  http.setConnectTimeout(8000);
  http.setReuse(false);
  if (!http.begin(client, url)) {
    g_error = "begin failed";
    return false;
  }
  http.addHeader("Accept", "application/json");
  g_httpCode = http.GET();
  if (g_httpCode != 200) {
    g_error = String("HTTP ") + g_httpCode;
    http.end();
    client.stop();
    return false;
  }
  body = http.getString();
  http.end();
  client.stop();
  return true;
}

// 连续两次 TLS 连接时，第二次偶发 CONNECTION_REFUSED(-1)：
// 上一个 socket 还没完全释放。等 400ms 重试一次即可。
static bool httpGet(const char *url, String &body) {
  if (WiFi.status() != WL_CONNECTED) {
    g_error = "wifi down";
    return false;
  }
  if (httpGetOnce(url, body)) return true;
  int first = g_httpCode;
  delay(400);
  if (httpGetOnce(url, body)) return true;
  g_error = String("HTTP ") + first + " (retry " + g_httpCode + ")";
  return false;
}

static void putStr(String &dst, JsonVariantConst v) {
  if (v.is<const char *>()) dst = asciiify(String(v.as<const char *>()));
  else dst = "";
}

bool fetchNow(NowModel &m) {
  String body;
  if (!httpGet(NOW_URL, body)) return false;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    g_error = err.c_str();
    return false;
  }

  NowModel n;
  n.valid = true;
  n.fetchedAtMs = millis();
  putStr(n.tsUtc, doc["ts_utc"]);
  setClockFromIso(n.tsUtc);

  JsonObjectConst loc = doc["location"];
  if (!loc.isNull()) {
    putStr(n.place, loc["place"]);
    putStr(n.city, loc["city"]);
    putStr(n.mode, loc["mode"]);
    n.stayMin = loc["stayMinutes"] | 0.0f;
    n.locStale = loc["stale"] | false;
    if (loc["lat"].is<float>() && loc["lon"].is<float>()) {
      n.hasPos = true;
      n.lat = loc["lat"].as<float>();
      n.lon = loc["lon"].as<float>();
    }
    if (n.place.isEmpty()) n.place = n.city;
  }

  JsonObjectConst d = doc["desktop"];
  if (!d.isNull()) {
    n.deskRunning = d["running"] | false;
    n.deskAfk = d["afk"] | false;
    putStr(n.app, d["app"]);
    putStr(n.title, d["title"]);
    putStr(n.category, d["category"]);
    putStr(n.since, d["since"]);
    n.todayHours = d["todayHours"] | 0.0f;
  }

  JsonObjectConst p = doc["phone"];
  if (!p.isNull()) {
    putStr(n.phoneApp, p["app"]);
    n.screenOn = p["screenOn"] | false;
    n.unlocks = p["unlocks"] | 0;
    n.notifications = p["notifications"] | 0;

    JsonObjectConst b = p["battery"];
    if (!b.isNull()) {
      n.batteryLevel = b["level"] | -1;
      n.batteryCharging = b["charging"] | false;
    }
    JsonObjectConst mu = p["music"];
    if (!mu.isNull()) {
      putStr(n.musicTitle, mu["title"]);
      putStr(n.musicApp, mu["app"]);
    }
  }

  JsonObjectConst w = doc["watch"];
  if (!w.isNull()) {
    n.steps = w["steps"] | 0.0f;
    n.hrAvg = w["hrAvg"] | 0.0f;
    n.hrMin = w["hrMin"] | 0.0f;
  }

  JsonObjectConst q = doc["qa"];
  if (!q.isNull()) {
    n.hasQa = true;
    putStr(n.question, q["question"]);
    putStr(n.answer, q["answer"]);
  }

  m = n;
  g_error = "";
  return true;
}

bool fetchToday(TodayModel &m) {
  String body;
  if (!httpGet(DAYS_URL, body)) return false;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    g_error = err.c_str();
    return false;
  }
  JsonArrayConst days = doc["days"];
  if (days.size() == 0) {
    g_error = "no days";
    return false;
  }
  JsonObjectConst day = days[days.size() - 1];

  TodayModel t;
  t.valid = true;
  t.fetchedAtMs = millis();
  putStr(t.day, day["day"]);
  t.trackedHours = day["trackedHours"] | 0.0f;
  t.collapsedHours = day["collapsedHours"] | 0.0f;
  t.aiCost = day["ai"]["cost"] | 0.0f;

  static const char *DIM_KEYS[7] = {"Work", "Study", "Life", "Transport",
                                    "Leisure", "Health", "Social"};
  for (int i = 0; i < 7; i++) t.dims[i] = day["dimensions"][DIM_KEYS[i]] | 0.0f;

  JsonObjectConst h = day["health"];
  t.steps = h["steps"] | 0;
  t.hrAvg = h["hrAvg"] | 0.0f;

  JsonObjectConst p = day["phone"];
  t.unlocks = p["unlocks"] | 0;
  t.phoneHours = (p["screenMinutes"] | 0.0f) / 60.0f;

  JsonObjectConst s = day["sleep"];
  t.hasSleep = !s.isNull();
  putStr(t.sleepOnset, s["onset"]);
  putStr(t.sleepWake, s["wake"]);
  t.sleepHours = s["hours"] | 0.0f;

  JsonObjectConst wttr = day["weather"];
  t.hasWeather = !wttr.isNull();
  t.tempMin = wttr["tempMin"] | 0.0f;
  t.tempMax = wttr["tempMax"] | 0.0f;
  putStr(t.condition, wttr["condition"]);

  m = t;
  g_error = "";
  return true;
}

}  // namespace net
