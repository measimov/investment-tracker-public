# 持仓摘要与表格验收

四个指标标签顶部对齐，币种采用普通文字。口径脚注合并到指标下方，平均成本/FIFO/今日汇率/不含汇兑损益说明及缺价警告完整保留，窄屏说明支持键盘且不出视口。

仅有多账户行时显示展开列；现价、币种、价格状态放在同一行，日期或来源入口位于次行。局部单元格上下padding10px，副行间距3px；既有金融数值格式、编辑/取消/错误状态保持。列表上方筛选口径说明保留，排序方式及缺值排末保留在排序按钮title/ARIA。

第6与第7项各自跑完整transactions-pilot与holdings-pilot，每次16项通过。各项截取1440/393浅深四态；额外641/1024/1440单账户/多账户12态验证列数、键盘展开、低价0.0851与行情日，正常虚构行高约87px。全部请求使用隔离库或明确虚构fixture。

| 场景 | 浅色 | 深色 |
| --- | --- | --- |
| 桌面 | [图](holdings-1440-light.png) | [图](holdings-1440-dark.png) |
| 手机 | [图](holdings-393-light.png) | [图](holdings-393-dark.png) |
| 带日期单账户 | [图](holding-1440-single-light.png) | [图](holding-1440-single-dark.png) |
| 多账户 | [图](holding-1440-multiple-light.png) | [图](holding-1440-multiple-dark.png) |

补充完整回归：全量套件178通过/4跳过，另1项为app.spec仍在price-date查币种的旧位置断言。改为验证price-line内的币种后，新隔离库完整app.spec + transactions-pilot共21项通过，包含全部IBKR迁板导入及财务数值断言。累计179个非跳过用例均已验证。

第二轮 #466：手机帮助按钮改为图标靠左，真实44×44点击框与键盘开关保持。在320/393浅深色下实测图标与指标标签左缘都为x16；移除两张未被README引用且逐字节重复的桌面details截图。交易与持仓pilot共16项通过，Chromium/Linux WebKit的相应脚注检查通过。
