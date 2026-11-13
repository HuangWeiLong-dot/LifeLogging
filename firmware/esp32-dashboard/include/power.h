#pragma once
// AXP2101 电源管理：屏幕 / 背光 / 外设的供电轨。
// 必须在 ui::begin()（点屏）之前调用，否则黑屏。
namespace power {
bool begin();
}
