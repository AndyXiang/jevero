# Tag 系统设计

状态：**P0 + P1 已实现**（代码 + 199 个测试全绿），**尚未对真实库运行**。§5.1 的 `formal/*` 拆分、§5.5 的 coverage 落地、`jevero vocab` / `jevero eval` 属于 P2/P3，**未实现**。
相关代码：`config.yaml`（词汇表与退役表）、`jev.py`（判定与指纹）、`policy.py`（选择规则与收敛）、`routing.py`（闭集投影）、`main.py`（候选集与使用摘要）。

## 决定记录

| # | 决定 | 依据 |
| --- | --- | --- |
| 1 | 选择规则 = **floor 0.60 判成员 + 排名截断判聚焦**，不用滞回 | 刀口要放在判断的空谷里；同配置重跑 0/29 篇变动（旧 0.85 是 3/29） |
| 2 | `topic/*` + `kind/*` + `jevero/*` 归**机器所有**，每次运行全量收敛；只删"已知名" | 幽灵标签必须能自动消失，而人工标签必须安全 |
| 3 | collection 投影**回到层级**：`02 Topics/<name>`、`03 Kinds/<name>`、`04 Review` | 你现有的平铺目录要手动删，`route` 会重建嵌套的那套 |
| 4 | 用途 = 按主题浏览 / 为具体课题检索 / 追踪项目进展 → **不加 reading-status 轴**；`projects` 继续暂缓 | 避免造一条你不用的轴 |
| 5 | topic 加**保证带** `topic_guaranteed: 0.95`：≥0.95 无条件收，`[floor, 0.95)` 按排名补齐到 `topic_max` | 实测 29 篇里没有一篇有 >2 个 ≥0.95，所以今天零成本；它是"4 个都是正题"时的安全网 |
| 6 | 改名：`role/` → `kind/`（含义已变成纯体裁）、`agent/` → `jevero/`（名不副实的记账）；`topic/` 不动 | 一次迁移用 `retired_prefixes` 清掉旧名 |
| 7 | 指纹**不写标签**，写进 item 的 `extra` 字段一行 `jevero-fingerprint: <digest>` | 需求（知道哪篇是哪版判的）保留，但不在标签面板里塞 29 个乱码；`extra` 本来就是 Zotero 放机器数据的地方 |
| 8 | kind 与 topic **共用同一个 floor**，只各自设 cap | kind 的 0.85 同样压在密集区（±0.05 内 22/203）；统一后规则只有一条 |
| 9 | `retired_kinds: [core]` | `core` 0 次应用（最高 0.44），它问的是"是否是你当前工作的核心"，属于暂缓的 `projects` 维度 |
| 11 | **状态按读者分层**：标签只留 `topic/*`、`kind/*`、`review/*`（人要看要筛的）；“是否判过”“上次失败”写进 `extra` 的 `jevero-fingerprint` / `jevero-error` | `jevero/processed` 其实冗余（有指纹即已判），而 `jevero/*` 标签在标签面板里是没人会搜的乱码。`kind/review` 同时改名为 `kind/overview`，避免与人工审查队列混淆 |
| 10 | kind 单独一条地板 `kind_floor: 0.70` | 两次独立运行之间 `kind/method` 有一个标签出现/消失：kind 判断密集在 0.60 附近（"这是什么类型的文章"比 topic 更爱对冲）。0.70 实测 0/29 变动 |

第 8、9 条是实现期间由我提出、你已确认的；第 5–7 条来自你最后一轮的三点反馈。

---

## 0. Tag 系统包含四件事，我们只设计了第三件

| 环节 | 含义 | 改之前 |
| --- | --- | --- |
| ① 词汇表 | 有哪些词、谁定义、什么时候删 | 手写 YAML，**从无生命周期** |
| ② 判定 | 每篇得到哪些词的概率 | 已实现，设计良好 |
| ③ 选择规则 | 概率 → 标签 | 已实现，但用一个数字干两件事 |
| ④ 生命周期与投影 | 词和标签怎么演化、怎么撤回、怎么变成目录 | **基本没设计** |

过去几轮所有痛点都落在 ①④：删词后库里留下 37 个幽灵标签要手动清；改一个词的描述让另一个词的概率整体漂移；`route` 会把已删的词重新建成目录。②本身没问题。

---

## 1. as-built 清单

