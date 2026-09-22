# CI 自愈 Skill

这是一个面向人类使用者的说明文件。它介绍本 Skill 能做什么、如何调用以及结果如何交付；Agent 的正式执行规则以同目录的 [SKILL.md](SKILL.md) 为准。

## 解决什么问题

本 Skill 用于处理已经发生、且有望通过代码或构建配置修复的确定性 CI 失败。它会：

1. 保存并绑定原始失败、仓库、基线和构建环境；
2. 获取与失败相关的 MR、提交历史、需求/设计材料、测试和实际调用上下文；
3. 判断目标行为、根因和可行的修复位置；
4. 在修改前记录修复准入；
5. 修改工作空间并执行分层验证；
6. 输出诊断与修复报告，或在证据不足时停止并说明需要补充什么。

“编译通过”只是验证信号，不等于已经证明修复符合开发者意图。

企业或个人可将自己的分类标准作为 Profile 接入，在定制版本的 `SKILL.md` 中固定绑定，由同一 Agent 分类并继续诊断；配置方法见[企业分类自定义](#企业分类自定义)。未固定也未在调用中指定时沿用通用诊断。

## 适用范围

- Java/Maven 源码、测试源码、生成代码和编译配置问题；
- C/C++ 预处理、编译、模板、符号、链接和 ABI 问题；
- 依赖声明、解析、获取、制品一致性和实际消费问题；
- 可以由 GitHub Actions、GitLab CI 或企业 CI 适配器提供证据的具体流水线失败。

以下事项不属于本 Skill 的默认范围：普通流水线编写、已确认的 Flaky/基础设施恢复、合并、部署、生产事故处置，以及没有证据支持的盲目改动。

## 调用方式

Skill 支持两个可选位置参数，顺序为 work_dir、output_dir：

~~~text
/ci-self-healing "/path/to/failure-scene" "/path/to/healing-output"
~~~

也可以使用宿主提供的命名输入：

~~~text
使用 $ci-self-healing 修复本次构建失败。
work_dir: /path/to/failure-scene
output_dir: /path/to/healing-output
~~~

参数含义：

| 参数 | 作用 |
| --- | --- |
| work_dir | 失败现场目录，可包含日志、源码/工作空间和构建上下文 |
| output_dir | 可选的报告输出目录；有效时生成 analysis-fix.md |

修复请求同时提供两个参数会进入企业 CI Job 失败现场场景；只提供 output_dir 不会改变验证模式，只读请求也不会因双参数变成修复请求。宿主不支持位置参数扩展时，使用命名输入或宿主自己的结构化参数接口。

## Provider 定制

Skill 按“复用本地证据 → Provider MCP → Provider CLI”获取当前失败的日志和运行信息；只补齐影响判断的缺口。Provider 由明确请求或可信宿主/配置指定，再加载对应访问说明；不根据日志猜系统、不全局扫描 CI 工具，也不自动安装 CLI、登录或修改认证配置。

企业可在 `references/providers/` 中增加自己的访问说明，通过可信命名输入 `provider`、`provider_reference` 关联；不增加现有两个位置参数。当前调用的选择和访问规则见 [Provider 访问](references/providers/provider-access.md)，以下内容用于维护接入包，无需在每次修复时阅读。

### 编写接入文件

纯 Markdown 接入即可，不要求动态插件、自动注册或配置扫描程序。**CLI 帮助充分时，接入文件只需声明入口、目标来源和必要的企业约定，无需详细命令清单，也无需“能力 → 子命令”的逐条映射。** 按实际接入方式保留以下信息：

| 内容 | 说明 |
| --- | --- |
| 身份与目标 | Provider ID、适用系统/主机、仓库与构建/Job 身份的可信来源，以及必要的版本约束 |
| 本地证据 | 实际文件布局与 metadata 对应关系，可无固定布局 |
| MCP | 可识别的服务归属与入口，能力和参数按实际工具说明核对；未提供则注明 |
| CLI | 真实可执行文件、帮助入口（如该 CLI 的 `--help`）；未提供则注明 |
| 必要补充 | 帮助未覆盖、但影响正确访问的运行/重试语义、分页截断、日志可用条件或副作用；没有则省略 |
| 能力缺口 | 未提供或未验证的访问方式，失败时应保留的事实 |

命令语法、参数与输出格式交给当前安装版本的帮助；Agent 按[CLI 访问规则](references/providers/provider-access.md#provider-cli)围绕缺口自行发现和组合读取命令。帮助不足时只补充缺失的约定或可信资料入口，不复制整份手册。通用失败绑定、权限与停止规则继续引用现有指南。

接入说明只定义如何访问；分类、业务修法、准入规则仍由对应指南维护。文件存在不证明工具可用，也不授予访问、修改、重跑或发布权限。

### 企业发行包与启动配置

企业发行包只需要包含实际使用的 Provider。通用性由统一的访问合同和扩展方式保证，不要求收录所有 CI 平台。

| 层面 | 控制内容 | 配置责任 |
| --- | --- | --- |
| 分发内容 | 包中包含哪些 Provider reference | 企业维护者按实际使用范围裁剪 |
| 本次选择 | 当前失败使用哪个 Provider | 可信启动配置或明确请求指定 |
| 实际权限 | 能访问哪些系统、仓库和动作 | 宿主工具、凭据与权限控制 |

单平台企业可以在保留其他通用文件的前提下，将 `references/providers/` 精简为 `provider-access.md` 和企业自己的接入文件。以下是可信启动配置的示意，`company-ci.md` 须先按实际接口编写，路径相对于 Skill 根目录：

~~~yaml
provider: company-ci
provider_reference: references/providers/company-ci.md
~~~

由启动器随调用提供这两项配置后，使用者仍只需传入 `work_dir`、`output_dir`。Skill 没有新增配置文件自动扫描或解析器；接入文件存在，甚至目录中只有一个 Provider，都不会自动成为本次选择。

多平台企业可分发多个 Provider reference，每次只加载选中的一个。所选文件缺失或访问不可用时，记录缺口，不切换到其他平台猜取日志；已绑定且足够的本地证据仍可使用。

本仓库保留 GitHub Actions 作为可选接入示例。企业不使用它时可以从发行包移除，并同步清理 [Provider 入口表](references/providers/provider-access.md#确定-provider-与加载入口)中的对应条目，以及本 README 中对应的示例和目录链接，检查剩余引用有效。通用流程无需加入企业专用分支。

多租户环境可由宿主额外提供 Provider/reference 允许范围，并限制实际工具与凭据。Skill 会遵守已声明的限制，但目前不实现允许列表解析或强制隔离机制；单平台接入不要求先建设这套机制。执行规则见[选择与权限](references/providers/provider-access.md#选择与权限)。

### 可选示例：GitHub Actions

例如 GitHub Actions 调用方可以通过可信命名输入提供以下信息，示例中的运行身份应替换为实际值：

~~~yaml
provider: github_actions
failure_ref:
  run_id: 12345
  run_attempt: 2
  job_id: 67890
~~~

这会选择 [GitHub Actions reference](references/providers/github-actions.md)。它提供有官方文档依据的只读 CLI 示例和 MCP 能力要求；是否可用仍需核对当前工具、主机、仓库、权限与实际响应，不代表已完成线上验收。企业 Provider 也可通过 `provider_reference` 指向 Skill 内的实际文件；未知 Provider 没有默认 CLI。

Provider 定制负责证据访问；分类体系、根因与目标行为判断、修复准入、修改验证和停止/移交规则由对应指南维护。只读日志能力不意味着已支持候选发布或远端验证。已有日志足够时，不要求为使用本 Skill 再安装平台工具。

### 接入验收

维护者应使用实际宿主观察工具调用及返回证据，而不只检查文案或字段。以下是待验证的行为标准，不表示这些平台路径已经通过验收：

| 场景 | 应观察到的行为 |
| --- | --- |
| 已有绑定准确且足够的本地证据，包括 Provider 未命名 | 直接诊断，不猜平台、不重复远端获取 |
| 本地缺日志，指定 MCP 可用 | 定向获取目标运行、Job 和日志，核对身份 |
| MCP 不可用或能力不足，声明的 CLI 可用 | 使用已有权限，只读补齐同一 Provider 的缺口 |
| reference 的 CLI 部分仅声明可执行文件/帮助入口，帮助足以说明相关读取操作 | 按缺口查帮助并自主组合命令；没有逐条映射也能取得并核对目标证据 |
| 帮助不足以确认目标重试或操作副作用 | 查阅获准补充资料；仍无法确认时暂停相关操作并披露缺口 |
| 帮助同时列出查询、重跑和发布功能 | 取证路径仅使用获准的读取能力，不因帮助列出写操作而执行 |
| 所有声明路径均不能满足需要 | 保留有效证据，按既有规则结束或协作，不自动安装、登录或猜日志 |
| 目标重试与旧重试并存 | 当前证据绑定目标准确实例；旧日志仅作独立对照 |
| 分页缺失、日志截断或多来源身份冲突 | 补齐可得内容并保留缺口/冲突，不冒充完整 |
| 只有一个 Provider 文件，但没有可信绑定 | 不凭文件唯一性选择平台；继续可用的本地路径 |
| 多 Provider 包明确选中其中一个 | 仅加载选中入口，不跨平台回退 |
| reference 与宿主允许范围冲突 | 暂停依赖冲突的平台访问，不用自定义路径绕过限制 |

只有真实执行证据才能支持对应能力已验收；远端发布和完整 `remote_verify` 另按[适配器验收证据](references/provider-contract.md#适配器验收证据)检查。

## 企业分类自定义

Classification Profile 是企业或个人分类标准的文档包，与 Provider 独立：Provider 负责取得日志，Profile 定义如何分类。通用发行包保持未配置，使用者可定制自己的版本并固定标准；分类不直接决定修法、修改权限或停止策略。Agent 执行规则见[企业分类执行](references/failure-classification.md)，以下用于维护和接入标准。

### 1. 准备目录与细则

沿用企业已有编号、名称、层级及定义，不必映射为通用分类树。若企业采用“3 阶段 → 11 大类 → 39 小类”，就在自己的 Profile 完整保留它；这些数量不是通用 Skill 的限制。建议放在 Skill 的 references 下：

~~~text
references/classification-profiles/<company>/<product>/
├── profile.md     # 标识、版本、范围、完整目录与规则索引
└── rules.md       # 各层定义、小类判据、排除与重叠边界
~~~

这些路径是企业创建资源时的示意，本仓库尚未附带真实企业类别。已有受信任文档能提供相同内容时可直接引用，避免平行副本。细则较长且宿主无法按章节读取时，可按大类拆分为 `rules/<大类编号>.md`；不默认创建 39 个文件或 39 份修复指南。

`profile.md` 可按以下骨架编写；示例行须替换为真实全量目录后再启用：

~~~markdown
# <实际 Profile 标识>

版本：<企业实际版本及来源；没有版本时注明未知>
适用范围：<产品、仓库、版本或构建条件>
分类维度：<阶段、大类、小类各自识别什么；症状、原因或责任的定义>
类别关系：<互斥、允许共存及已有优先规则；未规定处明确说明>

| 阶段编号与名称 | 大类编号与名称 | 小类编号与名称 | 一句话判别说明 | 细则位置 |
| --- | --- | --- | --- | --- |
| <沿用原标准> | <沿用原标准> | <沿用原标准> | <现象/机制及关键区分点> | <实际文件或章节引用> |

标准特有要求：<若有，说明分类与报告要求；不在此授予修复权限>
~~~

目录维护路径、名称和简述，细则维护判据；目录不能只有缩写，也不复制全部规则。阶段和大类同样需要定义，才能在证据不足以支持小类时有据地停留在上级。失败 Step 名称不自动等于分类阶段。

`rules.md` 按原标准组织，单个小类可用以下模板：

~~~markdown
## <现有小类编号>：<现有名称>

### 定义
本类识别的现象或机制。

### 分类依据
必须满足的条件、可替代条件及辅助线索；每项如何由日志/上下文核对。
原因型类别说明还须取得哪些源码、配置或其他证据。

### 易混淆、排除与共存
相邻类别、排除条件、区分证据，以及标准明确允许的共存或优先关系。

### 调查入口与检索提示（可选）
应核对的对象、实际可访问的指南、错误码/任务名/字面关键词。
检索命中不是分类证明，修复建议只是待核查线索。

### 示例
脱敏正例、相似反例、证据不足例；分别标明初始日志与补充证据允许的结论。
~~~

### 2. 在定制版本中固定 Profile

推荐企业或个人在拿到通用版本后，先准备自己的 Profile，再修改 [SKILL.md 的“分类 Profile 绑定”区](SKILL.md#分类-profile-绑定)。通用版本的两个值均为“未配置”；定制时在原位置替换为实际标识及入口，不在其他位置重复追加声明。例如：

~~~markdown
## 分类 Profile 绑定

- 固定 Profile：my-company/my-product
- Profile 入口：references/classification-profiles/my-company/my-product/profile.md
~~~

这是发行包级的明确绑定，不是可被本次调用自动覆盖的默认值；README 中的示例本身不生效。示例路径需先创建，标识须与入口声明一致。个人可以使用自己的命名，如 `personal/native-build`，不要求存在企业或产品组织层级。

定制完成后，Agent 每次使用该版本都会按绑定读取 Profile，调用者无需再提供分类说明。原有调用保持不变：

~~~text
/ci-self-healing "/path/to/failure-scene" "/path/to/healing-output"
~~~

仅当发行包**没有固定绑定**时，才可由明确请求或可信启动配置选择本次 Profile，例如：

~~~text
使用 $ci-self-healing 修复本次构建失败。
work_dir: /path/to/failure-scene
output_dir: /path/to/healing-output
本次分类 Profile：<company>/<product>
Profile 入口：references/classification-profiles/<company>/<product>/profile.md
~~~

启动器注入是可选方式，不是定制版本的必需步骤。上述分类说明属于可信任务上下文，不新增 CLI 参数、配置文件解析器或机器协议。固定绑定同样适用于仅诊断和其他修复调用，不依赖两个目录参数是否提供。

入口相对路径以 **Skill 根目录** 解析，包内链接以所在文件为基准，不是相对于 `work_dir`。发行包只需携带实际使用的标准；放入文件不会自动启用。每个调查对象使用一份 Profile，多产品选择不自动合并。

固定绑定冲突或失效时，Agent 会披露缺口而不是自动换标准；维护者需要更换时，更新唯一绑定区。来源权限、适用性和冲突处理统一见[选择标准与绑定对象](references/failure-classification.md#选择标准与绑定对象)，这些缺口不一律阻止通用取证。

### 3. 允许定制什么

企业可以维护类别路径、判据、排除与共存边界、检索提示、诊断入口和分类报告要求。类别到指南是多对多的建议映射，不要求每类有专用修法。

代码授权、目标行为核查、修复准入、验证和停止规则仍由通用流程及可信执行策略控制。若企业要求“确认某类后才可修改”或“特定情况转人工”，可按[企业修复策略定制](#企业修复策略定制)由可信发行包引用企业规范，或由可信宿主/适用仓库策略明确提供；普通 Profile 或类别名称本身不授予或改变权限。

### 4. 分类结果与修复结果分别交付

结果复用 `diagnosis.classification`，在现有 `analysis-fix.md` 的“失败与根因”中分别展示初始分类与最终分类，在“剩余问题与交接”中披露缺口，不生成第二份分类报告。初始分类允许没有小类；最终分类要求给出有证据支持的小类及父级路径。例如：

~~~text
使用标准：<实际 ID、版本、入口>
绑定来源：<SKILL.md 固定绑定区，或未固定时的可信调用>
当前失败：<仓库及 run/Job/重试/命令，或本地失败引用>
初始分类：<首次判断的阶段 → 大类 → 小类；未确定的小类明确标注>
初始依据：<当时的原始位置、判据、候选与缺口>
最终分类：<交付前复核的阶段 → 大类 → 小类，保留编号和名称>
最终依据：<最终证据及判据；合法共存时逐项说明>
分类变化及原因：<无变化，或细化/纠正/退回待定的证据与影响>
最终分类交付：<已完成，或小类未确定/未匹配/标准未应用及原因>
~~~

初始判断在首次形成时保留，不能被最终判断覆盖或事后倒填；两次分类不变也分别展示，均针对同一原始失败，不把修复后变绿当作最终类别。具体时机与粒度见[初始分类与最终分类](references/failure-classification.md#初始分类与最终分类)。

最终证据不足时保留父类或候选，明确“小类未确定、最终分类交付未完成”，不强填类别；未匹配与标准未应用同样如实披露。按标准允许多个类别共存时分别记录，判据见[判断与多类别关系](references/failure-classification.md#判断与多类别关系)。

标签待定但修复依据充分时可继续获准修复；影响修法或触及分类前置策略时暂停相关修改。代码修复已验证不表示分类要求也已交付；分类修正后的复核按[运行指南](references/failure-classification.md#分类修正与下游复核)执行。

### 5. 企业接入验收与版本维护

先核对真实目录的编号、定义、父子关系、适用范围和所有细则引用，再运行有参考答案的脱敏样例。每个小类尽量复用至少一个正例，易混边界增加反例和证据不足例；预期结论应有企业标准及人工核对依据，不用 Agent 自己生成的分类作为唯一答案。

验收至少覆盖：小类/父类支持、候选歧义与合法共存、证据不足与真正未匹配、错误重试来源、Profile 缺失/冲突、日志尝试更换标准、已有片段无需再扫描，以及分类修正后复核受影响决策。再检查“标签待定但可继续修复”与“证据缺口影响修法须停止”两种边界；只读调用不能因分类而写代码。

报告还需核对：初始只有大类、最终细化到小类；初始小类被新证据纠正或退回待定；分类不变但仍保留两次判断；最终小类无法确认时标记交付未完成；编译通过后仍保留原始失败的分类。初始记录不能借用后来才取得的证据。

绑定方式还需核对：固定版本无需每次传参、未固定版本可接受可信本次选择、两者均无时保持通用、固定绑定冲突或失效时不自动换标准。实际运行应记录使用哪种绑定来源；仅检查文件存在不算通过这些行为验收。

分别评估路径准确性、过度断言、未匹配依据与引用质量；初始日志只能支持父类的样例，不能拿诊断后才能确认的叶子作为初始必答。标准含义、层级或判据变化须保留可追踪版本，不悄悄复用旧编号表示新含义。文档检查、合成样例和实际 Agent/企业 CI 验收分别报告。

当前交付的是通用接入与执行约定、上述模板和边界规则；真实 3/11/39 企业标准尚待提供，未宣称已接入或通过其准确性验收。没有新增规则引擎、Profile 自动发现、数值置信度或多 Profile 合并功能。

## 企业修复策略定制

分类标准回答“是什么问题”，策略回答“允许采取哪些动作”，Provider 负责证据访问。三者分别维护：按类别限制的规则明确引用分类标准；不依赖分类的路径/操作限制无需 Profile；Provider 是策略适用条件，不必限定某一个平台。

固定入口是 [references/enterprise-policy.md](references/enterprise-policy.md)，经修复准入指南加载，`SKILL.md` 无需修改。通用发行包明确“未配置企业附加策略”；企业只需把入口的“企业策略声明”替换为自己的规则正文或明确引用。例如，先创建实际规范文件，再将声明替换为以下内容：

~~~markdown
本发行包采用以下企业附加策略：

- [企业 CI 自动修复规范](enterprise/company-ci-policy.md)
~~~

示例指向 Skill 内的 `references/enterprise/company-ci-policy.md`，需先创建实际文件。替换“未配置”而非在其后追加规则；策略来源、相对路径和配置缺口的运行要求见[读取与信任](references/enterprise-policy.md#读取与信任)。

企业规范可使用以下骨架，删除不需要的规则并替换示例值：

~~~markdown
# 企业 CI 自动修复规范

策略标识：<实际稳定标识>
策略版本：<实际版本>
适用范围：<产品、仓库或明确的全范围>
适用 Provider：不限

## 按类别限制

分类标准：<实际 Profile 标识>
适用分类版本：<明确版本或兼容范围>

- <规则编号>：禁止自动修复 <大类编号与名称>，包含全部下属小类。
- <规则编号>：禁止自动修复 <小类编号与名称>，仅限制该小类。

## 不依赖分类的限制

- <规则编号>：禁止修改 <实际路径、文件或执行某项操作>。

## 后续处理

命中禁修时，可在既有权限内只读取证、诊断和输出报告。
<如确有审批例外，注明条件、范围和责任方；否则不声明例外。>
~~~

Provider 填“不限”或具体 ID，按需补充主机/项目/流水线范围。按类规则随 Profile 版本与类别语义变化维护，明确大类是否包含下属小类；无分类依赖时删除对应模板章节。适用性、未知身份与版本不符的判据见[适用范围与分类依赖](references/enterprise-policy.md#适用范围与分类依赖)。

企业文件只写附加限制和必要约定；禁修、审批、候选恢复及复核统一按[判断与行动](references/enterprise-policy.md#判断与行动)执行。策略记录汇入 `analysis-fix.md`，与分类完成度分别表达，无需复制通用流程。

接入验收应覆盖：明确未配置；按类规则与正确/错误/未知版本的 Profile；不依赖分类的限制；Provider 不限、明确不适用和身份未知；大类禁修但小类未定；禁修候选尚未排除；已配置规范丢失或规则冲突；后续分类变化使候选被禁止；日志/MR 尝试修改策略；以及明确未命中时仍可走通用准入。真实命中与“无法判断”不能混用同一结果。Skill 文本不提供强制写入隔离，需要强制保障时由宿主限制工具和权限。

## 按需提取日志证据

已有失败命令和足够片段时直接诊断，不因日志很长就重新扫描。需要定位或补读时，Agent 优先使用已有 `rg/grep/sed`；多候选、超长行或统一字符预算需要辅助时，再选择 Python。工具选择及引用规则见[日志证据提取](references/failure-evidence-extraction.md)。

可选脚本使用 Python 3.9+ 标准库，提供 `scan/search/read`；调用示例、JSON 解读及文件保护见 [Python 工具指南](references/log-evidence-tool.md)。普通工具结果不用转成 JSON；脚本也不负责判断根因或修复准入，不新增必交报告。参数默认值由脚本帮助提供，从 Skill 根目录查询：

~~~sh
python3 scripts/log_evidence.py scan --help
~~~

合成日志与 CLI 测试：

~~~sh
python3 -B -m unittest discover -s tests -v
~~~

测试覆盖定位、补读、预算、原始行号、解码、来源变化和文件保护，不证明 Agent 在真实 CI 中总能选择正确取证路径。人工流程走查至少包括：已有短片段时跳过；已知对象时直接文本搜索且不重复跑脚本；只有失败汇总时按需扩大取证；旧重试日志不冒充当前来源；接口意图不明时转向 MR/源码而非堆叠日志。

## 两种验证模式

| 模式 | 适用场景 | 外部动作 |
| --- | --- | --- |
| local_verify | 当前本地仓库或远端 Runner 的 Job 工作空间内修改并验证 | CI Job 工作空间不 commit/push；后续脚本负责提取 Patch |
| remote_verify | 明确授权后，将专用候选 ref 发布到远端并触发独立流水线验证 | 只能使用专用候选命名空间；不写默认/受保护分支，不合并、部署或发布 |

运行在远端 Runner 本身不会自动切换到 remote_verify。

## 关键决策原则

- 用户意图和适用契约优先于“最小改动”以及“尽快变绿”。
- 调用方、被调用方、重复类型、适配层或依赖版本存在相反且都有依据的语义方向时，必须继续取证或停止，不能凭提交顺序、改动行数或编译通过自行选择。
- 绿色构建不能替代目标行为证据，也不能覆盖未处理的反证。
- 没有交互或移交通道时，输出 inconclusive 及缺口，不等待不存在的人工响应。
- Skill 不自动生成完整 Patch；在 CI Job 场景中保留稳定工作空间供后续脚本提取。

## 报告

最终报告标题为“CI 自愈诊断与修复报告”，固定包含：

1. 处理结论；
2. 失败与根因；
3. 目标行为与修复依据；
4. 实际修改；
5. 验证结果；
6. 剩余问题与交接。

提供有效的 output_dir 时，报告写入：

~~~text
<output_dir>/analysis-fix.md
~~~

没有 output_dir 时，直接在宿主消息中输出完整文字报告。报告交付失败不会被伪装成成功，也不会静默改名或换目录。

## 目录说明

- [SKILL.md](SKILL.md)：Agent 执行入口和硬边界。
- [references/diagnosis-and-repair.md](references/diagnosis-and-repair.md)：失败合同、上下文取证、意图判断和停止规则。
- [references/failure-classification.md](references/failure-classification.md)：可信 Profile 的分类执行、证据判定、修正和修复边界；企业维护模板见上方自定义指导。
- [references/failure-evidence-extraction.md](references/failure-evidence-extraction.md)：分类与诊断共用的按需日志检索、补读与引用规则。
- [references/log-evidence-tool.md](references/log-evidence-tool.md)：仅选择 Python 辅助工具时读取的操作、输出和预算说明。
- [scripts/log_evidence.py](scripts/log_evidence.py)：可选的本地日志流式 `scan/search/read` 辅助工具。
- [tests/test_log_evidence.py](tests/test_log_evidence.py)：合成日志单元测试与 CLI 集成测试。
- [references/repair-and-verification.md](references/repair-and-verification.md)：准入、候选、分层验证和结果判定。
- [references/enterprise-policy.md](references/enterprise-policy.md)：企业附加策略的固定入口、适用范围核对、禁修与审批边界；通用发行包默认未配置。
- [references/execution-modes.md](references/execution-modes.md)：本地/远端模式、工作空间保护和协作交付。
- [references/result-report.md](references/result-report.md)：文字报告结构和 analysis-fix.md 输出约定。
- [references/provider-contract.md](references/provider-contract.md)：CI Provider 能力与证据边界。
- [references/providers/provider-access.md](references/providers/provider-access.md)：Provider 选择与权限、本地/MCP/CLI 访问和证据交回规则。
- [references/providers/github-actions.md](references/providers/github-actions.md)：GitHub Actions 的身份核对与只读接入示例。
- [references/java-maven-compilation.md](references/java-maven-compilation.md)：Java/Maven 专项指导。
- [references/c-cpp-compilation.md](references/c-cpp-compilation.md)：C/C++ 专项指导。
- [references/dependency-diagnosis-and-repair.md](references/dependency-diagnosis-and-repair.md)：依赖问题专项指导。
- [references/ci-job-failure-directory.md](references/ci-job-failure-directory.md)：企业 CI Job 失败现场目录约定。

## 重要提醒

这是仓库内的 Skill，不需要全局安装。README 只服务于人类理解和接入；修改执行规则时，应更新 [SKILL.md](SKILL.md) 及对应 reference，并保持参数和报告一致。正式内容以本目录为准。
