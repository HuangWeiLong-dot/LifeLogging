// 生活数据看板 —— 常亮 3.5" 屏，每 60s 拉一次 cms.liveinpassion.me 的实时快照。
// 数据全部来自已发布的公开只读接口，设备侧不持有任何 token。
#include <Arduino.h>
#include <WiFi.h>

#include "board.h"
#include "model.h"
#include "net.h"
#include "power.h"
#include "ui.h"

namespace {

constexpr uint32_t REFRESH_NOW_MS   = 60UL * 1000;
constexpr uint32_t REFRESH_TODAY_MS = 10UL * 60 * 1000;
constexpr uint32_t HEADER_MS        = 20UL * 1000;

NowModel g_now;
TodayModel g_today;
int g_page = 0;                 // 0 = NOW, 1 = TODAY
bool g_dirty = true;            // 需要整屏重画
uint32_t g_lastNow = 0;
uint32_t g_lastToday = 0;
uint32_t g_lastHeader = 0;

// ---- 触摸手势：点按 = 立即刷新，左右滑 = 翻页 ----
struct TouchState {
  bool down = false;
  bool swiped = false;   // 本次按下已经翻过页，抬手时不再当点按
  int16_t x0 = 0, y0 = 0;
  int16_t x = 0, y = 0;
  uint32_t t0 = 0;
} g_touch;

bool refreshNow(bool force) {
  if (!force && millis() - g_lastNow < REFRESH_NOW_MS) return g_now.valid;
  g_lastNow = millis();
  bool ok = net::fetchNow(g_now);
  Serial.printf("[now ] %s http=%d %s\n", ok ? "ok" : "FAIL", net::lastHttpCode(),
                ok ? g_now.app.c_str() : net::lastError().c_str());
  if (ok) ui::invalidate();
  return ok;
}

bool refreshToday(bool force) {
  if (!force && millis() - g_lastToday < REFRESH_TODAY_MS) return g_today.valid;
  g_lastToday = millis();
  bool ok = net::fetchToday(g_today);
  Serial.printf("[day ] %s http=%d %s\n", ok ? "ok" : "FAIL", net::lastHttpCode(),
                ok ? (String(g_today.trackedHours, 2) + "h tracked").c_str()
                   : net::lastError().c_str());
  if (ok) ui::invalidate();
  return ok;
}

void redraw() {
  g_dirty = false;
  g_lastHeader = millis();
  ui::drawPage(g_page, g_now, g_today, millis());
}

void handleTouch(uint32_t now) {
  int16_t x, y;
  bool pressed = board::touchRead(x, y);

  if (pressed && !g_touch.down) {
    g_touch.down = true;
    g_touch.swiped = false;
    g_touch.x0 = x;
    g_touch.y0 = y;
    g_touch.x = x;
    g_touch.y = y;
    g_touch.t0 = now;
    board::backlightSet(100);
    return;
  }

  if (!pressed && g_touch.down) {
    g_touch.down = false;
    int16_t dx = g_touch.x - g_touch.x0;
    int16_t dy = g_touch.y - g_touch.y0;
    uint32_t dt = now - g_touch.t0;
    if (g_touch.swiped) return;  // 滑动时已经翻过页了
    if (abs(dx) > 40 && abs(dx) > abs(dy)) {
      g_page = (dx < 0) ? 1 : 0;
      g_dirty = true;
    } else if (dt < 1500 && abs(dx) < 30 && abs(dy) < 30) {
      Serial.println("[tp ] -> TAP");
      ui::toast("refreshing");
      uint32_t t0 = millis();
      refreshNow(true);
      refreshToday(true);
      // 保证提示至少可见 0.8 秒，否则会被随后到来的整屏重画盖掉
      uint32_t took = millis() - t0;
      if (took < 800) delay(800 - took);
      g_dirty = true;
    }
    return;
  }

  if (pressed) {
    g_touch.x = x;
    g_touch.y = y;
    // 手指还没抬起来就翻页，反馈更跟手；swiped 锁住，避免一次拖动翻好几次
    if (!g_touch.swiped) {
      int16_t dx = x - g_touch.x0;
      int16_t dy = y - g_touch.y0;
      if (abs(dx) > 40 && abs(dx) > abs(dy)) {
        g_page = (dx < 0) ? 1 : 0;
        g_dirty = true;
        g_touch.swiped = true;
      }
    }
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[boot] life dashboard");

  power::begin();  // 必须先给屏幕供电，再点屏
  ui::begin();
  ui::splash("connecting wifi", "");

  if (!net::wifiBegin(25000)) {
    ui::drawMessage("wifi not configured", "put credentials in include/secrets.h", ui::kDanger);
    Serial.println("[wifi] 未连接 —— 检查 include/secrets.h");
    return;
  }
  Serial.printf("[wifi] %s  ip=%s\n", WiFi.SSID().c_str(), WiFi.localIP().toString().c_str());

  net::timeBegin();  // 只设时区；时钟靠第一次拉取时的 ts_utc 校准
  ui::splash("loading data", WiFi.localIP().toString().c_str());
  refreshNow(true);
  refreshToday(true);
  redraw();
}

void loop() {
  uint32_t now = millis();

  net::wifiEnsure();
  handleTouch(now);

  if (net::wifiConnected()) {
    if (now - g_lastNow >= REFRESH_NOW_MS) {
      refreshNow(true);
      g_dirty = true;
    }
    if (now - g_lastToday >= REFRESH_TODAY_MS) {
      refreshToday(true);
      g_dirty = true;
    }
  }

  // 整屏只在数据变化 / 翻页时重画；平时只刷顶部时钟条
  if (g_dirty) {
    redraw();
  } else if (now - g_lastHeader >= HEADER_MS) {
    ui::redrawHeader(g_now, g_today, now, g_page);
    g_lastHeader = now;
  }

  delay(5);
}