| namespace | 语义 | 基数 | 选择规则 | 拥有者 | 投影 |
| --- | --- | --- | --- | --- | --- |
| `topic/*` | 研究对象 + 形式体系 + 方法（**三种轴混在一起**） | 0..N，`topic_max: 4` | `p >= 0.85` | 名义机器，实际**只增不减** | `02 Topics/<name>` |
| `kind/*` | 论文体裁 | 0..N | `p >= 0.85` | 同上 | `03 Kinds/<name>` |
| `jevero/*` | 流程状态 + review reason | 恰好 1 状态 + N reason | 确定性，全量收敛 | 机器 | `04 Review`（仅 review） |
| coverage | 词表是否够用 | 三选一 | 只驱动 review reason | — | **不落地为标签** |
| 其它（arXiv 分类、`to-read`…） | — | — | 人 | 人 | 不碰 |

词汇表规模：`topic` 11 词、`kind` 7 词、`coverage` 3 态。

---

## 2. 病灶（每条都有实测证据；样本 = 库中 29 篇论文）

### 2.1 词汇表没有生命周期，于是按想象写的词永远不会被删

29 篇上的应用次数：

```text
topic（11 词）   quarkonium 16  nrqcd 12  energy-correlator 11  scet 5  fragmentation 4
                pnrqcd 3  jet 2  lattice-qcd 1  tmd 1  chiral-dynamics 1
                heavy-flavor 0（最高 0.53）
已删（5 词）      perturbative-qcd / collider-phenomenology / experiment / amplitudes
                / loop-integrals   —— 全部 0 次应用
kind（7 词）     theory 6  phenomenology 5  method 5  review 2
                core 0（最高 0.44）  experiment 0（最高 0.04）  reference 0（最高 0.74）
```

**13 个 topic 里 5 个、7 个 kind 里 3 个从未被应用。** 它们不是"暂时没用上"，而是按想象写的：`collider-phenomenology` 就是 `kind/phenomenology`，`experiment` 就是 `kind/experiment`，`perturbative-qcd` 被 `nrqcd/pnrqcd/scet` 蕴含。

而 `topic/*` 只增不减，所以这些词在库里留下了幽灵标签：

```text
topic/perturbative-qcd 16 篇   topic/heavy-flavor 10 篇
topic/collider-phenomenology 10 篇   topic/loop-integrals 1 篇
```

`kind/core` 是个特例：它问"是否是你当前工作的核心"，而"当前工作"从没被描述过（`projects` 维度被暂缓）——**它是 project 相关性判定，错放在 kind 轴里**。

### 2.2 判定没有版本，于是只能全量重跑

标签里没有"这篇是用哪版词表 / 哪版 prompt / 哪个模型判的"。所以词表一改，唯一的做法是 `--include-processed` 把 29 篇全部重判（$0.0045/次）。无法回答"哪些篇过期了"，也无法增量收敛。

### 2.3 标签没有归属（机器 / 人），于是不敢撤回

`policy._converged()` 只对 `jevero/*` 做全量收敛；`topic/*`、`kind/*` 保持只增不减，理由写在代码注释里：*"人可能手工加过，且概率会漂移几个百分点，删除会删掉人的意图并来回抖动。"* 这个理由是对的，但它导致 2.1 的幽灵标签只能人工清理。

### 2.4 一个阈值同时干两件事，而且刀口落在判断最密的地方

`topic_apply: 0.85` 同时承担 **"算不算这个领域"** 和 **"要不要收进来"**。实测它正好压在模型判断的密集区：

```text
阈值        0.55  0.60  0.65  0.70  0.75  0.78  0.80  0.83  0.85  0.88  0.90
±0.05 内判断数  5     3     2     1     1     2     3     7    12    17    29     (n=319)
```

同 config、同 prompt 连跑两次（609 个判断）：

```text
|Δp|  中位 0.000   p90 0.020   最大 0.050
跨过 0.85 的判断：5 个        跨过 0.70 的判断：0 个
```

也就是说**光靠运行间抖动，每次重跑就有 5 个判断翻过 0.85**，对应 3/29 篇论文的标签集发生变化：

```text
[44XLA6U4] {nrqcd, pnrqcd, quarkonium} -> {pnrqcd, quarkonium}   （丢了主 topic）
[ZVB6B4ZK] {energy-correlator, scet}   -> {energy-correlator}
[PAVZQ7RG] {energy-correlator, jet}    -> {energy-correlator}
```

同一个病还有第二个症状：**词表规模与阈值耦合**。因为所有 topic 在一次请求里互相竞争，改一个词的描述会让另一个词整体漂移——实测 `scet` 在一篇 EEC 论文上从 0.72 掉到 0.13，只因为另一个词的描述被改了。

