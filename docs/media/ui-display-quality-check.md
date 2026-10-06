# 已观察显示与名称问题验收（2026-10-04）

独立 `codex/ui-display-quality` 基于 [#438](https://github.com/measimov/investment-tracker/pull/438) 实际db1b99c0，实施40.3b已存档有限范围。产品仅七文件：Import原两Select的公开名称与两Descriptions响应列、NotFound原ElResult标题槽/长路径局部重排、两成本环图公开标签/高亮参数、既有warning主值对齐。没有库迁移、依赖/API/schema/解析/财务/认证/保存控制器变化，没有主题、布局或图表框架。

标准导入真实输入名称为“归属账户”，券商模式为“导入账户”；复用现有useMediaQuery的手机首帧判断，原Descriptions手机一组/桌面三组，14字段/默认零/日期/原hash不删。404仅公开title槽生成同名H1与本页min/max-width/overflow-wrap，完整路由路径原值保留，不解码或修改登录与兜底守卫。

环图radius42/68、center50/44、原分类色/成本/一位占比、tooltip、DOM文字摘要、按需/resize/减弱动态偏好保持。默认公开labelLine15+15与distance5挤占窄屏宽度；两调用现在长度4/4、distance2、edge4，原市场名/占比分两行，正常与高亮均12px，不放大扇区/文字或加装饰阴影，统一朴素2px分隔。warning沿已验过的Naive深值#936326成为既有app/COLOR/EP主值，hover#875a21；Naive同值引用COLOR，Tag深值和分类橙不改。

| 验收维度 | 实际依据 | 限制与残项 |
| --- | --- | --- |
| 功能与交互 | 根两宽两模式combobox公开名称、原预览与第二次确认携同hash；公共六状态Enter返回原登录守卫；原导入/日期/基准断言保留 | 只严格虚构预览POST，不执行commit/真实导入、来源刷新或用户写入 |
| 内容与可信度 | 合法五行负额/HK$0/未知币种与金额/无code/价格精度、14字段和纯日期保持；两页成本tooltip与摘要不变；完整长路径不裁 | 无金融原值/算法调整，未知不补¥或0，不把百分比布局改为新数值 |
| 视觉与风格 | 根与实现方逐张查看最终四正式图及320/393/1440两页环图；宋体404 H1、暖色原成熟导入窗、警告深值与朴素环图 | 首版实际手机标签仍省略，保留失败图；修复后才确认完整，未用整页fit替代视觉判读 |
| 响应式与可访问性 | 公共320/1440六状态、导入393/1440单列/三列、原名字/键盘、根13页两宽26稳定样本；normal警告4.793:1、hover5.181:1，原约1.987:1 | 样本不是所有权限/交互或完整读屏认证；720×450等效重排沿前序专门证据，不冒充literal浏览器缩放 |
| 性能 | 当前生产引用4路由+HTML入口JS/CSS共6chunk gzip(level6)226337→226526bytes（+189bytes） | 仅这些chunk本机静态量，不代表整页加载/时延；没有性能改善或新五轮采样主张 |
| 工程与可维护性 | 安装ECharts公开types/labelLayout核对，原mediaquery/publicslot/主题值复用，新增真实匿名长404回归 | 七产品文件，未增加图表/helper/ARIA平台；#370只完成局部拆分，整体仍开放 |

门禁时点：首版七产品完整439单元/60（1.91s）、原shell/cash重复/import乱序/dashboard/statistics五spec12通过35.4s、类型/格式/生产构建5.92s、API真实venv生成exit0与SHA前后同11f166d5…通过。根发现首版393两页仍省略后，只收口两label公开参数与同值两行；最终类型/格式/构建5.86s及原dashboard/statistics六项24.4s通过，不将12+6拼成最后完整clean。新长404同一用例包含320/1440，保证真实H1/全文/body+document宽度/原键盘返回；本PR最终head完整CI创建后待完成。

根结果：`/tmp/ui-public-exit-root-fixed.json`六公共状态；`ui-import-display-root-fixed.json`两宽完整14字段/五行/第二预览原hash；`ui-warning-fixed-computed.json`normal/hover；`ui-market-quality-root.json`最终四hover；`ui-quality-root-pages.json`26基础样本。独立只读实际API origin18120，生产demo-output/dist与4173/frontend/dist E2E产物分离；全部真实写/外部/JS错误零。首次根导入脚本将标准/券商名字写反，纠正临时定位后通过，不当产品失败。首版四图/JSON保存`/tmp/ui-quality-first-ring-evidence`，数字通过不代表裁切已修；最后393/1440四图与实现方320两图逐张查看完整三市场/占比。临时Vue生产实例不能直接检查canvas字符，未据inspectable:false宣称字符验证，视觉证据来自实际PNG。

正式图：[桌面导入结果/警告/疑似明细](import-quality.png)1440×852@1（原窗已滚动，标题在上方，不含完整窗口）、[手机单列摘要](mobile-import-quality.png)393×852@2（保留原header/footer，14项及日期完整）、[手机统计环图hover](mobile-market-quality.png)393×852@2、[桌面长路径404](not-found-quality.png)1440×852@1。导入复用实际schema补齐默认零的明确虚构五行；待preview忙态false/消息消失及字体/真实过渡完成。图表仅同演示读取，404未登录；没有真实导入或删除。捕获日志/JSON `/tmp/ui-quality-{import,market,public}-capture.*`，root四文件哈希存`/tmp/ui-quality-formal-images-root-reviewed.sha256`。

其他证据：`/tmp/ui-quality-unit.log`、`ui-quality-e2e-target.log/.exit`、`ui-quality-chart-target-final.log`、`ui-quality-typecheck-final.log`、`ui-quality-format-final.log`、`ui-quality-build-final.log`、`ui-quality-api-types.log/.json`、`ui-quality-resource-final.json`。生成components.d.ts无关11删除恢复，不进入diff。父438 exactdb1完整CI37140658156 SUCCESS attempt1：3418后端/6跳过247.08s、439单元/60、162 E2E/4跳过7.4m，无失败或重试；本PR结果不能用父结果代替。逐条退出矩阵见计划42，保留合入/发布验收与#370范围残项，不自动merge/deploy/关闭issues。
