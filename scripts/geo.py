"""本地反向地理编码 —— 只查最近的城市，全程离线。

**为什么不用 Dawarich 内置的**

  Dawarich 的反向地理编码**默认关闭**，开启要配一个 provider。
  可选项（chibigeo / Geoapify / Photon / Nominatim）基本都是**托管服务**，
  也就是说你的坐标要发给第三方 —— 和「数据不上传第三方」直接冲突。

  自托管 Photon 是干净的方案，但它需要完整的 OSM 索引：
  全球索引 50GB 起步，而且要 Java 工具链去构建。50GB 的盘装不下。

**为什么这个方案够用**

  日记需要的粒度是「在上海」而不是「上海市徐汇区某某路 123 号」。
  GeoNames 的 cities1000 数据集（全球人口 >1000 的城市，约 10MB）
  本地做最近邻查找就能给出这个粒度，而且彻底离线。

  代价：只能到城市级，且依赖「离最近的城市 = 你在的城市」这个假设。
  对日记来说完全够用；对导航来说不够 —— 但这不是导航系统。
"""

from __future__ import annotations

import math
import os
import sys
import zipfile
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 数据表放哪。默认是仓库里的 data/（笔记本上就是这个）。
#
# ⚠️ **服务器上必须指走。** 那边的服务受 systemd 的 ProtectSystem=strict 约束，
#    仓库目录整个是只读的 —— 用默认值的话 ensure_dataset() 连 mkdir 都会失败，
#    而失败是**静默**的：_describe() 会退回坐标，于是机器人报出来的是
#    「(39.9042,116.4074)」而不是地名，看着像坏了但没有任何报错。
#    服务器侧的 unit 会把它指到 /var/lib/lifelogging/geo。
DATA_DIR = Path(os.environ.get("LIFELOGGING_DATA_DIR")
                or (Path(__file__).resolve().parent.parent / "data"))

# admin1 代码 → 上级行政区名。GeoNames 的地点表里 admin1 是数字代码
# （中国是 "23" 这种），没有这张表就写不出「青浦, 上海」这种带上级的描述。
ADMIN1 = DATA_DIR / "admin1CodesASCII.txt"
ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"

# 可选的数据集，按精度从粗到细：
#   cities15000  人口 >15000   只有大城市
#   cities5000   人口 >5000
#   cities1000   人口 >1000    到区/县
#   cities500    人口 >500     到镇/街道
#   allCountries 全部居民点     含 PPLX（小区级），但解压上 GB，需要建索引
DATASETS = ("cities15000", "cities5000", "cities1000", "cities500", "allCountries")
DEFAULT_DATASET = "cities1000"

_dataset: str = DEFAULT_DATASET


def dataset_name() -> str:
    """当前使用的数据集。优先读 config.yaml 的 geocoding.dataset。"""
    global _dataset
    try:
        from common import load_config

        name = (load_config().get("geocoding") or {}).get("dataset")
        if name in DATASETS:
            _dataset = name
    except Exception:  # noqa: BLE001
        pass
    return _dataset


def txt_path() -> Path:
    return DATA_DIR / f"{dataset_name()}.txt"


def url_for(name: str) -> str:
    return f"https://download.geonames.org/export/dump/{name}.zip"


class Place(NamedTuple):
    name: str
    country: str
    admin1: str
    lat: float
    lon: float
    population: int
    fcode: str = ""     # GeoNames feature code: PPLA / PPLA2 / PPLA3 / PPLA4 / PPL ...
    admin2: str = ""    # 地级单位代码。同一个 admin2 = 同一个上级，可用来验父子关系
    admin3: str = ""



_cache: list[Place] | None = None


def ensure_dataset(quiet: bool = False) -> bool:
    """确保城市表和 admin1 表都在。缺哪个下哪个。

    返回是否可用。下载失败不抛异常 —— 调用方应该优雅降级成「只有坐标」。
    """
    import httpx

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    name = dataset_name()
    txt = DATA_DIR / f"{name}.txt"
    zpath = DATA_DIR / f"{name}.zip"

    if not txt.exists():
        if not quiet:
            print(f"  · first run: downloading {name} (one-off)")
        try:
            with httpx.stream("GET", url_for(name), timeout=600.0,
                              follow_redirects=True) as r:
                r.raise_for_status()
                with zpath.open("wb") as f:
                    for chunk in r.iter_bytes(65536):
                        f.write(chunk)
            with zipfile.ZipFile(zpath) as z:
                with z.open(f"{name}.txt") as src, txt.open("wb") as dst:
                    dst.write(src.read())
            zpath.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            print(f"  WARN could not fetch {name}: {e}")
            print(f"       download it manually to {zpath}")
            return False

    # admin1 表很小（约 250KB），丢了也能退化工作，所以失败不阻断
    if not ADMIN1.exists():
        try:
            r = httpx.get(ADMIN1_URL, timeout=60.0, follow_redirects=True)
            r.raise_for_status()
            ADMIN1.write_bytes(r.content)
        except Exception as e:  # noqa: BLE001
            if not quiet:
                print(f"  · admin1 names unavailable ({e}); district names will lack the parent region")

    if not quiet:
        print(f"  OK  city dataset ready: {txt}")
    return True


