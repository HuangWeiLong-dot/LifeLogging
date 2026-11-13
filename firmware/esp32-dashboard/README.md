# 生活数据看板（ESP32-S3 3.5" 触摸屏）

常亮小屏，每 60 秒拉一次 `cms.liveinpassion.me/api/life/now`，显示「我现在在干嘛」：
位置、电脑在做什么、手机、手表、待答题。第二页是当天七维与睡眠 / 步数 / 天气 / AI 花费。

数据全部来自站点**已发布的公开只读接口**，设备侧不持有任何凭证。

## 硬件

| 项 | 值 |
|---|---|
| 型号 | 吹雪 ESP32-S3-Touch-LCD-3.5-C（= 微雪 ESP32-S3-Touch-LCD-3.5 带壳版） |
| 主控 | ESP32-S3R8，16MB Flash，8MB **OPI** PSRAM |
| 屏幕 | ST7796，SPI，320×480 RGB565 |
| 触摸 | FT6336，I2C `0x38` |

引脚取自官方 BSP（`examples/factory/components/esp32_s3_touch_lcd_3_5/include/bsp/`）：

```
I2C   SDA=8  SCL=7
LCD   MOSI=1 SCK=5 DC=3 MISO=2 BL=6    CS/RST 不接 GPIO（复位走 TCA9554 的 P1）
触摸  FT6336 @0x38（与 TCA9554@0x20 / AXP2101@0x34 / PCF85063@0x51 / QMI8658@0x6B 共用 I2C）
```

USB 是 ESP32-S3 原生 USB-JTAG，不需要 CH340/CP210x 驱动，枚举成 `COM3`。

## 一次性准备

```bash
uv venv -p 3.12 ../.venv
uv pip install --python ../.venv/Scripts/python.exe platformio

cp include/secrets.h.example include/secrets.h   # 填 Wi-Fi，已被 .gitignore 排除
```

## 构建 / 烧录

```bash
../.venv/Scripts/pio.exe run                     # 编看板
../.venv/Scripts/pio.exe run -e probe            # 硬件自检
../.venv/Scripts/pio.exe run -t upload           # 烧录（COM3）
../.venv/Scripts/pio.exe device monitor          # 串口日志
```

## 关于 `platform-slim/`

`platformio.ini` 用的是项目内的精简 platform 定义，而不是仓库里的
`platform = espressif32`。原因是 espressif32 8.0.0 的 `platform.json` 会无条件安装
**riscv32 工具链（653MB）+ 两套 GDB（约 400MB）**，对 ESP32-S3（Xtensa）完全用不上。
`platform-slim/platform.py` 末尾把这三包 pop 掉，安装量从 ~1.4GB 降到 ~500MB。

升级上游时：把 `~/.platformio/platforms/espressif32` 重新拷过来，再把那段 pop 加回去。

## 交互

- **点按**：立即刷新（底部弹一个 `refreshing` 气泡）
- **左右滑**：在 `NOW` / `TODAY` 两页之间切换，手指一划过阈值就翻，不等抬手

## 性能

整屏 480×320 RGB565 = 307KB，SPI 80MHz 下纯传输就要 ~30ms，所以做了三层优化：

| 做法 | 效果 |
|---|---|
| 两页各一块 PSRAM 帧缓冲，命中缓存只推屏不重画 | 翻页 ~105ms → ~65ms |
| 绘制时直接按 32 位字填帧缓冲，不走逐像素 `fillScreen` | 绘制 53ms → 42ms |
| 顶部时钟单独推 46 行（整行在内存里是连续的） | 每秒刷新 63ms → 9ms |

`Arduino_GFX` 画 GFXfont 是**逐像素 `writePixel`**，直接画屏的话每个字都要几十次
地址窗口设置；必须先把整帧画进 PSRAM 再一次性推。

## 踩过的坑（都是实测，别改回去）

1. **必须 `SPI_MODE0`**。`Arduino_GFX` 在 ESP32 上对 ST7796 默认走 `SPI_MODE3`，
   这块屏只认 MODE0——表现是**背光亮、屏幕全黑**，不看代码根本想不到。
   `ui.cpp` 里显式调 `panel->begin(80000000, SPI_MODE0)`。
2. **AXP2101 必须初始化**。LCD / 背光 / 外设的供电挂在 PMIC 各路输出上，
   不初始化就是黑屏。见 `src/power.cpp`，照抄厂商 `bsp_power_init()`。
3. **GFXfont 的位图是连续位流**，行与行之间**不补字节**。
   按行补齐会让宽度不是 8 倍数的字形整体错位（表现为文字变成散点）。
   见 `tools/gen_gfxfont.py`，改完用它自带的离线渲染验证一遍再烧。
4. **LCD_RST 在 TCA9554 的 P1**，不是 GPIO；`LCD_CS` 硬件上常低，不用管。
   同一颗 TCA9554 的 P7 是 `PA_CTRL`（功放使能），跟屏幕无关。
5. **别用 SNTP**。SNTP 的域名解析会和 HTTPS 解析撞上 lwIP 的 DNS 缓存清理，
   触发 `Required to lock TCPIP core functionality` 断言。改用接口自带的 `ts_utc` 校时。
6. **触摸坐标要换轴，不是缩放**。面板原生 320×480 竖，显示是 480×320 横：
   物理 x ← 原始 y，物理 y ← 原始 x 取反。见 `board.h::touchRead`，
   用 `probe` 固件的四角标定（TL/TR/BL/BR + 红点跟随）验证过。
7. **每帧必须整屏刷底色**。卡片之间有缝隙，只画卡片会留下上一帧的字。
