# 流程图目录说明

依据《meta-research 元循环系统 · 施工说明书 v2.2》（`../施工说明书-v2.2.md`）绘制的全套流程图。

| 文件 | 说明 |
|------|------|
| `流程图.md` | **唯一图源**：25 张 Mermaid 图 + 导读（Cursor/VS Code/GitHub 可直接预览） |
| `流程图.html` | 自包含查看版：浏览器直接打开，左侧目录导航，每张图右上角可「导出 PNG」（mermaid.js 已内嵌，离线可用） |
| `png/` | 服务器本地渲染好的 PNG（2× 缩放，可直接引用/贴文档） |
| `tools/` | 构建工具（build.js + 本地 mermaid-cli），`build/` 为中间产物 |

图集结构：00 总览 · 01 主循环 · 02–05 四阶段（idea→plan→bundle→reasoning）· 06a–06g 实体状态机（含 06e2 evaluation_attempt）· 06h 对象关系 · 06i–06m 五张机制图（召回/md 生命周期/池两层面/一轮 IO/池生命周期）· 07 人类介入 · 08 上下文编译 · 09 长时运行。

## 改图后如何再生成

只改 `流程图.md`，然后：

```bash
cd flowcharts
node tools/build.js          # 重新提取 build/*.mmd 并生成 流程图.html
# 重渲染全部 PNG（本地 chrome-headless-shell，禁第三方渲染服务）：
for f in build/*.mmd; do n=$(basename "$f" .mmd); \
  ./tools/node_modules/.bin/mmdc -i "$f" -o "png/$n.png" -b white -s 2 \
  -c tools/mermaid-config.json --puppeteerConfigFile tools/puppeteer.json; done
```

新增一张图：在 md 里图的 ```` ```mermaid ```` 代码块前加一行 `<!-- png: 编号-名称 -->` 标记即可被工具识别。

## 渲染器修复（chrome-headless-shell 缺失时）

若报 `Could not find Chrome (ver. 148.x)`，是 puppeteer 浏览器缓存丢失。经国内镜像直连重装（SSH 代理对大文件会 ECONNRESET，故不走代理）：

```bash
cd tools
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
node node_modules/@puppeteer/browsers/lib/cjs/main-cli.js install chrome-headless-shell@148.0.7778.97 \
  --base-url https://cdn.npmmirror.com/binaries/chrome-for-testing --path /root/.cache/puppeteer
```

字体注意：本机有 Noto CJK（中文正常），**无 emoji 字体**——图源里避免 emoji（会渲成方框），★⚡①→等 BMP 符号正常。
