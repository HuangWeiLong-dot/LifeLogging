// 触摸坐标标定：屏幕四角写 TL/TR/BL/BR，手指按下就在换算后的位置画点。
// 圆点跟着手指走就说明映射正确；偏到别的角说明需要换一种映射。

#include <Arduino.h>
#include <Wire.h>
#include <Arduino_GFX_Library.h>

#include "board.h"
#include "power.h"

using namespace board;

Arduino_DataBus *bus = new Arduino_ESP32SPI(PIN_LCD_DC, GFX_NOT_DEFINED, PIN_LCD_SCLK,
                                            PIN_LCD_MOSI, PIN_LCD_MISO);
Arduino_ST7796 *gfx = new Arduino_ST7796(bus, GFX_NOT_DEFINED, 1, true, 320, 480);

static void vendorInit() {
  bus->beginWrite();
  bus->writeCommand(0x11);
  bus->endWrite();
  delay(120);
  bus->beginWrite();
  bus->writeCommand(0x3A); bus->write(0x05);
  bus->writeCommand(0xF0); bus->write(0xC3);
  bus->writeCommand(0xF0); bus->write(0x96);
  bus->writeCommand(0xB4); bus->write(0x01);
  bus->writeCommand(0xB7); bus->write(0xC6);
  bus->writeCommand(0xC0); bus->write(0x80); bus->write(0x45);
  bus->writeCommand(0xC1); bus->write(0x13);
  bus->writeCommand(0xC2); bus->write(0xA7);
  bus->writeCommand(0xC5); bus->write(0x0A);
  bus->writeCommand(0xE8);
  bus->write(0x40); bus->write(0x8A); bus->write(0x00); bus->write(0x00);
  bus->write(0x29); bus->write(0x19); bus->write(0xA5); bus->write(0x33);
  bus->writeCommand(0xE0);
  bus->write(0xD0); bus->write(0x08); bus->write(0x0F); bus->write(0x06);
  bus->write(0x06); bus->write(0x33); bus->write(0x30); bus->write(0x33);
  bus->write(0x47); bus->write(0x17); bus->write(0x13); bus->write(0x13);
  bus->write(0x2B); bus->write(0x31);
  bus->writeCommand(0xE1);
  bus->write(0xD0); bus->write(0x0A); bus->write(0x11); bus->write(0x0B);
  bus->write(0x09); bus->write(0x07); bus->write(0x2F); bus->write(0x33);
  bus->write(0x47); bus->write(0x38); bus->write(0x15); bus->write(0x16);
  bus->write(0x2C); bus->write(0x32);
  bus->writeCommand(0xF0); bus->write(0x3C);
  bus->writeCommand(0xF0); bus->write(0x69);
  bus->endWrite();
  delay(120);
  bus->beginWrite();
  bus->writeCommand(0x21);
  bus->writeCommand(0x29);
  bus->endWrite();
}

// 面板原生 320x480 竖，屏幕 480x320 横 -> 触摸坐标必须换轴。
// 实测横向滑动只改变原始 py，所以物理 x ← 原始 y。
static void mapTouch(int16_t px, int16_t py, int16_t &sx, int16_t &sy) {
  sx = (int16_t)((int32_t)py * SCREEN_W / PANEL_H);
  sy = (int16_t)((int32_t)(PANEL_W - 1 - px) * SCREEN_H / PANEL_W);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== 触摸标定 ===");
  i2cBegin();
  power::begin();
  lcdReset();
  gfx->begin(20000000, SPI_MODE0);
  vendorInit();
  gfx->setRotation(1);
  pinMode(PIN_LCD_BL, OUTPUT);
  digitalWrite(PIN_LCD_BL, HIGH);

  gfx->fillScreen(RGB565_BLACK);
  gfx->setTextColor(RGB565_WHITE);
  gfx->setTextSize(3);

  gfx->setCursor(8, 8);
  gfx->print("TL");
  gfx->setCursor(SCREEN_W - 52, 8);
  gfx->print("TR");
  gfx->setCursor(8, SCREEN_H - 44);
  gfx->print("BL");
  gfx->setCursor(SCREEN_W - 52, SCREEN_H - 44);
  gfx->print("BR");

  gfx->drawRect(0, 0, SCREEN_W, SCREEN_H, RGB565_BLUE);
  gfx->setTextSize(2);
  gfx->setTextColor(RGB565_GREEN);
  gfx->setCursor(SCREEN_W / 2 - 60, SCREEN_H / 2 - 10);
  gfx->print("TOUCH ME");
}

static void drawLabels() {
  gfx->fillScreen(RGB565_BLACK);
  gfx->setTextColor(RGB565_WHITE);
  gfx->setTextSize(3);
  gfx->setCursor(8, 8);
  gfx->print("TL");
  gfx->setCursor(SCREEN_W - 52, 8);
  gfx->print("TR");
  gfx->setCursor(8, SCREEN_H - 44);
  gfx->print("BL");
  gfx->setCursor(SCREEN_W - 52, SCREEN_H - 44);
  gfx->print("BR");
  gfx->drawRect(0, 0, SCREEN_W, SCREEN_H, RGB565_BLUE);
  gfx->setTextSize(2);
  gfx->setTextColor(RGB565_GREEN);
  gfx->setCursor(SCREEN_W / 2 - 60, SCREEN_H / 2 - 10);
  gfx->print("TOUCH ME");
}

void loop() {
  int16_t px, py;
  bool pressed = touchReadRaw(px, py);
  static bool wasPressed = false;
  static int16_t lastX = -1000, lastY = -1000;

  if (!pressed) {
    wasPressed = false;
    delay(20);
    return;
  }

  int16_t sx, sy;
  mapTouch(px, py, sx, sy);

  if (!wasPressed) {
    wasPressed = true;
    drawLabels();  // 每次按下都清屏，保证屏幕上只有一个红点
  }

  if (abs(sx - lastX) > 3 || abs(sy - lastY) > 3) {
    lastX = sx;
    lastY = sy;
    Serial.printf("[TP ] raw=(%d,%d) -> screen=(%d,%d)\n", px, py, sx, sy);
  }

  gfx->fillCircle(lastX, lastY, 8, RGB565_RED);
  gfx->drawFastHLine(lastX - 14, lastY, 28, RGB565_RED);
  gfx->drawFastVLine(lastX, lastY - 14, 28, RGB565_RED);
  delay(30);
}
