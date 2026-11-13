#pragma once
// 从 /api/life/now 与 /api/life?days=N 解析出来的数据结构。
// 字段名对齐 Worker 发布契约；不是字段就不显示，不做猜测。
#include <Arduino.h>

struct NowModel {
  bool valid = false;
  uint32_t fetchedAtMs = 0;   // millis()，用于算「多久前」
  String tsUtc;

  // location
  String place;      // "Yingpu, Shanghai, CN"
  String city;
  String mode;       // stationary / moving / ...
  float stayMin = 0;
  bool locStale = false;
  bool hasPos = false;
  float lat = 0, lon = 0;

  // desktop
  bool deskRunning = false;
  bool deskAfk = false;
  String app;
  String title;
  String category;
  String since;      // "20:16"
  float todayHours = 0;

  // phone
  String phoneApp;
  bool screenOn = false;
  int unlocks = 0;
  int batteryLevel = -1;      // -1 = 手机没上报
  bool batteryCharging = false;
  String musicTitle;          // 正在听的歌（没在听就是空）
  String musicApp;
  int notifications = 0;

  // watch
  float steps = 0;
  float hrAvg = 0;
  float hrMin = 0;

  // qa
  bool hasQa = false;
  String question;
  String answer;
};

struct TodayModel {
  bool valid = false;
  uint32_t fetchedAtMs = 0;
  String day;

  float trackedHours = 0;
  float collapsedHours = 0;
  float dims[7] = {0, 0, 0, 0, 0, 0, 0};  // Work Study Life Transport Leisure Health Social
  float phoneHours = 0;

  int steps = 0;
  float hrAvg = 0;
  int unlocks = 0;
  float screenMinutes = 0;

  bool hasSleep = false;
  String sleepOnset, sleepWake;
  float sleepHours = 0;

  bool hasWeather = false;
  float tempMin = 0, tempMax = 0;
  String condition;

  float aiCost = 0;
};