### 2.5 维度混在一个 namespace 里

`topic/*` 同时装研究对象（quarkonium）、观测量（energy-correlator）、形式体系（nrqcd/scet/tmd）、方法（lattice-qcd）。它们的**可靠性和合适的选择规则并不相同**，但共用一个阈值：

```text
topic 用 floor 0.60 + 排名前 3：平均 2.03 个/篇，同配置重跑 0 篇变化（好）
kind  用 floor 0.60 + 排名前 2：method 17 篇、theory 16 篇、phenomenology 11 篇
                               平均 1.62 个/篇（当前 0.62），判别力消失（坏）
```

### 2.6 coverage 的判定不落地

`covered` / `missing-topic` / `irrelevant` 只决定 review reason，不写标签。于是"系统认为这篇超出范围"和"这篇没有 topic 过线"在库里**不可区分**，无法审计。

### 2.7 投影不闭合：`route` 会把已删的词重新建成目录

`routing.target_paths()` 按前缀盲投（`tag.startswith("topic/")`），不查当前词表。实测 `jevero route --all --dry-run` 现在会输出：

```text
+ 02 Topics/collider-phenomenology     <- 词已删，标签还在
+ 02 Topics/perturbative-qcd           <- 词已删，标签还在
+ 02 Topics/energy-correlator          <- 顶层已有同名目录，会造出第二套
+ 02 Topics/jet   (does not exist yet)
```

同时你的 collection 已被拍平到顶层（15 个 topic/kind 目录 + `00–04` 空壳），而 config 仍是 `topics_parent: "02 Topics"`——配置与现实不符，跑一次就产生平行目录树。

---

## 3. 实测：三种选择规则

用 run3 与 run4（同 config、同 prompt）对比，衡量"标注质量"和"稳定性"：

| 方案 | 平均标签数 | 分布 | 同配置重跑后变化的篇数 |
| --- | --- | --- | --- |
| **S1 绝对阈值 0.85（旧）** | 1.86 | 1个:7 2个:16 3个:5 | **3 / 29** |
| **S2 低地板 0.60 + 排名前 3（采用）** | **2.03** | 1个:5 2个:15 3个:8 | **0 / 29** |
| S2′ 两带（0.95 保证 + `[0.60,0.95)` 排名前 3） | 2.03 | 同 S2 | 0 / 29 |
| S3 滞回 0.85 / 0.70 | 1.86 | 同 S1 | 0 / 29 |

S2 与 S1 的行为差异**只有 3 个标签、2 篇论文**，且都在对的论文上，S2 一个也没丢：

```text
[L7IX3YK3] Fragmentation, Factorization and Infrared Poles...  + fragmentation 0.70
[8XE5WCXB] Energy Correlators in Semi-Inclusive e+e- ...       + fragmentation 0.78  + scet 0.81
```

S2 的机制：**把"算不算"和"收几个"拆开**。地板（0.60）只回答"是否属于这个领域"，放在判断的空谷里；"聚焦到 2–3 个"由排名截断完成。好处是**与词表规模无关**：加一个新词不会改变任何已有论文的标签集，除非它挤进前三。

S2′ 是在 S2 上加保证带。实测 29 篇里每篇 ≥0.95 的 topic 数分布是 `0个:3篇 1个:20篇 2个:6篇`（最大 2），所以**与 S2 结果完全相同、0 篇差异**——它今天是零成本的安全网。

S3 也能稳住，但它是创可贴：标签一旦在 0.86 被加入，就要跌破 0.70 才会移除（单向棘轮，慢慢变多），而且没有解决 2.4 的耦合。

**floor 的调控空间**（两带规则下）：

| floor | 平均 | 与 0.60 不同的篇数 | ±0.05 内判断数 |
| --- | --- | --- | --- |
| 0.50 | 2.17 | 4 | — |
| 0.55 | 2.07 | 1 | 5 |
| **0.60** | **2.03** | — | 3 |
| 0.65 | 2.03 | **0** | 2 |
| 0.70 | 2.03 | **0** | 1 |
| 0.75 | 2.00 | 1 | 1 |
| 0.80 | 1.97 | 2 | — |

0.60–0.70 是一整片结果完全相同的高原；离 0.60 最近的判断是 `tmd` = 0.58（只有它）。取 0.60 而不是 0.65 是为了让 `ambiguous` 审查带更窄（[0.55,0.60) 有 6 个判断，[0.55,0.65) 有 10 个）。

---

## 4. 设计原则

