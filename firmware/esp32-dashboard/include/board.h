#pragma once
// 板级封装：引脚、TCA9554 复位、背光、FT6336 触摸。
// 引脚全部来自微雪官方 BSP，见 platformio.ini 顶部注释。
#include <Arduino.h>
#include <Wire.h>

namespace board {

// ---- 引脚 ----
constexpr int PIN_I2C_SDA = 8;
constexpr int PIN_I2C_SCL = 7;
constexpr int PIN_LCD_MOSI = 1;
constexpr int PIN_LCD_SCLK = 5;
constexpr int PIN_LCD_DC   = 3;
constexpr int PIN_LCD_MISO = 2;
constexpr int PIN_LCD_BL   = 6;
constexpr int PIN_BTN_BOOT = 0;

constexpr uint8_t TCA9554_ADDR = 0x20;
constexpr uint8_t FT6336_ADDR  = 0x38;
constexpr uint8_t TCA_REG_OUTPUT = 0x01;
constexpr uint8_t TCA_REG_CONFIG = 0x03;
constexpr uint8_t TCA_PIN_LCD_RST = 1;  // P1

inline void i2cBegin() { Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 400000); }

inline void tcaWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(TCA9554_ADDR);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

inline uint8_t tcaRead(uint8_t reg) {
  Wire.beginTransmission(TCA9554_ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom((uint8_t)TCA9554_ADDR, (uint8_t)1);
  return Wire.available() ? Wire.read() : 0xFF;
}

// LCD 复位接在 TCA9554 的 P1，P7 是同一条供电/使能控制。
// 官方 bsp_io_expander_init() 的做法：P1、P7 设为输出 → P1 拉低 → 100ms →
// P1+P7 一起拉高 → 100ms。**P7 不拉高屏幕就是全黑**，只做 P1 复位没用。
constexpr uint8_t TCA_PIN_LCD_PWR = 7;

inline void lcdReset() {
  tcaWrite(TCA_REG_CONFIG, tcaRead(TCA_REG_CONFIG) & ~(1 << TCA_PIN_LCD_RST));
  uint8_t out = tcaRead(TCA_REG_OUTPUT);
  tcaWrite(TCA_REG_OUTPUT, out & ~(1 << TCA_PIN_LCD_RST));  // P1 低
  delay(100);

  tcaWrite(TCA_REG_CONFIG,
           tcaRead(TCA_REG_CONFIG) & ~((1 << TCA_PIN_LCD_RST) | (1 << TCA_PIN_LCD_PWR)));
  tcaWrite(TCA_REG_OUTPUT, (out | (1 << TCA_PIN_LCD_RST) | (1 << TCA_PIN_LCD_PWR)));
  delay(100);
}

// 背光 GPIO6。
// 之前用 LEDC 调光，但 ledcAttach 失败时 ledcWrite 是空操作，GPIO6 会一直低电平
// —— 表现就是「屏幕全黑」。实测 digitalWrite 可靠，先用纯开关，调光后面再说。
inline void backlightBegin(uint8_t percent = 100) {
  pinMode(PIN_LCD_BL, OUTPUT);
  digitalWrite(PIN_LCD_BL, percent > 0 ? HIGH : LOW);
}

inline void backlightSet(uint8_t percent) {
  digitalWrite(PIN_LCD_BL, percent > 0 ? HIGH : LOW);
}

// FT6336：0x02 = 触点数（低 4 位），0x03..0x06 = P1 的 XH/XL/YH/YL。
// 返回值已经是**屏幕坐标**（480x320 横屏），映射见下。
inline bool touchReadRaw(int16_t &tx, int16_t &ty) {
  Wire.beginTransmission(FT6336_ADDR);
  Wire.write(0x02);
  Wire.endTransmission(false);
  if (Wire.requestFrom((uint8_t)FT6336_ADDR, (uint8_t)5) != 5) return false;
  uint8_t n = Wire.read() & 0x0F;
  uint8_t xh = Wire.read(), xl = Wire.read();
  uint8_t yh = Wire.read(), yl = Wire.read();
  if (n == 0) return false;
  tx = ((xh & 0x0F) << 8) | xl;
  ty = ((yh & 0x0F) << 8) | yl;
  return true;
}

// 面板原生 320x480（竖），显示 rotation=1 横过来是 480x320，
// 所以触摸坐标必须**换轴**，不是简单缩放：
//   物理 x  <- 原始 y（实测横向滑动只改变 py）
//   物理 y  <- 原始 x 取反
// 用 probe 固件的四角标定（TL/TR/BL/BR + 红点跟随）验证过。
constexpr int PANEL_W = 320;
constexpr int PANEL_H = 480;
constexpr int SCREEN_W = 480;
constexpr int SCREEN_H = 320;

inline bool touchRead(int16_t &sx, int16_t &sy) {
  int16_t px, py;
  if (!touchReadRaw(px, py)) return false;
  sx = (int16_t)((int32_t)py * SCREEN_W / PANEL_H);
  sy = (int16_t)((int32_t)(PANEL_W - 1 - px) * SCREEN_H / PANEL_W);
  return true;
}

}  // namespace board
