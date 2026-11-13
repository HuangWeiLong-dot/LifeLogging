#pragma once
#include <Arduino.h>
#include "model.h"

namespace ui {

// 页面主色，供 drawMessage 复用
constexpr uint16_t kDanger = 0xFB8E;   // #F87171
constexpr uint16_t kAccent = 0x651F;   // #60A5FA

void begin();
void splash(const char *line1, const char *line2 = "");
void drawNow(const NowModel &now, const TodayModel &today, uint32_t nowMs);
void drawToday(const TodayModel &today, const NowModel &now, uint32_t nowMs);
void drawMessage(const char *title, const char *sub, uint16_t color);

// 只重画顶部 46px（时钟 / 日期 / 数据新鲜度），避免整屏闪烁
void redrawHeader(const NowModel &now, const TodayModel &today, uint32_t nowMs, int page);

// 底部短暂提示（点按刷新时用），会被下一次整屏重画覆盖
void toast(const char *msg);

// 显示某一页（0 = NOW，1 = TODAY）。命中 PSRAM 缓存时只推屏不重画。
void drawPage(int page, const NowModel &now, const TodayModel &today, uint32_t nowMs);

// 数据变化后调用，让两页缓存作废
void invalidate();

}  // namespace ui
