# 可选：把反思卡片接进陪伴面板

`astrbot_plugin_feiyu_reflection` 本身已经有独立的「反思与感悟」插件页。
如果你还想在 `astrbot_plugin_private_companion` 的「06 观察」里看到卡片，可以把
`reflection-card.html` 里的两段内容加到陪伴面板的 `pages/companion-panel/index.html`：

1. 在 `#panel-memory` 的 `.observation-grid` 最前面加入卡片 `<article>`。
2. 在文件末尾 `</body>` 前加入 `<script>`。

卡片通过 AstrBot 插件页桥接读取：

```text
/astrbot_plugin_feiyu_reflection/reflection/overview
```

注意：

- 这是**可选**集成，不影响反思生成和提示注入。
- 修改的是 `private_companion` 的页面，不是本插件的运行逻辑。
- 升级 `private_companion` 后可能需要重新加一次。
- 不要把 `friend` 字段接到群聊或公开页面。
