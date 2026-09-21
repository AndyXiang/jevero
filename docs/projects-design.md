# projects 结构设计（草案）

状态：**草案，尚未实现**。本文只描述设计，不改变任何分类行为。
相关代码：`src/jevero/jev.py::build_questions`（判定）、`src/jevero/policy.py::plan`（决策）、`config.yaml`（定义）。

---

## 1. 现状与局限

当前 `projects` 是四个维度里最薄的一层：

```yaml
projects:
  qec:
    description: >
      Papers useful for quarkonium energy-correlator research, including ...
```

一个项目 = 一个名字 + 一段自由文本。判定问题是"N 选 1 个 noul：这篇对项目 X 有用吗"。

在项目只有 3 个、描述由人偶尔手写时这够用。项目变多、描述需要持续更新之后，有六个具体问题：

| # | 局限 | 后果 |
| --- | --- | --- |
| 1 | 判定证据只有一段文字，没有**正例** | 描述写得好不好无法度量；改描述没有依据 |
| 2 | 没有**负例/边界** | `general-hep` 这类"什么都能沾"的项目会吸走误判 |
| 3 | 项目与 `topics`/`thresholds` **变更节奏不同**，却共用 `config.yaml` 与同一套评审纪律 | 高频更新被迫和低频高风险变更绑在一起 |
| 4 | 项目数量与问题数、`state` 体积**线性增长** | token 成本上升，且注意力被稀释 |
| 5 | 缺少**生命周期**字段 | 暂停/归档的项目仍在参与分类 |
| 6 | "时刻修改描述"缺少**可观测性** | 改了描述无法判断变好还是变坏 |

第 6 条是关键：**自动维护的前提是能度量**。没有回归度量，自动改描述只是不可观测的漂移。

---

## 2. 设计原则

1. **画像只是输入。** 项目画像只进入 `state`，判定仍然是 Jev 的 `noul`，动作仍然只由 `policy.py` 产出。加字段不改变"Jev 判断 / Python 决策"的分工。
2. **自动维护 = 提案 → 校验 → 批准**，与主流程同构（模型提案 / Python 校验 / 人落账）。绝不让模型直接把新描述写进生效配置。
3. **先加结构，不加层。** 所有新字段落在 YAML；只有"提案"命令需要新模块，且它可以复用 `jev.py` 的 transport。
4. **可回滚、有出处。** 每次画像变更记录时间、来源、依据论文。

---

## 3. project profile 的结构

```yaml
projects:
  qec:
    title: Quarkonium energy correlators
    status: active            # active | paused | archived
    description: >            # 现有字段：项目目标（人写，稳定）
      Papers useful for quarkonium energy-correlator research, ...
    anchors:                  # 正例：已确认属于该项目的代表论文
      - doi: 10.1007/JHEP03(2024)123
      - arxiv: 2305.12345
    focus:                    # 当前关注点（会被提案更新，时效信息）
      - energy-energy correlators in quarkonium decay
      - nonperturbative matrix elements for EEC
    exclude:                  # 负例：什么不算这个项目
      - generic QCD material without energy-flow observables
    updated_at: 2026-05-01
    updated_by: human         # human | agent:<model> | import
    based_on:                 # 促成这次更新的论文（审计）
      - ABCD2345
      - EFGH6789
```

字段如何被使用（这是设计的核心表）：

| 字段 | 去向 | 谁决定 |
| --- | --- | --- |
| `description` | `criteria.true`（判定问题） | 人写，稳定 |
| `exclude` | `criteria.false`（判定问题） | 人写 / 提案 |
| `anchors` | `state.project_anchors`（few-shot 正例，标题+摘要） | 人挑，提案候选由 Python 聚合 |
| `focus` | `state.project_focus` | 提案更新的主要对象 |
| `status` | Python 过滤：`archived` 不提问 | 纯确定性代码 |
| `updated_*` / `based_on` | 审计，不进 state | 工具写入 |

三个要点：

- **`anchors` 是性价比最高的补充。** 3–5 篇已确认论文的标题+摘要作为 few-shot 证据，比任何形容词堆砌都稳定；而且它天然适合半自动维护（从已带 `project/qec` 标签的论文里挑代表）。
- **`exclude` 是第二高。** 现在 `criteria.false` 是一句通用套话（"not useful beyond generic background"），写进具体负例能直接压低 `general-hep` 类型的误判。
- **主键稳定性**：锚点用 `doi` / `arxiv` 而不是 Zotero key（重装库或换库时 key 会变）。

---

## 4. 存储布局：把 projects 从 config.yaml 拆出去

理由只有一条：**演化节奏不同**。`thresholds` 变更直接改变分类行为，必须谨慎评审；项目画像会频繁更新，甚至由工具提案。放在同一文件会让两件事互相拖累。

