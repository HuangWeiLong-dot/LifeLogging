# engage —— App 侧「带锚点的问题」

固定七维由 App 本地生成；这里只做 ASSOC 层：**看着当天真实数据，写出 1–3 道
只有本人能回答的问题**，推到中继，App 启动时拉取。

## 部署

```bash
sudo cp /opt/lifelogging/repo/server/engage/lifelogging-engage.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lifelogging-engage.timer
sudo systemctl list-timers lifelogging-engage.timer
```

## 手动验证

```bash
sudo -u root /opt/lifelogging/venv/bin/python \
  /opt/lifelogging/repo/server/engage/engage.py --day today --dry-run
```

`--dry-run` 只打印摘要，不调模型、不推送。

## 两条硬规则

1. **必须有锚点**：题面里要引用摘要里真实存在的字符串（时刻 / 歌名 / 地点），
   模型给的 `anchor` 不在摘要里就直接丢弃。
2. **没有素材就返回空**：空列表是合法结果，App 会退回固定七维。
