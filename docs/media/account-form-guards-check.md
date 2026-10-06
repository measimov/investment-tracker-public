# 账户与现金表单保存保护验收（2026-10-04）

按已存档第40节首组，独立 `codex/account-form-busy-guards` 基于开放 [#435](https://github.com/measimov/investment-tracker/pull/435)。只修改 AccountsTab、CashEventsTab 与两处真实调用共用的 makeSaver；不修改原字段、校验、POST/PUT payload、归属/脱敏/删除保护、现金方向/正数事实或8位股息税分摊。既有成熟 ElDialog 和遮罩保护保留。

原版只读严格模拟证据：`/tmp/account-state-root-before.json` 的 Space 后视觉/ARIA 与 native checked 分歧；`account-save-root-before.json`、`cash-save-root-before.json` 的唯一挂起POST中保存焦点BODY、无busy且Cancel可关窗。账户Boolean改用已有NCheckbox，两个保存按钮改用已有NButton；沿原saving禁Cancel、关闭和Esc，完成后恢复。makeSaver仅在异步校验前后检查原saving，阻止并发校验和在途重复写，不新增状态或手工焦点机制。

| 验收维度 | 实际依据 | 边界与残项 |
| --- | --- | --- |
| 功能与交互 | 393/1440两窗严格mock：Space两次、原payload、挂起唯一写、重复鼠标/Enter、Esc保护、503草稿与重试200、Cancel/Esc/保存后返回入口 | 原读取/保存/重载不变；无实际账户或现金写入 |
| 内容与可信度 | checkbox真实名称“启用账户”，保存公开aria-label稳定“保存”；busy真实aria-busy/aria-disabled，关闭按钮沿成熟库中文名称 | 不把库loading图标当新动作，不承诺完整读屏认证 |
| 视觉与风格 | 手机账户与桌面现金两张实际未保存草稿图，暖表面/原表单层级保留，正文与控件清晰，无错误通知 | 不是表单重设计或全站重新截图 |
| 响应式与可访问性 | 手机checkbox和保存44px；body/document等于视口；初始busy保存焦点成立，完成后库机制返回常驻入口 | 实际鼠标点击disabled Cancel后焦点可以离开保存，不声称任意鼠标操作锁住焦点 |
| 性能 | 同预览构建当前引用的AccountData chunk gzip(level6)23553→22394bytes，入口133947→133953bytes | 仅两个chunk的本机产物口径，非已加载全页资源/时延；未增加依赖，不据此宣称整体性能提升 |
| 工程与可维护性 | 3产品文件、1条有意义的异步校验/并发写/失败重试unit、4实际浏览器回归；无API/模型/迁移，API真实生成exit0且零漂移 | 不建设表单、键盘、焦点或保存框架；后续第40节两组仍独立进行 |

门禁分时点：首轮既有13项账户/现金/财务浏览器回归通过，新4项失败仅名称locator错误（实际slot名“启用账户”，忙时按钮名“loading 保存”）。删除无效冗余checkbox aria-label、两处使用公开保存aria-label，并按实际中文关闭按钮验证；最后新4项全部通过25.3s，未放宽busy/单写/草稿/金融断言。最终完整unit439通过/60文件，类型/Prettier/生产构建5.84s通过；本组无后端修改，不重复后端财务全套。以上分次定向不拼成最终完整E2E，新PR全量远端门禁待完成。

根独立 `/tmp/account-form-guards-root.json` 最终四场景通过，失败与重试各一个模拟POST；503原草稿保留、200关闭返回、重新打开清空、Esc正常返回、body/documentfit、真实写/外部/JS错误零。根前两次定位问题是测试实际名称判断，部分结果不当产品保护失败。保存中初始焦点与busy成立；结果busyFocused=false来自额外真实点击disabled Cancel后的采样，已准确限定。

实际API生成补证：D3最后一次本机调用漏Python PATH，python not found后cmp成功不足以证明生成；原D3及本树现用venv PATH、显式退出码重新执行，均exit0、SHA前后同`11f166d5…`。生成内容无变化，远端父435真实生成/零漂移门禁也通过。父fc4216ac完整CI37135427341：3418后端/6跳过、438单元/60文件、158 E2E/4跳过，无失败或重试；不冒充本组CI。

正式图：[手机新增账户](mobile-account-form-guards.png)393×852@2、[桌面新增现金事件](cash-form-guards.png)1440×852@1。沿同一只读演示库，账户列表为合法明确虚构浏览器GET fixture，草稿未保存；等待加载指示、字体和弹窗动画完成后截图。原表单成熟库保留，宽度/移动底部动作均可见。

证据：`/tmp/account-form-busy-e2e-target.log`（首轮13通过/4名称失败）、`account-form-busy-e2e-final-target.log`（最终4通过）、`account-form-busy-unit-final.log`、`account-form-busy-typecheck-final.log`、`account-form-busy-format-final.log`、`account-form-busy-build-final.log`、`account-form-busy-api-types-confirmed.log/.json`、`account-form-busy-resource.json`、`account-form-busy-capture.json`及根`account-form-guards-root.json`。UI4196/API18117仅隔离demo，E2E用专用库；所有写严格mock，无生产凭据或账本修改。未合并/部署/关闭总体issues。

当前[#436](https://github.com/measimov/investment-tracker/pull/436) head97a70972完整远端CI37137124831 SUCCESS、attempt1：3418后端/6跳过（242.70s）、439单元/60文件、162 E2E/4既有跳过（6.8m），无失败或重试，生成漂移/类型/格式/构建全部通过。该完整结果与本地分次13+4独立，不再为历史状态重推原PR。