```text
P1  一个 namespace 一个语义、一个选择规则。轴不同就分家，规则不同也分家。
P2  词汇表是版本化实体：有稳定 id、有指纹、有使用统计，能按证据修剪。
P3  标签有归属：机器拥有的 namespace 可以被收敛（加 + 删），人的 namespace 永不触碰。
P4  收敛不靠"只增不减"。机器命名空间的期望集合每次运行全量计算。
P5  判定带版本指纹，staleness 是查询出来的，不引入本地数据库。
P6  投影只读当前词表（闭集）：未知词报告而不是路由。
```

---

## 5. 目标形态

### 5.1 namespace 与选择规则（**已实现**）

| namespace | 语义 | 基数 | 选择规则 | 投影 |
| --- | --- | --- | --- | --- |
| `topic/*` | 研究对象 / 观测量 / 形式体系 | 0..3（≥0.95 不受限） | **两带**：≥0.95 无条件收；`[0.60, 0.95)` 按排名补齐到 3 | `02 Topics/` |
| `kind/*` | 论文体裁 | 0..2 | **kind_floor 0.70** + 排名前 2 | `03 Kinds/` |
| `review/*` | 需要人看的原因 | 0..N | 已知集合全量收敛 | `04 Review` |
| `coverage/*` | 词表是否够用 | 0..1 | 落地为标签（可选，见 5.5） | 不投影 |
| 其它 | 你的 | — | 人不碰机器，机器不碰人 | 不投影 |

两带规则的理由：独立判定没有天然截断，而"≥0.95"和"排名前 3"各自都有答案。保证带只在论文有超过 `topic_max` 个正题时才起作用——实测 29 篇里最多 2 个，所以它今天零成本，只是防止计数规则吃掉真正的正题。

`formal/*` 拆分仍留到 P3（见 §7）：它的价值不在选择质量（S2 已经够好），而在于**形式体系与研究对象可能需要不同规则**（"我有哪些能用 SCET 的论文"是检索型问题，要小而准），以及目录上分开更符合检索习惯。

### 5.2 版本指纹（解决 2.2，**已实现**）

```text
fingerprint = sha256(model.name + task/note/scope + questions + thresholds)[:8]
```

`questions` 含全部 instructions 与 taxonomy 描述。**凡是能改变判定或标签的东西都进指纹**，包括 prompt 文案与阈值——所以改一句 prompt 或调一个阈值都不需要人工升版本号。

- 落在每个 item 的 `extra` 字段：一行 `jevero-fingerprint: <digest>`。**不是标签**，所以标签面板干净；`extra` 本来就是 Zotero 放机器数据的地方（`Citation Key:`），合并时只替换自己那一行。
- `process` 的候选集 = 收件箱里的 + 指纹 ≠ 当前 的（**全库**范围，因为 `route` 会把论文挪出收件箱）+ 从未判过的。`--include-processed` 退化为"强制重判"。
- 没有指纹、但带旧 namespace 状态标签（`agent/*`、`jevero/*`，都在 `retired_prefixes` 里）的论文算**过期**：它一定是用旧配置判的，否则不会有那些标签。这条让迁移那次运行能接上，而不是静默什么都不做。
- 词表变更 → 指纹变 → 下一次 `process` 自动重判受影响的全库（29 篇 / $0.0045），这正是我们之前手工在做的事。

### 5.3 收敛语义（解决 2.3）

每个受管 namespace 声明期望集合；运行时的动作 = 期望 − 现有（加） ∪ 现有 − 期望（删）。

区分"未知词"的两种来源，用一个显式的退役表：

```yaml
retired_topics: [perturbative-qcd, collider-phenomenology, experiment, amplitudes, loop-integrals]
retired_kinds:  [core]                  # 移交给 projects 维度
retired_prefixes: ["role/", "agent/"]   # 整个 namespace 被改名
```

- 在词表里的词：可以加、可以删（收敛）。
- 在 `retired_*` 里的词：只删不加（自动清幽灵标签，不需要人工）。
- 在 `retired_prefixes` 下的任何标签：整棵子树清掉（改名迁移用）。删除在加标签之前执行，所以同一个计划里加的新名不会被误删。
- 既不在词表、也不在退役表或退役前缀下的 `topic/*` / `kind/*`：**不动，只报告**（那是你手工加的）。

这样 `.env` 之外没有第二个状态源，幽灵标签由下一次 `process` 自动消失，而人工标签仍然安全。

### 5.4 词汇表卫生（解决 2.1）

