# 机构浏览器流程

仅在适用 OA／出版商 API 路线已失败，且当前访问模式授权机构浏览器时读取。OA-only 和 provided-only 不进入本流程。

## 复用已授权浏览器

使用用户已完成机构登录的同一受控 Chrome 配置。图书馆首页证明连接，明确机构身份的订阅出版商页面或该路线返回的核实正文才证明权限。

从配置的图书馆资源 URL 出发。`.ivpn.` 入口下，批量下载器会将 DOI／出版商 URL 改写为同一已认证 WebVPN 命名空间；实际门户、改写主机、解析器和联合登录回调比按机构名称猜测可靠。原始 Shibboleth／CARSI URL 可能需要另一次登录，不能据此否定已授权 WebVPN 页面。

Chrome 已暴露 `127.0.0.1:9222` CDP 时，用 `scripts/direct_cdp_proxy.mjs` 复用，不创建丢失当前登录状态的新配置。仅观察可见标题、URL、权限标签和正文控件；cookie、凭据、local storage、保存密码及配置文件不进入上下文。

## 获取一篇论文

1. 在已认证路线打开精确 DOI／标题结果。
2. 跟随正文控件前核实标题或 DOI。
3. 使用该文实际展示的出版商 PDF 或完整 HTML／XML 入口。
4. 优先用随包批量路线；诊断具体浏览器响应时，通过 `browser_pdf_downloader.mjs --help` 与 `browser_cdp_stream_downloader.mjs` 查看同会话传输方式。
5. 按[交付验证](delivery-verification-and-failures.md)核实正文后返回。

同时处理少量机构页面，完成或放弃后关闭本次创建且不再需要的页面，保留等待用户的页面。

## 登录交接与终态

SSO、CAS、CARSI／Shibboleth、OpenAthens、机构选择、数据库登录或 WebVPN 过期时，说明可见页面并暂停该论文。请用户在同一受控浏览器登录、提供正文或放弃；用户确认后沿同一路线重试一次。

CAPTCHA、QR、SMS／OTP、推送批准、密码重置、安全告警、机器人验证或不明确同意时立即暂停。身份秘密由用户自行输入，Agent 不读取、复制或提交。明确无凭据的机构选择，只在用户已授权该选择时操作。

终态按事实返回：`obtained` 为身份匹配且核实正文；`institutional_login_required` 需恢复登录；`library_no_permission` 为明确无机构权限；`anti_bot_or_security_challenge` 需手动处理；`institutional_transfer_failed` 为已到授权页但一次聚焦重试后仍未取得有效正文。及时返回受影响项，不阻塞独立研究。
