#pragma once
// Wi-Fi / NTP / HTTPS 拉取。数据只读，公开接口，不带 token。
#include <Arduino.h>
#include "model.h"

namespace net {

bool wifiBegin(uint32_t timeoutMs = 25000);
bool wifiConnected();
void wifiEnsure();          // 掉线重连，loop 里低频调用

void timeBegin(const char *tz = "CST-8");   // 只设时区，不启 SNTP
void setClockFromIso(const String &iso);    // 用接口的 ts_utc 校准本地时钟
bool timeSynced();
String clockHm();           // "20:41"
String clockDate();         // "THU 05 NOV"

bool fetchNow(NowModel &out);
bool fetchToday(TodayModel &out);

int  lastHttpCode();
const String &lastError();

}  // namespace net
