#include "ui.h"

#include <Arduino_GFX_Library.h>

// 由 tools/gen_gfxfont.py 从 Segoe UI 生成的 1-bit GFXfont（ASCII 0x20-0x7E）
#include "fonts/font_big.h"
#include "fonts/font_bold.h"
#include "fonts/font_small.h"

#include "board.h"
#include "net.h"

namespace {

Arduino_DataBus *g_bus = nullptr;
Arduino_ST7796 *panel = nullptr;   // 真实面板
Arduino_Canvas *cv[2] = {nullptr, nullptr};  // 两页各一块 PSRAM 帧缓冲
Arduino_Canvas *sel = nullptr;               // 正在绘制的那块
Arduino_GFX *g = nullptr;                    // 绘制目标（= sel 或 panel）
bool cvValid[2] = {false, false};            // 该页是否已渲染
uint32_t cvGen[2] = {0, 0};                  // 渲染时的数据版本
uint32_t g_gen = 1;                          // 数据每更新一次 +1，缓存作废

// 厂商 esp_lcd_st7796.c 的私有初始化序列。
// Arduino_GFX 自带的通用序列能用，但厂商这份是这块屏标定过的，照抄更稳。
void vendorInit() {
  g_bus->beginWrite();
  g_bus->writeCommand(0x11);  // SLPOUT
  g_bus->endWrite();
  delay(120);

  g_bus->beginWrite();
  g_bus->writeCommand(0x3A); g_bus->write(0x05);   // COLMOD = 16bit
  g_bus->writeCommand(0xF0); g_bus->write(0xC3);   // 解锁厂商寄存器
  g_bus->writeCommand(0xF0); g_bus->write(0x96);
  g_bus->writeCommand(0xB4); g_bus->write(0x01);
  g_bus->writeCommand(0xB7); g_bus->write(0xC6);
  g_bus->writeCommand(0xC0); g_bus->write(0x80); g_bus->write(0x45);
  g_bus->writeCommand(0xC1); g_bus->write(0x13);
  g_bus->writeCommand(0xC2); g_bus->write(0xA7);
  g_bus->writeCommand(0xC5); g_bus->write(0x0A);
  g_bus->writeCommand(0xE8);
  g_bus->write(0x40); g_bus->write(0x8A); g_bus->write(0x00); g_bus->write(0x00);
  g_bus->write(0x29); g_bus->write(0x19); g_bus->write(0xA5); g_bus->write(0x33);
  g_bus->writeCommand(0xE0);
  g_bus->write(0xD0); g_bus->write(0x08); g_bus->write(0x0F); g_bus->write(0x06);
  g_bus->write(0x06); g_bus->write(0x33); g_bus->write(0x30); g_bus->write(0x33);
  g_bus->write(0x47); g_bus->write(0x17); g_bus->write(0x13); g_bus->write(0x13);
  g_bus->write(0x2B); g_bus->write(0x31);
  g_bus->writeCommand(0xE1);
  g_bus->write(0xD0); g_bus->write(0x0A); g_bus->write(0x11); g_bus->write(0x0B);
  g_bus->write(0x09); g_bus->write(0x07); g_bus->write(0x2F); g_bus->write(0x33);
  g_bus->write(0x47); g_bus->write(0x38); g_bus->write(0x15); g_bus->write(0x16);
  g_bus->write(0x2C); g_bus->write(0x32);
  g_bus->writeCommand(0xF0); g_bus->write(0x3C);   // 重新上锁
  g_bus->writeCommand(0xF0); g_bus->write(0x69);
  g_bus->endWrite();
  delay(120);

  g_bus->beginWrite();
  g_bus->writeCommand(0x21);  // INVON
  g_bus->writeCommand(0x29);  // DISPON
  g_bus->endWrite();
}

const GFXfont *F_SMALL = &UIFontSmall;  // 11px
const GFXfont *F_BOLD  = &UIFontBold;   // 15px
const GFXfont *F_BIG   = &UIFontBig;    // 22px

// 配色对齐站点：#050505 近黑 + #60A5FA 蓝
constexpr uint16_t C_BG      = 0x0020;  // #050505
constexpr uint16_t C_CARD    = 0x1082;  // #101013
constexpr uint16_t C_BORDER  = 0x1944;  // #1F1F23
constexpr uint16_t C_TEXT    = 0xFFFF;  // #FAFAFA
constexpr uint16_t C_DIM     = 0x8C52;  // #8A8A93
constexpr uint16_t C_ACCENT  = 0x651F;  // #60A5FA
constexpr uint16_t C_GREEN   = 0x3693;  // #34D399
constexpr uint16_t C_AMBER   = 0xFDE4;  // #FBBF24
constexpr uint16_t C_RED     = 0xFB8E;  // #F87171

constexpr int SW = board::SCREEN_W;
constexpr int SH = board::SCREEN_H;

struct TextBox {
  int16_t x1, y1;
  uint16_t w, h;
};

TextBox measure(const char *s, const GFXfont *f) {
  TextBox b{0, 0, 0, 0};
  g->setFont(f);
  g->getTextBounds(s, 0, 0, &b.x1, &b.y1, &b.w, &b.h);
  return b;
}

// (x, yTop) 是文本框左上角，不是基线
void text(int16_t x, int16_t yTop, const char *s, uint16_t color, const GFXfont *f,
          bool rightAlign = false) {
  TextBox b = measure(s, f);
  int16_t cx = rightAlign ? (int16_t)(x - b.w - b.x1) : (int16_t)(x - b.x1);
  g->setFont(f);
  g->setTextColor(color);
  g->setCursor(cx, yTop - b.y1);
  g->print(s);
}

int16_t textW(const char *s, const GFXfont *f) { return measure(s, f).w; }

String elide(const String &s, const GFXfont *f, int16_t maxW) {
  if (s.isEmpty()) return s;
  if (measure(s.c_str(), f).w <= maxW) return s;
  String out = s;
  while (out.length() > 1) {
    out.remove(out.length() - 1);
    String t = out + "...";
    if (measure(t.c_str(), f).w <= maxW) return t;
  }
  return String("...");
}

void card(int16_t x, int16_t y, int16_t w, int16_t h) {
  g->fillRoundRect(x, y, w, h, 8, C_CARD);
  g->drawRoundRect(x, y, w, h, 8, C_BORDER);
}

void label(int16_t x, int16_t y, const char *s) { text(x, y, s, C_DIM, F_SMALL); }

String hm(float hours) {
  int m = (int)lroundf(hours * 60.0f);
  if (m <= 0) return String("0m");
  if (m < 60) return String(m) + "m";
  return String(m / 60) + "h" + String(m % 60) + "m";
}

String thousands(int v) {
  String s = String(v);
  String out;
  int n = s.length();
  for (int i = 0; i < n; i++) {
    if (i > 0 && (n - i) % 3 == 0) out += ',';
    out += s[i];
  }
  return out;
}

void header(const char *rightText, uint16_t dotColor, const char *leftLabel = nullptr) {
  g->fillRect(0, 0, SW, 46, C_BG);
  if (leftLabel) {
    text(12, 12, leftLabel, C_DIM, F_SMALL);
    String d = net::clockDate();
    text(12 + textW(leftLabel, F_SMALL) + 10, 12, d.c_str(), C_DIM, F_SMALL);
    text(12 + textW(leftLabel, F_SMALL) + 10 + textW(d.c_str(), F_SMALL) + 10, 14,
         net::clockHm().c_str(), C_TEXT, F_BOLD);
  } else {
    text(12, 2, net::clockHm().c_str(), C_TEXT, F_BIG);
    String t = net::clockHm();
    text(12 + textW(t.c_str(), F_BIG) + 12, 12, net::clockDate().c_str(), C_DIM, F_SMALL);
  }
  g->fillCircle(SW - 22, 16, 4, dotColor);
  text(SW - 32, 9, rightText, C_DIM, F_SMALL, true);
}

void pageDots(int active) {
  for (int i = 0; i < 2; i++) {
    uint16_t c = (i == active) ? C_ACCENT : C_BORDER;
    g->fillCircle(SW / 2 - 8 + i * 16, SH - 8, 3, c);
  }
}

uint16_t ageColor(uint32_t ageMs) {
  if (ageMs < 180000UL) return C_GREEN;
  if (ageMs < 600000UL) return C_AMBER;
  return C_RED;
}

String ageText(uint32_t ageMs) {
  uint32_t s = ageMs / 1000;
  if (s < 90) return String(s) + "s ago";
  return String(s / 60) + "m ago";
}

// 把当前这块帧缓冲整屏推给面板。
void present() {
  if (sel) sel->flush();
}

// 只推顶部 h 行：帧缓冲里整行是连续的，可以直接当一块位图发。
// 时钟每秒/每 20 秒刷一次，没必要每次都推 307KB。
void presentTop(int16_t h) {
  if (!sel) return;
  panel->draw16bitRGBBitmap(0, 0, sel->getFramebuffer(), SW, h);
}

// 整屏刷底色。走 canvas 时直接按 32 位字填 PSRAM，
// 比 Arduino_GFX 的逐像素 fillScreen 快一个数量级。
void clearScreen(uint16_t color) {
  if (!sel) {
    g->fillScreen(color);
    return;
  }
  uint32_t *p = (uint32_t *)sel->getFramebuffer();
  uint32_t v = ((uint32_t)color << 16) | color;
  for (uint32_t i = 0; i < (uint32_t)SW * SH / 2; i++) p[i] = v;
}

void selectCanvas(int page) {
  sel = cv[page & 1];
  g = sel ? (Arduino_GFX *)sel : (Arduino_GFX *)panel;
}

}  // namespace

