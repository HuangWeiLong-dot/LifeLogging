// AXP2101 电源初始化。
//
// 这块板子的 LCD / 背光 / 外设供电全挂在 AXP2101 的各路输出上，
// 不初始化 PMIC 就是黑屏。寄存器设置照抄微雪官方 BSP 的 bsp_power_init()，
// 只去掉看门狗相关的部分（默认不开，开了不喂会被 PMIC 复位）。
#include <Arduino.h>
#include <Wire.h>
#include <XPowersLib.h>

#include "board.h"
#include "power.h"

namespace {

XPowersAXP2101 PMU;

}  // namespace

namespace power {

bool begin() {
  board::i2cBegin();
  if (!PMU.begin(Wire, AXP2101_SLAVE_ADDRESS, board::PIN_I2C_SDA, board::PIN_I2C_SCL)) {
    Serial.println("[pmu ] AXP2101 未找到");
    return false;
  }

  PMU.setVbusVoltageLimit(XPOWERS_AXP2101_VBUS_VOL_LIM_4V36);
  PMU.setVbusCurrentLimit(XPOWERS_AXP2101_VBUS_CUR_LIM_1500MA);
  PMU.setSysPowerDownVoltage(2600);

  PMU.setDC1Voltage(3300);
  PMU.setDC2Voltage(1000);
  PMU.setDC3Voltage(3300);
  PMU.setDC4Voltage(1000);
  PMU.setDC5Voltage(3300);
  PMU.setALDO1Voltage(3300);
  PMU.setALDO2Voltage(3300);
  PMU.setALDO3Voltage(3300);
  PMU.setALDO4Voltage(3300);
  PMU.setBLDO1Voltage(1500);
  PMU.setBLDO2Voltage(2800);
  PMU.setCPUSLDOVoltage(1000);
  PMU.setDLDO1Voltage(3300);
  // DLDO2 用默认电压（XPowersLib 上限 1400mV，显式设 3300 会被拒）

  PMU.enableDC2();
  PMU.enableDC3();
  PMU.enableDC4();
  PMU.enableDC5();
  PMU.enableALDO1();
  PMU.enableALDO2();
  PMU.enableALDO3();
  PMU.enableALDO4();
  PMU.enableBLDO1();
  PMU.enableBLDO2();
  PMU.enableCPUSLDO();
  PMU.enableDLDO1();
  PMU.enableDLDO2();

  // DCDC1 供 3.3V；强制 PWM 避免低亮度闪烁（官方 BSP 的做法）
  PMU.settDC1WorkModeToPwm(true);

  PMU.setPowerKeyPressOffTime(XPOWERS_POWEROFF_4S);
  PMU.setPowerKeyPressOnTime(XPOWERS_POWERON_128MS);
  PMU.disableTSPinMeasure();
  PMU.enableBattDetection();
  PMU.enableVbusVoltageMeasure();
  PMU.enableBattVoltageMeasure();
  PMU.enableSystemVoltageMeasure();
  PMU.setChargingLedMode(XPOWERS_CHG_LED_CTRL_CHG);
  PMU.disableIRQ(XPOWERS_AXP2101_ALL_IRQ);
  PMU.clearIrqStatus();

  Serial.printf("[pmu ] AXP2101 ok  batt=%d%%  vbus=%dmV\n", PMU.getBatteryPercent(),
                PMU.getVbusVoltage());
  return true;
}

}  // namespace power