def _load() -> list[Place]:
    """加载城市表。GeoNames 的格式是 tab 分隔，无表头。

    ⚠️ 全量读进内存。cities500 约 25 万条、cities1000 约 17 万条都没问题，
       但 allCountries 有千万级，这样读会吃掉几个 GB —— 那个档位需要先建索引。
    """
    global _cache
    if _cache is not None:
        return _cache

    out: list[Place] = []
    p = txt_path()
    if not p.exists():
        _cache = out
        return out

    with p.open("r", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 15:
                continue
            try:
                out.append(Place(
                    name=parts[1],
                    lat=float(parts[4]),
                    lon=float(parts[5]),
                    country=parts[8],
                    admin1=parts[10],
                    population=int(parts[14] or 0),
                    fcode=parts[7],
                    admin2=parts[11],
                    admin3=parts[12],
                ))
            except (ValueError, IndexError):
                continue

    _cache = out
    return out


def nearest(lat: float, lon: float, max_km: float = 50.0) -> Place | None:
    """**严格最近**的城市。技术上准确，但对人常常没用 ——
    上海市中心的坐标会匹配到某个区（青浦、岗厦），而人会说自己"在上海"。

    需要给人看的描述时用 `describe()`，它走人口加权。
    """
    cands = _candidates(lat, lon, max_km)
    if not cands:
        return None
    return min(cands, key=lambda cd: cd[0])[1]


def _candidates(lat: float, lon: float, max_km: float) -> list[tuple[float, Place]]:
    """返回 max_km 内的 [(距离km, Place)]。

    ⚠️ 用等距圆柱投影算距离（小范围内足够准），不做大圆距离 ——
       这里只需要「谁更近」这个相对关系，不需要真实测地距离。
       纬度 1 度 ≈ 111km 恒定，经度方向要乘 cos(lat)。
    """
    cities = _load()
    if not cities:
        return []

    lat_scale = 111.0
    lon_scale = 111.0 * math.cos(math.radians(lat))
    out: list[tuple[float, Place]] = []

    for c in cities:
        dy = (c.lat - lat) * lat_scale
        dx = (c.lon - lon) * lon_scale
        d = math.sqrt(dx * dx + dy * dy)
        if d <= max_km:
            out.append((d, c))
    return out


# 在这个半径内取人口最多的地点，而不是取严格最近的。
# 理由：严格最近常常匹配到一个小区/居民点（「岗厦」「Eifuku」这种），
# 而用户要的是**区级**粒度 —— 青浦区、黄浦区。区中心的人口量级差得很明显，
# 用「近邻 + 人口」两个条件一起筛，就能稳定落到区中心而不是某个门牌号。
DISTRICT_RADIUS_KM = 10.0

# 从细到粗的行政级别链。describe() 按这个顺序逐级拼接。
# PPLX 只有 allCountries 数据集才有 —— 想细到小区就得用那个。
CHAIN = ("PPLX", "PPLA4", "PPLA3", "PPLA2", "PPLA", "PPLC")

# 「城市级」—— 可以安全地当作细一级容器的几级。
#   PPLA2  地级市    （深圳、苏州、佛山……没有它，「Gangxia, Guangdong」会丢掉城市名）
#   PPLA   省会 / 直辖市
#   PPLC   首都
#
# ⚠️ 地级市离你几十公里也是对的：它**确实**把下面那个区县整个包住了。
#    这正是它和「区」的区别 —— 区与区之间可能是邻居，市与区之间不会。
CITY_LEVELS = ("PPLA2", "PPLA", "PPLC")

_admin1_cache: dict[str, str] | None = None


def _load_admin1() -> dict[str, str]:
    """admin1 代码 → 上级行政区名。例如 CN.23 -> Shanghai。

    没有这张表的话只能说「Qingpu」，别人不知道那是哪；
    有了才能说「Qingpu, Shanghai」。
    """
    global _admin1_cache
    if _admin1_cache is not None:
        return _admin1_cache

    out: dict[str, str] = {}
    if ADMIN1.exists():
        with ADMIN1.open("r", encoding="utf-8") as f:
            for line in f:
                p = line.rstrip("\n").split("\t")
                if len(p) >= 2:
                    out[p[0]] = p[1]
    _admin1_cache = out
    return out


def describe(lat: float, lon: float, max_km: float = 50.0) -> str:
    """给人看的地点描述：**最近的细一级 + 城市级 + 省 + 国家**。

    输出形如：``Zhabei, Shanghai, CN``
              片区     市/省     国家
    或（郊区）：``Yingpu, Shanghai, CN``

    ## 行政级别怎么判 —— 用 feature code，不是用人口

        PPLX   居民点的一部分（小区 / 社区）
        PPLA4  乡级：街道 / 镇
        PPLA3  县级：区 / 县
        PPLA2  地级：市
        PPLA   省级：省会 / 直辖市
        PPLC   首都

    ⚠️ 两个反直觉的点，都踩过坑：

    1. **不能用人口选级别**。深圳 1700 万永远盖过福田 150 万 ——
       按人口选会永远停在「市」，永远到不了区、更到不了街道。

    2. **「就近找上级」是错的**。行政归属是**数据**，不是几何关系。

    ## 为什么只拼「细一级 + 城市」，不拼「细一级 + 区 + 市」

    ⚠️ 这里原来是从细到粗**逐级各取最近**，拼成两级地名。它会拼出
    **互不包含**的假地名：

        Zhabei, Pudong, Shanghai, CN

    闸北（PPLX，浦西）和浦东（PPLA4）隔着 4.4 公里、谁也不属于谁，但两句
    都是「浦东」附近最近的候选，于是被粘成了一个**看着完全正常**的地名。
    GeoNames 的 admin2 代码也救不了 —— 这两个条目的 admin2 都是 12324204。

    根本原因：cities500 这类数据集**只有坐标，没有边界**。没有边界就无法
    判断 A 是否包含 B，而「各自取最近」本来就是在猜。两个同级邻居之间的距离，
    和真正的父子距离，在几何上没有区别。

    所以改成一条**结构上不可能错**的规则：细一级之后只接**城市级**
    （PPLA / PPLC）。城市级地名离你 50 公里以内，一定在你的行政树上方 ——
    「街区, 城市, 省, 国家」这句话永远成立，不需要边界数据去验证。

    代价：丢掉区/县这一级（``Yingpu, Qingpu, Shanghai`` 只剩下
    ``Yingpu, Shanghai``）。这是**有意的**取舍 —— 对日记来说城市级就够了，
    而一个错的区名比少一个区名糟得多。
    """
    cands = _candidates(lat, lon, max_km)
    if not cands:
        return f"{lat:.3f}, {lon:.3f}"

    near_pool = [c for c in cands if c[0] <= DISTRICT_RADIUS_KM]
    pool = near_pool or cands

    # ── 1. 最细一级：先按**距离**取，再在「几乎一样近」的里面挑级别最细的 ──
    #
    # ⚠️ 顺序不能反。原来是「按 CHAIN 从细到粗，取第一个有候选的级别」，
    #    于是只要 3 公里外有一个 PPLX，它就盖过脚下 10 米的那个 PPLA4 ——
    #    浦东南边的坐标因此被叫成「Puxi」。距离是硬约束，粒度只在打平时才用。
    #
    # ⚠️ 也不能按人口选：朱家角(7km, 6万人) 会盖过 盈浦街道(0.33km, 0人)。
    d0 = min(d for d, _ in pool)
    tied = [c for c in pool if c[0] <= d0 + 0.5]
    rank = {fc: i for i, fc in enumerate(CHAIN)}
    known = [c for c in tied if c[1].fcode in rank]
    _, finest = (min(known, key=lambda cd: rank[cd[1].fcode])
                 if known else min(tied, key=lambda cd: cd[0]))

    names: list[str] = [finest.name]

    # ── 2. 城市级：同国同省里最近的那个 PPLA / PPLC ──
    # ⚠️ 同国同省这条不能省：只按距离选会把行政区划外的地方拉进来
    #    （纽约 -> 「Tribeca, Jersey City」，隔一条河就是另一个州）。
    if finest.fcode not in CITY_LEVELS:
        cities = [
            c for c in pool
            if c[1].fcode in CITY_LEVELS
            and c[1].country == finest.country
            and c[1].admin1 == finest.admin1
        ]
        if cities:
            _, city = min(cities, key=lambda cd: cd[0])
            if city.name not in names:
                names.append(city.name)

    # ── 3. 省 / 直辖市：admin1 字段是权威值，不靠距离推 ──
    admin = _load_admin1()
    parent = admin.get(f"{finest.country}.{finest.admin1}", "")
    if parent and parent not in names:
        names.append(parent)

    names.append(finest.country)
    return ", ".join(names)
def main() -> None:
    """命令行自测：python scripts/geo.py 31.1566 121.1027"""
    if len(sys.argv) >= 3:
        lat, lon = float(sys.argv[1]), float(sys.argv[2])
    else:
        # 默认拿一个已知坐标试：上海
        lat, lon = 31.1566, 121.1027
        print(f"(no args, testing with Shanghai: {lat}, {lon})")

    if not ensure_dataset():
        sys.exit(1)

    cities = _load()
    print(f"  loaded {len(cities):,} cities")
    p = nearest(lat, lon)
    if p is None:
        print(f"  no city within 50 km of {lat}, {lon}")
    else:
        print(f"  {lat}, {lon}  ->  {p.name} ({p.admin1}, {p.country}) pop={p.population:,}")


if __name__ == "__main__":
    main()
