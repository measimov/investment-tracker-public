# 前端字体审计与最小修正

2026-10-04，基于已发布的 `37cae99`。保留已认可的暖白、暖灰、陶土与宋体标题；本次只统一字体 family，并补齐既有手机可编辑输入规则，不改业务、API、数据或页面结构。

## 审计结论与选择

原审计覆盖 14 路由 × 1440/393 两宽，并使用 Chromium CDP 核对实际字形。现有不同宋体声明的中文都落到 Noto Serif CJK SC，不能解释为当前中文标题显示错误。登录和 404 的 Element 按钮实际使用 Arial；手机日期弹窗与统计基准搜索为 14px。

继续使用系统字体，不引入字体下载、子集服务、依赖或禁止字体合成。`--app-font-serif` 合并既有宋体栈，顺序为 Noto Serif CJK SC、Noto Serif SC、Source Han Serif SC、Songti SC、STSong、SimSun、serif；17 处原声明只替换 family，交易页的原 font 简写保留 500 / 34px / 1.4。正文的系统无衬线栈保持，`CHART_FONT_FAMILY` 补齐到同一完整栈，Naive 继续复用。

Element 按钮继承正文 family。<=640px 的可编辑 Element Input/Select、Naive Input/InputNumber 和 filterable Select 输入使用 16px；对应 placeholder 和测宽 mirror 同字号。只读、禁用和非文本控件不包含在新增选择器中。移除持仓页重复的所有 input/textarea 16px 规则，避免它覆盖只读/禁用字段。日期输入节点沿用 tabular-nums；其他已有数字格式和字号不改。

## 验证与边界

| 项目       | 实际验证                                                                                                           |
| ---------- | ------------------------------------------------------------------------------------------------------------------ |
| 字体与布局 | 14 路由 × 1440/393 的 H1 family、CDP 平台字形与 body/document 宽度；实际中文仍为 Noto Serif CJK SC Medium/SemiBold |
| 公共按钮   | 两宽登录/404 的 Element 按钮 family 与 body 完全相同                                                               |
| 日期输入   | 两宽真实聚焦，手机16px/桌面14px，tabular-nums；反向日期禁用应用，合法 ISO 日期启用，Esc 返回原触发器               |
| 编辑控件   | 基准搜索与 Element 测宽镜像、Naive 四位价格草稿、持仓单/多选及多选镜像、teleport 转入账户；手机16px，桌面14px      |
| 排除边界   | 转仓禁用证券/转出账户与管理员只读用户选择保持14px；不提交价格或转仓                                                |
| 前端门禁   | 439 单元测试/60 文件通过；最终 Prettier、typecheck、生产构建通过。最终提交的远端完整 CI 另在 PR 正文记录           |

证据为本机隔离 demo、合法虚构管理员 UI 与只读 fixture：`/tmp/font-source-inventory.json`、`/tmp/font-live-audit.json`、`/tmp/font-state-audit.json`（改前），`/tmp/font-live-fixed.json`、`/tmp/font-controls-fixed.json`、`/tmp/font-selection-fixed.json`（改后），对应截图与日志均留在 `/tmp/font-*`。没有生产截图、真实业务写入或外部请求。

首轮日期父节点的 tabular-nums 没有作用到真实 input，定向检查发现后移到输入节点。新预览4200与原演示API18120的跨端口 CORS 由临时只读浏览器 GET 代理适配，不改服务器或生产配置；首轮脚本把 Naive 数字输入误当 spinbutton，以及管理员用户 GET 未采用既有虚构 fixture，均按真实 DOM/权限条件修正。上述过程不作为产品成功证据；以最后完整浏览器结果为准。

本轮只实测 Linux Chromium。未验证 Windows/macOS/iOS 真机；16px 规则旨在满足手机输入的既有防缩放条件，不宣称已完成 iOS 真机缩放验收。字体回退取决于用户系统，未来若需要自托管应另行决定中文覆盖、授权和加载成本。没有变化的财务算法、输入校验、查询与提交控制器不另写镜像样式测试。
