# Java/Maven 编译诊断与修复

本指南只补充 Java/Maven 的构建上下文、取证入口与验证注意事项；共用 [诊断与上下文](diagnosis-and-repair.md)。只读任务保持只读取证，以下涉及修改或验证候选的内容仅在修复分支使用。

## 适用范围与方法选择

适用于 Maven 驱动的 Java 主源码、测试源码编译失败，以及导致这些失败的源码、编译依赖、Maven 配置、注解处理和生成代码问题。先依据实际日志识别失败来源；不要把所有 Maven 失败都当成 javac 源码错误。依赖下载、认证或解析失败转[依赖诊断与修复](dependency-diagnosis-and-repair.md)，与源码编译同时相关时按信息缺口组合使用。测试运行、应用或构建插件运行时的类加载/链接异常，以及非 Java 工具的失败，先按[通用失败准入](diagnosis-and-repair.md#准入正确的失败)判断；有依赖相关证据时再结合依赖指南，不直接套用源码修复。

## 按信息缺口取证

选择修法前完成[变更上下文与预期行为](diagnosis-and-repair.md#变更上下文与预期行为)确认，联查本次 MR 或提交中的源码、POM、配置、生成任务、测试和文档变化。按当前信息缺口选择下表中的证据入口，并复用当前候选仍然有效的证据。

| 需要回答的问题 | 相关证据与可用入口 |
| --- | --- |
| 哪个模块、哪类源码失败？ | 原始命令、工作目录、失败插件 goal/execution、Reactor 摘要和诊断上下文；区分主源码与测试源码，按模块和执行归属关联交错日志 |
| 实际生效的配置来自哪里？ | 当前及父 POM、BOM、Profile 和命令行属性；必要时使用 `help:effective-pom`、`help:active-profiles`，追溯配置而不是只读当前 POM |
| 实际由哪个编译器、按什么目标编译？ | 项目 Maven/Wrapper 版本、运行 Maven 的 JDK、Toolchains、Compiler Plugin 版本及 `compilerId`、`fork`/`executable`、`release/source/target` 等相关配置与执行证据 |
| 类或方法为什么不可见或不匹配？ | 定义与调用、Git 差异、实际解析的依赖版本与 scope、编译 classpath/module-path；按需使用 `dependency:tree`、仓库搜索、`javap` 或已有语义工具，核对当前使用的类与签名 |
| 预期生成的内容为什么不存在？ | 生成任务、处理器及其配置、源码根目录、模板或协议等输入，以及实际执行和输出；不要从缺失类名直接推断必须手写实现 |
| 修改会影响哪些模块？ | Reactor 选择范围、上下游依赖、实际使用的模块产物与近期接口变化；不要假设从子模块启动就保留了原 CI 的构建范围 |

Maven Help 的 Effective POM 包含继承与已激活 Profile 的结果，`verbose` 可帮助追溯元素来源；Toolchain 可让编译 JDK 不同于运行 Maven 的 JDK。仅查看某个 `pom.xml` 或终端的 `java -version`，不一定能解释实际编译条件。[Help Plugin](https://maven.apache.org/plugins/maven-help-plugin/)、[编译 JDK 与 Toolchains](https://maven.apache.org/plugins/maven-compiler-plugin/examples/compile-using-different-jdk.html)

依赖树提供依赖关系线索，不单独证明编译器实际加载了哪个类。查看外部类时，应关联实际使用的产物、classpath/module-path 和相关版本；不要用另一版本的源码或文档替代当前证据。[Dependency Plugin](https://maven.apache.org/plugins/maven-dependency-plugin/tree-mojo.html)、[javap](https://docs.oracle.com/en/java/javase/21/docs/specs/man/javap.html)

使用项目已有的 Maven/Wrapper 入口和适用版本；注解处理等配置须匹配实际 JDK、Maven 和插件版本，不照搬最新示例。只在有助于当前问题时提高日志详细程度；原始日志保留受控引用，不将敏感 settings、凭据或全量环境变量复制到提示词、收据或提交中。[注解处理器配置](https://maven.apache.org/plugins/maven-compiler-plugin-4.x/examples/annotation-processor.html)

## Java 修复注意事项

- 修复位置沿用 [通用选择原则](diagnosis-and-repair.md#选择修复位置)。`cannot find symbol` 可来自源码引用、依赖可见性或生成内容；参数/签名不匹配使用下方检查入口，不把报错文本当成修法。
- 类型转换、接口调整、依赖/BOM/Profile 或 Java 目标变更须符合已确认行为、兼容性和授权；空实现、默认返回值或排除源码不能只为压制编译错误。
- 生成物有模板、协议或处理器时，修正输入或配置并按项目方式生成，避免只修改会被下次构建覆盖的输出。
- 新增工具、安装或升级 JDK/Maven、修改全局配置或访问未授权资源不是默认前置步骤；确有需要时明确能力缺口和具体动作，按 [自主推进与停止](diagnosis-and-repair.md#自主推进与停止)处理。

### 参数或签名不匹配

围绕以下问题补足判断，不由报错类别直接选择删参数或扩展接口：

- 编译器实际使用的类、签名和版本是否符合本次变更要求？先区分源码契约不一致与错误依赖、导入或生成产物，不依据另一版本的定义修复。
- 当前证据是否支持不同目标行为，例如调用方误用，或同一特性的接口/实现扩展遗漏？结合完整变更与适用契约判断；没有有据的相反解释时，无需强行构造分支。
- 若存在相反方向，什么输入、条件或兼容性要求能区分它们，已有证据要求哪种结果？按 [语义方向歧义](diagnosis-and-repair.md#语义方向歧义)取证；修复任务中仍未解决时停止相关候选修改，按 [自主推进与停止](diagnosis-and-repair.md#自主推进与停止)澄清、移交或结束。证据充分时按通用准入自主修复，不另设 Java 专属准入标准。

## Maven 验证与交接

修复任务在制定计划和执行候选验证前读取 [修复准入与验证](repair-and-verification.md)，以下只补充 Maven 相关检查。

- 以项目已有入口、原失败命令和已声明检查为依据选择验证。区分 `compiler:compile`、`compiler:testCompile` 与测试执行；直接调用某个插件 goal 不一定包含生成源码等前置阶段，按项目实际生命周期确认所需步骤。[Maven 生命周期](https://maven.apache.org/guides/introduction/introduction-to-the-lifecycle.html)
- 可以用局部编译或聚焦检查缩短反馈，但不能把它冒充原始 CI 或完整影响范围的通过。共享接口或依赖变更须考虑调用方；Reactor 的 `--also-make` 加入上游依赖，不等于验证了全部下游消费者。[多模块构建](https://maven.apache.org/guides/mini/guide-multiple-modules.html)
- 保留原验证要求的 Profile、关键属性、编译目标和工具链条件。核对检查是否命中了正确模块、源码范围和候选；仅有 `BUILD SUCCESS`、检查被跳过或验证了错误目标，都不足以确认修复。已有通过证据发生相关漂移时按核心合同失效处理。
- 不默认执行 `clean`、删除 `target`、清空本地依赖缓存或拉取最新依赖。若有证据需要排除陈旧产物干扰，采用范围受控的重建方式，先确认不会破坏 Job 已准备且无法重建的必要输入，并遵循[基线与候选恢复](execution-modes.md#基线与候选恢复)。

修复收据在 [既有字段](core-contracts.md#收据绑定与失效)中补充与结论相关的模块、编译环境及证据引用；交付遵循 [执行模式](execution-modes.md#结果报告)。