- `process` 每次运行结束打印一行词汇表使用摘要：`applied/total`，0 次应用的词标出来。**这行摘要如果在第一版就有，前面几轮的死词根本不会积累到今天。**
- `jevero vocab`（免费、离线）：读 Zotero 现有标签，报告每个词的"持有篇数"，0 篇的词标为候选退役。
- 退役是人的决定（与"topic 只能由人添加"一致）；工具只负责让证据可见。

### 5.5 coverage 落地（解决 2.6）

三个选项，见 §7。最小改动是"不落地，但把 `irrelevant` 也变成 review reason"，最大改动是 `coverage/*` 持久标签。

---

## 6. 分期

| 阶段 | 内容 | 改判定行为？ | 成本 |
| --- | --- | --- | --- |
| **P0** | 闭集投影（2.7）+ `route` 父目录配置与现实对齐 + 词汇表使用摘要（5.4） | 否 | 小 |
| **P1** | 版本指纹 + 候选集自动选取（5.2）+ 退役表与收敛（5.3）+ topic 改 S2（3） | 是 | 中 |
| **P2** | kind 轴重校准、`kind/core` 移交 projects、coverage 落地（5.5） | 是 | 中 |
| **P3** | `formal/*` 拆分（5.1）、`jevero vocab`、`jevero eval` 验证集 | 是 | 较大 |

P0 可以立刻做且零风险。P1 是"解决这个问题"的主体。

---

## 7. 待决定 / 已决定

已决定（见"决定记录"）：用途与 reading-status 轴、选择规则（floor 0.60 + 排名 + 0.95 保证带）、归属与收敛、指纹载体（`extra`）、投影目录（层级）、命名（`kind/` + `jevero/`）。

仍然开放：

1. **`formal/*` 拆分**：现在做、P3 再做、还是不做？（当前 11 个 topic 混着研究对象与形式体系，但 S2 的选择质量已经够好；拆分的收益主要在检索与目录习惯。）
2. **coverage 落地**：不落地（现状）/ 只把 `irrelevant` 变成 reason / `coverage/*` 持久标签？现状是"系统认为超出范围"和"没有 topic 过线"在库里不可区分。
3. **`kind/experiment`、`kind/reference`**：这两个也是 0 次应用，但语义上没错。留着让每次运行的使用摘要去点名，还是现在就退掉？
4. **`jevero vocab` / `jevero eval`**：词汇表卫生与回归度量的自动化（P3）。

---

## 8. 观察记录：review 队列点出来的候选词（**未采用**，2026-09）

词汇表演化的流程是 `classify → 收集 review/taxonomy-gap → 人判断 → 改 config.yaml`。
下面是第一次真实攒到的证据，**决定是暂不加入**，留待同类论文继续出现。

用 3 篇论文各测一次候选词（每次一次判定调用，未写库）：

| 论文 | 候选词 | 该词概率 | coverage 变化 | 若采用的效果 |
| --- | --- | --- | --- | --- |
| `CDV2WJT7` Exact Amplitude Reconstruction（small-x 衍射能流） | `amplitudes` | **0.94** | missing-topic 0.67 → 0.13 | gap 消失，拿到 `topic/amplitudes` |
| `NI2SM37I` Nuclear Many-Body → Energy Detector Correlators | `heavy-ion` | **0.88** | covered 0.91（不变） | 拿到 `topic/heavy-ion`；说明**当初没标 gap 不是模型错，而是词表真的缺这个词** |
| `CM92MSVI` Genus drop in Feynman integrals | `loop-integrals` | **0.71** | covered 0.71 | 拿到 `topic/loop-integrals`，出 review 队列 |

全库影响（候选 config 干跑 31 篇，$0.005）：**4/31 篇变化**，其中 3 篇是上面这三个词各命中 1 篇，
第 4 篇（`4VS4HYP2`）是 kind 在 0.70 门口的抖动，与新词无关；其余 27 篇一字未动；review 队列会清空。

**决定：暂不采用。** 当前证据是 1 篇明确 + 1 篇边缘 + 1 篇（loop-integrals）属于早先误删，数量还不足以支撑扩张词表。

### 教训：小库里"0 次应用"是删词的弱证据

`amplitudes` 和 `loop-integrals` 在 2026-09 因为"29 篇里 0 次应用"被删掉，两天后一篇新论文正好建立在
`amplitudes` 上（p=0.94）。使用统计衡量的是**当前**使用，不是**未来需要**——库里只有 31 篇、
还在增长时，"0 次应用"更像"还没遇到"，而不是"不需要"。判据应改成
"0 次应用**且**没有合理预期它会到来"，后者只能由人判断。