namespace ui {

void begin() {
  board::i2cBegin();
  board::backlightBegin(0);  // 先压住背光，避免复位期间闪白
  board::lcdReset();
  Serial.printf("[io  ] tca cfg=0x%02X out=0x%02X\n", board::tcaRead(board::TCA_REG_CONFIG),
                board::tcaRead(board::TCA_REG_OUTPUT));
  g_bus = new Arduino_ESP32SPI(board::PIN_LCD_DC, GFX_NOT_DEFINED, board::PIN_LCD_SCLK,
                               board::PIN_LCD_MOSI, board::PIN_LCD_MISO);
  panel = new Arduino_ST7796(g_bus, GFX_NOT_DEFINED, 1 /* rotation: 480x320 横屏 */,
                             true /* IPS */, 320, 480);
  // 这块屏必须用 SPI MODE0。Arduino_GFX 在 ESP32 上对 ST7796 默认走 MODE3，
  // 表现就是完全黑屏（背光亮、面板无响应）。
  panel->begin(80000000, SPI_MODE0);
  vendorInit();
  panel->setRotation(1);

  // 两页各占一块 PSRAM 帧缓冲：翻页时只推屏、不重画。
  // Arduino_GFX 画 GFXfont 是逐像素 writePixel，直接画屏的话每个字都要几十次
  // 地址窗口设置，翻页肉眼可见地卡；走帧缓冲后同样的绘制只是 RAM 写入。
  for (int i = 0; i < 2; i++) {
    cv[i] = new Arduino_Canvas(SW, SH, panel);
    if (!cv[i]->begin(GFX_SKIP_OUTPUT_BEGIN)) {
      delete cv[i];
      cv[i] = nullptr;
    }
  }
  selectCanvas(0);
  Serial.printf("[lcd] 帧缓冲 A=%s B=%s\n", cv[0] ? "ok" : "fail", cv[1] ? "ok" : "fail");

  clearScreen(C_BG);
  present();
  board::backlightSet(100);
}

void splash(const char *line1, const char *line2) {
  clearScreen(C_BG);
  text(SW / 2, 110, "LIFE", C_ACCENT, F_BIG, true);
  TextBox b = measure("LIFE", F_BIG);
  text((SW + b.w) / 2 + 8, 116, "LOGGING", C_DIM, F_SMALL);
  text(SW / 2, 170, line1, C_TEXT, F_SMALL, true);
  if (line2 && *line2) text(SW / 2, 194, line2, C_DIM, F_SMALL, true);
  present();
}

void drawMessage(const char *title, const char *sub, uint16_t color) {
  g->fillRect(0, 46, SW, SH - 46, C_BG);
  text(SW / 2, 120, title, color, F_BOLD, true);
  if (sub && *sub) text(SW / 2, 152, sub, C_DIM, F_SMALL, true);
  present();
}

void toast(const char *msg) {
  int16_t tw = textW(msg, F_SMALL);
  int16_t w = tw + 28;
  int16_t x = (SW - w) / 2;
  g->fillRoundRect(x, SH - 56, w, 34, 8, C_ACCENT);
  text(x + 14, SH - 47, msg, 0x0020, F_SMALL);
  present();
}

void redrawHeader(const NowModel &n, const TodayModel &t, uint32_t nowMs, int page) {
  selectCanvas(page);
  if (page == 0) {
    uint32_t age = nowMs - n.fetchedAtMs;
    header(ageText(age).c_str(), ageColor(age));
  } else {
    uint32_t age = nowMs - t.fetchedAtMs;
    String right = t.valid ? ageText(age) : String("--");
    header(right.c_str(), t.valid ? ageColor(age) : C_RED, "TODAY");
  }
  presentTop(46);  // 只推顶部一条，9ms 而不是 63ms
}

void drawNow(const NowModel &n, const TodayModel &today, uint32_t nowMs) {
  clearScreen(C_BG);  // 卡片之间有缝隙，不整屏清会留下上一帧的字
  uint32_t age = nowMs - n.fetchedAtMs;
  header(ageText(age).c_str(), ageColor(age));

  // ---- LOCATION ----
  card(12, 50, 292, 96);
  label(24, 58, "LOCATION");
  String place = n.place.isEmpty() ? String("unknown") : n.place;
  text(24, 76, elide(place, F_BOLD, 268).c_str(), C_TEXT, F_BOLD);

  String meta;
  if (!n.mode.isEmpty()) meta = n.mode;
  if (n.stayMin > 0) meta += (meta.isEmpty() ? "" : "  |  ") + String((int)n.stayMin) + "m here";
  if (n.locStale) meta += "  |  stale";
  text(24, 102, elide(meta, F_SMALL, 268).c_str(), C_DIM, F_SMALL);

  if (n.hasPos) {
    String coords = String(n.lat, 4) + ", " + String(n.lon, 4);
    text(24, 122, coords.c_str(), C_BORDER, F_SMALL);
  }

  // ---- DESKTOP ----
  card(12, 150, 292, 96);
  label(24, 158, "DESKTOP");
  String desk;
  if (n.deskRunning) {
    desk = n.app;
    if (!n.category.isEmpty()) desk += "  |  " + n.category;
  } else {
    desk = "not running";
  }
  text(24, 176, elide(desk, F_BOLD, 268).c_str(), n.deskAfk ? C_DIM : C_TEXT, F_BOLD);
  text(24, 202, elide(n.title, F_SMALL, 268).c_str(), C_DIM, F_SMALL);
  String dmeta;
  if (!n.since.isEmpty()) dmeta = "since " + n.since;
  dmeta += (dmeta.isEmpty() ? "" : "  |  ") + hm(n.todayHours) + " tracked today";
  text(24, 220, elide(dmeta, F_SMALL, 268).c_str(), C_ACCENT, F_SMALL);

  // ---- WATCH ----
  card(312, 50, 156, 96);
  label(324, 58, "WATCH");
  text(324, 74, thousands((int)n.steps).c_str(), C_TEXT, F_BIG);
  text(324, 102, "steps", C_DIM, F_SMALL);
  String hr = n.hrAvg > 0 ? String((int)lroundf(n.hrAvg)) + " bpm avg" : "no hr";
  text(324, 120, hr.c_str(), C_DIM, F_SMALL);

  // ---- PHONE ----
  card(312, 150, 156, 96);
  label(324, 158, "PHONE");
  // 电量放标签行右侧；充电时用绿色，一眼能分辨
  if (n.batteryLevel >= 0) {
    String batt = String(n.batteryLevel) + "%";
    text(456, 158, batt.c_str(), n.batteryCharging ? C_GREEN : C_DIM, F_SMALL, true);
  }
  text(324, 174, String(n.unlocks).c_str(), C_TEXT, F_BIG);
  text(324, 202, "unlocks", C_DIM, F_SMALL);
  String ph = n.phoneApp.isEmpty() ? (n.screenOn ? "screen on" : "screen off") : n.phoneApp;
  if (n.screenOn) ph += "  |  on";
  text(324, 220, elide(ph, F_SMALL, 132).c_str(), n.screenOn ? C_ACCENT : C_DIM, F_SMALL);

  // ---- FOOTER ----
  card(12, 252, 456, 52);
  if (n.hasQa && !n.question.isEmpty()) {
    text(24, 258, "Q", C_ACCENT, F_SMALL);
    text(40, 258, elide(n.question, F_SMALL, 412).c_str(), C_TEXT, F_SMALL);
    text(24, 280, "A", C_DIM, F_SMALL);
    text(40, 280, elide(n.answer, F_SMALL, 412).c_str(), C_DIM, F_SMALL);
  } else if (n.hasQa) {
    text(24, 266, "answered:", C_DIM, F_SMALL);
    text(94, 266, elide(n.answer, F_SMALL, 358).c_str(), C_TEXT, F_SMALL);
  } else {
    text(24, 266, "no open question", C_DIM, F_SMALL);
    if (!n.musicTitle.isEmpty()) {
      // 在听歌就把歌名顶到右下角，比「今天记录了多少」有用
      text(456, 266, elide(n.musicTitle, F_SMALL, 250).c_str(), C_ACCENT, F_SMALL, true);
    } else {
      String u = "tracked " + hm(today.trackedHours) + " today";
      text(456, 266, u.c_str(), C_DIM, F_SMALL, true);
    }
  }

  pageDots(0);
}

void drawToday(const TodayModel &t, const NowModel &n, uint32_t nowMs) {
  (void)n;
  clearScreen(C_BG);
  uint32_t age = nowMs - t.fetchedAtMs;
  String right = t.valid ? ageText(age).c_str() : "--";
  header(right.c_str(), t.valid ? ageColor(age) : C_RED, "TODAY");

  // ---- 七维条形 ----
  card(12, 50, 292, 196);
  static const char *DIM_NAMES[7] = {"Work", "Study", "Life", "Transport",
                                     "Leisure", "Health", "Social"};
  float maxDim = 0.5f;
  for (int i = 0; i < 7; i++) maxDim = max(maxDim, t.dims[i]);

  const int rowY0 = 62;
  const int rowH = 26;
  const int barX = 96;
  const int barW = 140;
  for (int i = 0; i < 7; i++) {
    int y = rowY0 + i * rowH;
    text(24, y, DIM_NAMES[i], C_DIM, F_SMALL);
    g->fillRoundRect(barX, y + 3, barW, 8, 4, C_BORDER);
    int w = (int)(barW * (t.dims[i] / maxDim));
    if (w > 0) g->fillRoundRect(barX, y + 3, w, 8, 4, C_ACCENT);
    text(292, y, hm(t.dims[i]).c_str(), C_TEXT, F_SMALL, true);
  }

  // ---- 右列统计 ----
  card(312, 50, 156, 196);
  struct Row { const char *k; String v; uint16_t c; };
  Row rows[5];
  rows[0] = {"SLEEP", t.hasSleep ? (t.sleepOnset + "-" + t.sleepWake) : String("--"), C_TEXT};
  rows[1] = {"STEPS", thousands(t.steps), C_TEXT};
  rows[2] = {"PHONE", hm(t.phoneHours), C_TEXT};
  rows[3] = {"WEATHER",
             t.hasWeather ? (String((int)t.tempMin) + "-" + String((int)t.tempMax) + "C") : String("--"),
             C_TEXT};
  rows[4] = {"AI COST", t.aiCost > 0 ? (String("$") + String(t.aiCost, 2)) : String("--"), C_TEXT};

  for (int i = 0; i < 5; i++) {
    int y = 62 + i * 37;
    label(324, y, rows[i].k);
    text(324, y + 13, elide(rows[i].v, F_BOLD, 132).c_str(), rows[i].c, F_BOLD);
  }

  // ---- 底部 ----
  card(12, 252, 456, 52);
  String l = "tracked " + hm(t.trackedHours);
  text(24, 266, l.c_str(), C_TEXT, F_SMALL);
  if (t.hasSleep) {
    String s = "sleep " + String(t.sleepHours, 1) + "h (inferred)";
    text(160, 266, s.c_str(), C_DIM, F_SMALL);
  }
  String r = "unique " + hm(t.collapsedHours);
  text(456, 266, r.c_str(), C_ACCENT, F_SMALL, true);

  pageDots(1);
}

// 显示某一页：缓存命中就只推屏，未命中才重画。
void drawPage(int page, const NowModel &now, const TodayModel &today, uint32_t nowMs) {
  page &= 1;
  selectCanvas(page);
  if (!cv[page] || !cvValid[page] || cvGen[page] != g_gen) {
    if (page == 0) drawNow(now, today, nowMs);
    else drawToday(today, now, nowMs);
    if (cv[page]) {
      cvValid[page] = true;
      cvGen[page] = g_gen;
    }
  }
  present();
}

// 数据变了：两块缓存都作废，下次显示时重画
void invalidate() { g_gen++; }

}  // namespace ui