- **阶段 A（推荐先做）**：`config.yaml` 保留 `model` / `zotero` / `classification` / `topics` / `thresholds`，新增 `projects_file: projects.yaml`；`config.py` 合并加载，`Config.projects` 对外形状不变，因此**下游代码零改动**。
- **阶段 B（项目 > 约 15 个时）**：`projects/<id>.yaml` 每项目一文件，`projects_dir:` 加载。好处是 diff 干净、可并行编辑、单个项目可独立评审。

不做阶段 B 直到有真实需要（避免过早设计）。

---

## 5. 自动维护：谁提案、谁校验、谁批准

```
Zotero 中已带 project/<id> 的人工确认论文
        ↓  ① Python 聚合（确定性）
   最近 N 篇 / 去重 / topic 分布 / 时间分布
        ↓  ② 提案（唯一需要模型的一步）
   候选 anchors / focus 短语 / exclude 短语 的 diff
        ↓  ③ Python 校验（确定性）
   schema + 长度上限 + 规模上限 + 禁止删除 anchors + 禁止新增项目
        ↓  ④ 产出待批准 diff
   projects.proposed.yaml + 变更摘要（人可读）
        ↓  ⑤ 人批准
   人工 merge 进 projects.yaml（版本控制留痕）
```

①③⑤ 都是确定性的，只有 ② 需要模型，而 ② 有两套方案：

- **方案 1（推荐，不引入新模型）**：仍然用 Jev 的决策接口。
  - 给**人工预设的候选 focus/exclude 短语**逐条问 `score`（"这句话在多大程度上概括了这批论文？"），Python 取 top-k；
  - 给候选锚点问 `noul`（"这篇是否是该项目的核心代表？"），Python 取 top-k。
  - 这条路径完全落在"决策模型"能力内，**不违反 AGENTS 的 "no second LLM"**。
- **方案 2（需要一次显式设计变更）**：用文本模型生成/改写描述文本。Jev 是决策模型，不能产文本，所以这必然引入第二个 LLM——AGENTS.md §3 明确排除。**必须单独批准，且应放在 MVP 在 30–50 篇验证集上跑通之后。**

③ 的校验规则建议（防止提案失控）：

| 规则 | 理由 |
| --- | --- |
| 一次最多改 3 个字段、新增 focus ≤ 5 条 | 限制单次漂移幅度 |
| 不得删除已有 `anchors` | 锚点是人工确认过的事实 |
| 不得新增/删除项目 | 与"topic 只能由人添加"保持一致 |
| 描述长度上限（建议 ≤ 400 字符） | 控制 token 与注意力稀释 |
| `updated_by` 必填，`based_on` 不得为空 | 保证可追溯 |

---

## 6. 规模化：问题数与 state 体积

项目到 20 个时，判定问题从 24 增至 40+，描述文本线性增长。按代价排序的出路：

1. **`status: archived` 过滤**（确定性，零成本）：归档项目不提问。
2. **描述长度上限 + lint**：`config.py` 校验里加长度约束。
3. **两阶段路由**：先一个 `choice` 粗问题（"这篇属于哪一类研究"），再由 **Python** 决定只对候选组内的项目问细问题。候选集由代码算，不让模型自由发挥。
4. **embedding/RAG 预筛**：AGENTS 明确禁止，只作为 MVP 验证之后的远期路线记录在此。

---

## 7. 度量与回归（自动维护的前置条件）

- `validation/labels.yaml`：冻结的 30–50 篇人工标注（`zotero_key → 期望 topics/projects`）。
- `jevero eval`（未实现）：输出 per-project precision/recall，以及与上一次画像版本的差异对比。
- 建议门槛：在 `--apply` 之前，project 维度 precision ≥ 0.9；任何画像更新都必须让 eval 结果不下降。
- **没有这一步就不要开自动维护。** 否则"描述被自动改了"与"分类质量下降了"之间无法建立因果。

---

## 8. 分期实施建议

| 阶段 | 内容 | 是否改变分类行为 |
| --- | --- | --- |
| P0 | 拆出 `projects.yaml`；新增 `status`/`title`/`anchors`/`exclude` 等**可选**字段 | 否（缺省等价现状） |
| P1 | `anchors` 进 `state`（先人工挑 3–5 篇）；`exclude` 进 `criteria.false` | 是，需跑验证集 |
| P2 | `validation/labels.yaml` + `jevero eval` | 否 |
| P3 | `jevero projects propose`（方案 1：Jev `score`/`noul` 排序） | 否（只产出 diff） |
| P4 | 文本模型改写描述（需显式批准 AGENTS 变更） | 是 |

---

## 9. 待决定的开放问题

1. 锚点主键：`doi` / `arxiv`（推荐）还是 Zotero key？
2. `projects.yaml` 单文件，还是直接上每项目一文件？
3. 是否允许提案**新增项目**？（建议不允许，与 topic 规则一致）
4. 是否需要子项目层级？（建议不做）
5. `focus` 的更新频率与人工确认强度：每次提案都要人批准，还是低风险字段可自动？
6. 是否把 `topics` 也一起搬出 `config.yaml`？（建议暂缓：topic 变更慢，且与阈值同属高风险变更）
