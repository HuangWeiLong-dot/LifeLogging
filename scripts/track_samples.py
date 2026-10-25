"""把锻炼文件里的逐帧数据压成站点能画的采样数组。

GPX 和 FIT 两条链路都要用，所以放在一处 —— 两边各写一份迟早会漂移：
站点只认下面这个形状。

    {
      "t":    [0, 4, 8, ...],         # 秒，相对开始时间
      "hr":   [132, 141, ...] | 缺省  # BPM，只有 FIT 有
      "v":    [2.6, 2.8, ...] | 缺省  # 速度 m/s
      "alt":  [12.4, 13.0, ...] | 缺省
      "d":    [0, 9.8, 21.4, ...],    # 累计距离 m —— 分段配速和坡度都要它
      "temp": [18, 19, ...] | 缺省    # °C
    }

点数是**并行数组**而不是 [t, hr, v, alt] 的数组：JSON 体积小一半，
站点画线时直接按索引取。全为 None 的那一维直接省掉（GPX 没有心率）。

`d` 尽量用设备自己的累计距离（FIT record 的 distance 字段）；没有时由调用方
按速度积分或轨迹累加算好再传进来 —— 别在这里重算，两种来源的算法不一样。
"""
from __future__ import annotations

import math
from datetime import datetime

MAX_SAMPLES = 240

# rows 的每一项：(时间, 心率, 速度 m/s, 海拔 m, 累计距离 m, 温度 °C)
Row = tuple[datetime, float | None, float | None, float | None, float | None, float | None]


def _clean(values: list[float | None], digits: int) -> list[float | None]:
    return [None if v is None else round(v, digits) for v in values]


def build_samples(rows: list[Row], start: datetime, max_samples: int = MAX_SAMPLES) -> dict[str, list] | None:
    """rows 必须按时间排好序。"""
    if len(rows) < 2:
        return None

    step = max(1, math.ceil(len(rows) / max_samples))
    sampled = rows[::step]
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])

    t: list[int] = []
    hr: list[float | None] = []
    speed: list[float | None] = []
    alt: list[float | None] = []
    dist: list[float | None] = []
    temp: list[float | None] = []
    for when, h, v, a, dm, tp in sampled:
        offset = (when - start).total_seconds()
        if offset < 0:
            continue
        t.append(int(round(offset)))
        hr.append(h)
        speed.append(v)
        alt.append(a)
        dist.append(dm)
        temp.append(tp)

    if len(t) < 2:
        return None

    out: dict[str, list] = {"t": t}
    if any(x is not None for x in hr):
        out["hr"] = [None if x is None else int(round(x)) for x in hr]
    if any(x is not None for x in speed):
        out["v"] = _clean(speed, 2)
    if any(x is not None for x in alt):
        out["alt"] = _clean(alt, 1)
    if any(x is not None for x in dist):
        out["d"] = _clean(dist, 1)
    if any(x is not None for x in temp):
        out["temp"] = _clean(temp, 1)
    return out
